"""
run_pipeline.py — CTP (PCR) scheduler CLI orchestrator.

Self-contained, file-based. Loads all inputs once, selects the SKU set (all SKUs
when slice is off), then runs phase0 -> phase6 in order (including the new phase1c
WIP/MRP netting), printing each phase's output and an honest KPI block at the end.

Usage:
  python run_pipeline.py                          # full plant (all SKUs)
  python run_pipeline.py path/to/curing.xlsx      # override the drum
"""
from __future__ import annotations
import sys

import pipeline as P


def main(drum_override: str | None = None) -> int:
    cfg = P.load_config()
    ctx = P.load_context(cfg, drum_override=drum_override)
    ctx["slice_skus"] = P.select_slice_skus(ctx, cfg)
    print(f"[run] curing schedule: {ctx['drum_path']}")
    print(f"[run] scheduling {len(ctx['slice_skus'])} SKU(s) "
          f"(slice.enabled={cfg['slice'].get('enabled')})\n")

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
    print(f"  {'artifacts':22s}: {ctx['outputs2_dir']}")
    print("====================================================\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else None))
