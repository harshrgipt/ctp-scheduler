"""
phase1.5 — wave builder.

Slices the curing horizon into fixed 3-day waves anchored on the 07:00 IST
plant-day frame; each curing block falls into the wave whose interval contains
its need_by (press start). In the thin slice, only wave 0 is kept.
"""
from __future__ import annotations
import os
import pandas as pd


def run(ctx: dict, cfg: dict) -> dict:
    drum = ctx["drum"]
    skus = ctx["slice_skus"]
    wave_days = cfg["wave_duration_days"]
    hour = cfg["plant_day_start_hour"]

    prod = drum[(~drum["is_occupancy"]) & (drum["sku"].isin(skus))
                & (~drum["block_id"].isin(ctx["bad_cure_blocks"]))].copy()
    prod["need_by"] = prod["start_ts"]

    first = prod["need_by"].min()
    # Anchor to the 07:00 plant-day mark on/before the first need_by.
    anchor = first.normalize() + pd.Timedelta(hours=hour)
    if anchor > first:
        anchor -= pd.Timedelta(days=1)
    ctx["plan_start"] = anchor

    span = pd.Timedelta(days=wave_days)
    prod["wave_id"] = ((prod["need_by"] - anchor) // span).astype(int)

    b2w = prod[["block_id", "sku", "need_by", "wave_id"]].copy()
    ctx["block_to_wave"] = b2w

    waves = (b2w.groupby("wave_id")
             .agg(n_blocks=("block_id", "size"),
                  wave_start=("need_by", "min"), wave_end=("need_by", "max"))
             .reset_index())
    waves.to_csv(os.path.join(ctx["outputs_dir"], "phase1_5_waves.csv"), index=False)
    b2w.to_csv(os.path.join(ctx["outputs_dir"], "phase1_5_block_to_wave.csv"), index=False)

    if cfg["slice"].get("one_wave_only"):
        keep = set(b2w.loc[b2w["wave_id"] == 0, "block_id"])
        ctx["slice_blocks"] = keep
        ctx["demand"] = ctx["demand"][ctx["demand"]["block_id"].isin(keep)].reset_index(drop=True)
    else:
        ctx["slice_blocks"] = set(b2w["block_id"])

    print(f"[phase1.5] waves={len(waves)} anchor={anchor} "
          f"| wave0 blocks={len(ctx['slice_blocks'])} "
          f"| demand rows now={len(ctx['demand'])}")
    return ctx
