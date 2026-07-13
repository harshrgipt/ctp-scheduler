#!/usr/bin/env python3
"""
run_pipeline.py — run v6's phases, unmodified, on CTP data.

`phases/*.py` are byte-identical copies of `v6_wave_p2/phases/*.py`. They are standalone
scripts: each loads its own inputs and writes its own outputs. We invoke them as
subprocesses, exactly as v6's own orchestrator does, so nothing about their behaviour
changes.

Phase order is v6's. phase1a (MG) is NOT run — but its output IS synthesised by
adapt_inputs.py, because CTP's BOM is single-variant and v6's MG filter is therefore a
no-op. See MEMORY.md.

    phase0 -> phase1b -> phase1_5 -> phase1c -> phase2 -> phase3 -> phase4 -> phase5 -> phase6

Run `python adapt_inputs.py` first.

Usage:
    python run_pipeline.py
    python run_pipeline.py --from phase2
    python run_pipeline.py --only phase5
"""
from __future__ import annotations
import os
import sys
import time
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))

PHASES = [
    ("phase0",   "phase0_validate_inputs.py",      "Validate inputs (report only)"),
    ("phase1b",  "phase1b_demand_explosion.py",    "Demand explosion"),
    ("phase1_5", "phase1_5_wave_builder.py",       "Wave builder (+ balance pass)"),
    ("phase1c",  "phase1c_mrp_netting.py",         "MRP / WIP netting"),
    ("phase2",   "phase2_lot_sizing.py",           "Lot sizing"),
    ("phase3",   "phase3_dag_construction.py",     "DAG construction"),
    ("phase4",   "phase4_cpm.py",                  "CPM time windows"),
    ("phase5",   "phase5_forward_placement_v2.py", "Forward placement"),
    ("phase6",   "phase6_curing_revision.py",      "Curing revision"),
]


def preflight() -> None:
    """The checks that stop a SILENTLY-wrong plan. Each maps to a MEMORY.md hazard."""
    import pandas as pd
    inp = os.path.join(HERE, "inputs")
    if not os.path.exists(os.path.join(inp, "routing.csv")):
        sys.exit("inputs/ is empty — run `python adapt_inputs.py` first.")

    it = pd.read_csv(os.path.join(inp, "itemtype_master.csv"), dtype=str)
    types = {str(t).strip().upper() for t in it["ItemType"].dropna()}
    if "GREEN TYRES" not in types:
        sys.exit("PREFLIGHT FAIL (hazard #2): nothing is typed 'Green Tyres'. v6 keys "
                 "BUILDING_ITYPES and AGE_OVERRIDE_H on that exact string — without it every "
                 "green tyre takes the wrong placement path and its aging silently becomes "
                 "(8h, 8760h) instead of (0, 72).")

    rt = pd.read_csv(os.path.join(inp, "routing.csv"), dtype=str)
    depts = {str(d).upper() for d in rt["department"].dropna()}
    if not any(tok in d for d in depts for tok in ("BUILD", "TBM", "CURING")):
        sys.exit("PREFLIGHT FAIL (hazard #1): no department contains BUILD/TBM/CURING. "
                 "v6's cascade_orphans would treat EVERY lot as an orphan and place nothing, "
                 "without error.")

    if not os.path.exists(os.path.join(HERE, "outputs", "mg_assignment.csv")):
        sys.exit("PREFLIGHT FAIL: outputs/mg_assignment.csv missing. v6's phase1b and phase3 "
                 "abort without it. Run `python adapt_inputs.py`.")

    stale = os.path.join(HERE, "outputs2", "WIP_simulation_May2026.csv")
    if os.path.exists(stale):
        sys.exit(f"PREFLIGHT FAIL (hazard #5): {stale} exists. v6's phase5 would net WIP a "
                 "SECOND time on top of phase1c and double-count the stock. Delete it.")

    gt = int((it["ItemType"].astype(str).str.strip().str.upper() == "GREEN TYRES").sum())
    print(f"  preflight OK — {gt} green tyres typed | BUILDING + CURING departments present")


def run_phase(key: str, script: str, title: str) -> bool:
    print("\n" + "=" * 74)
    print(f"  {key.upper():<9} {title}")
    print("=" * 74)
    t0 = time.time()
    # v6's phases print unicode (arrows, symbols). The Windows console is cp1252 and
    # raises UnicodeEncodeError on them. Force UTF-8 for the child's stdio — this changes
    # only how output is ENCODED, never what the phase computes.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    r = subprocess.run([sys.executable, os.path.join(HERE, "phases", script)],
                       cwd=HERE, env=env)
    dt = time.time() - t0
    if r.returncode != 0:
        print(f"\n  [{key}] FAILED after {dt:.0f}s (exit {r.returncode})")
        return False
    print(f"  [{key}] ok in {dt:.0f}s")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="start")
    ap.add_argument("--only")
    ap.add_argument("--skip", nargs="*", default=[])
    a = ap.parse_args()

    os.makedirs(os.path.join(HERE, "outputs"), exist_ok=True)
    os.makedirs(os.path.join(HERE, "outputs2"), exist_ok=True)

    print("=" * 74)
    print("  CTP scheduler — v6_wave_p2 algorithm, unmodified")
    print("=" * 74)
    preflight()

    todo = PHASES
    if a.only:
        todo = [p for p in PHASES if p[0] == a.only]
    elif a.start:
        keys = [p[0] for p in PHASES]
        if a.start not in keys:
            sys.exit(f"unknown phase {a.start!r}; expected one of {keys}")
        todo = PHASES[keys.index(a.start):]
    todo = [p for p in todo if p[0] not in a.skip]

    t0 = time.time()
    for key, script, title in todo:
        if not run_phase(key, script, title):
            return 1
    print("\n" + "=" * 74)
    print(f"  DONE — {len(todo)} phases in {time.time()-t0:.0f}s")
    print("=" * 74)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
