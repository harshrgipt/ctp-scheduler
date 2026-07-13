#!/usr/bin/env python3
"""
db_loader.py — Unified input loader (CSV or MySQL DB) for V6_wave pipeline.

USAGE
  Each phase imports `from db_loader import load_input` and replaces direct
  pd.read_csv(INPUTS/cfg["files"]["bom"]) with load_input("bom", cfg).

  The function transparently picks between CSV and DB based on cfg["data_source"]:
     "csv" (default) — read from inputs/<file>
     "db"            — read from MySQL table jkt_<key>

  When reading from DB, columns are renamed to match what existing pipeline
  code expects (DB uses snake_case for some columns where the pipeline uses
  "Item Type"-style spaces).

CONFIG (config.yaml)
  data_source: db          # or "csv"
  db:
    url: "mysql+pymysql://user:pass@host/db"

ENV OVERRIDES
  JK_DATA_SOURCE = "db" | "csv"      # overrides cfg["data_source"]
  JK_DB_URL      = "mysql+pymysql://..."   # overrides cfg["db"]["url"]
"""
from __future__ import annotations
import os
import pathlib
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT / "inputs"

# Default DB connection (override via cfg["db"]["url"] or JK_DB_URL env var)
DEFAULT_DB_URL = "mysql+pymysql://root:Dev112233@35.208.174.2/jkplanningV1"

# Config-key -> (DB table name, default CSV filename, column-rename map)
TABLES = {
    "bom": (
        "jkt_bom",
        "bom_updatedV7.csv",
        {
            "Super_parent":   "Super parent",
            "Parent_qty":     "Parent qty",
            "Parent_unit":    "Parent unit",
            "child_quantity": "child quantity",
            "child_Unit":     "child Unit",
        },
    ),
    "routing": (
        "jkt_routing",
        "routing_updatedV36.csv",
        {},  # routing column names already align
    ),
    "plan": (
        "jkt_plan_in_schedule",
        "curing_schedule_may2026(in).csv",
        {},
    ),
    "itemtype_master": (
        "jkt_itemType_master",
        "itemtype_master.csv",
        {},
    ),
    "aging_master": (
        "jkt_aging_master",
        "aging_masterV4.csv",
        {},
    ),
    "mpq": (
        "jkt_mpq",
        "mpq_v3.csv",
        {
            "Item_Type":         "Item Type",
            "Minimum_Run_Qty":   "Minimum Run Qty",
            "Maximum_Run_Qty":   "Maximum Run Qty",
            "Fraction_Allowed":  "Fraction Allowed",
        },
    ),
    "buffer_master": (
        "jkt_buffer_master",
        "buffer_master.csv",
        {},  # "Item type" and "Buffer Level (Hrs)" already aligned
    ),
    "mg_preference": (
        "jkt_mg_preference",
        "mg_preference.csv",
        {
            "BJ_GROUP":         "BJ GROUP",
            "TWO_STAGE_TBM":    "TWO STAGE TBM",
            "UNISTAGE_GROUP":   "UNISTAGE GROUP",
            "VMIMAXX_GROUP":    "VMIMAXX GROUP",
        },
    ),
    "belt_wire_mapping": (
        "jkt_belt_wire_mapping",
        "belt_wire_mapping.csv",
        {},
    ),
    "building_cycle_times": (
        "jkt_building_cycle_times",
        "building_cycle_times.csv",
        {},  # Machine cast to str inside loader
    ),
    "planning_max_aging": (
        "jkt_planning_max_aging",
        "planning_max_aging.csv",
        {},
    ),
    # === Plant inventory (regenerated daily, "today's WIP as of 7 AM") ===
    # Replaces the old WIP_simulation_May2026.csv. Each row is one production
    # batch — pipeline aggregates by itemcode to get total WIP.
    # DB table: jkt_wip   (schema: production_id, produced_time, itemcode,
    #                      item_type, unit, inventory, MaxAging, MaxAgingUnit,
    #                      MinAging, MinAgingUnit, updatedAt)
    # CSV/XLSX fallback: inventory_combined.xlsx (matches DB schema except updatedAt)
    "inventory": (
        "jkt_wip",
        "inventory_combined.xlsx",   # plant script writes both xlsx + jkt_wip
        {},
    ),
}


# ============================================================
# Connection management
# ============================================================
_ENGINE = None

def get_engine(cfg=None):
    """Return a cached SQLAlchemy engine. Resolves URL in this priority:
    JK_DB_URL env var -> cfg["db"]["url"] -> DEFAULT_DB_URL.
    """
    global _ENGINE
    if _ENGINE is not None:
        return _ENGINE
    from sqlalchemy import create_engine
    url = os.environ.get("JK_DB_URL")
    if not url and cfg:
        url = (cfg.get("db") or {}).get("url")
    if not url:
        url = DEFAULT_DB_URL
    _ENGINE = create_engine(url, pool_pre_ping=True)
    return _ENGINE


def _resolve_source(cfg):
    """Decide CSV vs DB. Env var JK_DATA_SOURCE wins over cfg, default = csv."""
    src = os.environ.get("JK_DATA_SOURCE")
    if not src and cfg:
        src = cfg.get("data_source")
    return (src or "csv").lower().strip()


# ============================================================
# File reader — handles CSV and XLSX
# ============================================================
def _read_csv(path):
    """Best-effort CSV read with encoding fallback. Auto-detects .xlsx and
    routes to pd.read_excel — useful for files that the plant team writes
    in Excel format (e.g. inventory_combined.xlsx)."""
    path = pathlib.Path(path)
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm", ".xls"):
        return pd.read_excel(path)
    # Try the original CSV path; if missing, also try .xlsx fallback
    if not path.exists():
        alt = path.with_suffix(".xlsx")
        if alt.exists():
            return pd.read_excel(alt)
    for enc in ("utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, engine="python",
                               on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError:
            continue
    raise IOError(path)


# ============================================================
# DB reader
# ============================================================
def _read_db(table, rename_map, cfg):
    from sqlalchemy import text
    eng = get_engine(cfg)
    df = pd.read_sql(text(f"SELECT * FROM {table}"), eng)
    if rename_map:
        df = df.rename(columns=rename_map)
    return df


# ============================================================
# Public API: load_input
# ============================================================
def load_input(config_key, cfg=None, csv_path_override=None):
    """Load one input table. Returns a pandas DataFrame.

    config_key : str  — one of TABLES.keys()  (e.g. "bom", "routing", "plan")
    cfg        : dict — loaded config.yaml (for source switch + db URL)
    csv_path_override : optional pathlib.Path — explicit CSV path

    Picks DB or CSV per cfg["data_source"] (default: "csv").
    """
    if config_key not in TABLES:
        raise KeyError(f"Unknown input config_key '{config_key}'. "
                       f"Known: {list(TABLES.keys())}")
    table, default_csv, rename_map = TABLES[config_key]
    src = _resolve_source(cfg)
    if src == "db":
        df = _read_db(table, rename_map, cfg)
    else:
        # CSV mode — use config-provided filename if any, else default
        if csv_path_override is not None:
            path = pathlib.Path(csv_path_override)
        else:
            fname = default_csv
            if cfg and "files" in cfg and config_key in cfg["files"]:
                fname = cfg["files"][config_key]
            path = INPUTS / fname
        df = _read_csv(path)

    # === Per-table post-processing ===
    if config_key == "building_cycle_times" and "Machine" in df.columns:
        # CSV stores Machine as int; DB stores as bigint. Code expects str.
        df["Machine"] = df["Machine"].astype(str).str.strip()
    return df


# ============================================================
# Convenience aliases (so phases can also do
#     from db_loader import load_bom, load_routing, ... )
# ============================================================
def load_bom(cfg=None):                  return load_input("bom", cfg)
def load_routing(cfg=None):              return load_input("routing", cfg)
def load_plan(cfg=None):                 return load_input("plan", cfg)
def load_itemtype(cfg=None):             return load_input("itemtype_master", cfg)
def load_aging(cfg=None):                return load_input("aging_master", cfg)
def load_mpq(cfg=None):                  return load_input("mpq", cfg)
def load_buffer(cfg=None):               return load_input("buffer_master", cfg)
def load_mg_preference(cfg=None):        return load_input("mg_preference", cfg)
def load_belt_wire(cfg=None):            return load_input("belt_wire_mapping", cfg)
def load_building_cycle_times(cfg=None): return load_input("building_cycle_times", cfg)
def load_planning_max_aging(cfg=None):   return load_input("planning_max_aging", cfg)
def load_inventory(cfg=None):            return load_input("inventory", cfg)


def load_wip_by_item(cfg=None):
    """Return a flat dict mapping itemcode -> total available WIP qty.

    Aggregates rows of the inventory snapshot (DB: jkt_wip; CSV fallback:
    inventory_combined.xlsx) by itemcode and sums the quantity column.

    The DB schema uses `inventory` for qty; the legacy CSV used
    `availableinventory`. We try both, in that order.
    """
    df = load_inventory(cfg)
    if df is None or len(df) == 0:
        return {}
    df = df.copy()
    # itemcode column resolution
    item_col = None
    for c in ("itemcode", "ItemCode", "item_code", "Item_Code"):
        if c in df.columns:
            item_col = c
            break
    if item_col is None:
        return {}
    # qty column resolution
    qty_col = None
    for c in ("inventory", "availableinventory", "available_inventory",
              "Inventory", "qty", "Qty", "quantity"):
        if c in df.columns:
            qty_col = c
            break
    if qty_col is None:
        return {}
    df[item_col] = df[item_col].astype(str).str.strip()
    df[qty_col] = pd.to_numeric(df[qty_col], errors="coerce").fillna(0.0)
    # === UNIT NORMALISATION (length -> MM, the pipeline's demand convention) ===
    # The inventory snapshot stores length components (cap strip, plies, calendered
    # rolls, nylon) in METRES, but the BOM/demand explosion denominates them in
    # MILLIMETRES. Summing raw qty credited only 1/1000th of the real stock, so the
    # netting ignored ~99.9% of available length material and over-produced it
    # (driving the cap-strip / ply over-ageing). Convert M->MM (x1000), CM->MM (x10);
    # NOS and KG already match the demand UOM and are left as-is.
    _LEN_TO_MM = {"M": 1000.0, "MTR": 1000.0, "METER": 1000.0, "METRE": 1000.0,
                  "CM": 10.0, "MM": 1.0}
    unit_col = None
    for c in ("unit", "Unit", "uom", "UOM", "unitOfMeasure"):
        if c in df.columns:
            unit_col = c
            break
    if unit_col is not None:
        _u = df[unit_col].astype(str).str.strip().str.upper()
        _factor = _u.map(_LEN_TO_MM).fillna(1.0)   # non-length units (NOS, KG) -> x1
        df[qty_col] = df[qty_col] * _factor
    grouped = df.groupby(item_col)[qty_col].sum()
    return {str(k).strip(): float(v) for k, v in grouped.items() if v > 0}


def load_plan_horizon(cfg=None):
    """Return (start_dt, end_dt) derived from the plan table/CSV.

    Replaces the deprecated plan_params.csv. Used by phases that need to know
    the schedule's earliest and latest curing times.
    """
    plan = load_plan(cfg)
    if plan is None or len(plan) == 0:
        return (None, None)
    st = pd.to_datetime(plan.get("startTime"), errors="coerce")
    en = pd.to_datetime(plan.get("endTime"), errors="coerce")
    return (st.min() if st is not None else None,
            en.max() if en is not None else None)


# ============================================================
# OUTPUT TABLES — Phase 5 writes these back to the same DB
# ============================================================
OUTPUT_TABLES = {
    # Phase 5 outputs (logical key -> DB table name)
    "floor_schedule":       "jkt_floor_endfwd_schedule",
    "machine_utilization":  "jkt_machine_utilization_updated",
}


def write_to_db(df, output_key=None, table_name=None, cfg=None,
                truncate=True, chunksize=5000):
    """Write a DataFrame to a MySQL table (TRUNCATE+INSERT semantics).

    - Creates the table on first write (dtypes inferred from DataFrame).
    - On subsequent writes, optionally TRUNCATEs before inserting.

    Args:
      df          : pandas DataFrame to persist.
      output_key  : logical key from OUTPUT_TABLES (e.g. "floor_schedule").
      table_name  : raw table name (used if output_key is None).
      truncate    : if True, TRUNCATE existing rows before insert.
      chunksize   : pandas to_sql chunk size.

    Returns: dict {table, rows_written, created_or_replaced}.
    """
    from sqlalchemy import inspect, text
    if df is None:
        return {"table": None, "rows_written": 0, "created_or_replaced": False}
    if output_key:
        if output_key not in OUTPUT_TABLES:
            raise KeyError(f"Unknown output_key '{output_key}'. "
                           f"Known: {list(OUTPUT_TABLES.keys())}")
        table = OUTPUT_TABLES[output_key]
    elif table_name:
        table = table_name
    else:
        raise ValueError("write_to_db: must give output_key or table_name")

    eng = get_engine(cfg)
    insp = inspect(eng)
    table_exists = insp.has_table(table)

    n_before = len(df) if df is not None else 0
    if not table_exists:
        df.to_sql(table, eng, if_exists="replace", index=False, chunksize=chunksize)
        return {"table": table, "rows_written": n_before, "created_or_replaced": True}

    if truncate:
        with eng.begin() as conn:
            conn.execute(text(f"TRUNCATE TABLE `{table}`"))
    df.to_sql(table, eng, if_exists="append", index=False, chunksize=chunksize)
    return {"table": table, "rows_written": n_before, "created_or_replaced": False}
