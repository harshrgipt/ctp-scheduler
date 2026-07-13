#!/usr/bin/env python3
"""
phase1_5_wave_builder.py — assigns each curing block to a 3-day WAVE.

ALGORITHM
  1. Read the plan.
  2. Identify the curing-window start (earliest block) and end (latest block).
  3. Divide that span into N waves of `wave_duration_days` each (default 3 d).
  4. Each curing block falls into the wave whose interval contains its
     `effective_start` time.

OUTPUT
  outputs/phase1_5_waves.csv          one row per wave (wave_id, start, end, n_blocks, tyres)
  outputs/phase1_5_block_to_wave.csv  block_id -> wave_id
"""
from __future__ import annotations
import sys, pathlib, yaml, math
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT/"inputs"
OUTPUTS = ROOT/"outputs"
OUTPUTS.mkdir(exist_ok=True)

WAVE_DURATION_DAYS = 3   # mean tyre lead time
# Plant day = 07:00 IST → 07:00 IST next day (3 shifts: A 07-15, B 15-23, C 23-07).
# Wave boundaries snap to this 07:00 frame so a curing slot at 02:15 lives in
# yesterday's plant-day (and yesterday's wave), matching how operators read the calendar.
PLANT_DAY_START_HOUR = 7


def load_cfg(): return yaml.safe_load(open(ROOT/"config.yaml"))


def _read(p):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(p, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(p)

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


def parse_plan_blocks(plan):
    df = plan.copy()
    df["sku"] = df["skuCode"].astype(str).str.strip()
    df = df[df["sku"] != "CHANGEOVER"]
    df["qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df = df[df["qty"] > 0]
    # Plan CSV timestamps are naive IST. Parse as-is; only normalise if a row
    # happens to carry an explicit tz suffix (defensive — current file has none).
    parsed = pd.to_datetime(df["startTime"], errors="coerce")
    if hasattr(parsed, "dt") and parsed.dt.tz is not None:
        parsed = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    df["need_by"] = parsed
    df = df.dropna(subset=["need_by"]).reset_index(drop=True)
    df["block_id"] = [f"B{i:04d}" for i in range(len(df))]
    return df[["block_id","sku","qty","need_by"]]


def assign_waves(blocks, wave_dur_days, plan_horizon_end=None):
    """Assign each block to a wave based on need_by.

    plan_horizon_end: explicit horizon (e.g., 6/1 07:00 for May plan).
       If provided, the last wave extends to this timestamp regardless of
       last block's need_by. This honors the plant convention of plant-day
       running 07:00 -> next day 07:00 — so "May 31" plant-day actually
       runs through 6/1 07:00.

    Any blocks past the planned horizon (the small tail) are merged into the
    LAST wave so the schedule horizon is bounded to plan-only.
    """
    # Anchor plan_start to the plant-day 07:00 mark immediately ON or BEFORE the
    # first need_by.
    first = blocks["need_by"].min()
    plan_start = first.normalize() + pd.Timedelta(hours=PLANT_DAY_START_HOUR)
    if first < plan_start:
        plan_start -= pd.Timedelta(days=1)
    if plan_horizon_end is not None:
        plan_end_actual = plan_horizon_end
    else:
        plan_end_actual = blocks["need_by"].max()

    plan_span_days = (plan_end_actual - plan_start).total_seconds() / 86400
    n_waves = max(1, int(plan_span_days // wave_dur_days))
    # NEW: ANY tail beyond n_waves * wave_dur gets its OWN wave (was 0.5).
    # This prevents the last wave from being inflated by an extended tail.
    last_full_end = plan_start + pd.Timedelta(days = n_waves * wave_dur_days)
    tail_days = (plan_end_actual - last_full_end).total_seconds() / 86400
    if tail_days > 0.0:
        n_waves += 1

    # Build initial wave intervals
    def _build_intervals(n):
        ws = []
        for i in range(n):
            w_start = plan_start + pd.Timedelta(days = i * wave_dur_days)
            if i == n - 1:
                w_end = plan_end_actual + pd.Timedelta(seconds=1)
            else:
                w_end = plan_start + pd.Timedelta(days = (i+1) * wave_dur_days)
            ws.append({"wave_id": f"W{i+1:02d}",
                       "wave_start": w_start, "wave_end": w_end})
        return ws

    waves = _build_intervals(n_waves)

    def _assign(waves):
        wid_for = {}
        for _, b in blocks.iterrows():
            t = b["need_by"]
            placed = False
            for w in waves:
                if w["wave_start"] <= t < w["wave_end"]:
                    wid_for[b["block_id"]] = w["wave_id"]
                    placed = True
                    break
            if not placed:
                wid_for[b["block_id"]] = waves[-1]["wave_id"]
        return wid_for

    wave_id_for = _assign(waves)

    # === WAVE-BALANCE PASS ===
    # Iteratively split overloaded waves (tyres > overload_threshold * average)
    # at their midpoint. Stops after 3 passes.
    OVERLOAD_THRESHOLD = 1.20  # 20% above average -> split
    block_qty = dict(zip(blocks["block_id"], blocks["qty"]))
    for pass_n in range(3):
        wave_tyres = {}
        for bid, wid in wave_id_for.items():
            wave_tyres[wid] = wave_tyres.get(wid, 0) + float(block_qty.get(bid, 0))
        if not wave_tyres:
            break
        avg = sum(wave_tyres.values()) / len(wave_tyres)
        cap = avg * OVERLOAD_THRESHOLD
        overloaded_ids = {w_id for w_id, t in wave_tyres.items() if t > cap}
        if not overloaded_ids:
            break
        new_waves = []
        for w in waves:
            if w["wave_id"] in overloaded_ids:
                mid = w["wave_start"] + (w["wave_end"] - w["wave_start"]) / 2
                new_waves.append({"wave_id": w["wave_id"] + "_a",
                                  "wave_start": w["wave_start"], "wave_end": mid})
                new_waves.append({"wave_id": w["wave_id"] + "_b",
                                  "wave_start": mid, "wave_end": w["wave_end"]})
            else:
                new_waves.append(w)
        # Re-number all waves sequentially after split
        for i, w in enumerate(new_waves):
            w["wave_id"] = f"W{i+1:02d}"
        waves = new_waves
        wave_id_for = _assign(waves)

    return waves, wave_id_for


def main():
    print("\n" + "=" * 65)
    print("  PHASE 1.5 — Wave Builder  (3-day waves)")
    print("=" * 65)
    cfg = load_cfg()
    fp  = cfg["files"]
    plan = _db_or_csv("plan", cfg)
    blocks = parse_plan_blocks(plan)
    print(f"  Curing blocks: {len(blocks):,}")
    print(f"  Block window:  {blocks['need_by'].min()}  →  {blocks['need_by'].max()}")
    print(f"  Total tyres:   {int(blocks['qty'].sum()):,}")
    print(f"  Wave duration: {WAVE_DURATION_DAYS} days")

    # PLAN HORIZON END: extend last wave to the day AFTER the last need_by date
    # at 07:00 — plant convention "plant-day X" runs from X 07:00 to X+1 07:00.
    # So a curing block on 5/31 23:00 belongs to plant-day 5/31 which ends 6/1 07:00.
    last_need_by = blocks["need_by"].max()
    plan_horizon_end = last_need_by.normalize() + pd.Timedelta(days=1, hours=PLANT_DAY_START_HOUR)
    # If last need_by is exactly AT or after 07:00 on its day, we cover that day.
    # If before 07:00, the previous plant-day covers it — back off by 1.
    if last_need_by.hour < PLANT_DAY_START_HOUR:
        plan_horizon_end -= pd.Timedelta(days=1)
    print(f"  Plan horizon end: {plan_horizon_end}  (last need_by {last_need_by} -> next plant-day 07:00)")

    waves, wave_id_for = assign_waves(blocks, WAVE_DURATION_DAYS, plan_horizon_end=plan_horizon_end)
    print(f"\n  Waves generated: {len(waves)}")

    blocks["wave_id"] = blocks["block_id"].map(wave_id_for)
    wave_summary = blocks.groupby("wave_id").agg(
        n_blocks=("block_id","count"),
        n_SKUs=("sku","nunique"),
        tyres=("qty","sum"),
        first_need=("need_by","min"),
        last_need=("need_by","max"),
    ).reset_index()

    # Merge with wave bounds
    waves_df = pd.DataFrame(waves)
    wave_summary = wave_summary.merge(waves_df, on="wave_id", how="left")

    print(f"\n  PER-WAVE SUMMARY:")
    print(f"    {'wave_id':>7s}  {'wave_start':>20s}  {'wave_end':>20s}  {'blocks':>6s}  {'SKUs':>5s}  {'tyres':>8s}")
    for _, r in wave_summary.iterrows():
        print(f"    {r['wave_id']:>7s}  {str(r['wave_start'])[:19]:>20s}  {str(r['wave_end'])[:19]:>20s}  "
              f"{r['n_blocks']:>6d}  {r['n_SKUs']:>5d}  {int(r['tyres']):>8,}")

    avg_tyres = wave_summary["tyres"].mean()
    print(f"\n  Average tyres/wave: {avg_tyres:,.0f}")
    print(f"  Tyres/day average:  {avg_tyres / WAVE_DURATION_DAYS:,.0f}")
    max_t = wave_summary['tyres'].max()
    min_t = wave_summary['tyres'].min()
    print(f"  Max wave tyres: {max_t:,.0f}  Min wave tyres: {min_t:,.0f}  Spread: {max_t/min_t:.2f}x")

    wave_summary.to_csv(OUTPUTS/"phase1_5_waves.csv", index=False)
    blocks[["block_id","sku","qty","need_by","wave_id"]].to_csv(
        OUTPUTS/"phase1_5_block_to_wave.csv", index=False)

    with pd.ExcelWriter(OUTPUTS/"phase1_5_wave_summary.xlsx", engine="openpyxl") as w:
        wave_summary.to_excel(w, sheet_name="waves", index=False)
        blocks.head(10000).to_excel(w, sheet_name="blocks_sample", index=False)

    print(f"\n[OK] phase1_5_waves.csv (waves)")
    print(f"     phase1_5_block_to_wave.csv (per-block)")
    print(f"     phase1_5_wave_summary.xlsx")
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
