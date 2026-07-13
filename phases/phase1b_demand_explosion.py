#!/usr/bin/env python3
"""
phase1b_demand_explosion.py — MG-AWARE per-SKU BOM explosion.

NEW STRUCTURE
  Stage 1 (per-SKU walk):
    For each SKU in plan, look up its assigned MG.
    Filter BOM rows: Super_parent == SKU AND (Equipment == MG OR blank).
    Walk the BOM tree with q0 = 1 NOS (per-tyre basis).
    Produce per-tyre demand rate for every routed intermediate item.
    When the same (Parent, child) edge has multiple BOM rows with different
    UOMs, pick the row whose child Unit matches routing's expected UOM
    (no global dedup; selection is local to the edge).

  Stage 2 (expand to per-block):
    For each curing block (SKU, qty Q):
        demand_qty[item, block] = per_tyre_rate[SKU, item] * Q

  Stage 3 (cross-SKU consolidation):
    Sum same item across all SKUs to produce per-item totals.

OUTPUTS
  phase1_demand.csv             - per (item, sku, block_id) demand row
  phase1_per_sku_demand.csv     - per-tyre rate per (sku, item)
  phase1_item_consolidated.csv  - per-item total across all SKUs
  phase1_demand.xlsx            - 5 tabs: sample + item totals + per-SKU + per-MG + summary
  phase1b_mg_filter_audit.csv   - per-SKU rows kept / dropped diagnostic
"""
from __future__ import annotations
import sys, pathlib, yaml, re, os
import pandas as pd
from collections import defaultdict, deque

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT/"inputs"
OUTPUTS = ROOT/"outputs2"           # NEW WRITE LOCATION
OUTPUTS.mkdir(parents=True, exist_ok=True)
PRIOR_OUTPUTS = ROOT/"outputs"      # Phase 1a / 1.5 outputs (already-run, re-used)
RUN_SUFFIX = "_updated"


def _suff(name):
    """Insert _updated before the extension (handles .csv.gz, .xlsx, .csv)."""
    if name.endswith(".csv.gz"):
        return name[:-7] + RUN_SUFFIX + ".csv.gz"
    if name.endswith(".csv"):
        return name[:-4] + RUN_SUFFIX + ".csv"
    if name.endswith(".xlsx"):
        return name[:-5] + RUN_SUFFIX + ".xlsx"
    return name + RUN_SUFFIX


# OneDrive throttles big-file writes. Stage CSVs to a local tmp dir first,
# then atomically copy into OUTPUTS at the end of main().
import tempfile, shutil
_STAGE = OUTPUTS  # write directly to outputs2 (portable across Windows/Linux)
_STAGE.mkdir(parents=True, exist_ok=True)
def _stage_csv(df, name, big_threshold_rows=200000):
    """Write CSV to local tmp first, then copy to OUTPUTS.
    For large frames, write gzip-compressed (.csv.gz) to fit through
    OneDrive's write-throttling without truncation."""
    is_big = len(df) > big_threshold_rows
    write_name = _suff(name + ".gz" if is_big else name)
    tmp = _STAGE / write_name
    df.to_csv(tmp, index=False, compression="gzip" if is_big else None)
    final = OUTPUTS / write_name
    if tmp.resolve() != final.resolve():
        shutil.copy(str(tmp), str(final))
    return final
def _stage_xlsx_sheets(sheets, name):
    """Write xlsx directly to OUTPUTS."""
    write_name = _suff(name)
    tmp = _STAGE / write_name
    with pd.ExcelWriter(tmp, engine="openpyxl") as w:
        for k, df in sheets.items():
            clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
            df.to_excel(w, sheet_name=clean, index=False)
    final = OUTPUTS / write_name
    if tmp.resolve() != final.resolve():
        shutil.copy(str(tmp), str(final))
    return final


def _read(path):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(path, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(f"Could not parse {path}")

# ============================================================
# DB-AWARE MASTER LOADER (auto-inserted by DB-only migration)
# Routes `load master` calls to MySQL when cfg["data_source"]=="db".
# Falls back to the existing local CSV read otherwise.
# ============================================================
def _db_or_csv(_key, _cfg=None):
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_key, _cfg)
    except Exception as _e:
        # Fall back to CSV when DB or db_loader is unavailable
        _fp = (_cfg or {}).get("files", {})
        _fname = _fp.get(_key)
        if _fname is None:
            raise
        return _read(INPUTS / _fname)


def load_cfg(): return yaml.safe_load(open(ROOT/"config.yaml"))


def load_inputs(cfg):
    f = cfg["files"]
    bom = _db_or_csv("bom", cfg).rename(columns={
        "Super_parent":"Super parent", "grand_parent":"grand parent",
        "Parent_qty":"Parent qty", "Parent_unit":"Parent unit",
        "child_quantity":"child quantity", "child_Unit":"child Unit",
        "child_description":"child description"})
    return bom, _db_or_csv("routing", cfg), _db_or_csv("plan", cfg)


def canon_uom(u):
    if u is None or (isinstance(u, float) and pd.isna(u)): return ""
    return str(u).strip().upper()


def canon_mg(v):
    if v is None: return ""
    if isinstance(v, float) and pd.isna(v): return ""
    s = str(v).strip().upper()
    if s in ("", "NAN", "NONE"): return ""
    return s


def parse_plan_to_curing(plan, selected_skus):
    df = plan.copy()
    df["SKUCode"] = df["skuCode"].astype(str).str.strip()
    if selected_skus:
        df = df[df["SKUCode"].isin(selected_skus)]
    df = df[df["SKUCode"] != "CHANGEOVER"]
    df["Qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df = df[df["Qty"] > 0]
    if df.empty:
        return pd.DataFrame(columns=["SKUCode","Qty","effective_start","effective_end","block_id"])

    def parse_ts(s):
        r = pd.to_datetime(s, errors="coerce")
        if hasattr(r,"dt") and r.dt.tz is not None:
            return r.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return r

    start = parse_ts(df["startTime"])
    end   = parse_ts(df["endTime"])
    if start.isna().any():
        ds = parse_ts(df["date"]).dt.strftime("%Y-%m-%d")
        start = start.fillna(pd.to_datetime(ds + " " + df["startTime"].astype(str), errors="coerce"))
    if end.isna().any():
        ds = parse_ts(df["date"]).dt.strftime("%Y-%m-%d")
        end = end.fillna(pd.to_datetime(ds + " " + df["endTime"].astype(str), errors="coerce"))
    overnight = end < start
    end = end.where(~overnight, end + pd.Timedelta(days=1))
    out = pd.DataFrame({
        "SKUCode": df["SKUCode"].values,
        "Qty":     df["Qty"].astype(int).values,
        "effective_start": start.values,
        "effective_end":   end.values,
        "block_id": [f"B{i:04d}" for i in range(len(df))],
    })
    return out.dropna(subset=["effective_start","effective_end"]).reset_index(drop=True)


def build_routing_uom_map(rt) -> dict:
    """item_code -> expected UOM derived from routing's first matching row."""
    out = {}
    for _, r in rt.iterrows():
        rp = str(r["routed_product"]).strip()
        if not rp or rp in out: continue
        u  = str(r["proc_time_UOM"]).strip().upper()
        bu = str(r["batch_UNIT"]).strip().upper()
        if u in ("MM/MIN","M/MIN","MTR/MIN"):       out[rp] = "MM"
        elif u == "NOS/MIN":                          out[rp] = "NOS"
        elif u.startswith("CUTS"):                    out[rp] = "NOS"
        elif u == "SEC/BATCH":                        out[rp] = bu or "KG"
        elif u in ("SEC","MIN"):                      out[rp] = bu or "NOS"
        elif u.endswith("/MIN"):
            head = u.split("/")[0]
            out[rp] = "MM" if head in ("MM","M","MTR") else \
                       ("NOS" if head == "NOS" else ("KG" if head=="KG" else ""))
    return out


def per_tyre_demand(bom_sku_mg, routed, routing_uom_map):
    """
    Compute per-tyre demand for each routed child item in this (SKU, MG) BOM.

    RULE (from JK Tyre BOM convention):
        Every BOM row's `child quantity` is the per-tyre consumption of that
        child item directly. No chain multiplication. To get total demand
        for a curing block, multiply per_tyre x block.qty.

    When a child appears as the consumed side of MULTIPLE (Parent, child)
    edges (e.g., a master compound consumed by several final compounds),
    sum the child_quantity across those edges.

    When the SAME (Parent, child) edge has multiple BOM rows with different
    UOMs (e.g., BJNYL27-680MM-90 deg appears as both 1470 MM and 1 NOS),
    pick the row whose child Unit matches the routing's expected UOM for
    that item.

    Returns: dict {child_item -> (per_tyre_qty, uom)}, stats dict.
    """
    # Step 1: group rows by (parent, child) edge
    edges = defaultdict(list)
    rows_total = len(bom_sku_mg)
    for _, r in bom_sku_mg.iterrows():
        par = str(r["Parent"]).strip()
        ch  = str(r["child"]).strip()
        edges[(par, ch)].append(r)

    # Step 2: for each edge, pick UOM-matching row, then accumulate per-tyre qty
    per_tyre = defaultdict(lambda: {"qty": 0.0, "uom": ""})
    rows_uom_corrected = 0
    for (par, ch), rows in edges.items():
        if ch not in routed:
            continue  # skip raw materials
        rt_uom = routing_uom_map.get(ch, "")
        best_row = None
        best_score = -1
        for r in rows:
            cu = canon_uom(r["child Unit"])
            score = 1 if (rt_uom and cu == rt_uom) else 0
            if score > best_score:
                best_row = r
                best_score = score
        if best_row is None:
            continue
        try:
            cqty = float(best_row["child quantity"]) if pd.notna(best_row["child quantity"]) else 0.0
        except (ValueError, TypeError):
            cqty = 0.0
        cu = canon_uom(best_row["child Unit"])
        per_tyre[ch]["qty"] += cqty
        per_tyre[ch]["uom"]  = cu  # all picked rows should share UOM for a given child
        if len(rows) > 1 and best_score == 1:
            rows_uom_corrected += (len(rows) - 1)

    return ({k: (v["qty"], v["uom"]) for k, v in per_tyre.items()},
            {"rows_total": rows_total, "edges": len(edges),
             "rows_uom_corrected": rows_uom_corrected,
             "routed_items": len(per_tyre)})


def explode_demand(curing, bom, routing, mg_assign, block_mg_map=None):
    """V6_wave: block-level MG support.

    Args:
      mg_assign     : dict[sku -> primary MG]  (legacy SKU-level)
      block_mg_map  : dict[block_id -> MG]      (optional - block-level overrides)

    If block_mg_map is provided, Stage 1 builds per-(sku, mg) BOM walks for every
    unique (sku, mg) pair seen at block level. Stage 2 then resolves each block's
    MG via block_mg_map and pulls the right per-tyre map.
    """
    routed = set(routing["routed_product"].astype(str).str.strip()) - {""}
    routing_uom_map = build_routing_uom_map(routing)
    bom["__SP__"] = bom["Super parent"].astype(str).str.strip()
    bom["__EQ__"] = bom["Equipment"].apply(canon_mg)
    bom_by_sku = {sku: g for sku, g in bom.groupby("__SP__")}

    block_mg_map = block_mg_map or {}

    # Build (sku, mg) pairs we need to walk. Always include SKU-level primary MG,
    # plus any extra MGs that block-level assignment uses.
    sku_mg_pairs = set()
    for sku in curing["SKUCode"].unique():
        sku_mg_pairs.add((sku, canon_mg(mg_assign.get(sku, ""))))
    for _, blk in curing.iterrows():
        bid = blk["block_id"]
        if bid in block_mg_map:
            sku_mg_pairs.add((blk["SKUCode"], canon_mg(block_mg_map[bid])))

    # ---- STAGE 1: per-(SKU,MG) explosion to per-tyre rates ----
    per_sku_rows = []
    mg_stats = []
    sku_mg_to_per_tyre = {}  # (sku, mg) -> {item: (qty_per_tyre, uom)}

    for sku, mg in sku_mg_pairs:
        if sku not in bom_by_sku:
            mg_stats.append({"sku": sku, "assigned_MG": mg, "rows_total":0,
                             "edges":0, "rows_uom_corrected":0, "routed_items":0})
            sku_mg_to_per_tyre[(sku, mg)] = {}
            continue
        sub = bom_by_sku[sku]
        mask = (sub["__EQ__"] == mg) | (sub["__EQ__"] == "")
        bom_sku_mg = sub[mask]
        per_tyre_map, stats = per_tyre_demand(bom_sku_mg, routed, routing_uom_map)
        mg_stats.append({"sku": sku, "assigned_MG": mg, **stats})

        sku_mg_to_per_tyre[(sku, mg)] = per_tyre_map
        for item, (qty, uom) in per_tyre_map.items():
            per_sku_rows.append({"sku": sku, "MG": mg, "item_code": item,
                                  "per_tyre_qty": qty, "uom": uom})

    # ---- STAGE 2: expand to per-block demand (block-level MG lookup) ----
    rows = []
    for _, blk in curing.iterrows():
        sku, bid, need, q0 = blk["SKUCode"], blk["block_id"], blk["effective_start"], float(blk["Qty"])
        # Block-level MG wins if available; else fall back to SKU-level primary MG
        if bid in block_mg_map:
            mg = canon_mg(block_mg_map[bid])
        else:
            mg = canon_mg(mg_assign.get(sku, ""))
        per_tyre_map = sku_mg_to_per_tyre.get((sku, mg), {})
        for item, (per_tyre, uom) in per_tyre_map.items():
            rows.append({
                "item_code":  item,
                "sku":        sku,
                "block_id":   bid,
                "MG":         mg,
                "need_by":    need,
                "demand_qty": per_tyre * q0,
                "demand_uom": uom,
            })

    return pd.DataFrame(rows), pd.DataFrame(per_sku_rows), pd.DataFrame(mg_stats)


def save_xlsx(phase, name, sheets):
    OUTPUTS.mkdir(exist_ok=True)
    dest = OUTPUTS / _suff(f"phase{phase}_{name}.xlsx")
    tmp  = OUTPUTS / f".tmp_phase{phase}_{name}.xlsx"
    try:
        with pd.ExcelWriter(tmp, engine="openpyxl") as w:
            for k, df in sheets.items():
                clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
                df.to_excel(w, sheet_name=clean, index=False)
        if dest.exists():
            try: dest.unlink()
            except Exception: pass
        os.replace(tmp, dest)
    except Exception:
        alt = OUTPUTS / _suff(f"phase{phase}_{name}_v2.xlsx")
        with pd.ExcelWriter(alt, engine="openpyxl") as w:
            for k, df in sheets.items():
                clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
                df.to_excel(w, sheet_name=clean, index=False)
        return alt
    finally:
        if tmp.exists():
            try: tmp.unlink()
            except Exception: pass
    return dest


def main():
    print("\n" + "=" * 65)
    print("  PHASE 1b - Per-SKU MG-Aware Demand Explosion")
    print("=" * 65)
    cfg = load_cfg()
    bom, rt, plan = load_inputs(cfg)

    mg_csv = PRIOR_OUTPUTS / "mg_assignment.csv"
    if not mg_csv.exists():
        print(f"  ERROR: {mg_csv} not found. Run phase1a first."); return 2
    mga = pd.read_csv(mg_csv)
    mg_assign = dict(zip(mga["sku"].astype(str).str.strip(),
                         mga["MG"].astype(str).str.strip().str.upper()))
    print(f"  Loaded MG assignments: {len(mg_assign)} SKUs")

    # V6_wave: load block-level MG mapping if Phase 1a produced it
    block_mg_csv = PRIOR_OUTPUTS / "mg_assignment_blocks.csv"
    block_mg_map = {}
    if block_mg_csv.exists():
        bmg = pd.read_csv(block_mg_csv)
        block_mg_map = dict(zip(bmg["block_id"].astype(str).str.strip(),
                                 bmg["MG"].astype(str).str.strip().str.upper()))
        print(f"  Loaded block-level MG: {len(block_mg_map):,} blocks "
              f"(distinct MGs: {bmg['MG'].nunique()})")
        # Count SKUs that are actually block-split
        n_split = (bmg.groupby("sku")["MG"].nunique() > 1).sum()
        print(f"  SKUs split across multiple MGs at block level: {n_split}")

    cur = parse_plan_to_curing(plan, cfg.get("selected_skus", []))
    print(f"  Curing blocks loaded:  {len(cur):,}")
    print(f"  Distinct SKUs:         {cur['SKUCode'].nunique()}")
    print(f"  Total tyres:           {int(cur['Qty'].sum()):,}")

    print(f"  Stage 1: per-(SKU,MG) BOM walk -> per-tyre rates...")
    demand, per_sku, mg_stats = explode_demand(cur, bom, rt, mg_assign,
                                                  block_mg_map=block_mg_map)
    print(f"  Per-SKU items:         {len(per_sku):,}")
    print(f"  Demand rows (per-block): {len(demand):,}")

    tot_corr = int(mg_stats["rows_uom_corrected"].fillna(0).sum())
    n_skus = int((mg_stats["rows_uom_corrected"].fillna(0)>0).sum())
    print(f"  UOM-corrected edges:   {tot_corr:,} across {n_skus} SKUs")

    if not demand.empty:
        print(f"\n  Per-UOM totals:")
        for uom, n in demand.groupby("demand_uom")["item_code"].nunique().sort_values(ascending=False).items():
            tot = demand[demand["demand_uom"]==uom]["demand_qty"].sum()
            print(f"    {uom:6s}  {n:5d} items   total qty {tot:18,.1f}")
        tot_tyres = int(cur["Qty"].sum())
        for uom in ("KG","MM","NOS"):
            tot = demand[demand["demand_uom"]==uom]["demand_qty"].sum()
            print(f"    {uom:6s} per tyre: {tot/tot_tyres:,.2f}")

    # Stage 3: cross-SKU consolidation
    # Stage 3: cross-SKU consolidation
    item_consolidated = demand.groupby(["item_code","demand_uom"]).agg(
        n_skus=("sku","nunique"),
        n_blocks=("block_id","nunique"),
        total_qty=("demand_qty","sum"),
    ).reset_index().sort_values("total_qty", ascending=False)

    print(f"  Stage 3: cross-SKU consolidation - {len(item_consolidated):,} items")

    # Guard against OneDrive sync conflicts that leave a directory at the
    # file's path (with the actual file trapped inside). If we don't nuke
    # the directory first, pandas writes inside it and Phase 2 reads stale data.
    import shutil
    csv_path = OUTPUTS / _suff("phase1_demand.csv")
    gz_path  = OUTPUTS / _suff("phase1_demand.csv.gz")
    for _p in (csv_path, gz_path):
        if _p.is_dir():
            print(f"  WARNING: {_p} exists as a directory (OneDrive sync conflict). Removing.")
            shutil.rmtree(_p, ignore_errors=True)

    big = len(demand) > 200000
    if big:
        demand.to_csv(gz_path, index=False, compression="gzip")
        print(f"  Wrote: {gz_path.name} ({len(demand):,} rows)")
    else:
        demand.to_csv(csv_path, index=False)
        print(f"  Wrote: {csv_path.name} ({len(demand):,} rows)")

    sheets = {
        "02_per_SKU_rates":  per_sku,
        "03_per_MG_stats":   mg_stats,
        "04_top_items":      item_consolidated.head(200),
    }
    _stage_xlsx_sheets(sheets, "phase1_demand.xlsx")
    print(f"\n[OK] Phase 1b complete - {len(demand):,} demand rows.")
    return 0


if __name__ == '__main__':
    import sys as _sys
    import traceback as _tb
    try:
        _rc = main()
        raise SystemExit(_rc if _rc is not None else 0)
    except SystemExit:
        raise
    except Exception:
        _sys.stderr.write(chr(10) + '!! PHASE CRASHED !!' + chr(10))
        _sys.stderr.flush()
        _tb.print_exc()
        _sys.exit(1)
