"""
run_june_wip.py — full CTP pipeline on the JUNE curing plan (2026-06-01 -> 2026-07-01)
WITH the real opening-WIP snapshot so Phase 1c actually nets demand.

  drum        : ctp_inputfiles/CTP_PCR_Curing_Schedule_2026-07-03 1 (1).xlsx  (June plan)
  opening_wip : ctp_inputfiles/opening_wip.csv   (built by build_wip_ctp.py, as-of 2026-06-02)
  outputs     : run_june_wip/outputs{,2}/  (phase-wise)

Usage:  python run_june_wip.py
"""
from __future__ import annotations
import os
import pipeline as P

RUN_DIR = "run_june_wip"


def main() -> int:
    cfg = P.load_config()
    cfg["inputs"]["opening_wip"] = "../ctp_inputfiles/opening_wip.csv"   # ENABLE MRP netting
    cfg["outputs_dir"] = f"{RUN_DIR}/outputs"
    cfg["outputs2_dir"] = f"{RUN_DIR}/outputs2"

    ctx = P.load_context(cfg)
    ctx["slice_skus"] = P.select_slice_skus(ctx, cfg)
    print(f"[run] curing plan : {ctx['drum_path']}")
    print(f"[run] opening WIP : {cfg['inputs']['opening_wip']}")
    print(f"[run] SKUs        : {len(ctx['slice_skus'])}   outputs -> {RUN_DIR}/\n")

    for key, title, module in P.PHASES:
        print(f"\n{'='*72}\n  {title}\n{'='*72}")
        ok, out, tb = P.run_phase(ctx, cfg, module)
        if out:
            print(out, end="")
        if not ok:
            print(f"\n[run] ABORT — {key} failed:\n{tb}")
            return 1

    print("\n================ KPI BLOCK (honest) ================")
    for k, v in P.kpis(ctx, cfg).items():
        print(f"  {k:22s}: {v}")
    print(f"  {'artifacts':22s}: {os.path.abspath(ctx['outputs2_dir'])}")
    print("====================================================\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
