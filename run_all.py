#!/usr/bin/env python3
"""
run_all.py — one command, clean machine to finished schedule.

    python run_all.py

Does exactly two things, in order:
    1. adapt_inputs.py   CTP masters (ctp_inputfiles/) -> v6's input schema (inputs/)
    2. run_pipeline.py   v6's phases, in v6's order, on that data

Everything is resolved relative to THIS FILE, so it does not matter where you run it from.
"""
from __future__ import annotations
import os
import sys
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    # v6's phases print unicode (arrows). A Windows console is cp1252 and raises
    # UnicodeEncodeError on them. This changes only how output is ENCODED, never what
    # any phase computes.
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")

    # Any CLI args (--from / --only / --skip) belong to run_pipeline; adapt_inputs takes none.
    for script, args in (("adapt_inputs.py", []), ("run_pipeline.py", sys.argv[1:])):
        print("\n" + "#" * 74)
        print(f"#  {script}")
        print("#" * 74)
        r = subprocess.run([sys.executable, os.path.join(HERE, script)] + args,
                           cwd=HERE, env=env)
        if r.returncode != 0:
            print(f"\n  {script} FAILED (exit {r.returncode}). Stopping.")
            print("  If this is the phase-2 proc_time blocker, see README.md -> "
                  "'Known blocker'.")
            return r.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
