"""
run_0703.py — run the CTP scheduler on the 2026-07-03 curing schedule and save
every phase's output into a dedicated folder: run_2026-07-03/outputs{,2}/.

WIP netting is intentionally a no-op for this run (no opening-WIP file yet).

Usage:  python run_0703.py
"""
from __future__ import annotations
import os
import pipeline as P

RUN_DIR = "run_2026-07-03"


def main() -> int:
    cfg = P.load_config()
    # Drum + all masters come from ctp_inputfiles (see config.yaml). No WIP for this run.
    cfg["inputs"]["opening_wip"] = None
    # Phase-wise outputs into a dedicated dated folder.
    cfg["outputs_dir"] = f"{RUN_DIR}/outputs"
    cfg["outputs2_dir"] = f"{RUN_DIR}/outputs2"

    ctx = P.load_context(cfg)
    ctx["slice_skus"] = P.select_slice_skus(ctx, cfg)
    print(f"[run] curing schedule: {ctx['drum_path']}")
    print(f"[run] scheduling {len(ctx['slice_skus'])} SKU(s); outputs -> {RUN_DIR}/\n")

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
