#!/usr/bin/env python3
"""
phase6_curing_revision.py
=========================

Revises the curing schedule (jkt_plan_in_schedule) against the finalised
Building schedule produced by Phase 5.

ALGORITHM (per the V1 client spec):
  For each press independently:
    1. Sort all curing jobs by original startTime.
    2. For each curing job, FIFO-consume Building lots of the same SKU
       until the building qty satisfies the curing lot's qty. The "kit
       ready" time is the endTime of the last consumed building lot.
    3. candidate_start = max( original_start, kit_ready, previous_job_end_on_press )
    4. new_end = candidate_start + ORIGINAL DURATION  (duration preserved)
    5. Update startTime, endTime, shift, date.  Qty and press unchanged.
    6. Move to next job on the press.

INPUTS
  jkt_plan_in_schedule  (via db_loader.load_plan)  -- original curing plan
  Finalised Building schedule from Phase 5
        DB table:  jkt_floor_endfwd_schedule  (preferred)
        File:      outputs2/phase5_schedule_updated.csv  (fallback)

OUTPUTS
  DB table:  jkt_curing_schedule_revised  (TRUNCATE + INSERT)
  File   :   outputs2/phase6_curing_revision_updated.csv  (atomic write)

The script is fault-tolerant: if no Building schedule is available the original
plan is mirrored unchanged so the downstream output is never missing.
"""
from __future__ import annotations
import os
import sys
import pathlib
import tempfile
from datetime import timedelta

import pandas as pd
import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUTPUTS = ROOT / "outputs2"
OUTPUTS.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(ROOT / "phases"))

# Schema that Phase 5 uses for jkt_floor_endfwd_schedule / phase5_floor_schedule_updated.csv
FLOOR_SCHEMA = [
    "date", "machine", "shift", "item", "item_type", "process", "department",
    "start_time", "end_time", "produce_qty", "UOM", "lot_id",
]

# Names that identify a Building operation in the finalised schedule.
# We use the LAST building stage (Green Tyre Building / Stage-2) when present —
# that is the lot whose completion makes a complete tyre kit ready for curing.
BUILDING_PROCESS_TOKENS = (
    "GREEN TYRE BUILDING",   # Stage-2
    "GT BUILDING",
    "CAR BUILDING",          # Generic carcass-then-tyre line
    "TYRE BUILDING",
)
# Stage-2 / Green Tyre keywords (preferred when both stages are present)
STAGE2_TOKENS = ("GREEN TYRE", "STAGE2", "STAGE 2", "GT BUILDING")


# ------------------------------------------------------------------ helpers
def _load_cfg():
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def _atomic_to_csv(df: pd.DataFrame, dest: pathlib.Path) -> pathlib.Path:
    """Write CSV to a sibling tmp file then os.replace — OneDrive-safe."""
    tmp = dest.with_name(".tmp_" + dest.name)
    try:
        df.to_csv(tmp, index=False)
        if dest.exists():
            try:
                dest.unlink()
            except Exception:
                pass
        os.replace(tmp, dest)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except Exception:
                pass
    return dest


def _derive_shift_and_plant_day(ts: pd.Timestamp):
    """Plant convention:
        A = 07:00-15:00 (plant-day = calendar date)
        B = 15:00-23:00 (plant-day = calendar date)
        C = 23:00-07:00 (plant-day = the date when 23:00 fell)

    A timestamp like 03:00 AM belongs to the PREVIOUS calendar day's C shift.
    """
    if pd.isna(ts):
        return "", pd.NaT
    h = ts.hour
    d = ts.normalize().date()
    if 7 <= h < 15:
        return "A", d
    if 15 <= h < 23:
        return "B", d
    if h >= 23:
        return "C", d
    # 00:00 - 06:59
    prev = (ts - pd.Timedelta(days=1)).normalize().date()
    return "C", prev


# ------------------------------------------------------------------ loaders
def load_original_plan(cfg):
    """Return the original curing plan as a DataFrame with normalised cols."""
    from db_loader import load_plan
    plan = load_plan(cfg).copy()
    # Normalise expected column names (DB ↔ code aliases)
    rename = {
        "PlanId": "plan_id", "planId": "plan_id",
        "SKUCode": "skuCode", "sku_code": "skuCode",
        "PressNo": "pressNo", "press_no": "pressNo",
        "CycleTime": "cycleTime", "cycle_time": "cycleTime",
    }
    plan = plan.rename(columns={k: v for k, v in rename.items() if k in plan.columns})
    # Ensure all expected columns exist
    for c in ("plan_id", "skuCode", "date", "pressNo", "shift",
              "startTime", "endTime", "qty", "cycleTime", "remarks"):
        if c not in plan.columns:
            plan[c] = pd.NA
    # Parse timestamps
    plan["startTime"] = pd.to_datetime(plan["startTime"], errors="coerce")
    plan["endTime"]   = pd.to_datetime(plan["endTime"],   errors="coerce")
    plan["qty"]       = pd.to_numeric(plan["qty"], errors="coerce").fillna(0).astype(int)
    plan["skuCode"]   = plan["skuCode"].astype(str).str.strip()
    plan["pressNo"]   = plan["pressNo"].astype(str).str.strip()
    # Drop changeover / zero-qty rows
    plan = plan[(plan["skuCode"].str.upper() != "CHANGEOVER") & (plan["qty"] > 0)]
    plan = plan.reset_index(drop=True)
    return plan


def _select_building_subset(df: pd.DataFrame) -> pd.DataFrame:
    """Pick the rows that represent Building completion (the lots whose
    finish makes a kit available to curing). Strategy:
      1. Prefer rows whose process matches Stage-2 / Green Tyre Building.
      2. Else fall back to any BUILDING department row.
    """
    if df is None or df.empty:
        return pd.DataFrame()

    # Try every plausible column name we have seen produced by phase 5
    proc_col = next((c for c in ("process", "operation", "Process", "Operation") if c in df.columns), None)
    dept_col = next((c for c in ("department", "Department", "dept") if c in df.columns), None)

    candidate = df.copy()
    if proc_col is not None:
        proc_up = candidate[proc_col].astype(str).str.upper().fillna("")
        # Preferred: explicit Stage-2 / Green Tyre Building tokens
        s2 = candidate[proc_up.apply(lambda s: any(tok in s for tok in STAGE2_TOKENS))]
        if not s2.empty:
            return s2
        # Else: any building token
        any_b = candidate[proc_up.apply(lambda s: any(tok in s for tok in BUILDING_PROCESS_TOKENS))]
        if not any_b.empty:
            return any_b

    if dept_col is not None:
        dept_up = candidate[dept_col].astype(str).str.upper().fillna("")
        b = candidate[dept_up.str.contains("BUILDING", na=False)]
        if not b.empty:
            return b

    return pd.DataFrame()


def load_building_schedule(cfg):
    """Return a DataFrame of Building lots (one row per lot) with columns:
       skuCode, scheduled_finish (timestamp), qty (int).
    Pulls from MySQL jkt_floor_endfwd_schedule first, falls back to
    outputs2/phase5_schedule_updated.csv if DB has no rows.
    """
    df = pd.DataFrame()

    # -------- DB attempt --------
    try:
        from db_loader import get_engine, _resolve_source, OUTPUT_TABLES
        if _resolve_source(cfg) == "db":
            eng = get_engine(cfg)
            tbl = OUTPUT_TABLES["floor_schedule"]
            df = pd.read_sql(f"SELECT * FROM {tbl}", eng)
            print(f"  Building schedule from DB  {tbl}: {len(df):,} rows")
    except Exception as e:
        print(f"  [WARN] DB load failed: {e}")

    # -------- File fallback --------
    # v2 building schedule keys lots by GT CODE (item), not the tyre SKU, and only
    # the FLOOR schedule carries produce_qty + process. Prefer the floor schedule.
    if df is None or df.empty:
        for nm in ("phase5_floor_schedule_updated.csv", "phase5_schedule_updated.csv"):
            p = OUTPUTS / nm
            if p.exists():
                df = pd.read_csv(p, low_memory=False)
                print(f"  Building schedule from file {nm}: {len(df):,} rows")
                break

    if df is None or df.empty:
        return pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty"])

    # Filter to Building rows
    df = _select_building_subset(df)
    if df.empty:
        print("  [WARN] no Building rows found in the finalised schedule")
        return pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty"])

    # Normalise column names
    rename = {}
    if "scheduled_finish" not in df.columns:
        for alt in ("endTime", "scheduledFinish", "end_time", "finish_time"):
            if alt in df.columns:
                rename[alt] = "scheduled_finish"
                break
    if "qty" not in df.columns:
        for alt in ("produce_qty", "lot_qty", "Qty", "quantity"):
            if alt in df.columns:
                rename[alt] = "qty"
                break
    if "skuCode" not in df.columns:
        for alt in ("sku", "SKU", "SKUCode", "sku_code", "FG_SKU_CODE"):
            if alt in df.columns:
                rename[alt] = "skuCode"
                break
    df = df.rename(columns=rename)

    # === v2 ADAPTER: map GT-code (item) -> tyre SKU ===
    # v2's building lots carry only `item` (the GT code) and no tyre SKU. Phase 6
    # FIFO-matches curing jobs by tyre SKU, so map item -> skuCode via the demand
    # explosion's GT-code -> sku link (item_code starting "GT"/"CAR" -> sku).
    need_sku = ("skuCode" not in df.columns) or df.get("skuCode").isna().all() or \
               (df.get("skuCode").astype(str).str.strip() == "").all()
    if need_sku and "lot_id" in df.columns:
        # Reliable 1:1 map: building lot_id -> tyre SKU via phase-2 lots' skus_set.
        lot2sku = {}
        for ln in ("phase2_lots_updated.csv", "phase2_lots.csv"):
            lp = OUTPUTS / ln
            if lp.exists():
                _L = pd.read_csv(lp, usecols=["lot_id", "skus_set"], low_memory=False)
                _L["lot_id"] = _L["lot_id"].astype(str).str.strip()
                _L["sku1"] = (_L["skus_set"].astype(str).str.split(",").str[0].str.strip())
                lot2sku = dict(zip(_L["lot_id"], _L["sku1"]))
                break
        df["skuCode"] = df["lot_id"].astype(str).str.strip().map(lot2sku)
        print(f"  [v2-adapter] mapped {df['skuCode'].notna().sum():,}/{len(df):,} "
              f"building lots to tyre SKUs via lot_id->skus_set")
    if "skuCode" not in df.columns or "scheduled_finish" not in df.columns:
        print("  [WARN] building schedule missing skuCode or finish col; skipping")
        return pd.DataFrame(columns=["skuCode", "scheduled_finish", "qty"])
    if "qty" not in df.columns:
        df["qty"] = 0

    out = pd.DataFrame({
        "skuCode": df["skuCode"].astype(str).str.strip(),
        "scheduled_finish": pd.to_datetime(df["scheduled_finish"], errors="coerce"),
        "qty": pd.to_numeric(df["qty"], errors="coerce").fillna(0).astype(int),
    }).dropna(subset=["scheduled_finish"])
    out = out[(out["skuCode"].str.upper() != "CHANGEOVER") & (out["qty"] >= 0)]
    return out


# ------------------------------------------------------------------ kit FIFO
class KitPool:
    """Per-SKU FIFO pool of Building completions.

    For each SKU we hold a list of (endTime, qty_remaining) sorted by endTime.
    take(sku, qty) returns the endTime of the LAST building lot needed to
    accumulate `qty` units for this SKU. Building lots are consumed across
    consecutive calls (FIFO), reflecting Rule "consume them chronologically".
    """

    def __init__(self, build_df: pd.DataFrame):
        self.lots = {}
        if build_df is None or build_df.empty:
            return
        for sku, g in build_df.groupby("skuCode"):
            g2 = g.sort_values("scheduled_finish").reset_index(drop=True)
            self.lots[sku] = [
                [pd.Timestamp(t), int(q)]
                for t, q in zip(g2["scheduled_finish"], g2["qty"])
            ]

    def take(self, sku: str, qty: int):
        """Consume `qty` units of `sku` FIFO.

        Returns:
            kit_ready (pd.Timestamp or None) -- finish time of the last lot
                needed; None if not enough Building qty exists.
        """
        if sku not in self.lots or qty <= 0:
            return None
        need = int(qty)
        kit_ready = None
        bucket = self.lots[sku]
        i = 0
        while i < len(bucket) and need > 0:
            t, avail = bucket[i]
            if avail <= 0:
                i += 1
                continue
            consume = min(need, avail)
            avail -= consume
            need -= consume
            bucket[i][1] = avail
            kit_ready = t
            if avail == 0:
                i += 1
        if need > 0:
            return None
        return kit_ready


# ------------------------------------------------------------------ revise
def revise_curing_schedule(plan: pd.DataFrame, build_df: pd.DataFrame) -> pd.DataFrame:
    if plan.empty:
        return plan.copy()

    pool = KitPool(build_df)
    out_rows = []

    # Per-press loop
    for press, g in plan.groupby("pressNo"):
        g = g.sort_values("startTime").reset_index(drop=True)
        prev_end = None
        for _, r in g.iterrows():
            orig_start = pd.Timestamp(r["startTime"])
            orig_end   = pd.Timestamp(r["endTime"])
            duration = (orig_end - orig_start) if (pd.notna(orig_start) and pd.notna(orig_end)) \
                       else timedelta(0)
            sku = str(r["skuCode"]).strip()
            qty = int(r["qty"])

            # Building kit availability for this SKU
            kit_ready = pool.take(sku, qty)

            # Apply the spec exactly: max(original, kit_ready, prev_end_on_press)
            candidate_start = orig_start
            if kit_ready is not None and kit_ready > candidate_start:
                candidate_start = kit_ready
            if prev_end is not None and prev_end > candidate_start:
                candidate_start = prev_end

            new_end = candidate_start + duration

            # Recompute shift + date
            shift, plant_day = _derive_shift_and_plant_day(candidate_start)

            row = r.to_dict()
            row["startTime"] = candidate_start
            row["endTime"]   = new_end
            row["shift"]     = shift
            row["date"]      = plant_day
            # Annotate the delay reason for traceability
            delay_min = max(0.0, (candidate_start - orig_start).total_seconds() / 60.0)
            why = []
            if kit_ready is not None and kit_ready > orig_start:
                why.append("kit_late")
            if prev_end is not None and prev_end > orig_start and \
               (kit_ready is None or prev_end >= kit_ready):
                why.append("press_busy")
            existing = str(row.get("remarks") or "").strip()
            revision_note = f"REVISED(+{delay_min:.0f}min{', ' + '/'.join(why) if why else ''})"
            row["remarks"] = (existing + (" | " if existing else "") + revision_note) \
                             if delay_min > 0 else (existing or "ON_TIME")

            prev_end = new_end
            out_rows.append(row)

    return pd.DataFrame(out_rows)


# ------------------------------------------------------------------ floor-schedule augmentation
def curing_to_floor_rows(revised: pd.DataFrame) -> pd.DataFrame:
    """Convert revised curing rows into the floor-schedule schema used by
    Phase 5 (jkt_floor_endfwd_schedule / phase5_floor_schedule_updated.csv).
    One row per curing lot; the START shift / date are used (curing is a
    continuous press run, not split per shift)."""
    if revised is None or revised.empty:
        return pd.DataFrame(columns=FLOOR_SCHEMA)
    rows = []
    for _, r in revised.iterrows():
        st = pd.Timestamp(r["startTime"])
        en = pd.Timestamp(r["endTime"])
        if pd.isna(st) or pd.isna(en):
            continue
        shift, plant_day = _derive_shift_and_plant_day(st)
        rows.append({
            "date":         str(plant_day),
            "machine":      str(r.get("pressNo", "")),
            "shift":        shift,
            "item":         str(r.get("skuCode", "")),
            "item_type":    "CURED TYRE",
            "process":      "CURING",
            "department":   "CURING",
            "start_time":   st.strftime("%Y-%m-%d %H:%M:%S"),
            "end_time":     en.strftime("%Y-%m-%d %H:%M:%S"),
            "produce_qty":  int(r.get("qty", 0) or 0),
            "UOM":          "NOS",
            "lot_id":       f"CURE_{r.get('plan_id', '')}",
        })
    return pd.DataFrame(rows, columns=FLOOR_SCHEMA)


def append_curing_to_floor_schedule(cfg, curing_floor: pd.DataFrame):
    """Append curing rows to BOTH the DB table jkt_floor_endfwd_schedule and
    the on-disk file outputs2/phase5_floor_schedule_updated.csv."""
    if curing_floor is None or curing_floor.empty:
        print("  no curing rows to append to floor schedule")
        return

    # ---- DB: read existing -> concat -> TRUNCATE + INSERT ----
    try:
        from db_loader import get_engine, _resolve_source, OUTPUT_TABLES
        if _resolve_source(cfg) == "db":
            from sqlalchemy import inspect as _inspect, text as _text
            eng = get_engine(cfg)
            tbl = OUTPUT_TABLES["floor_schedule"]
            insp = _inspect(eng)
            existing = pd.DataFrame()
            if insp.has_table(tbl):
                existing = pd.read_sql(f"SELECT * FROM {tbl}", eng)
            # Align column sets so concat is safe regardless of which
            # extra columns Phase 5's output had.
            target_cols = list(existing.columns) if not existing.empty else FLOOR_SCHEMA
            for c in target_cols:
                if c not in curing_floor.columns:
                    curing_floor[c] = ""
            if existing.empty:
                combined = curing_floor[target_cols].copy()
            else:
                combined = pd.concat(
                    [existing[target_cols], curing_floor[target_cols]],
                    ignore_index=True,
                )
            with eng.begin() as conn:
                conn.execute(_text(f"TRUNCATE TABLE `{tbl}`"))
            combined.to_sql(tbl, eng, if_exists="append", index=False, chunksize=5000)
            print(f"  DB: {tbl} now has {len(combined):,} rows (+{len(curing_floor):,} curing)")
    except Exception as e:
        print(f"  [WARN] DB append (floor schedule) skipped: {e}")

    # ---- File: outputs2/phase5_floor_schedule_updated.csv ----
    fp = OUTPUTS / "phase5_floor_schedule_updated.csv"
    try:
        if fp.exists():
            existing_csv = pd.read_csv(fp, low_memory=False)
            target_cols = list(existing_csv.columns)
            for c in target_cols:
                if c not in curing_floor.columns:
                    curing_floor[c] = ""
            combined_csv = pd.concat([existing_csv, curing_floor[target_cols]], ignore_index=True)
        else:
            combined_csv = curing_floor.copy()
        _atomic_to_csv(combined_csv, fp)
        print(f"  File: {fp.name} now has {len(combined_csv):,} rows")
    except Exception as e:
        print(f"  [WARN] CSV append (floor schedule) skipped: {e}")


# ------------------------------------------------------------------ main
def main():
    print("\n" + "=" * 65)
    print("  PHASE 6 - Curing Schedule Revision (vs Building)")
    print("=" * 65)
    cfg = _load_cfg()

    plan = load_original_plan(cfg)
    print(f"  Original curing rows: {len(plan):,}")
    if plan.empty:
        print("  [WARN] no curing rows; nothing to revise")
        return 0

    build = load_building_schedule(cfg)
    if build.empty:
        print("  [WARN] no Building schedule rows found - mirroring original plan")

    revised = revise_curing_schedule(plan, build)
    print(f"  Revised curing rows : {len(revised):,}")

    # -------- write CSV (atomic) --------
    csv_path = OUTPUTS / "phase6_curing_revision_updated.csv"
    _atomic_to_csv(revised, csv_path)
    print(f"  Wrote: {csv_path}")

    # -------- write to MySQL (TRUNCATE + INSERT) --------
    try:
        from db_loader import get_engine, _resolve_source
        if _resolve_source(cfg) == "db":
            from sqlalchemy import inspect as _inspect, text as _text
            eng = get_engine(cfg)
            tbl = "jkt_curing_schedule_revised"
            insp = _inspect(eng)
            if insp.has_table(tbl):
                with eng.begin() as conn:
                    conn.execute(_text(f"TRUNCATE TABLE `{tbl}`"))
                revised.to_sql(tbl, eng, if_exists="append", index=False, chunksize=5000)
                print(f"  DB write: {tbl} refreshed ({len(revised):,} rows, TRUNCATE+INSERT)")
            else:
                revised.to_sql(tbl, eng, if_exists="replace", index=False, chunksize=5000)
                print(f"  DB write: {tbl} CREATED ({len(revised):,} rows)")
    except Exception as e:
        print(f"  [WARN] DB write skipped: {e}")

    # -------- append curing rows to the floor schedule --------
    curing_floor = curing_to_floor_rows(revised)
    print(f"  Curing rows to append to floor schedule: {len(curing_floor):,}")
    append_curing_to_floor_schedule(cfg, curing_floor)

    # -------- audit summary --------
    try:
        n_delayed = revised["remarks"].astype(str).str.contains("REVISED").sum()
        print(f"  Lots delayed: {int(n_delayed):,} / {len(revised):,}")
    except Exception:
        pass

    print(f"\n[OK] Phase 6 done")
    return 0


if __name__ == "__main__":
    import sys as _sys
    import traceback as _tb
    try:
        _rc = main()
        raise SystemExit(_rc if _rc is not None else 0)
    except SystemExit:
        raise
    except Exception:
        _sys.stderr.write(chr(10) + "!! PHASE CRASHED !!" + chr(10))
        _sys.stderr.flush()
        _tb.print_exc()
        _sys.exit(1)
