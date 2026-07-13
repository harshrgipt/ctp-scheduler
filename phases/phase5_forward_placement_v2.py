#!/usr/bin/env python3
"""
phase5_forward_placement.py -- Forward MRP placement with JIT + FRC campaigns.

KEY CHANGES (FRC release):
  1. Dispatch sort: (first_need asc, lot_id) -> natural interleave.
  2. Strict JIT placement -- every lot scheduled at target_finish = first_need - min_aging.
     Predecessor wins if pred.finish + min_aging > target_start.
  3. FRC campaigns placed atomically as one block on FRC machine.
     Same wire consecutive -> no cooldown. Different wire -> 6 h cool-down.
  4. Same-item cap implicit via (first_need asc) dispatch.

Optimised with bisect for first_fit + insort, and precomputed item aging in ns.
"""
from __future__ import annotations
import sys, pathlib, yaml, re, bisect, os
import pandas as pd
import numpy as np
from collections import defaultdict, deque

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS  = ROOT/"inputs"
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

import re as _re

DEFAULT_CHANGEOVER_MIN = 15
MIXING_CHANGEOVER_MIN  = 2
INTRA_CAMP_CHANGEOVER_MIN = 0
PRE_CURING_BUFFER_H    = 72.0
RESPECT_LFT            = False
LFT_GRACE_HOURS        = 24
MAX_HORIZON_DAYS       = 365

# === TARGET WIP HOURS (rolling-buffer maintenance per item-type) ===
# Producer must start no LATER than (consumer_first_need - target_wip_hours).
# This keeps a N-hour buffer alive throughout the schedule — plant never lets
# WIP deplete to zero before producing replacement.
TARGET_WIP_HOURS = {
    "FINAL COMPOUND":               72.0,   # avg max_aging 108h -> 72h buffer is safe
    "SideWall":                     24.0,
    "Tread":                        24.0,
    "Inner Liner":                  24.0,
    "Cap Strip":                     8.0,   # short aging 24h, tight buffer
    "Carcass":                      16.0,   # 24h max_aging - 8h safety = 16h buffer
                                            # Stage-1 produces ahead so Stage-2 (6 mach) is fed
                                            # continuously from carcass WIP pool of 15 Stage-1 machines
    "Green Tyres":                   8.0,   # 48h max_aging - shorter buffer, GT goes to curing fast
    "Bead Bundle":                  12.0,
    "Bead Apex":                    12.0,
    "Rubberized Steel Belt":        12.0,
    "Rubberized Ply":               12.0,
    "Rubberized Steel Belt-mother roll": 24.0,
    "Rubberized Ply-mother roll":   24.0,
    "Pre Cap Strip":                 8.0,
    "Chaffer":                      12.0,
    "Steel Belt Edge Strip":        12.0,
    "Steel Belt":                   12.0,
    "Apex":                          8.0,
    "Ply":                          12.0,
    "CALANDARED ROLL":              24.0,
    "PRE CUT ROLL MATERIAL":         8.0,
    "Bead Wire":                    24.0,
}

# V6_wave: ASAP for upstream / LFT for terminal building+curing.
TERMINAL_DEPT_TOKENS = ("BUILD", "TBM", "CURING")

# === PARENT-AWARE CHANGEOVER (cutter family) ===
PARENT_AWARE_CUTTERS = {"WBC", "WBC NEW", "LTBC", "LTBCNew", "LTBC NEW", "HTBC", "FISCHER"}
_PARENT_RE = _re.compile(r'^([A-Z]+\d+(?:_B\d+|_OUTER)?)')

def parent_of(item_code):
    if not item_code: return ""
    m = _PARENT_RE.match(str(item_code).strip())
    return m.group(1) if m else str(item_code).strip()

# === PER-MACHINE CHANGEOVER RULES (item-type-aware extruders + flat) ===
# Triplex/Quadraplex: same item-type = 5 min, cross-type (e.g. SW<->Tread) = 50 min
TRIPLEX_LIKE = {"TRIPLEX", "QUADRAPLEX"}
# Duplex: flat 5 min always
DUPLEX_FLAT_MIN = 5
# Dual / Quintuplex: flat 6 min always
DUAL_FLAT_MIN = 6
QUINTUPLEX_FLAT_MIN = 6

# === CUTTER MOTHER-ROLL MODEL (production-scheduling-realistic for tyre cutters) ===
# Plant reality: when a mother roll (e.g., CPJ120 = 400,000 MM lot from FRC) is
# loaded onto a cutter, the operator runs ALL the dim-variant cuts they can from
# THAT roll (different widths/angles) before swapping. 3 min between cuts (just
# re-set width/angle). 15 min to swap to a new mother roll (any new lot_id,
# even same parent, requires physical mount/unmount = different MHE).
MOTHER_ROLL_MAX_MM = 400_000.0   # mpq_max for Rubberized Steel Belt-mother roll
LFT_PRESSURE_THRESHOLD_H = 12.0  # if other-parent lot has <12h slack, force switch
# Per-cutter runtime state (initialized in main()):
#   cutter_loaded_lot_id[m]    -> str  (specific FRC mother-roll lot loaded)
#   cutter_loaded_parent[m]    -> str  (e.g., "CPJ120")
#   cutter_roll_remaining_mm[m] -> float (MM left on the loaded mother roll)


def compute_changeover_for_cutter(machine, prev_item, curr_item, curr_qty,
                                   mm_per_cut_map,
                                   cutter_loaded_parent, cutter_loaded_lot_id,
                                   cutter_roll_remaining_mm,
                                   curr_lot_id=None):
    """Cutter-specific changeover with mother-roll life-cycle model.
    Returns (changeover_min, mm_consumed, will_swap_roll).
    """
    if not prev_item:  # first lot on machine — no changeover, fresh roll
        mother_roll_parent, mm_per_unit = mm_per_cut_map.get(curr_item, (None, 0.0))
        mm_needed = curr_qty * mm_per_unit
        return 0, mm_needed, True  # treat first lot as "loading roll"
    parent_curr = parent_of(curr_item)
    parent_loaded = cutter_loaded_parent.get(machine, "")
    mother_roll_parent, mm_per_unit = mm_per_cut_map.get(curr_item, (None, 0.0))
    mm_needed = curr_qty * mm_per_unit
    roll_left = cutter_roll_remaining_mm.get(machine, 0.0)
    # Case A: same parent loaded AND roll has enough MM left -> 3 min cut re-set
    if parent_loaded == parent_curr and roll_left >= mm_needed:
        return 3, mm_needed, False  # no swap, just adjust width/angle
    # Case B/C: must swap to a new mother roll (15 min)
    return 15, mm_needed, True


# ═══ CTP DEVIATION #3 — THE PLANT CHANGEOVER MATRIX ════════════════════════════════
# v6 has NO changeover matrix: `compute_changeover_min` below derives setup minutes from
# machine NAMES (WBC / LTBC / HTBC / FISCHER / DUPLEX / TRIPLEX / …). NONE of CTP's 123
# numeric machine IDs match ANY of those branches — so under v6's own rule EVERY CTP
# machine falls to a flat DEFAULT_CHANGEOVER_MIN = 15 (or 2 for mixing). The user supplied
# a real plant matrix and instructed it be used.
#
# Keyed on (LINE, from_item, to_item). The matrix's `machine` column holds 22 LINE NAMES
# ("4 RC Calendar", "Belt Cutter PCR") while the routing holds numeric IDs ("901", "3409") —
# ZERO string overlap. The bridge is `machine_to_changeover_line` in config.yaml, built from
# the plant's own machine list. See adapt_inputs.changeover().
#
# Both populated in main(). Empty dicts = pure v6 behaviour.
CHANGEOVER_LUT:  dict[tuple[str, str, str], float] = {}   # (line, from_item, to_item) -> min
MACHINE_TO_LINE: dict[str, str] = {}                       # routing machine id -> line name


def compute_changeover_min(machine, dept, prev_item, prev_itype, curr_item, curr_itype):
    """Returns changeover minutes for THIS lot (after prev lot on the same machine).
    Production-scheduling-realistic rule set per machine.
    NOTE: For CUTTER family, this is a FALLBACK; the dispatcher uses
    compute_changeover_for_cutter() which is parent+roll-state-aware.
    """
    if not prev_item:  # first lot on machine — no changeover
        return 0
    # CTP: the real plant matrix WINS wherever it has a rule for this (line, item-pair).
    # A machine with no line, or a pair the matrix does not carry, falls through to v6's
    # own rules below, unchanged.
    if CHANGEOVER_LUT:
        line = MACHINE_TO_LINE.get(str(machine or "").strip().strip('"').strip())
        if line:
            hit = CHANGEOVER_LUT.get(
                (line, str(prev_item).strip(), str(curr_item).strip()))
            if hit is not None:
                return hit
    m_up = str(machine or "").upper()
    d_up = str(dept or "").upper()
    # Cutter family — parent-aware fallback (replaced by full mother-roll model in dispatcher)
    if str(machine).strip() in PARENT_AWARE_CUTTERS:
        return 3 if parent_of(prev_item) == parent_of(curr_item) else 15
    # Duplex — flat 5
    if "DUPLEX" in m_up:
        return DUPLEX_FLAT_MIN
    # Quintuplex — flat 6
    if "QUINTUPLEX" in m_up:
        return QUINTUPLEX_FLAT_MIN
    # Triplex / Quadraplex — item-type-aware
    if any(t in m_up for t in TRIPLEX_LIKE):
        if str(prev_itype).strip() == str(curr_itype).strip():
            return 5
        return 50
    # Dual — flat 6 (be careful: must not match "DUAL" inside other names; check token)
    if m_up == "DUAL" or m_up.startswith("DUAL "):
        return DUAL_FLAT_MIN
    # Mixing / Final Mixing — 2 min (existing behavior)
    if "MIX" in d_up:
        return MIXING_CHANGEOVER_MIN
    # Default
    return DEFAULT_CHANGEOVER_MIN


def build_mm_per_cut_map(bom_df):
    """From BOM, build: cut_item_code -> (mother_roll_parent_code, mm_per_unit).
    Looks for BOM rows where Parent looks like 'CPJ120-108MM/29°' (dim-variant)
    and child is the mother roll (e.g., 'CPJ120') with child_Unit == MM.
    """
    out = {}
    if bom_df is None or bom_df.empty:
        return out
    # The BOM row format: Parent (cut), child (mother roll), child_quantity (MM per cut)
    for _, r in bom_df.iterrows():
        par = str(r.get("Parent", "")).strip()
        ch  = str(r.get("child", "")).strip()
        unit = str(r.get("child_Unit", "")).strip().upper()
        try:
            qty = float(r.get("child_quantity", 0))
        except (TypeError, ValueError):
            qty = 0
        if not par or not ch or qty <= 0 or unit != "MM":
            continue
        # Heuristic: a "cut variant" parent has a hyphen-dimension suffix like -108MM/
        if "-" not in par or "MM" not in par.upper():
            continue
        # If we already have a row for this cut item, keep first
        if par in out:
            continue
        out[par] = (ch, qty)
    return out

# === AEST CAPACITY-AWARE for compounds (replaces blanket NO_AEST) ===
# Old behavior: MASTER/FINAL COMPOUND bypass AEST → ASAP from schedule_open → 706h
# producer-consumer gaps when consumer is pulled late by downstream bottleneck.
# New behavior: target start = max(earliest, consumer_first_need - max_aging - safety)
# This keeps mixer fed but never produces > max_aging ahead of consumer.
NO_AEST_ITEM_TYPES = set()  # disabled — use capacity-aware AEST for everyone
AGING_SAFETY_BUFFER_H = 6   # 6h buffer before producer can age out

# === PRODUCTION DE-PULSE (binding feeders -> JIT-to-deadline) ===
# Deadlines are already smooth daily (phase-2 de-pulse), but AEST pulls feeder
# production EARLY, which fills the shared upstream machines with future demand
# and DEFERS the near-term feeders into bursts (e.g. tread makes nothing 05-03/04/05
# then dumps 2.5 days on 05-06) -> kit-readiness lumps -> building dips. Placing the
# binding feeders JIT (finish at their already-smooth deadline) makes their production
# track the level daily demand, so kits flow evenly. Mixing/FRC are intentionally
# excluded (over-splitting them caused changeover/util blow-ups before).
FEEDER_JIT_TYPES = {"Tread", "SideWall", "Inner Liner", "Rubberized Ply",
                    "Cap Strip", "Pre Cap Strip"}
FEEDER_JIT_BUFFER_H = 4.0   # finish a few hours before the deadline (small safety)

def _is_terminal_dept(dept_str: str) -> bool:
    d = str(dept_str or "").upper()
    return any(tok in d for tok in TERMINAL_DEPT_TOKENS)


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


def load_cfg(): return yaml.safe_load(open(ROOT/"config.yaml"))


def aging_hours(v, u):
    try: v = float(v)
    except (TypeError, ValueError): return 0.0
    u = str(u or "").strip().upper()
    if u.startswith("DAY"): return v*24.0
    if u.startswith("MIN"): return v/60.0
    return v


def build_aging_map(am):
    out = {}
    for _, r in am.iterrows():
        code = str(r["ItemCode"]).strip()
        if code:
            out[code] = (aging_hours(r["MinAging"], r["MinAgingUnit"]),
                         aging_hours(r["MaxAging"], r["MaxAgingUnit"]))
    return out


def _load_csv(name_candidates):
    for n in name_candidates:
        p = OUTPUTS/_suff(n)
        if p.exists() and p.is_file():
            df = pd.read_csv(p, low_memory=False)
            print(f"  loaded {p.name}  rows={len(df):,}", flush=True)
            return df
    raise FileNotFoundError(f"no file found from {name_candidates}")


def parse_machines(s):
    if pd.isna(s) or not s: return []
    out = []
    for tok in str(s).split(","):
        m = tok.strip().strip("'\"")
        if m and m.upper() not in ("NAN","NONE",""):
            out.append(m)
    return out


def first_fit_jit(busy, earliest_ns, target_start_ns, duration_ns):
    """JIT-aware first-fit (bisect-optimised). busy = sorted [(start_ns,end_ns)]."""
    n = len(busy)
    if n == 0:
        return max(earliest_ns, target_start_ns)
    last_end = busy[-1][1]
    if target_start_ns >= last_end and target_start_ns >= earliest_ns:
        return target_start_ns
    if earliest_ns >= last_end:
        return max(earliest_ns, target_start_ns)
    idx = bisect.bisect_left(busy, (earliest_ns,))
    cur = earliest_ns
    if idx > 0:
        prev_end = busy[idx - 1][1]
        if prev_end > cur:
            cur = prev_end
    best = None
    for i in range(idx, n):
        s, e = busy[i]
        if s > cur and (s - cur) >= duration_ns:
            place_start = max(cur, min(target_start_ns, s - duration_ns))
            if place_start >= target_start_ns:
                return place_start
            if best is None or place_start > best:
                best = place_start
        if e > cur:
            cur = e
    place_start = max(cur, target_start_ns)
    if best is None or place_start > best:
        best = place_start
    return best


def main():
    print("\n" + "=" * 65)
    print("  PHASE 5 - Forward Placement (JIT + FRC campaigns)")
    print("=" * 65)
    cfg = load_cfg()
    pre_curing_buffer_h = float(cfg.get("pre_curing_buffer_h", PRE_CURING_BUFFER_H))
    frc_cooldown_min    = float(cfg.get("frc_cooldown_min", 360))
    print(f"  pre_curing_buffer_h: {pre_curing_buffer_h}")
    print(f"  frc_cooldown_min:    {frc_cooldown_min}")

    print("\n  Loading inputs...")
    lots = _load_csv(["phase2_lots.csv","phase2_lots_v2.csv"])
    edges = _load_csv(["phase3_dag_edges.csv.gz","phase3_dag_edges.csv"])
    blocks = _load_csv(["phase2_lot_blocks.csv.gz","phase2_lot_blocks.csv"])
    times = _load_csv(["phase4_lot_times.csv"])
    am = _db_or_csv("aging_master", cfg)
    aging_map = build_aging_map(am)

    blocks["need_by"] = pd.to_datetime(blocks["need_by"], errors="coerce")
    first_curing = blocks["need_by"].min()

    # V1 fix: anchor T0 (schedule turn-on) at plan.startTime.min().
    # All machines begin from this moment — not earlier and not later.
    plan_start = None
    try:
        _plan_df = _db_or_csv("plan", cfg)
        _st_col = pd.to_datetime(_plan_df["startTime"], errors="coerce")
        if _st_col.notna().any():
            plan_start = _st_col.min()
    except Exception as _pe:
        print(f"  [WARN] could not derive plan.startTime.min ({_pe})")
    if plan_start is not None and not pd.isna(plan_start):
        T0 = plan_start
        print(f"  T0 = plan.startTime.min() = {T0}  (all machines start here)")
    else:
        T0 = first_curing - pd.Timedelta(hours=pre_curing_buffer_h)
        print(f"  T0 (fallback) = {T0}")

    lots["duration_h"]  = pd.to_numeric(lots["duration_hours"], errors="coerce").fillna(0)
    lots["machines_l"]  = lots["machines"].apply(parse_machines)
    # --- BUILDING machine eligibility STRICTLY FROM ROUTING (operation + product) ---
    # Stage-1 carcass (15 machines) and Stage-2 GT (24 machines) are DISJOINT pools;
    # carcass feeds GT in the two-stage process, while single-stage groups (BJ /
    # Unistage / VMIMaxx) build the whole GT in one go on a stage-2 machine. Upstream
    # lot 'machines' can merge both stages, which put ~half of GT lots onto carcass
    # machines. Take each building lot's eligible machines from the ROUTING's own
    # operation-specific machine list for that routed_product, so GT only ever runs
    # on stage-2 GT machines and carcass only on stage-1 machines.
    try:
        _rt = _db_or_csv("routing", cfg)
        _route_m, _op_all = {}, {}
        for _, _rr in _rt.iterrows():
            _opn  = str(_rr.get("operation_name", "")).strip().upper()
            _prod = str(_rr.get("routed_product", "")).strip()
            _ms   = set(parse_machines(_rr.get("machines", "")))
            if not _ms:
                continue
            _route_m.setdefault((_opn, _prod), set()).update(_ms)
            _op_all.setdefault(_opn, set()).update(_ms)
        _BUILD_OPS = {"GT BUILDING", "CAR BUILDING"}
        def _route_machines(row):
            op = str(row["operation"]).strip().upper()
            if op not in _BUILD_OPS:
                return row["machines_l"]
            ms = _route_m.get((op, str(row["item_code"]).strip()))
            if ms:
                return sorted(ms)
            # product not matched by code -> restrict to the operation's machine pool
            # (never let a GT lot fall back onto carcass machines, and vice-versa)
            pool = _op_all.get(op, set())
            keep = [m for m in row["machines_l"] if m in pool]
            return keep if keep else sorted(pool)
        lots["machines_l"] = lots.apply(_route_machines, axis=1)
        print(f"  Building eligibility from routing: "
              f"GT={len(_op_all.get('GT BUILDING', []))} machines, "
              f"CAR={len(_op_all.get('CAR BUILDING', []))} machines")
    except Exception as _re:
        print(f"  [WARN] could not load routing machine pools ({_re}); "
              f"using upstream lot eligibility")
    lots["is_belt"]     = lots["is_belt"].astype(str).str.lower().isin(("true","1","yes","y"))
    lots["first_need"]  = pd.to_datetime(lots["first_need"], errors="coerce")
    lots["last_need"]   = pd.to_datetime(lots["last_need"], errors="coerce")

    # ------------------------------------------------------------------
    # V6_wave WIP INJECTION (FIFO):
    # Earliest-needed consumer lots of each item draw from starting WIP first.
    # When cumulative lot_qty would exceed the available WIP for that item,
    # we stop (don't over-shrink) and let real production take over.
    # WIP-covered lots are dropped from the working set before topo / scheduling.
    # ------------------------------------------------------------------
    wip_path = OUTPUTS / "WIP_simulation_May2026.csv"
    wip = pd.read_csv(wip_path) if wip_path.exists() else pd.DataFrame()
    wip_by_item = {}
    if not wip.empty:
        for _, r in wip.iterrows():
            item = str(r["itemcode"]).strip()
            try:
                qty = float(r["availableinventory"])
            except (TypeError, ValueError):
                qty = 0.0
            wip_by_item[item] = wip_by_item.get(item, 0.0) + qty
    print(f"  WIP loaded: {len(wip_by_item):,} items, {sum(wip_by_item.values()):,.0f} total qty")

    # Build per-item lot list sorted FIFO by first_need; consume WIP greedily.
    wip_covered_lots = set()
    if wip_by_item:
        if "lot_qty" in lots.columns:
            _lot_qty_series = pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)
        else:
            _lot_qty_series = pd.Series(0.0, index=lots.index)
        lots["_wip_lot_qty"] = _lot_qty_series
        for item, group in lots.sort_values(["item_code", "first_need"]).groupby("item_code"):
            avail = wip_by_item.get(item, 0.0)
            if avail <= 0:
                continue
            consumed = 0.0
            for _, lot in group.iterrows():
                if consumed >= avail:
                    break
                lq = float(lot["_wip_lot_qty"])
                if lq <= 0:
                    continue
                if consumed + lq <= avail:
                    wip_covered_lots.add(lot["lot_id"])
                    consumed += lq
                else:
                    # partial coverage - keep this lot for v1 (don't over-shrink)
                    break
        lots.drop(columns=["_wip_lot_qty"], inplace=True)
    print(f"  WIP-covered lots (will be skipped): {len(wip_covered_lots):,}")
    print(f"  Total Phase 2 lots: {len(lots):,}")
    print(f"  Lots that will be scheduled: {len(lots) - len(wip_covered_lots):,}")

    # Capture lot_qty / item mapping BEFORE we drop covered lots so audit can use them.
    if "lot_qty" in lots.columns:
        lot_qty_full = dict(zip(lots["lot_id"],
                                pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0.0)))
    else:
        lot_qty_full = {}
    lot_item_full = dict(zip(lots["lot_id"], lots["item_code"]))

    # Filter lots to remove WIP-covered ones.
    if wip_covered_lots:
        before = len(lots)
        lots = lots[~lots["lot_id"].isin(wip_covered_lots)].copy()
        print(f"  Lots after WIP filter: {len(lots):,} (dropped {before - len(lots):,})")

    # Filter edges to remove any whose producer OR consumer was WIP-covered.
    if wip_covered_lots:
        before_e = len(edges)
        edges = edges[
            ~edges["producer_lot"].isin(wip_covered_lots) &
            ~edges["consumer_lot"].isin(wip_covered_lots)
        ].copy()
        print(f"  Edges after WIP filter: {len(edges):,} (dropped {before_e - len(edges):,})")
    # ------------------------------------------------------------------

    lot_ids       = lots["lot_id"].tolist()
    lot_set       = set(lot_ids)
    duration_h    = dict(zip(lots["lot_id"], lots["duration_h"]))
    lot_item      = dict(zip(lots["lot_id"], lots["item_code"]))
    # V6_wave selective AEST: item_type drives whether AEST applies (off for bottleneck
    # items like MASTER COMPOUND / FINAL COMPOUND so the bottleneck mixer never starves).
    if "item_type" in lots.columns:
        lot_item_type = dict(zip(lots["lot_id"], lots["item_type"].fillna("").astype(str).str.strip().str.upper()))
    else:
        lot_item_type = {}
    # item_code -> itemtype (UPPER). Lets aging be standardized by TYPE (GT / carcass)
    # rather than per-SKU aging_master rows. Built from the lot table.
    item_itype = {}
    for _lid, _ic in lot_item.items():
        _it = lot_item_type.get(_lid, "")
        if _ic and _it:
            item_itype[_ic] = _it
    lot_op        = dict(zip(lots["lot_id"], lots["operation"]))
    lot_dept      = dict(zip(lots["lot_id"], lots["department"]))
    lot_eq        = dict(zip(lots["lot_id"], lots["equipment"]))
    lot_machines  = dict(zip(lots["lot_id"], lots["machines_l"]))
    lot_is_belt   = dict(zip(lots["lot_id"], lots["is_belt"]))
    lot_first_need= dict(zip(lots["lot_id"], lots["first_need"]))
    # CUTTER MODEL: per-lot qty (NOS) needed for mother-roll MM consumption math
    if "lot_qty" in lots.columns:
        lot_qty = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
    else:
        lot_qty = {}
    lot_camp_id   = dict(zip(lots["lot_id"], lots["campaign_id"].fillna("").astype(str)))
    lot_wire      = dict(zip(lots["lot_id"], lots["wire_type"].fillna("").astype(str)))
    lot_camp_start= dict(zip(lots["lot_id"], lots["is_campaign_start"].astype(str).str.lower().isin(("true","1","yes","y"))))
    lot_camp_end  = dict(zip(lots["lot_id"], lots["is_campaign_end"].astype(str).str.lower().isin(("true","1","yes","y"))))
    # V6_wave: wave_first for wave-priority dispatch
    if "wave_first" in lots.columns:
        lot_wave_first = dict(zip(lots["lot_id"], lots["wave_first"].fillna("Z99").astype(str)))
    else:
        lot_wave_first = {lid: "Z99" for lid in lots["lot_id"]}

    # V6_wave MHE model: per-MHE drum material flow.
    # mhe_total = drums produced sequentially during the lot's duration.
    # First drum ready at p_start + p_dur / mhe_total; nth drum at p_start + n*p_dur/mhe_total.
    # Downstream may pull as soon as drum 1 ages out - no need to wait for whole batch.
    if "mhe_total" in lots.columns:
        lot_mhe_total = dict(zip(lots["lot_id"],
                                  pd.to_numeric(lots["mhe_total"], errors="coerce").fillna(1).astype(int)))
    else:
        lot_mhe_total = {lid: 1 for lid in lots["lot_id"]}

    # V6_wave anti-monopoly: dispatch MHE-drum 0 of every item before drum 1 etc.
    if "mhe_index" in lots.columns:
        lot_mhe_index = dict(zip(lots["lot_id"],
                                   pd.to_numeric(lots["mhe_index"], errors="coerce").fillna(0).astype(int)))
    else:
        lot_mhe_index = {lid: 0 for lid in lots["lot_id"]}
    if "batch_id" in lots.columns:
        lot_batch_id = dict(zip(lots["lot_id"], lots["batch_id"].fillna("").astype(str)))
    else:
        lot_batch_id = {lid: lid for lid in lots["lot_id"]}
    # format="mixed": Phase-4 writes timestamps in two formats — with sub-second
    # precision (…07:00:00.000000000) and without (…15:00:00). A single-format
    # parse infers ONE format from the first row and silently coerces the other
    # ~40% to NaT, which made those lots look deadline-less (due=BIG) and get
    # shoved to the end of every machine queue (the entire June tail). Parse each
    # element independently so every LFT is honoured.
    lst_map = dict(zip(times["lot_id"], pd.to_datetime(times["LST"], errors="coerce", format="mixed")))
    lft_map = dict(zip(times["lot_id"], pd.to_datetime(times["LFT"], errors="coerce", format="mixed")))
    # V6_wave AEST: aging-aware earliest-start floor from Phase 4 planning policy.
    # If column is absent (legacy Phase 4 output), falls back to pure ASAP behaviour.
    if "AEST" in times.columns:
        aest_map = dict(zip(times["lot_id"], pd.to_datetime(times["AEST"], errors="coerce", format="mixed")))
        n_aest = sum(1 for v in aest_map.values() if pd.notna(v))
        print(f"  AEST data loaded for {n_aest:,} lots (upstream ASAP will be bounded by AEST)")
    else:
        aest_map = {}
        print(f"  AEST column missing - upstream uses pure ASAP (no aging-aware floor)")
    # OPTION C: slack-based placement. Slack <= 0 -> critical -> ASAP.
    slack_map = {}
    if "slack_h" in times.columns:
        slack_map = dict(zip(times["lot_id"], pd.to_numeric(times["slack_h"], errors="coerce").fillna(1e9)))
    elif "slack_hours" in times.columns:
        slack_map = dict(zip(times["lot_id"], pd.to_numeric(times["slack_hours"], errors="coerce").fillna(1e9)))
    else:
        # Fallback: compute slack ourselves from LFT and a rough est
        for lid in times["lot_id"]:
            slack_map[lid] = 1e9   # treat all as non-critical
    print(f"  Slack data loaded for {sum(1 for v in slack_map.values() if v <= 0):,} critical lots (slack <= 0)")

    successors   = defaultdict(list)
    predecessors = defaultdict(list)
    for u, v in zip(edges["producer_lot"], edges["consumer_lot"]):
        if u in lot_set and v in lot_set:
            successors[u].append(v); predecessors[v].append(u)

    campaigns = {}
    for lid in lot_ids:
        cid = lot_camp_id.get(lid, "")
        if not cid: continue
        c = campaigns.setdefault(cid, {"wire": lot_wire.get(lid, ""), "lots": []})
        c["lots"].append(lid)
    for cid, c in campaigns.items():
        big_n = pd.Timestamp("2099-12-31")
        c["lots"].sort(key=lambda l: (lot_first_need.get(l, big_n), l))
        c["n_total"] = len(c["lots"])
        c["ready"] = 0
    print(f"  Belt campaigns: {len(campaigns)}")

    in_deg = {l: 0 for l in lot_ids}
    for u, succs in successors.items():
        for v in succs:
            if v in in_deg: in_deg[v] += 1
    import heapq
    big_need = pd.Timestamp("2099-12-31")
    # V6_wave ROUND-ROBIN dispatch: (wave_first, mhe_index, first_need, item_code, lot_id).
    #   wave_first    -> W01 lots dispatch before W02 etc.
    #   mhe_index     -> drum 0 of every item across all items dispatches BEFORE drum 1
    #                   of any item. This is the anti-monopoly key: prevents one item
    #                   from running consecutively on a bottleneck mixer for hours.
    #   first_need    -> within same drum index, earlier-needed lots go first.
    #   item_code     -> alphabetical tie-break (consistent interleaving across runs).
    #   lot_id        -> final tie-break.
    def _key(l):
        return (lot_wave_first.get(l, "Z99"),
                lot_mhe_index.get(l, 0),
                lot_first_need.get(l, big_need),
                lot_item.get(l, ""),
                l)
    heap = []
    for l, d in in_deg.items():
        if d == 0:
            heapq.heappush(heap, (_key(l), l))
    topo = []
    while heap:
        _, u = heapq.heappop(heap)
        topo.append(u)
        for v in successors.get(u, ()):
            in_deg[v] -= 1
            if in_deg[v] == 0:
                heapq.heappush(heap, (_key(v), v))
    if len(topo) < len(lot_ids):
        seen = set(topo)
        for l in lot_ids:
            if l not in seen: topo.append(l)
    print(f"  Topological order computed: {len(topo):,} lots")

    # === CASCADING ORPHAN DETECTION (production-scheduling expert fix) ===
    # A lot is an orphan if it has NO downstream consumer that reaches a
    # terminal (Building / Curing). Single-level orphan check misses
    # CASCADES: cutter → orphan, then Stage-1 → orphan, then FRC → orphan.
    # We iteratively mark orphans backward from terminals until convergence.
    #
    # Algorithm (reverse-topo backward sweep):
    #   1. Mark all terminal lots (Building / Curing) as "reaches_terminal".
    #   2. For each lot in REVERSE topo order, if any of its successors
    #      reaches a terminal, this lot also reaches terminal.
    #   3. Otherwise, this lot is an orphan (no productive consumer).
    #
    # Lots marked as orphans are SKIPPED in the placement loop below.
    reaches_terminal = set()
    for lid in lot_ids:
        if _is_terminal_dept(lot_dept.get(lid, "")):
            reaches_terminal.add(lid)
    # Process in REVERSE topo so we mark consumers before producers.
    for lid in reversed(topo):
        if lid in reaches_terminal:
            continue
        for s in successors.get(lid, ()):
            if s in reaches_terminal:
                reaches_terminal.add(lid)
                break
    cascade_orphans = set()
    for lid in lot_ids:
        if lid not in reaches_terminal:
            cascade_orphans.add(lid)
    print(f"  Cascading orphans detected: {len(cascade_orphans):,} lots "
          f"(out of {len(lot_ids):,} total — {100*len(cascade_orphans)/max(len(lot_ids),1):.1f}%)")

    # === QUANTITY-AWARE OVER-PRODUCTION DETECTION (DISABLED) ===
    # Previously: counted consumer LOTS but a single Stage-2 GT lot might
    # consume MULTIPLE carcass MHEs. Counting by lot drastically under-
    # estimated demand and dropped 78% of valid lots (47K -> 10K placed,
    # 0 GT produced — completely broken schedule).
    #
    # The proper fix requires QUANTITY tracking: sum producer qtys per item
    # and compare to BOM-cascaded consumer qty demand. That's a Phase 1c/
    # Phase 2 fix, not a Phase 5 retrofit.
    #
    # For now, rely on:
    #   1. Cascading orphan detection (reaches_terminal) — handles dead-end chains
    #   2. Phase 1c MRP cascade — already removes WIP-covered demand
    # The remaining FRC > Cutter inversion is acceptable: FRC has some
    # over-production but it doesn't break the schedule horizon.

    machine_busy = defaultdict(list)
    machine_last_camp_wire = defaultdict(str)
    machine_last_camp_end_ns = defaultdict(int)
    machine_busy_ns = defaultdict(int)
    machine_last_item = defaultdict(str)
    machine_last_itype = defaultdict(str)

    # === BUILDING MACHINE CYCLE TIMES (per-machine real plant data) ===
    # Override routing's per-SKU proc_time with per-machine cycle_sec (if a file is
    # provided). Empty -> _bld_dur_ns falls back to routing-based duration.
    machine_cycle_sec = {}  # machine_code -> cycle_sec for building MGs
    try:
        try:
            bct = _db_or_csv("building_cycle_times", cfg)
        except Exception:
            bct_path = INPUTS / "building_cycle_times.csv"
            bct = pd.read_csv(bct_path) if bct_path.exists() else pd.DataFrame()
        if not bct.empty:
            for _, r in bct.iterrows():
                m = str(r["Machine"]).strip()
                cs = float(r["cycle_sec"])
                if m and cs > 0:
                    machine_cycle_sec[m] = cs
            print(f"  BUILDING CYCLE TIMES: {len(machine_cycle_sec)} machines loaded")
    except Exception as _e:
        print(f"  BUILDING CYCLE TIMES: load skipped ({_e})")

    # === BUILDING CAMPAIGN INPUTS (v2) ===
    # Inch per SKU (inch-lock) + lot->SKU (primary). Building lots are placed
    # as inch-locked, per-SKU campaigns with a flat 30-min changeover — see
    # place_building_campaigns() below.
    BUILDING_ITYPES = {"GREEN TYRES", "CARCASS"}
    # Changeover from the previous building code (Master_Building_ChangeoverTime
    # fallback): same-size 40 min, different-size 60 min. Machines are inch-locked,
    # so changeovers are almost always same-size (formula change) = 40 min.
    BUILDING_CO_SAME_MIN = 40.0
    BUILDING_CO_DIFF_MIN = 60.0
    # Machine portfolio: max DISTINCT SKUs a building machine runs over the run.
    # High-efficiency machines (fast cycle, e.g. 6001-6004 / VMI lines) can carry a
    # bigger portfolio; slower machines stay tighter.
    BUILDING_PORTFOLIO_MAX      = 4     # default (slower machines)
    BUILDING_PORTFOLIO_MAX_FAST = 6     # fast / high-efficiency machines
    BUILDING_SKU_MACHINE_MAX    = 3     # a SKU runs on at most this many machines (1, up to 2-3)
    # Cap-and-spill: max build-WORK hours per machine before a SKU spills to an
    # eligible sibling. ~one month at ~90% (leaves room for changeovers + freshness
    # gaps so the layout still finishes inside the month).
    BUILDING_MONTH_CAP_H        = 31 * 24 * 0.90   # ~669 h
    # Inch-lock: when True a machine is pinned to ONE tyre size (fewer changeovers
    # but strands capacity — an idle machine locked to size A can't take size-B work).
    # When False, machines run any routing-eligible size (better balance / absorbs the
    # tail, but more different-size changeovers, which cost more time).
    BUILDING_INCH_LOCK          = True
    # === CTB (CLEAR-TO-BUILD) QUANTITY GATE ===
    # When True, a GT/carcass building lot cannot start until the CUMULATIVE
    # supply of EACH input component (opening WIP + component production finished
    # so far) covers the CUMULATIVE consumption through that lot. This closes the
    # gap in the full-kit rule (which only checks the EARLIEST single component
    # lot exists, not whether enough quantity is produced) — so building can no
    # longer outrun upstream material. Consumption rates come from the Phase-1
    # demand explosion (scheduler-native item codes, no BOM alias issue).
    # v1 (pre-pass floor) does NOT achieve true material-feasibility — a
    # single-pass per-lot floor can't model the parallel machine-interleaved
    # consumption chronology, so stockouts persist. Kept OFF until an
    # event-driven (live stock during layout) version is built. See README.
    BUILDING_CTB_GATE           = False
    # === HEIJUNKA DAILY-LEVELING BUILD LAYOUT (option b) ===
    # Instead of building each SKU as a long campaign ASAP (which front-loads and
    # collapses daily SKU diversity to ~23, dipping output on trough days), level
    # the build to the daily curing takt: each day, round-robin a broad mix of the
    # ripe SKUs across machines so ~35-40 distinct SKUs build every day and output
    # holds ~the curing rate. LEAD = how many days ahead of curing a lot may build
    # (bounded by freshness). MINI = max consecutive lots of one code per machine
    # visit (controls changeover count: ~total_lots/MINI changeovers).
    BUILDING_LEVEL              = False  # reverted: naive takt-cap hurt throughput; diagnosing dip cause first
    BUILDING_LEVEL_LEAD_DAYS    = 3.0   # may build up to 3 days ahead (= GT freshness)
    BUILDING_LEVEL_MINI_CAMPAIGN = 8    # ~15.9k lots / 8 ≈ ~2000 changeovers
    BUILDING_LEVEL_TAKT_HEADROOM = 1.12 # daily qty cap = takt × this (level, don't front-load)
    BUILDING_FAST_CYCLE_SEC     = 70.0  # machines at/below this cycle_sec are "fast"
    BUILDING_FAST_MACHINES      = {"6001", "6002", "6003", "6004"}  # always treated fast
    _sku_inch = {}
    try:
        _im = pd.read_csv(INPUTS / "sku_inch_map.csv")
        _sku_inch = dict(zip(_im["sku_code"].astype(str).str.strip(),
                             _im["inch"].astype(str).str.strip()))
        print(f"  BUILDING INCH MAP: {len(_sku_inch):,} SKUs")
    except Exception as _e:
        print(f"  BUILDING INCH MAP load skipped ({_e})")
    # Robust inch resolver: FG-code digits 9-10 first (covers ~all SKUs), then
    # the sku_inch_map, then a tiny manual override for lettered codes. This makes
    # the building inch complete (not just map-covered) so the machine-level
    # inch-lock (Step 2) applies to every lot.
    _INCH_VALID = {str(n) for n in range(10, 20)}
    _INCH_OVR = {"ET1658014UXRSMTTL": "14"}
    def _resolve_inch(sku):
        s = str(sku).strip()
        d = s[8:10] if len(s) >= 10 else ""
        if d.isdigit() and d in _INCH_VALID:
            return d
        return _sku_inch.get(s) or _INCH_OVR.get(s)

    # === STEP 2: MACHINE-LEVEL INCH-LOCK from the plant allowed-sets ===
    # Each machine may run only the inches in its allowed-set (inputs/machine_inch.csv),
    # and we PREFER to load it with its dominant inch. When on, phase-5 locks each
    # building machine to an inch from ITS OWN allowed-set (instead of first-come),
    # so e.g. a 16" machine never gets pinned to 13". Toggle with JK_INCH_AWARE=0.
    BUILDING_MACHINE_INCH = os.environ.get("JK_INCH_AWARE", "1") not in ("0", "false", "False")
    # SOFT INCH (default ON): inch is a preference with a size-change penalty, NOT a
    # hard wall. No machine is permanently inch-fixed — the constraint is released
    # for off-pattern work only when better-matched machines are full (e.g. an idle
    # 6909 takes overflow at a penalty). Set JK_SOFT_INCH=0 to restore the hard gate.
    BUILDING_SOFT_INCH = os.environ.get("JK_SOFT_INCH", "1") not in ("0", "false", "False")
    MACHINE_ALLOWED, MACHINE_DOM = {}, {}
    if BUILDING_MACHINE_INCH:
        try:
            _mi = pd.read_csv(INPUTS / "machine_inch.csv", dtype=str)
            for _, _r in _mi.iterrows():
                _m = str(_r["machine"]).strip()
                _al = {x.strip() for x in str(_r.get("allowed_inches", "")).split(",") if x.strip()}
                if _m and _al:
                    MACHINE_ALLOWED[_m] = _al
                    MACHINE_DOM[_m] = str(_r.get("dominant_inch", "")).strip()
            print(f"  STEP2 MACHINE INCH-LOCK: {len(MACHINE_ALLOWED)} machines with allowed-sets")
        except Exception as _e:
            print(f"  STEP2 MACHINE INCH-LOCK load skipped ({_e})")
    _lot_sku1 = {}
    try:
        _sk = pd.read_csv(OUTPUTS / _suff("phase2_lot_skus.csv"))
        for _l, _s in zip(_sk["lot_id"], _sk["skus_set"].astype(str)):
            _toks = [t.strip() for t in str(_s).split(",")
                     if t.strip() and t.strip().lower() != "nan"]
            if _toks:
                _lot_sku1[_l] = _toks[0]
        print(f"  BUILDING lot->SKU: {len(_lot_sku1):,} lots")
    except Exception as _e:
        print(f"  BUILDING lot->SKU load skipped ({_e})")
    # Per-machine changeover from Master_Building_ChangeoverTime
    # (machine_code -> (same_size_min, different_size_min)).
    _bld_co = {}
    try:
        _co = pd.read_csv(INPUTS / "building_changeover.csv")
        for _m, _sm, _dm in zip(_co["machine_code"].astype(str).str.strip(),
                                pd.to_numeric(_co["same_min"], errors="coerce"),
                                pd.to_numeric(_co["diff_min"], errors="coerce")):
            if pd.notna(_sm) and pd.notna(_dm):
                _bld_co[_m] = (float(_sm), float(_dm))
        print(f"  BUILDING CHANGEOVER MAP: {len(_bld_co)} machines")
    except Exception as _e:
        print(f"  BUILDING CHANGEOVER MAP load skipped ({_e})")
    # SKU -> current-running machine(s) (tbs2): the REAL machine each SKU runs on.
    # Used as the building machine set so machines stay single-inch (their real
    # SKUs) and SKU portfolio stays ~1 (up to the 2-3 listed). Overrides routing
    # eligibility for covered SKUs; uncovered SKUs fall back to routing.
    _sku_machines = {}
    try:
        import ast as _ast
        _tm = pd.read_csv(INPUTS / "tbs2_current_running_machine.csv")
        for _rc, _mn in zip(_tm["RecipeCode"].astype(str).str.strip(), _tm["machine_numbers"]):
            try:
                _ml = [str(x).strip() for x in _ast.literal_eval(str(_mn))]
            except Exception:
                _ml = []
            if _ml:
                _sku_machines[_rc] = _ml
        print(f"  BUILDING SKU->MACHINE (tbs2): {len(_sku_machines)} SKUs")
    except Exception as _e:
        print(f"  BUILDING SKU->MACHINE (tbs2) load skipped ({_e})")
    # CUTTER MOTHER-ROLL STATE — per-cutter tracking for parent batching.
    # When a cut variant comes in, dispatcher checks if parent is already loaded
    # AND mother roll has enough MM left → 3 min cut re-set. Else 15 min swap.
    cutter_loaded_parent = defaultdict(str)
    cutter_loaded_lot_id = defaultdict(str)
    cutter_roll_remaining_mm = defaultdict(lambda: 0.0)
    # Load BOM mm-per-cut map ONCE — for cutter-family lots only.
    try:
        _bom_df = _db_or_csv("bom", cfg)
        mm_per_cut_map = build_mm_per_cut_map(_bom_df)
        print(f"  CUTTER MODEL: mm-per-cut entries: {len(mm_per_cut_map):,}")
    except Exception as _e:
        print(f"  CUTTER MODEL: BOM load failed ({_e}) - defaulting to flat 3/15 min")
        mm_per_cut_map = {}

    DEFAULT_MIN_AGE_H = pre_curing_buffer_h
    DEFAULT_MAX_AGE_H = 365 * 24.0
    NS_PER_H = int(3600 * 1e9)
    NS = 1e9
    item_min_age_ns = {}
    item_max_age_ns = {}
    # Standardized building-product aging (policy override, applied by ITEMTYPE):
    #   GREEN TYRES -> min 0 d, max 3 d (72 h)   CARCASS -> min 0 d, max 1 d (24 h)
    # A GT/carcass cannot be built earlier than (curing - max_age) without over-aging,
    # and needs >= min_age rest before it is consumed/cured. These caps override any
    # per-SKU aging_master row for building products; all other items use aging_map.
    AGE_OVERRIDE_H = {"GREEN TYRES": (0.0, 72.0), "CARCASS": (0.0, 24.0)}

    # ═══ CTP DEVIATION — TRANSFER TIME FROM THE ROUTING ════════════════════════════
    # v6 never reads `transfer_time_min`. The user instructed that it always be used.
    # It is a MANDATORY LAG between a producer finishing and its consumer starting —
    # the same shape as min_aging — so it is ADDED TO MIN here, at the single funnel
    # every placement gap goes through (the p_min_ns checks at ~:997, ~:1243, ~:1300).
    #
    # It is applied AFTER AGE_OVERRIDE_H, so a green tyre with min=0 and a 90-min
    # building->curing transfer correctly gets a 1.5h floor before it may be cured.
    #
    # NOT added to max: max_aging is a shelf-life CEILING, not a lag. Adding transfer
    # there would EXTEND shelf life, which is the opposite of the truth.
    # ═══ CTP DEVIATION #3 — load the plant changeover matrix (item-pair keyed) ═══
    _co_p = INPUTS / "changeover_lookup.csv"
    if _co_p.exists():
        MACHINE_TO_LINE.update({str(k).strip(): str(v).strip()
                                for k, v in (cfg.get("machine_to_changeover_line") or {}).items()})
        _co = pd.read_csv(_co_p)
        _co["changeover_min"] = pd.to_numeric(_co["changeover_min"], errors="coerce")
        _co = _co.dropna(subset=["changeover_min"])
        CHANGEOVER_LUT.update({
            (str(r.line).strip(), str(r.from_item).strip(), str(r.to_item).strip()):
                float(r.changeover_min)
            for r in _co.itertuples()
        })
        print(f"  CHANGEOVER MATRIX (plant): {len(CHANGEOVER_LUT):,} rules across "
              f"{len(set(MACHINE_TO_LINE.values()))} lines, {len(MACHINE_TO_LINE)} machine ids "
              f"mapped. Unmapped machines / uncovered pairs fall back to v6 (mixing 2 / 15).")
    else:
        print("  [WARN] inputs/changeover_lookup.csv absent — using v6's name-based rules "
              "only, which give EVERY CTP machine a flat 15 min. Run adapt_inputs.py.")

    _tr_h = {}
    if "transfer_time_h" in lots.columns:
        _t = (lots[["item_code", "transfer_time_h"]]
              .assign(transfer_time_h=lambda d: pd.to_numeric(d["transfer_time_h"],
                                                              errors="coerce").fillna(0.0))
              .groupby("item_code")["transfer_time_h"].max())
        _tr_h = {str(k).strip(): float(v) for k, v in _t.items() if v > 0}
        print(f"  TRANSFER TIME (routing): {len(_tr_h):,} items carry a transfer lag; "
              f"added to min_aging at every producer->consumer gap.")
    else:
        print("  [WARN] lots have no `transfer_time_h` column — transfer time NOT applied.")

    def _ages_ns(item):
        if item in item_min_age_ns:
            return item_min_age_ns[item], item_max_age_ns[item]
        ov = AGE_OVERRIDE_H.get(item_itype.get(item, ""))
        if ov is not None:
            mn, mx = ov
        else:
            mn, mx = aging_map.get(item, (DEFAULT_MIN_AGE_H, DEFAULT_MAX_AGE_H))
        mn = mn + _tr_h.get(str(item).strip(), 0.0)      # CTP: + routing transfer lag
        mn_ns = int(mn * NS_PER_H)
        mx_ns = int(mx * NS_PER_H)
        item_min_age_ns[item] = mn_ns
        item_max_age_ns[item] = mx_ns
        return mn_ns, mx_ns

    schedule = {}
    infeas   = []
    aging_v  = []
    T0_ns = pd.Timestamp(T0).value
    HORIZON_NS = int(MAX_HORIZON_DAYS * 24 * NS_PER_H)
    horizon_cutoff_ns = T0_ns + HORIZON_NS
    # V6_wave: derive horizon from PLAN's max endTime (not plan_params), so the
    # horizon = actual last block-finish in the plan. plan_params.planEndDate
    # can be stale/conservative; the plan itself is the ground truth.
    horizon = None
    try:
        plan = _db_or_csv("plan", cfg)
        if "endTime" in plan.columns:
            et = pd.to_datetime(plan["endTime"], errors="coerce")
            if et.notna().any():
                horizon = et.max()
                if pd.isna(horizon): horizon = None
    except Exception as _e:
        pass
    # V6_wave: NEG_INF_NS is a "very early" timestamp used as the floor for root
    # lots when no other floor applies.
    NEG_INF_NS = pd.Timestamp("2000-01-01").value
    # Global safety belt for AEST (non-bottleneck items only).
    GLOBAL_MAX_LOOKBACK_H = float(cfg.get("global_max_lookback_h", 168))
    GLOBAL_MAX_LOOKBACK_NS = int(GLOBAL_MAX_LOOKBACK_H * 3600 * NS)
    # SCHEDULE OPEN: plant turn-on date. Floor for root upstream lots and for
    # bottleneck items (which bypass AEST). Set in config; 168 h = 7 days.
    SCHEDULE_OPEN_H = float(cfg.get("schedule_open_h", 168))
    SCHEDULE_OPEN_NS = pd.Timestamp(first_curing - pd.Timedelta(hours=SCHEDULE_OPEN_H)).value
    print(f"  Schedule-open floor: {pd.Timestamp(SCHEDULE_OPEN_NS)}  (= first_curing - {SCHEDULE_OPEN_H:.0f} h)")
    # UPSTREAM LEAD: let UPSTREAM (mixing/compound/calandar/belt/feeders) turn on
    # earlier than the building floor, so the deep belt cascade pre-builds a buffer
    # before building starts. Building/terminal lots still floor at SCHEDULE_OPEN_NS.
    # JK_UPSTREAM_LEAD_H=72 => upstream starts 3 days before building. Default 0 (off).
    UPSTREAM_LEAD_H = float(os.environ.get("JK_UPSTREAM_LEAD_H", "0") or 0)
    UPSTREAM_OPEN_NS = SCHEDULE_OPEN_NS - int(UPSTREAM_LEAD_H * NS_PER_H)
    if UPSTREAM_LEAD_H > 0:
        print(f"  Upstream-lead floor: {pd.Timestamp(UPSTREAM_OPEN_NS)}  "
              f"(= {UPSTREAM_LEAD_H:.0f} h before building floor)")
    # HORIZON enforcement: if any lot's finish > horizon, flag PAST_HORIZON.
    ENFORCE_HORIZON = bool(cfg.get("enforce_strict_horizon", False))
    HORIZON_LIMIT_NS = pd.Timestamp(horizon).value if (horizon is not None) else None
    print(f"  Strict horizon:      {ENFORCE_HORIZON} (horizon={horizon})")

    # V6_wave PIPELINE PLACEMENT: anchor each lot to its wave window.
    # wave_anchor_ns[wave_id] = T0 + (wave_idx - 1) * wave_duration
    # Each lot starts ASAP within its wave; if predecessors push later, follow them.
    WAVE_DUR_NS = int(3 * 24 * 3600 * NS)
    def _wave_anchor(wave_id):
        try:
            wi = int(str(wave_id).lstrip("W"))
        except (ValueError, TypeError):
            return T0_ns
        # W01 -> anchor T0; W02 -> T0 + 3d; W03 -> T0 + 6d; ...
        return T0_ns + (wi - 1) * WAVE_DUR_NS

    def insert_busy(machine, s_ns, e_ns):
        bisect.insort(machine_busy[machine], (s_ns, e_ns))
        # Track accumulated busy time for tie-break (drains over-loaded machines).
        machine_busy_ns[machine] += max(0, e_ns - s_ns)

    def place_one_lot_jit(lid):
        """V6_wave per-MHE-flow placement (ASAP upstream / LFT terminal).

        Material availability floor (earliest_ns):
          For each predecessor sub-lot p with mhe_total=N:
            first_drum_ready  = p_start + p_dur / N
            material_ready    = first_drum_ready + min_aging[p_item]
          earliest_ns = max(material_ready) across all preds.

        Target start:
          Terminal dept (BUILDING/TBM/CURING) -> LFT-driven (target = LFT - dur).
          Upstream dept (Mixing/Calendar/Cutter/etc.) -> ASAP (target = earliest).
        """
        dur_h = float(duration_h.get(lid, 0))
        dur_ns = int(dur_h * 3600 * NS)
        item = lot_item.get(lid, "")
        machines = lot_machines.get(lid, [])
        if not machines:
            return None

        # === POOLED-MHE PREDECESSOR MODEL ===
        # PRODUCTION-SCHEDULING-EXPERT RULE:
        #   - Predecessors of the SAME ITEM are ALTERNATIVES (any 1 MHE supplies).
        #     -> use EARLIEST first_drum_ready (whichever MHE arrives first).
        #     -> Example: 10 Stage-1 carcass MHE producers feed 1 Stage-2 consumer.
        #     -> Stage-2 takes the earliest carcass MHE, doesn't wait for all 10.
        #   - Predecessors of DIFFERENT ITEMS are REQUIRED (need all components).
        #     -> use MAX across items (need belt + ply + carcass + bead etc.).
        #
        # This matches plant reality: Stage-2 consumes from a pooled carcass WIP
        # rack — any compatible carcass works. With 15 Stage-1 machines feeding
        # 6 Stage-2 machines, Stage-2 never starves as long as one Stage-1 is
        # running ahead.
        earliest_ns = NEG_INF_NS
        preds_by_item = {}
        for p in predecessors.get(lid, ()):
            if p in schedule:
                item_p = lot_item.get(p, "")
                preds_by_item.setdefault(item_p, []).append(p)

        BIG_NS = (1 << 62)
        for item_p, p_list in preds_by_item.items():
            # For each item, find EARLIEST available producer (alternatives).
            item_earliest = BIG_NS
            for p in p_list:
                p_finish_ns = schedule[p]["finish_ns"]
                p_start_ns = schedule[p]["scheduled_start"].value
                p_dur_ns = max(0, p_finish_ns - p_start_ns)
                p_mhe = max(1, lot_mhe_total.get(p, 1))
                # First MHE drum of this sub-lot ready at p_start + p_dur / p_mhe.
                first_drum_ready_ns = p_start_ns + (p_dur_ns // p_mhe)
                p_min_ns, _ = _ages_ns(item_p)
                v = first_drum_ready_ns + p_min_ns
                if v < item_earliest:
                    item_earliest = v
            # Across DIFFERENT items: need them all -> take MAX.
            if item_earliest < BIG_NS and item_earliest > earliest_ns:
                earliest_ns = item_earliest

        # ASAP upstream (bounded by max_aging) / LFT terminal
        dept = lot_dept.get(lid, "")
        lft = lft_map.get(lid)
        lft_ns_val = pd.Timestamp(lft).value if (lft is not None and not pd.isna(lft)) else None
        if _is_terminal_dept(dept):
            # FIX 1 (top quick-win): Building has 38% spare capacity but tail extends 11 days.
            # Old: LFT-dur (JIT-late) made building wait → upstream cascade stretched.
            # New: pull building to AEST so the chain compresses upstream.
            aest = aest_map.get(lid)
            if aest is not None and not pd.isna(aest):
                aest_ns = pd.Timestamp(aest).value
                if aest_ns < SCHEDULE_OPEN_NS:
                    aest_ns = SCHEDULE_OPEN_NS
                target_start_ns = max(earliest_ns, aest_ns)
            elif lft_ns_val is not None:
                target_start_ns = max(earliest_ns, lft_ns_val - dur_ns)
            else:
                wave_id = lot_wave_first.get(lid, "Z99")
                target_start_ns = max(earliest_ns, _wave_anchor(wave_id))
        else:
            # V6_wave SELECTIVE AEST.
            itype = lot_item_type.get(lid, "")
            if itype in FEEDER_JIT_TYPES and lft_ns_val is not None:
                # PRODUCTION DE-PULSE: place this feeder JIT — finish just before its
                # (already-smooth) deadline instead of AEST-early. Spreads component
                # production to track the level daily demand -> even kit-readiness.
                _floor = UPSTREAM_OPEN_NS if earliest_ns == NEG_INF_NS else max(earliest_ns, UPSTREAM_OPEN_NS)
                _jit = lft_ns_val - dur_ns - int(FEEDER_JIT_BUFFER_H * NS_PER_H)
                target_start_ns = max(_floor, _jit)
            elif itype in NO_AEST_ITEM_TYPES:
                # Bottleneck: pure ASAP from (upstream) schedule-open date.
                if earliest_ns == NEG_INF_NS:
                    target_start_ns = UPSTREAM_OPEN_NS
                else:
                    target_start_ns = max(earliest_ns, UPSTREAM_OPEN_NS)
            else:
                # Non-bottleneck: AEST applies, but FIX 2 caps it at consumer_first_need - max_aging.
                # NEW: target_wip floor — keep N hours of rolling buffer alive.
                aest = aest_map.get(lid)
                lft_floor = lft_map.get(lid)
                item_min_ns, item_max_ns = _ages_ns(lot_item.get(lid, ""))
                safety_ns = int(AGING_SAFETY_BUFFER_H * NS_PER_H)
                aging_cap_ns = None
                # === F3 v3 (REVERTED) SAFE WIP TARGET ===
                # Lesson: target_wip = (max_aging-safety)/2 was too aggressive — caused
                # producer to be placed too far back which broke supply chain.
                # New rule: use itemtype default, but cap at max_aging - safety - headroom (16h).
                # For short-aging items, target_wip = 0 (JIT placement).
                target_wip_h_default = TARGET_WIP_HOURS.get(itype, 0.0)
                item_max_h = item_max_ns / NS_PER_H if item_max_ns > 0 else 0.0
                _headroom_h = AGING_SAFETY_BUFFER_H + 8.0   # 16h headroom
                if item_max_h > 0:
                    # Maximum safe WIP buffer leaving headroom inside max_aging
                    max_safe_wip_h = max(0.0, item_max_h - _headroom_h)
                    if target_wip_h_default > 0:
                        # Cap default at max_safe
                        target_wip_h = min(target_wip_h_default, max_safe_wip_h)
                    else:
                        # No itemtype default → JIT (target_wip = 0, no buffer pull)
                        target_wip_h = 0.0
                    # Force short-aging items to JIT (item_max < default+headroom -> 0)
                    if item_max_h < target_wip_h_default + _headroom_h:
                        target_wip_h = 0.0
                else:
                    target_wip_h = 0.0
                target_wip_ns = int(target_wip_h * NS_PER_H)
                wip_cap_ns = None
                if lft_floor is not None and not pd.isna(lft_floor):
                    lft_ns_local = pd.Timestamp(lft_floor).value
                    # max_aging cap: never produce more than max_aging+safety ahead of consumer
                    aging_cap_ns = lft_ns_local - dur_ns - item_max_ns - safety_ns
                    # WIP cap: start no LATER than consumer needs - target_wip_h.
                    if target_wip_h > 0:
                        wip_cap_ns = lft_ns_local - dur_ns - target_wip_ns
                if aest is not None and not pd.isna(aest):
                    aest_ns = pd.Timestamp(aest).value
                    # Safety belt: AEST cannot be earlier than LST - global_max_lookback_h
                    if lft_floor is not None and not pd.isna(lft_floor):
                        lst_ns = pd.Timestamp(lft_floor).value - dur_ns
                        aest_floor_ns = lst_ns - GLOBAL_MAX_LOOKBACK_NS
                        if aest_ns < aest_floor_ns:
                            aest_ns = aest_floor_ns
                    # FIX 2 enforce: never earlier than aging_cap_ns
                    if aging_cap_ns is not None and aest_ns < aging_cap_ns:
                        aest_ns = aging_cap_ns
                    # WIP MAINTENANCE: producer must start no later than wip_cap_ns
                    # (consumer_first_need - target_wip_hours). Keeps buffer alive.
                    if wip_cap_ns is not None and aest_ns > wip_cap_ns:
                        aest_ns = wip_cap_ns
                    # Final floor: never earlier than the UPSTREAM schedule-open date
                    # (lets the deep belt cascade pre-build ahead of the building floor).
                    if aest_ns < UPSTREAM_OPEN_NS:
                        aest_ns = UPSTREAM_OPEN_NS
                    if earliest_ns == NEG_INF_NS:
                        target_start_ns = aest_ns
                    else:
                        target_start_ns = max(earliest_ns, aest_ns)
                else:
                    # No AEST data -> CAPACITY-AWARE FALLBACK.
                    if aging_cap_ns is not None:
                        target_start_ns = max(earliest_ns, aging_cap_ns) if earliest_ns > NEG_INF_NS else aging_cap_ns
                        if target_start_ns < UPSTREAM_OPEN_NS:
                            target_start_ns = UPSTREAM_OPEN_NS
                    else:
                        target_start_ns = max(earliest_ns, UPSTREAM_OPEN_NS) if earliest_ns > NEG_INF_NS else UPSTREAM_OPEN_NS

        # PER-MACHINE CHANGEOVER + DURATION + SMART PICKER.
        # For each candidate machine compute:
        #   - per-machine duration (building MGs use building_cycle_times.csv override)
        #   - per-machine changeover (cutter model, item-type, etc.)
        #   - expected_finish = start + dur_m + co_ns_m
        # Pick by EARLIEST expected_finish (naturally favors fast + lightly-loaded).
        curr_item = lot_item.get(lid, "")
        curr_itype = lot_item_type.get(lid, "")
        curr_qty = float(lot_qty.get(lid, 0) or 0)
        TOLERANCE_NS = int(3600 * NS)
        best_machine = None
        best_start_ns = None
        best_co_ns = None
        best_dur_ns = None
        best_mm_consumed = 0.0
        best_will_swap = False
        best_score = None
        for m in machines:
            # Per-machine duration (Building MGs override from cycle_sec map)
            if m in machine_cycle_sec and curr_qty > 0:
                # cycle_sec is seconds per 1 NOS; duration = qty × cycle_sec
                dur_m_ns = int(curr_qty * machine_cycle_sec[m] * NS)
            else:
                dur_m_ns = dur_ns  # routing default
            # Per-machine changeover
            if m in PARENT_AWARE_CUTTERS and mm_per_cut_map:
                co_min_m, mm_cons, will_swap = compute_changeover_for_cutter(
                    m, machine_last_item.get(m, ""), curr_item, curr_qty,
                    mm_per_cut_map, cutter_loaded_parent, cutter_loaded_lot_id,
                    cutter_roll_remaining_mm, curr_lot_id=lid)
            else:
                co_min_m = compute_changeover_min(
                    m, lot_dept.get(lid,""),
                    machine_last_item.get(m, ""), machine_last_itype.get(m, ""),
                    curr_item, curr_itype)
                mm_cons, will_swap = 0.0, False
            co_ns_m = int(co_min_m * 60 * NS)
            start = first_fit_jit(machine_busy[m], earliest_ns, target_start_ns, dur_m_ns + co_ns_m)
            # SMART PICKER v2: score by expected_finish_ns (start + dur + co).
            # The 3-min vs 15-min changeover difference is ALREADY encoded in
            # expected_finish_ns via compute_changeover_for_cutter. So we don't
            # need a separate same_parent_bonus — including it as the PRIMARY
            # sort key was the bug that over-concentrated CPJ1218 cuts on WBC
            # (1,914 lots vs WBC NEW's 568 — 3.4x imbalance). Letting
            # expected_finish_ns decide naturally balances load across cutters
            # while still preferring same-parent (because the 12-min savings
            # makes the same-parent machine's expected_finish_ns earlier when
            # both machines have similar queue length).
            expected_finish_ns = start + dur_m_ns + co_ns_m
            score = (expected_finish_ns, machine_busy_ns[m])
            if best_score is None or score < best_score:
                best_machine = m
                best_start_ns = start
                best_co_ns = co_ns_m
                best_dur_ns = dur_m_ns
                best_mm_consumed = mm_cons
                best_will_swap = will_swap
                best_score = score
        co_ns = best_co_ns
        finish_ns = best_start_ns + best_dur_ns
        # Update per-machine last-item AFTER placement
        machine_last_item[best_machine] = curr_item
        machine_last_itype[best_machine] = curr_itype
        # Update cutter mother-roll state
        if best_machine in PARENT_AWARE_CUTTERS and mm_per_cut_map:
            if best_will_swap:
                # Loaded a new mother roll
                cutter_loaded_parent[best_machine] = parent_of(curr_item)
                cutter_loaded_lot_id[best_machine] = lid
                cutter_roll_remaining_mm[best_machine] = max(0.0, MOTHER_ROLL_MAX_MM - best_mm_consumed)
            else:
                # Same roll, just consumed more MM
                cutter_roll_remaining_mm[best_machine] = max(0.0, cutter_roll_remaining_mm[best_machine] - best_mm_consumed)
        return (best_machine, best_start_ns, finish_ns, co_ns)

    def place_campaign(cid):
        c = campaigns[cid]
        wire = c["wire"]
        camp_lots = c["lots"]
        if not camp_lots:
            return False
        elig = None
        for l in camp_lots:
            m_set = set(lot_machines.get(l, []))
            elig = m_set if elig is None else (elig & m_set)
        if not elig:
            for l in camp_lots:
                infeas.append({"lot_id": l, "item": lot_item.get(l,""),
                               "reason": "no_common_machine_for_campaign", "campaign_id": cid})
            return False
        dur_per_lot_ns = {}
        total_dur_ns = 0
        for l in camp_lots:
            d_ns = int(float(duration_h.get(l, 0)) * 3600 * NS)
            dur_per_lot_ns[l] = d_ns
            total_dur_ns += d_ns
        intra_co_ns = int(INTRA_CAMP_CHANGEOVER_MIN * 60 * NS)
        total_dur_ns += intra_co_ns * max(0, len(camp_lots) - 1)
        # CPM-driven: campaign anchored to the earliest LFT of its lots
        camp_target_finish_ns = None
        for l in camp_lots:
            lft = lft_map.get(l)
            if lft is not None and not pd.isna(lft):
                lns = pd.Timestamp(lft).value
                if camp_target_finish_ns is None or lns < camp_target_finish_ns:
                    camp_target_finish_ns = lns
        if camp_target_finish_ns is None:
            # fallback to wave anchor
            wa = None
            for l in camp_lots:
                wid = lot_wave_first.get(l, "Z99")
                a = _wave_anchor(wid)
                if wa is None or a < wa:
                    wa = a
            camp_target_finish_ns = (wa or T0_ns) + total_dur_ns
        target_start_ns = camp_target_finish_ns - total_dur_ns
        # FIX 3: Clamp campaign target start by lookback safety belt to prevent
        # campaigns from being anchored months in the past (e.g. WBC campaigns
        # were starting Jun 2025 because total_dur_ns was 7000+ hours).
        _safety_floor_ns = SCHEDULE_OPEN_NS - GLOBAL_MAX_LOOKBACK_NS
        if target_start_ns < _safety_floor_ns:
            target_start_ns = _safety_floor_ns
        # Per-MHE predecessor floor for FRC campaign - no T0 floor (V6_wave change)
        min_start_ns = NEG_INF_NS
        for l in camp_lots:
            for p in predecessors.get(l, ()):
                if p in schedule:
                    p_finish_ns = schedule[p]["finish_ns"]
                    p_start_ns = schedule[p]["scheduled_start"].value
                    p_dur_ns = max(0, p_finish_ns - p_start_ns)
                    p_mhe = max(1, lot_mhe_total.get(p, 1))
                    first_drum_ready_ns = p_start_ns + (p_dur_ns // p_mhe)
                    p_min_ns, _ = _ages_ns(lot_item.get(p, ""))
                    v = first_drum_ready_ns + p_min_ns
                    if v > min_start_ns:
                        min_start_ns = v
        best_machine = None
        best_start_ns = None
        best_score = None
        for m in elig:
            cur_earliest = min_start_ns
            if machine_last_camp_wire.get(m, "") and machine_last_camp_wire[m] != wire:
                cd_end = machine_last_camp_end_ns[m] + int(frc_cooldown_min * 60 * NS)
                if cd_end > cur_earliest:
                    cur_earliest = cd_end
            start = first_fit_jit(machine_busy[m], cur_earliest, target_start_ns, total_dur_ns)
            # FIX 2 (campaign path): same load-balance rule as main dispatcher.
            _TOL_NS = int(3600 * NS)
            _delta = abs(start - target_start_ns)
            score = (_delta // _TOL_NS, machine_busy_ns[m], _delta)
            if best_score is None or score < best_score:
                best_machine = m
                best_start_ns = start
                best_score = score
        if best_machine is None:
            for l in camp_lots:
                infeas.append({"lot_id": l, "item": lot_item.get(l,""),
                               "reason": "no_machine_slot_for_campaign", "campaign_id": cid})
            return False
        cur = best_start_ns
        for i, l in enumerate(camp_lots):
            s_ns = cur
            f_ns = cur + dur_per_lot_ns[l]
            schedule[l] = {
                "lot_id":   l,
                "item":     lot_item.get(l, ""),
                "machine":  best_machine,
                "operation": lot_op.get(l, ""),
                "department": lot_dept.get(l, ""),
                "equipment":  lot_eq.get(l, ""),
                "scheduled_start":  pd.Timestamp(s_ns),
                "scheduled_finish": pd.Timestamp(f_ns),
                "duration_h": round(float(duration_h.get(l, 0)), 3),
                "finish_ns": f_ns,
                "campaign_id": cid,
                "wire_type":   wire,
                "is_camp_first": (i == 0),
                "is_camp_last":  (i == len(camp_lots) - 1),
            }
            cur = f_ns + (intra_co_ns if i < len(camp_lots) - 1 else 0)
        insert_busy(best_machine, best_start_ns, cur)
        machine_last_camp_wire[best_machine] = wire
        machine_last_camp_end_ns[best_machine] = cur
        for l in camp_lots:
            s_l = schedule[l]["scheduled_start"].value
            for p in predecessors.get(l, ()):
                if p in schedule:
                    p_item = lot_item.get(p, "")
                    p_mn_ns, p_mx_ns = _ages_ns(p_item)
                    gap_ns = s_l - schedule[p]["finish_ns"]
                    if gap_ns < p_mn_ns:
                        aging_v.append({"producer_lot": p, "consumer_lot": l,
                                        "item": p_item, "gap_h": round(gap_ns/3600/NS,2),
                                        "min_aging_h": p_mn_ns/3600/NS, "type": "TOO_FRESH",
                                        "deficit_h": round((p_mn_ns - gap_ns)/3600/NS,2)})
                    elif gap_ns > p_mx_ns:
                        aging_v.append({"producer_lot": p, "consumer_lot": l,
                                        "item": p_item, "gap_h": round(gap_ns/3600/NS,2),
                                        "max_aging_h": p_mx_ns/3600/NS, "type": "EXPIRED",
                                        "excess_h": round((gap_ns - p_mx_ns)/3600/NS,2)})
        return True

    # ============================================================
    # BUILDING CAMPAIGN PLACEMENT (v2) — inch-locked, per-SKU campaigns
    # ------------------------------------------------------------
    # Green-Tyre / Carcass (dept "building") lots are NOT placed by the
    # per-lot JIT loop (which interleaves SKUs -> many changeovers, mixed
    # inches, tiny runs). Instead they are collected and placed here, AFTER
    # the main loop (so all component predecessors are already scheduled):
    #   - each building machine is LOCKED to a single inch (only formula/SKU
    #     changes within it);
    #   - lots are routed to an eligible same-inch machine (load-balanced);
    #   - per machine, lots are grouped by SKU into consecutive campaigns
    #     (A-A-A-B-B-C), ordered by urgency (earliest need first);
    #   - a flat 30-min changeover is inserted only between different SKUs
    #     (0 within a SKU's sub-lots);
    #   - start respects material-ready time (pooled-MHE predecessor floor).
    # ============================================================
    def _bld_earliest_ns(lid):
        earliest_ns = NEG_INF_NS
        pbi = {}
        for p in predecessors.get(lid, ()):
            if p in schedule:
                pbi.setdefault(lot_item.get(p, ""), []).append(p)
        BIGV = (1 << 62)
        for ip, pl in pbi.items():
            ie = BIGV
            for p in pl:
                pf = schedule[p]["finish_ns"]; ps = schedule[p]["scheduled_start"].value
                pdur = max(0, pf - ps); pm = max(1, lot_mhe_total.get(p, 1))
                fdr = ps + (pdur // pm)
                pmin, _ = _ages_ns(ip)
                v = fdr + pmin
                if v < ie:
                    ie = v
            if ie < BIGV and ie > earliest_ns:
                earliest_ns = ie
        return earliest_ns if earliest_ns > NEG_INF_NS else SCHEDULE_OPEN_NS

    def _bld_dur_ns(lid, m):
        q = float(lot_qty.get(lid, 0) or 0)
        if m in machine_cycle_sec and q > 0:
            return int(q * machine_cycle_sec[m] * NS)
        return int(float(duration_h.get(lid, 0)) * 3600 * NS)

    def place_building_campaigns(blds):
        if not blds:
            return 0, 0
        CO_SAME = int(BUILDING_CO_SAME_MIN * 60 * NS)
        CO_DIFF = int(BUILDING_CO_DIFF_MIN * 60 * NS)
        DAY_NS = int(24 * 3600 * NS)
        # Per-component cart-ready times (first-drum) from already-placed producers
        # — lets a building lot consume the FRESHEST cart available at its start.
        comp_carts = defaultdict(list)
        for _p, _r in schedule.items():
            _ps = _r["scheduled_start"].value; _pf = _r["finish_ns"]
            _m = max(1, lot_mhe_total.get(_p, 1))
            comp_carts[lot_item.get(_p, "")].append(_ps + (_pf - _ps) // _m)
        for _k in comp_carts:
            comp_carts[_k].sort()
        # Per-lot window:
        #   due       = curing deadline (Phase-4 LFT)  -> MUST finish by this.
        #   kit_ready = max over components (first cart ready + comp min_age).
        #   fresh_floor = due - (GT max_age - GT min_age): GT cannot be built
        #               earlier than its own shelf life before curing, else it
        #               expires WAITING for curing.
        #   earliest  = max(kit_ready, fresh_floor, schedule-open).
        BIG = pd.Timestamp("2100-01-01").value
        info = {}
        for lid in blds:
            sku = _lot_sku1.get(lid, "")
            # FULL-KIT hard rule: building cannot start until EACH component TYPE
            # has a FINISHED, aged lot ready. For each component item we take the
            # earliest fully-produced lot (min finish), then wait for the latest
            # such across all component types (max). WIP-covered components are
            # already in stock so they don't gate. (We may delay building, but
            # never build without the full kit.)
            _pbi = defaultdict(list)
            for p in predecessors.get(lid, ()):
                if p in schedule:
                    _pbi[lot_item.get(p, "")].append(p)
            kit_ready = NEG_INF_NS
            for _ip, _pl in _pbi.items():
                _ir = min(schedule[p]["finish_ns"] for p in _pl) + _ages_ns(_ip)[0]
                if _ir > kit_ready:
                    kit_ready = _ir
            lft = lft_map.get(lid)
            due = pd.Timestamp(lft).value if (lft is not None and not pd.isna(lft)) else BIG
            gmin, gmax = _ages_ns(lot_item.get(lid, ""))
            # GT-freshness early floor applies ONLY with a real curing deadline.
            # Without an LFT (due == BIG sentinel) there is no early bound — never
            # push the lot toward the sentinel date.
            fresh_floor = (due - max(0, gmax - gmin)) if due < BIG else NEG_INF_NS
            _cand = [x for x in (kit_ready, fresh_floor, SCHEDULE_OPEN_NS) if x > NEG_INF_NS]
            earliest = max(_cand) if _cand else SCHEDULE_OPEN_NS
            # machine set (MG-consistent):
            # The BOM is exploded per machine-GROUP, so a lot's components are made
            # for its exploded MG (= its routing-eligible machines, which are
            # group-specific). The tbs2 GT machine may belong to a DIFFERENT group
            # than the components were produced for.
            #   GT (stage-2): use the tbs2 machine ONLY IF it's in this lot's MG
            #     group (i.e. present in its routing-eligible set). If the tbs2
            #     machine is a different group, DON'T use it (components wouldn't
            #     match) -> fall back to the lot's own MG machines.
            #   CARCASS (stage-1): not in tbs2 -> use its own (stage-1) MG machines.
            # tbs2 DISABLED: schedule building purely on MG-routing eligibility
            # (group/inch-specific) with load balancing — no single-machine pinning.
            machines = list(lot_machines.get(lid, []))
            info[lid] = {"sku": sku, "inch": _resolve_inch(sku),
                         "machines": machines, "tbs2": set(),
                         "earliest": earliest, "due": due,
                         "due_day": due // DAY_NS,
                         # SCHEDULING IDENTITY = the GT / carcass ITEM code, NOT the
                         # tyre SKU. A GT and its carcass share the SAME SKU code but
                         # are distinct products (GT… vs CAR…) on DISJOINT machine
                         # pools (stage-2 vs stage-1). Grouping by SKU merged them and
                         # collapsed eligibility; grouping by item code keeps them
                         # separate and on their own machines.
                         "code": lot_item.get(lid, "")}

        # ================== CTB (CLEAR-TO-BUILD) MATERIAL GATE ==================
        # Compute a per-lot material_ready_ns floor: the time when CUMULATIVE
        # component supply (opening WIP + finished production) first covers the
        # CUMULATIVE consumption through this lot. Consumption is swept in
        # earliest-order (the order lots want to build), per component, globally.
        material_ready = {}
        if BUILDING_CTB_GATE and blds:
            import numpy as _np
            # 1) per-tyre consumption rates from the Phase-1 demand explosion
            #    rate[(component_item, sku)] = demand_qty(comp) / demand_qty(GT)
            _rate = {}
            try:
                _dem = pd.read_csv(OUTPUTS / "phase1_demand_updated.csv.gz")
                _dem["item_code"] = _dem["item_code"].astype(str).str.strip()
                _dem["sku"] = _dem["sku"].astype(str).str.strip()
                _dem["demand_qty"] = pd.to_numeric(_dem["demand_qty"], errors="coerce").fillna(0.0)
                _bld_codes = {info[l]["code"] for l in blds}
                _gtq = (_dem[_dem["item_code"].isin(_bld_codes)]
                        .groupby("sku")["demand_qty"].sum().to_dict())
                _csum = _dem.groupby(["sku", "item_code"])["demand_qty"].sum()
                for (sku_k, ic), q in _csum.items():
                    g = _gtq.get(sku_k, 0.0)
                    if g > 0:
                        _rate[(ic, sku_k)] = q / g
            except Exception as _e:
                print(f"  CTB: demand-rate load failed ({_e}); gate disabled")
                _rate = {}

            if _rate:
                # 2) opening WIP per component (length units normalised to MM)
                _wip = {}
                try:
                    _inv = pd.read_csv(INPUTS / cfg["files"]["inventory"], dtype=str)
                    _inv["inventory"] = pd.to_numeric(_inv["inventory"], errors="coerce").fillna(0.0)
                    _L2MM = {"M":1000.,"MTR":1000.,"METER":1000.,"METRE":1000.,"CM":10.,"MM":1.}
                    _f = _inv["unit"].astype(str).str.upper().str.strip().map(_L2MM).fillna(1.0)
                    _inv["_q"] = _inv["inventory"] * _f
                    _wip = _inv.groupby("itemcode")["_q"].sum().to_dict()
                except Exception as _e:
                    print(f"  CTB: WIP load failed ({_e}); using 0 opening stock")

                # 3) component set = direct predecessor items of building lots
                _comp_of = {}            # lid -> {component_item: consume_qty}
                _comps = set()
                for lid in blds:
                    sku = info[lid]["sku"]; q = float(lot_qty.get(lid, 0) or 0)
                    cc = {}
                    seen_items = set()
                    for p in predecessors.get(lid, ()):
                        ip = lot_item.get(p, "")
                        if ip in seen_items or ip in _bld_codes:
                            # skip already-seen and carcass/GT items that are ALSO
                            # built in this same layout (their finishes aren't in
                            # `schedule` yet, so their supply can't be gated here).
                            continue
                        seen_items.add(ip)
                        r = _rate.get((ip, sku))
                        if r and r > 0:
                            cc[ip] = q * r
                            _comps.add(ip)
                    _comp_of[lid] = cc

                # 4) per-component supply curve: sorted finish_ns + cumulative qty
                #    (WIP is available from the start of the horizon).
                _sup = {}
                _prod = defaultdict(list)
                for p, srec in schedule.items():
                    ip = lot_item.get(p, "")
                    if ip in _comps:
                        _prod[ip].append((srec["finish_ns"], float(lot_qty.get(p, 0) or 0)))
                for c in _comps:
                    evs = sorted(_prod.get(c, []))
                    fins = _np.array([SCHEDULE_OPEN_NS] + [e[0] for e in evs], dtype=_np.int64)
                    qs   = _np.array([_wip.get(c, 0.0)] + [e[1] for e in evs], dtype=_np.float64)
                    _sup[c] = (fins, _np.cumsum(qs))

                # 5) sweep building lots in earliest-order; accumulate consumption
                #    and find the supply-catch-up time per component.
                _consumed = defaultdict(float)
                # Sweep in the SAME order the per-machine layout will run lots
                # (curing-due order), so the cumulative-consumption frame matches
                # the actual chronological build order as closely as possible.
                _order = sorted(blds, key=lambda l: (info[l]["due_day"], info[l]["code"],
                                                     info[l]["due"], l))
                _n_gated = 0
                for lid in _order:
                    mr = NEG_INF_NS
                    for c, qc in _comp_of[lid].items():
                        need = _consumed[c] + qc
                        _consumed[c] = need
                        fins, cum = _sup.get(c, (None, None))
                        if fins is None:
                            continue
                        # earliest index where cumulative supply >= need
                        idx = int(_np.searchsorted(cum, need, side="left"))
                        if idx >= len(fins):
                            t_ready = int(fins[-1])      # supply never fully covers -> last finish
                        else:
                            t_ready = int(fins[idx])
                        if t_ready > mr:
                            mr = t_ready
                    if mr > info[lid]["earliest"]:
                        material_ready[lid] = mr
                        _n_gated += 1
                print(f"  CTB GATE: {len(_comps)} components tracked | "
                      f"{_n_gated:,}/{len(blds):,} building lots material-gated later than full-kit")
        # ---- VOLUME-FIRST balanced lot -> machine assignment ----
        # Root cause of the long tail was a bin-packing ORDER problem: assigning lots
        # in due-date order let many SMALL SKUs fill the machines' portfolios first,
        # so by the time a HIGH-VOLUME SKU needed a 2nd/3rd machine every eligible one
        # was "full" -> the big SKU got stranded on ONE machine and ran past 100%.
        #
        # Fix (LPT / longest-processing-time): rank SKUs by TOTAL build-hours and place
        # the BIGGEST first. Each SKU claims as many of its eligible machines as its
        # volume genuinely needs (= ceil(hours / fair-share)), spreading its lots evenly
        # so no machine saturates; THEN small SKUs pack into the leftover gaps. This
        # holds freshness (unchanged), full-kit (unchanged) and inch-lock, while pulling
        # every machine toward the feasible ~fair-share utilisation.
        machine_inch, machine_load, machine_lots = {}, defaultdict(int), defaultdict(list)
        machine_skus = defaultdict(set)   # machine -> distinct SKUs (portfolio)
        sku_machines_used = defaultdict(set)   # SKU -> machines it runs on (SKU portfolio)
        _all_m, _tot = set(), 0
        for _l in blds:
            _ms = info[_l]["machines"]
            if _ms:
                _tot += _bld_dur_ns(_l, _ms[0]); _all_m.update(_ms)
        LOAD_CAP = (_tot / max(len(_all_m), 1)) if _all_m else 0
        def _pcap(m):
            # per-machine portfolio cap: fast/high-efficiency machines get a bigger one
            if str(m) in BUILDING_FAST_MACHINES or machine_cycle_sec.get(m, 1e9) <= BUILDING_FAST_CYCLE_SEC:
                return BUILDING_PORTFOLIO_MAX_FAST
            return BUILDING_PORTFOLIO_MAX

        # ---- aggregate lots into ITEM groups (GT / carcass CODE + inch) ----
        # Group by the GT/carcass item CODE (not the tyre SKU): GT and carcass share
        # a SKU code but are different products on disjoint machine pools.
        sku_group = defaultdict(lambda: {"lots": [], "hours_ns": 0, "inch": None,
                                         "elig": None, "no_mach": False})
        for lid in blds:
            elig = info[lid]["machines"]
            if not elig:
                infeas.append({"lot_id": lid, "item": lot_item.get(lid, ""),
                               "reason": "no_building_machine", "campaign_id": ""})
                continue
            s = info[lid]["code"]; inch = info[lid]["inch"]
            g = sku_group[(s, str(inch))]
            g["lots"].append(lid)
            g["hours_ns"] += _bld_dur_ns(lid, elig[0])
            g["inch"] = inch; g["sku"] = s
            # eligible set = intersection across the item's lots (all should match)
            g["elig"] = set(elig) if g["elig"] is None else (g["elig"] & set(elig))
        # items whose lots disagree on eligibility fall back to union (rare)
        for key, g in sku_group.items():
            if not g["elig"]:
                g["elig"] = set()
                for lid in g["lots"]:
                    g["elig"].update(info[lid]["machines"])

        # ====== HEIJUNKA DAILY-LEVELING LAYOUT (option b) ======
        # Level the build to the daily curing takt with high SKU diversity:
        # process day-by-day; each day, round-robin the ripe SKUs (curing due
        # within LEAD days, kit-ready) across eligible machines, building a small
        # MINI-lot mini-campaign per machine-visit. This spreads ~all SKUs being
        # cured across every day -> ~35-40 distinct SKUs/day and a level ~takt qty,
        # instead of front-loaded single-SKU campaigns that dip on trough days.
        if BUILDING_LEVEL:
            from collections import deque
            DAYn  = int(24 * 3600 * NS)
            LEADn = int(BUILDING_LEVEL_LEAD_DAYS * 24 * 3600 * NS)
            MINI  = max(1, int(BUILDING_LEVEL_MINI_CAMPAIGN))
            pend = defaultdict(list); all_m = set()
            for lid in blds:
                if not info[lid]["machines"]:
                    continue          # no-machine infeasibles already logged above
                pend[info[lid]["code"]].append(lid); all_m.update(info[lid]["machines"])
            for c in pend:
                pend[c].sort(key=lambda l: (info[l]["due"], info[l]["earliest"], l))
            pend = {c: deque(v) for c, v in pend.items()}
            mnext = {m: (machine_busy[m][-1][1] if machine_busy.get(m) else SCHEDULE_OPEN_NS)
                     for m in all_m}
            minch = {}; mlast = {}; mlast_inch = {}
            n_placed = n_co = n_late = 0
            _dues = [info[l]["due"] for l in blds if info[l]["due"] < BIG]
            horizon_end = (max(_dues) if _dues else SCHEDULE_OPEN_NS) + 25 * DAYn
            # TAKT: level daily build qty to the demand rate so we neither front-load
            # (which starves later trough days) nor JIT-starve (which tails out).
            _tot_q = sum(float(lot_qty.get(l, 0) or 0) for l in blds)
            _span_days = max(1.0, (horizon_end - 25 * DAYn - SCHEDULE_OPEN_NS) / DAYn)
            TAKT_CAP = (_tot_q / _span_days) * BUILDING_LEVEL_TAKT_HEADROOM

            def _inch_ok2(m, inch):
                if not BUILDING_INCH_LOCK:
                    return True
                return (m not in minch) or inch is None or minch[m] == inch

            def _emit(lid, m, code, inch):
                nonlocal n_placed, n_co, n_late
                start = max(mnext[m], info[lid]["earliest"])
                _mr = material_ready.get(lid)
                if _mr is not None and _mr > start:
                    start = _mr
                if mlast.get(m) is not None and mlast[m] != code:
                    _ia = mlast_inch.get(m); _ib = inch
                    diff = (_ia is not None and _ib is not None and _ia != _ib)
                    co_sm, co_dm = _bld_co.get(str(m), (BUILDING_CO_SAME_MIN, BUILDING_CO_DIFF_MIN))
                    start += int((co_dm if diff else co_sm) * 60 * NS); n_co += 1
                dur = _bld_dur_ns(lid, m); finish = start + dur
                if info[lid]["due"] < BIG and finish > info[lid]["due"]:
                    n_late += 1
                schedule[lid] = {
                    "lot_id": lid, "item": lot_item.get(lid, ""), "machine": m,
                    "operation": lot_op.get(lid, ""), "department": lot_dept.get(lid, ""),
                    "equipment": lot_eq.get(lid, ""),
                    "scheduled_start": pd.Timestamp(start),
                    "scheduled_finish": pd.Timestamp(finish),
                    "duration_h": duration_h.get(lid, 0.0),
                    "campaign_id": "", "wire_type": "",
                    "is_camp_first": False, "is_camp_last": False, "finish_ns": finish,
                }
                bisect.insort(machine_busy[m], (start, finish))
                machine_busy_ns[m] += (finish - start)
                mnext[m] = finish
                if inch is not None:
                    minch.setdefault(m, inch)
                mlast[m] = code; mlast_inch[m] = inch
                n_placed += 1

            day0 = SCHEDULE_OPEN_NS
            while any(pend[c] for c in pend) and day0 < horizon_end:
                de = day0 + DAYn
                day_qty = 0.0          # tyres built this plant-day (for takt leveling)
                progress = True
                while progress:
                    progress = False
                    ripe = []
                    for c, dq in pend.items():
                        if not dq:
                            continue
                        l0 = dq[0]
                        if info[l0]["earliest"] <= de and (info[l0]["due"] <= de + LEADn or info[l0]["due"] >= BIG):
                            ripe.append((info[l0]["due"], c))
                    if not ripe:
                        break
                    ripe.sort()
                    for _, c in ripe:
                        dq = pend[c]
                        if not dq:
                            continue
                        l0 = dq[0]
                        if info[l0]["earliest"] > de:
                            continue
                        if not (info[l0]["due"] <= de + LEADn or info[l0]["due"] >= BIG):
                            continue
                        # LEVELING: a lot is URGENT if its curing is due today/tomorrow
                        # (must build now) — always allowed. Build-AHEAD lots only fill
                        # up to the daily takt cap, so the day stays level and the rest
                        # spreads to later days (keeps every day diverse + ~takt).
                        _urgent = info[l0]["due"] <= de + DAYn
                        if (not _urgent) and day_qty >= TAKT_CAP:
                            continue
                        inch = info[l0]["inch"]
                        cands = [m for m in info[l0]["machines"] if mnext[m] < de and _inch_ok2(m, inch)]
                        if not cands:
                            continue
                        m = min(cands, key=lambda x: mnext[x])
                        built = 0
                        while dq and built < MINI:
                            lid = dq[0]
                            if info[lid]["earliest"] > de:
                                break
                            if not (info[lid]["due"] <= de + LEADn or info[lid]["due"] >= BIG):
                                break
                            if built > 0 and mnext[m] >= de:
                                break
                            _u = info[lid]["due"] <= de + DAYn
                            if (not _u) and day_qty >= TAKT_CAP:
                                break
                            _emit(lid, m, info[lid]["code"], inch)
                            day_qty += float(lot_qty.get(lid, 0) or 0)
                            dq.popleft(); built += 1; progress = True
                day0 = de
            # Leftover (couldn't fit within any day window) -> ASAP on least-loaded eligible.
            for c, dq in list(pend.items()):
                while dq:
                    lid = dq.popleft(); ms = info[lid]["machines"]
                    m = min(ms, key=lambda x: mnext.get(x, SCHEDULE_OPEN_NS))
                    _emit(lid, m, info[lid]["code"], info[lid]["inch"])
            print(f"  BUILDING (LEVELED): placed {n_placed:,} lots on {len(all_m)} machines | "
                  f"{n_co} changeovers | lead={BUILDING_LEVEL_LEAD_DAYS:.0f}d mini={MINI} | "
                  f"late-vs-curing: {n_late:,}")
            return n_placed, n_co

        # ---- place SKUs largest-first with CAP-AND-SPILL load balancing ----
        # Each machine is capped at ~one month of work (MACHINE_CAP_NS). While the
        # cap is reached and an ELIGIBLE sibling still has room, the SKU spills its
        # remaining lots to the least-loaded eligible machine instead of pushing one
        # machine past 100%. Changeovers are cheap (budget ~2,000+/month vs ~300 now),
        # so spreading aggressively for balance is the right trade-off: it levels each
        # routing group toward its average util and pulls the tail back into the month.
        MACHINE_CAP_NS = int(BUILDING_MONTH_CAP_H * 3600 * NS)
        # TWO-PHASE order:
        #   Phase A — SKUs eligible on exactly ONE machine. They have no choice, so
        #     they RESERVE that machine first; flexible SKUs then see the reserved
        #     load and avoid piling on top (which previously forced >100%).
        #   Phase B — all multi-eligible SKUs, largest-volume first (LPT) with
        #     cap-and-spill, balancing into whatever capacity Phase A left.
        # (Sorting ALL SKUs by ascending eligibility instead overloads machines that
        #  are the sole option for several 2-3-eligible SKUs, so we gate strictly on
        #  len==1 for the reservation phase.)
        _locked = sorted([kv for kv in sku_group.items() if len(kv[1]["elig"]) == 1],
                         key=lambda kv: -kv[1]["hours_ns"])
        _flex   = sorted([kv for kv in sku_group.items() if len(kv[1]["elig"]) > 1],
                         key=lambda kv: -kv[1]["hours_ns"])
        for key, g in _locked + _flex:
            s = g["sku"]; inch = g["inch"]; elig = list(g["elig"])
            def _inch_ok(m):
                if not BUILDING_INCH_LOCK:
                    return True
                # SOFT INCH (default): never hard-reject — _inch_penalty ranks instead,
                # so NO machine is permanently inch-fixed; an off-inch machine is used
                # only when better-matched machines are full, at a size-change penalty.
                if BUILDING_SOFT_INCH:
                    return True
                # legacy HARD gate (JK_SOFT_INCH=0): allowed-set is a wall.
                if (BUILDING_MACHINE_INCH and inch is not None
                        and str(m) in MACHINE_ALLOWED and inch not in MACHINE_ALLOWED[str(m)]):
                    return False
                return (m not in machine_inch) or inch is None or machine_inch[m] == inch
            # SOFT INCH penalty tiers (lower = cheaper). Released only at a cost:
            #   0  this is the machine's running/dominant inch, or an allowed inch
            #   1  flexible machine (no plant inch data)
            #   +3 machine already locked to a DIFFERENT inch (switch a busy machine)
            #   4  OFF-pattern inch on an inch-locked machine (size-change penalty)
            def _inch_penalty(x):
                if inch is None or not BUILDING_MACHINE_INCH:
                    return 0
                xs = str(x)
                base = 3 if (x in machine_inch and machine_inch[x] != inch
                             and MACHINE_DOM.get(xs) != inch) else 0
                al = MACHINE_ALLOWED.get(xs)
                if al is None:
                    tier = 1
                elif inch in al:
                    tier = 0
                else:
                    tier = 4
                return base + tier
            def _sel_key(x):
                return (_inch_penalty(x), machine_load[x])
            chosen = []                       # machines this SKU runs on (grows on demand)
            def _open(m):
                if inch is not None:
                    machine_inch.setdefault(m, inch)
                machine_skus[m].add(s); sku_machines_used[s].add(m); chosen.append(m)
            # distribute lots in due order; spill to a fresh eligible machine whenever
            # every currently-chosen machine is at the monthly cap.
            for lid in sorted(g["lots"], key=lambda l: (info[l]["due"], l)):
                avail = [m for m in chosen if machine_load[m] < MACHINE_CAP_NS]
                if not avail:
                    # spill: dominant-inch-preferred, allowed, under cap, not yet used
                    cand = sorted([m for m in elig if m not in chosen and _inch_ok(m)
                                   and machine_load[m] < MACHINE_CAP_NS],
                                  key=_sel_key)
                    if cand:
                        _open(cand[0]); avail = [cand[0]]
                    else:
                        # no allowed machine has room -> least-loaded eligible overall
                        # (feasibility fallback; prefers allowed+dominant where possible)
                        avail = chosen or sorted(elig, key=_sel_key)[:1]
                        if not chosen and avail:
                            _open(avail[0])
                m = min(avail, key=lambda x: machine_load[x])
                machine_lots[m].append(lid)
                machine_load[m] += _bld_dur_ns(lid, m)

        # ---- per-machine layout: curing-deadline order, SKU-grouped per day ----
        # Lots are ordered by (curing-day, SKU); within a day a SKU runs as one
        # consecutive campaign. Start is floored at earliest (kit ready & not too
        # early to expire). Changeover: same-inch 40 min, different-inch 60 min.
        n_placed = n_co = n_late = 0
        for m, mlots in machine_lots.items():
            # order/changeover by GT|carcass item CODE (not tyre SKU) so consecutive
            # lots of the same product form one campaign on the machine.
            order = sorted(mlots, key=lambda l: (info[l]["due_day"], info[l]["code"], info[l]["due"], l))
            cursor = machine_busy[m][-1][1] if machine_busy.get(m) else SCHEDULE_OPEN_NS
            last_sku = None
            for lid in order:
                s = info[lid]["code"]
                if last_sku is not None and s != last_sku:
                    # "different size" only when BOTH inches are known and differ;
                    # unknown inch defaults to same-size (machine is inch-locked).
                    _ia = info[_last_lid]["inch"]; _ib = info[lid]["inch"]
                    diff_inch = (_ia is not None and _ib is not None and _ia != _ib)
                    # per-machine changeover from Master_Building_ChangeoverTime
                    # (same-size vs different-size); fall back to the flat constants.
                    co_sm, co_dm = _bld_co.get(str(m),
                                               (BUILDING_CO_SAME_MIN, BUILDING_CO_DIFF_MIN))
                    cursor += int((co_dm if diff_inch else co_sm) * 60 * NS)
                    n_co += 1
                start = max(cursor, info[lid]["earliest"])
                # CTB: don't start before cumulative component supply covers it.
                _mr = material_ready.get(lid)
                if _mr is not None and _mr > start:
                    start = _mr
                dur = _bld_dur_ns(lid, m)
                finish = start + dur
                if info[lid]["due"] < BIG and finish > info[lid]["due"]:
                    n_late += 1
                schedule[lid] = {
                    "lot_id": lid, "item": lot_item.get(lid, ""), "machine": m,
                    "operation": lot_op.get(lid, ""), "department": lot_dept.get(lid, ""),
                    "equipment": lot_eq.get(lid, ""),
                    "scheduled_start": pd.Timestamp(start),
                    "scheduled_finish": pd.Timestamp(finish),
                    "duration_h": duration_h.get(lid, 0.0),
                    "campaign_id": "", "wire_type": "",
                    "is_camp_first": False, "is_camp_last": False,
                    "finish_ns": finish,
                }
                bisect.insort(machine_busy[m], (start, finish))
                machine_busy_ns[m] += (finish - start)
                cursor = finish; n_placed += 1
                last_sku = s; _last_lid = lid
        print(f"  BUILDING CAMPAIGNS: placed {n_placed:,} lots on {len(machine_lots)} "
              f"machines | {n_co} changeovers (per-machine same/diff) | inch-locked | "
              f"late-vs-curing: {n_late:,}")
        try:
            _dbg = [{"lid": l, "code": info[l]["code"],
                     "earliest": pd.Timestamp(info[l]["earliest"]),
                     "due": (pd.Timestamp(info[l]["due"]) if info[l]["due"] < BIG else pd.NaT),
                     "qty": float(lot_qty.get(l, 0) or 0),
                     "start": (schedule[l]["scheduled_start"] if l in schedule else pd.NaT),
                     "machine": (schedule[l]["machine"] if l in schedule else "")}
                    for l in blds if info[l]["machines"]]
            pd.DataFrame(_dbg).to_csv(OUTPUTS / "building_debug.csv", index=False)
            print(f"  [debug] wrote building_debug.csv ({len(_dbg)} lots)")
        except Exception as _e:
            print(f"  [debug] dump failed: {_e}")
        return n_placed, n_co

    print("\n  Placing lots (JIT + campaign-atomic for belt)...")
    placed = 0
    placed_belt_campaigns = 0
    n_orphans_skipped = 0
    building_lots = []
    import time as _t
    _t0 = _t.time()
    for k, lid in enumerate(topo):
        if k % 1000 == 0 and k > 0:
            _el = _t.time() - _t0
            _rt = k / _el if _el > 0 else 0
            print(f"    [{k:6d}/{len(topo):6d}] placed={placed} infeas={len(infeas)} "
                  f"elapsed={_el:.1f}s rate={_rt:.0f}/s", flush=True)

        if lot_is_belt.get(lid, False):
            cid = lot_camp_id.get(lid, "")
            if cid:
                c = campaigns.get(cid)
                if c is None:
                    continue
                c["ready"] += 1
                if c["ready"] < c["n_total"]:
                    continue
                ok = place_campaign(cid)
                if ok:
                    placed_belt_campaigns += 1
                    placed += sum(1 for l in c["lots"] if l in schedule)
                continue

        # === CASCADING ORPHAN-LOT SKIP ===
        # A lot is skipped if it doesn't reach any terminal (Building / Curing)
        # via the DAG. This catches MULTI-LEVEL orphans:
        #   FRC mother roll -> orphan cutter cut -> orphan Stage-1 carcass
        #   (Stage-2 had its own demand filled elsewhere, so the carcass and
        #    everything upstream of it become effectively orphan.)
        # cascade_orphans was computed BEFORE the placement loop by reverse-topo
        # reachability analysis (see above).
        if lid in cascade_orphans:
            n_orphans_skipped += 1
            continue

        # Building (Green Tyre / Carcass) lots are placed by the campaign pass
        # AFTER this loop (so their predecessors are already scheduled).
        if lot_item_type.get(lid, "") in BUILDING_ITYPES:
            building_lots.append(lid)
            continue

        result = place_one_lot_jit(lid)
        if result is None:
            continue
        machine, start_ns, finish_ns, co_ns = result
        co_ns_int = int(co_ns or 0)
        machine_busy_ns[machine] += max(0, finish_ns - start_ns) + co_ns_int
        bisect.insort(machine_busy[machine], (start_ns, finish_ns + co_ns_int))
        schedule[lid] = {
            "lot_id":           lid,
            "item":             lot_item.get(lid, ""),
            "machine":          machine,
            "operation":        lot_op.get(lid, ""),
            "department":       lot_dept.get(lid, ""),
            "equipment":        lot_eq.get(lid, ""),
            "scheduled_start":  pd.Timestamp(start_ns),
            "scheduled_finish": pd.Timestamp(finish_ns),
            "duration_h":       duration_h.get(lid, 0.0),
            "campaign_id":      lot_camp_id.get(lid, ""),
            "wire_type":        lot_wire.get(lid, ""),
            "is_camp_first":    False,
            "is_camp_last":     False,
            "finish_ns":        finish_ns,
        }
        placed += 1

    # === Building campaign pass (inch-locked, per-SKU, 30-min CO) ===
    n_bld, n_bld_co = place_building_campaigns(building_lots)
    placed += n_bld

    # === SHORT-LIFE COMPONENT RESYNC (post-building) ===
    # Short-shelf-life components (capstrip 24h, chafer 24h, beads 48h, …) were
    # placed front-loaded against the curing LFT, but building now runs all month,
    # so they age out before their tyre is built. Re-time each short-life component
    # lot LATER to track actual building consumption: spread an item's producer lots
    # across its consumers' timeline so each batch is consumed within its max-age.
    # SAFETY: these last-stage parts are made from long-shelf-life compounds, so a
    # producer lot is only moved as late as (a) its earliest served consumer minus
    # min-age and (b) the point where its OWN inputs would over-age — whichever is
    # tighter. We only ever move LATER (front-loaded -> JIT); never earlier.
    def resync_short_life_components(short_life_h=72.0):
        NS_H = 3600 * 1e9
        moved = held = 0
        # 1) collect short-life, non-building producer lots by item
        item_prod = defaultdict(list)
        for _lid, _s in schedule.items():
            if lot_item_type.get(_lid, "") in BUILDING_ITYPES:
                continue
            _it = lot_item.get(_lid, "")
            _mn, _mx = _ages_ns(_it)
            if 0 < _mx <= short_life_h * NS_H:
                item_prod[_it].append(_lid)
        for _it, _plots in item_prod.items():
            _mn, _mx = _ages_ns(_it)
            # building consumers of this item (across all its producer lots)
            _cons = []
            for _p in _plots:
                for _v in successors.get(_p, ()):
                    if _v in schedule and lot_item_type.get(_v, "") in BUILDING_ITYPES:
                        _cons.append(schedule[_v]["scheduled_start"].value)
            if not _cons:
                continue
            _cons.sort()
            _n = len(_plots)
            # spread producer lots across the consumption timeline (quantile match)
            _plots_sorted = sorted(_plots, key=lambda l: schedule[l]["scheduled_start"].value)
            for _i, _p in enumerate(_plots_sorted):
                _idx = min(len(_cons) - 1, int((_i + 0.5) / _n * len(_cons)))
                _cstart = _cons[_idx]
                _e = schedule[_p]
                _old_start = _e["scheduled_start"].value
                _old_fin   = _e["finish_ns"]
                _dur       = _old_fin - _old_start
                _m         = _e["machine"]
                # target: finish min_age before the consumer it serves
                _tgt_fin   = _cstart - _mn
                _tgt_start = _tgt_fin - _dur
                if _tgt_start <= _old_start:
                    continue  # only ever move LATER toward consumption
                # upper bound from this lot's OWN inputs over-aging: new_start must
                # keep every predecessor within its max-age.
                _pred_cap = None
                for _pp in predecessors.get(_p, ()):
                    if _pp in schedule:
                        _pmn, _pmx = _ages_ns(lot_item.get(_pp, ""))
                        _cap = schedule[_pp]["finish_ns"] + _pmx
                        _pred_cap = _cap if _pred_cap is None else min(_pred_cap, _cap)
                if _pred_cap is not None:
                    _tgt_start = min(_tgt_start, _pred_cap - _dur)
                if _tgt_start <= _old_start:
                    held += 1
                    continue
                # re-slot on the same machine: remove old interval, first-fit at target
                _busy = machine_busy.get(_m)
                if _busy is None:
                    continue
                _j = bisect.bisect_left(_busy, (_old_start,))
                if _j < len(_busy) and _busy[_j][0] == _old_start:
                    _occ = _busy[_j][1] - _busy[_j][0]
                    _busy.pop(_j)
                else:
                    _occ = _dur
                _new_start = first_fit_jit(_busy, _old_start, _tgt_start, _occ)
                _new_fin   = _new_start + _dur
                bisect.insort(_busy, (_new_start, _new_start + _occ))
                _e["scheduled_start"]  = pd.Timestamp(_new_start)
                _e["scheduled_finish"] = pd.Timestamp(_new_fin)
                _e["finish_ns"]        = _new_fin
                if _new_start > _old_start:
                    moved += 1
        print(f"  SHORT-LIFE RESYNC: re-timed {moved:,} component lots toward "
              f"consumption ({held:,} held back by input over-age limit)")
        return moved

    resync_short_life_components()

    _total = _t.time() - _t0
    print(f"\n  Placed: {placed:,} / {len(topo):,}   "
          f"belt_campaigns: {placed_belt_campaigns}   "
          f"infeas: {len(infeas):,}   aging_violations: {len(aging_v):,}   "
          f"orphans_skipped: {n_orphans_skipped:,}   "
          f"placement_time={_total:.1f}s")

    sched_df = pd.DataFrame([{k:v for k,v in s.items() if k != "finish_ns"}
                              for s in schedule.values()])
    # === ATOMIC WRITE (prevents OneDrive sync truncation) ===
    # OneDrive can leave the file half-written if pandas.to_csv is interrupted
    # during sync, causing 40-50% of rows to be silently lost. Write to a tmp
    # file first, then atomic-rename to the final path.
    _sched_final = OUTPUTS / _suff("phase5_schedule.csv")
    _sched_tmp   = OUTPUTS / (".tmp_" + _sched_final.name)
    try:
        sched_df.to_csv(_sched_tmp, index=False)
        if _sched_final.exists():
            try: _sched_final.unlink()
            except Exception: pass
        os.replace(_sched_tmp, _sched_final)
        print(f"    {_sched_final.name} ({len(sched_df):,})")
    finally:
        if _sched_tmp.exists():
            try: _sched_tmp.unlink()
            except Exception: pass

    if not sched_df.empty:
        camp = sched_df[sched_df["campaign_id"].astype(str) != ""]
        if not camp.empty:
            camp_summary = camp.groupby(["wire_type","campaign_id","machine"]).agg(
                n_lots=("lot_id","count"),
                start=("scheduled_start","min"),
                finish=("scheduled_finish","max"),
                items=("item", lambda s: ", ".join(sorted(set(s)))),
            ).reset_index().sort_values(["machine","start"])
            camp_summary["duration_h"] = ((camp_summary["finish"] - camp_summary["start"]).dt.total_seconds()/3600).round(2)
            camp_summary.to_csv(OUTPUTS/_suff("phase5_campaign_schedule.csv"), index=False)

    util = []
    for m, intervals in machine_busy.items():
        busy_ns = sum((e - s) for s,e in intervals)
        if intervals:
            span_ns = intervals[-1][1] - intervals[0][0]
            util.append({
                "machine": m, "n_lots": len(intervals),
                "busy_hours": round(busy_ns / 3600 / NS, 2),
                "span_hours": round(span_ns / 3600 / NS, 2),
                "utilisation_%": round(busy_ns / max(span_ns,1) * 100, 1),
                "first_start": pd.Timestamp(intervals[0][0]),
                "last_finish": pd.Timestamp(intervals[-1][1]),
            })
    util_df = pd.DataFrame(util).sort_values("busy_hours", ascending=False) if util else pd.DataFrame()
    if not util_df.empty:
        util_df.to_csv(OUTPUTS/_suff("phase5_machine_utilization.csv"), index=False)
        # === DB output: jkt_machine_utilization_updated ===
        try:
            import sys as _sys
            _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
            from db_loader import write_to_db, _resolve_source
            if _resolve_source(cfg) == "db":
                res = write_to_db(util_df, output_key="machine_utilization", cfg=cfg)
                print(f"    DB write: {res['table']} ({res['rows_written']:,} rows, "
                      f"{'CREATED' if res['created_or_replaced'] else 'TRUNCATED+INSERT'})")
        except Exception as _de:
            print(f"    DB write skipped (machine_utilization): {_de}")

    if infeas:
        pd.DataFrame(infeas).to_csv(OUTPUTS/_suff("phase5_infeasibilities.csv"), index=False)
    if aging_v:
        pd.DataFrame(aging_v).to_csv(OUTPUTS/_suff("phase5_aging_violations.csv"), index=False)

    # ===================================================================
    # PLANT CAP WIP AUDIT - Carcass + Green Tyre rolling inventory
    # ===================================================================
    try:
        TBM_S1 = {"7801","7802","7803","7804","7701","8101","7601",
                  "8001","8002","8003","6909","6911","6801","6802","6803"}
        TBM_S2 = {"8301","8302","8501","8502","8201","7301"}
        wip_audit_rows = []
        if sched_df is not None and not sched_df.empty:
            sd = sched_df.copy()
            sd["scheduled_start"] = pd.to_datetime(sd["scheduled_start"])
            sd["scheduled_finish"] = pd.to_datetime(sd["scheduled_finish"])
            try:
                lot_qty_map = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
            except Exception:
                lot_qty_map = {}
            sd["qty"] = sd["lot_id"].map(lot_qty_map).fillna(0)
            s1 = sd[sd["machine"].isin(TBM_S1)]
            s2 = sd[sd["machine"].isin(TBM_S2)]
            evt = []
            for _, r in s1.iterrows():
                evt.append((r["scheduled_finish"], +float(r["qty"]), "CARCASS_MADE"))
            for _, r in s2.iterrows():
                evt.append((r["scheduled_start"], -float(r["qty"]), "CARCASS_CONSUMED"))
            for _, r in s2.iterrows():
                evt.append((r["scheduled_finish"], +float(r["qty"]), "GT_MADE"))
            evt.sort(key=lambda x: x[0])
            carcass_wip = 0.0
            gt_wip = 0.0
            carcass_peak = 0.0
            carcass_min = 0.0
            gt_peak = 0.0
            for ts, delta, kind in evt:
                if kind == "CARCASS_MADE":
                    carcass_wip += delta
                elif kind == "CARCASS_CONSUMED":
                    carcass_wip += delta
                elif kind == "GT_MADE":
                    gt_wip += delta
                if carcass_wip > carcass_peak: carcass_peak = carcass_wip
                if carcass_wip < carcass_min:  carcass_min = carcass_wip
                if gt_wip > gt_peak:           gt_peak = gt_wip
            if evt:
                t0 = evt[0][0]; tN = evt[-1][0]
                days = pd.date_range(t0.floor("D"), tN.ceil("D"), freq="D")
                evt_idx = 0
                carcass = 0.0; gt = 0.0
                for d in days:
                    while evt_idx < len(evt) and evt[evt_idx][0] < d:
                        _, delta, kind = evt[evt_idx]
                        if kind == "CARCASS_MADE" or kind == "CARCASS_CONSUMED":
                            carcass += delta
                        elif kind == "GT_MADE":
                            gt += delta
                        evt_idx += 1
                    wip_audit_rows.append({
                        "date": str(d.date()),
                        "carcass_wip_eod": int(carcass),
                        "carcass_cap_violation": carcass > 4000,
                        "carcass_negative":     carcass < 0,
                        "green_tyre_cumulative": int(gt),
                    })
            print(f"\n  === CARCASS + GT WIP AUDIT ===")
            print(f"    Carcass WIP range: min={int(carcass_min):,} max={int(carcass_peak):,}  (plant cap 4,000)")
            print(f"    Carcass cap violations: {sum(1 for r in wip_audit_rows if r['carcass_cap_violation'])} days")
            print(f"    Carcass negative days:  {sum(1 for r in wip_audit_rows if r['carcass_negative'])} days")
            print(f"    Total Green Tyres made: {int(gt_peak):,}")
            if wip_audit_rows:
                pd.DataFrame(wip_audit_rows).to_csv(OUTPUTS/_suff("phase5_wip_carcass_gt.csv"), index=False)
                print(f"    {_suff('phase5_wip_carcass_gt.csv')} (refreshed)")
    except Exception as _e:
        print(f"    WIP audit skipped: {_e}")

    # ===================================================================
    # PER-DAY GREEN TYRE PRODUCTION (plant-day = 7 AM -> next 7 AM)
    # Green Tyre output = TBM Stage-2 + BJ GROUP + VMIMAXX + UNISTAGE
    #
    # PRORATED across plant-day boundary:
    # If a lot starts at 06:30 and finishes at 07:30 producing 60 tyres,
    # the model splits production by overlap:
    #   - 06:30 → 07:00 (30 min) → previous plant-day's bucket: 30 tyres
    #   - 07:00 → 07:30 (30 min) → next plant-day's bucket: 30 tyres
    # ===================================================================
    gt_daily_rows = []
    try:
        TBM_S2_GT  = {"8301","8302","8501","8502","8201","7301"}
        BJ_GT      = {"7101","7102","7103","7104","7105","7106","7201"}
        VMI_GT     = {"6001","6002","6003","6004","7001","7002","7003","7004"}
        UNI_GT     = {"7501","7502","7503"}
        ALL_GT_MACHINES = TBM_S2_GT | BJ_GT | VMI_GT | UNI_GT
        if sched_df is None or sched_df.empty:
            print(f"    GT daily: sched_df empty, skipping.")
        else:
            sd = sched_df.copy()
            sd["scheduled_start"]  = pd.to_datetime(sd["scheduled_start"])
            sd["scheduled_finish"] = pd.to_datetime(sd["scheduled_finish"])
            try:
                lot_qty_map = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
            except Exception:
                lot_qty_map = {}
            sd["qty"] = sd["lot_id"].map(lot_qty_map).fillna(0)
            gt_sched = sd[sd["machine"].isin(ALL_GT_MACHINES)].copy()
            print(f"    GT daily: {len(gt_sched):,} GT lots across {len(ALL_GT_MACHINES)} machines")
            if not gt_sched.empty:
                def _mg_label(m):
                    if m in TBM_S2_GT: return "TBM Stage-2"
                    if m in BJ_GT:     return "BJ GROUP"
                    if m in VMI_GT:    return "VMIMAXX GROUP"
                    if m in UNI_GT:    return "UNISTAGE GROUP"
                    return "OTHER"
                gt_sched["mg"] = gt_sched["machine"].apply(_mg_label)

                # === PRORATED BY 7 AM BOUNDARY ===
                # For each lot, walk through every plant-day window that the lot
                # overlaps. Allocate qty proportional to time overlap.
                # plant-day D starts at D 07:00 and ends at (D+1) 07:00.
                pd_bucket = {}  # (plant_day_date, mg) -> qty (float)

                lot_starts  = gt_sched["scheduled_start"].values
                lot_finishes = gt_sched["scheduled_finish"].values
                lot_qtys    = gt_sched["qty"].values.astype(float)
                lot_mgs     = gt_sched["mg"].values

                ONE_DAY = pd.Timedelta(days=1)
                SHIFT_H = pd.Timedelta(hours=7)

                for i in range(len(gt_sched)):
                    s = pd.Timestamp(lot_starts[i])
                    f = pd.Timestamp(lot_finishes[i])
                    q = lot_qtys[i]
                    mg = lot_mgs[i]
                    if q <= 0 or f <= s:
                        continue
                    dur_ns = (f - s).value
                    # First plant-day boundary at or before lot start:
                    # plant_day D starts at (D + 7h). For lot starting at 06:30,
                    # plant_day = (start - 7h).normalize() = previous day.
                    pd_start_of_s = (s - SHIFT_H).normalize() + SHIFT_H
                    # Walk forward through plant-day windows
                    cur_start = max(s, pd_start_of_s)
                    cur_pd    = pd_start_of_s
                    while cur_start < f:
                        cur_end = cur_pd + ONE_DAY
                        seg_end = min(f, cur_end)
                        seg_dur_ns = (seg_end - cur_start).value
                        prorated = q * (seg_dur_ns / dur_ns)
                        plant_day_date = (cur_pd - SHIFT_H).normalize().date() if cur_pd.hour >= 7 else cur_pd.normalize().date()
                        # Simpler: plant_day_date = (cur_pd - 7h).normalize().date()
                        plant_day_date = (cur_pd - SHIFT_H).date()
                        key = (plant_day_date, mg)
                        pd_bucket[key] = pd_bucket.get(key, 0.0) + prorated
                        # advance
                        cur_start = cur_end
                        cur_pd = cur_pd + ONE_DAY

                # Build aggregated DataFrame
                if pd_bucket:
                    rows_long = []
                    for (pd_date, mg), qty in pd_bucket.items():
                        rows_long.append({"plant_day": pd_date, "mg": mg, "qty": qty})
                    long_df = pd.DataFrame(rows_long)
                    agg = (long_df.groupby(["plant_day","mg"])["qty"].sum()
                                  .unstack(fill_value=0).reset_index())
                    for col in ["TBM Stage-2","BJ GROUP","VMIMAXX GROUP","UNISTAGE GROUP"]:
                        if col not in agg.columns:
                            agg[col] = 0
                    agg["TOTAL_GT"] = (agg["TBM Stage-2"] + agg["BJ GROUP"]
                                       + agg["VMIMAXX GROUP"] + agg["UNISTAGE GROUP"])
                    agg["window_start_07AM"] = pd.to_datetime(agg["plant_day"]) + pd.Timedelta(hours=7)
                    agg["window_end_07AM_next"] = pd.to_datetime(agg["plant_day"]) + pd.Timedelta(days=1, hours=7)
                    cols_order = ["plant_day","window_start_07AM","window_end_07AM_next",
                                  "TBM Stage-2","BJ GROUP","VMIMAXX GROUP","UNISTAGE GROUP","TOTAL_GT"]
                    agg = agg[cols_order].sort_values("plant_day").reset_index(drop=True)
                    for c in ["TBM Stage-2","BJ GROUP","VMIMAXX GROUP","UNISTAGE GROUP","TOTAL_GT"]:
                        agg[c] = agg[c].round(0).astype(int)
                    gt_daily_rows = agg.to_dict("records")
                    # === ATOMIC WRITE for GT CSV (prevents OneDrive truncation) ===
                    csv_out = OUTPUTS/_suff("phase5_gt_daily_production.csv")
                    csv_tmp = OUTPUTS/(".tmp_" + csv_out.name)
                    try:
                        agg.to_csv(csv_tmp, index=False)
                        if csv_out.exists():
                            try: csv_out.unlink()
                            except Exception: pass
                        os.replace(csv_tmp, csv_out)
                    finally:
                        if csv_tmp.exists():
                            try: csv_tmp.unlink()
                            except Exception: pass
                    print(f"\n  === PER-DAY GREEN TYRE PRODUCTION (plant-day 7 AM -> 7 AM, prorated) ===")
                    print(f"    {'plant_day':12s} {'TBM_S2':>8s} {'BJ':>8s} {'VMI':>8s} {'UNI':>8s} {'TOTAL':>8s}")
                    for r in gt_daily_rows:
                        print(f"    {str(r['plant_day']):12s} {r['TBM Stage-2']:>8d} {r['BJ GROUP']:>8d} "
                              f"{r['VMIMAXX GROUP']:>8d} {r['UNISTAGE GROUP']:>8d} {r['TOTAL_GT']:>8d}")
                    tot = agg['TOTAL_GT'].sum()
                    print(f"    {'TOTAL':12s} {'':>8s} {'':>8s} {'':>8s} {'':>8s} {int(tot):>8d}")
                    print(f"    {csv_out.name} (written)")
    except Exception as _e:
        import traceback
        print(f"    GT daily audit FAILED: {_e}")
        traceback.print_exc()

    # ===================================================================
    # PLANT-FLOOR SHIFT-WISE SCHEDULE (publishable)
    # COVERS THE FULL PIPELINE: from FINAL MIXING -> ... -> GREEN TYRE.
    # Every lot from sched_df is included (mixers, calenders, FRC, extruders,
    # cutters, building - all of them).
    #
    # Columns: date | machine | shift | item | item_type | process |
    #          start_time | end_time | produce_qty | UOM | lot_id |
    #          Consumption_lot_id (";"-joined component lots from BOM recipe, wave-matched) |
    #          FG_SKU_CODE | FG_DESCRIPTION (Green Tyres / Carcass only, from BOM12JUNE; FormulaCode as-is)
    # Shifts: A 07:00-15:00, B 15:00-23:00, C 23:00-07:00(next day)
    # Lots spanning shift boundary are SPLIT into multiple rows
    # with prorated qty.
    # ===================================================================
    shift_rows = []
    try:
        if sched_df is not None and not sched_df.empty:
            sd_shift = sched_df.copy()
            sd_shift["scheduled_start"]  = pd.to_datetime(sd_shift["scheduled_start"])
            sd_shift["scheduled_finish"] = pd.to_datetime(sd_shift["scheduled_finish"])
            try:
                lot_qty_map   = dict(zip(lots["lot_id"], pd.to_numeric(lots["lot_qty"], errors="coerce").fillna(0)))
                lot_uom_map   = dict(zip(lots["lot_id"], lots["lot_uom"].astype(str).fillna("")))
                lot_itype_map = dict(zip(lots["lot_id"], lots["item_type"].astype(str).fillna("")))
            except Exception:
                lot_qty_map   = {}
                lot_uom_map   = {}
                lot_itype_map = {}
            # === FG_SKU_CODE / FG_DESCRIPTION (Green Tyres + Carcass only) ===
            # From BOM12JUNE.xlsx: filter MaterialCode_O == produced item code,
            # take the LATEST Revision, then PLCBOMName -> FG_SKU_CODE and
            # FormulaCode -> FG_DESCRIPTION (pasted as-is, no stripping).
            fg_skucode_map, fg_desc_map = {}, {}
            FG_ITYPES = {"Green Tyres", "Carcass"}
            try:
                _bom_fg = pd.read_excel(
                    INPUTS / "BOM12JUNE.xlsx", sheet_name="sheet1",
                    usecols=["MaterialCode_O", "Revision", "PLCBOMName", "FormulaCode"])
                _bom_fg["MaterialCode_O"] = _bom_fg["MaterialCode_O"].astype(str).str.strip()
                _bom_fg["Revision"] = pd.to_numeric(_bom_fg["Revision"], errors="coerce").fillna(-1)
                # latest revision per material code
                _bom_fg = (_bom_fg.sort_values("Revision")
                                  .drop_duplicates("MaterialCode_O", keep="last"))
                fg_skucode_map = dict(zip(_bom_fg["MaterialCode_O"],
                                          _bom_fg["PLCBOMName"].astype(str).fillna("")))
                fg_desc_map = dict(zip(_bom_fg["MaterialCode_O"],
                                       _bom_fg["FormulaCode"].astype(str).fillna("")))
                print(f"  FG MAP (BOM12JUNE): {len(fg_skucode_map):,} material codes loaded")
            except Exception as _fge:
                print(f"  FG MAP load skipped ({_fge})")
            # === Consumption_lot_id: from the PHASE-3 DAG (actual scheduled links) ===
            # phase3_dag_construction builds the real producer_lot -> consumer_lot
            # edges the scheduler used (BOM + block alignment + aging). So a lot's
            # consumed components ARE its DAG predecessors. We read the raw
            # phase3_dag_edges (the full DAG, before the WIP-edge filter) so the
            # mapping matches Phase 3 exactly. A producer that wasn't separately
            # scheduled (covered by WIP) is tagged "WIP:<item>" so the row is never
            # blank; a lot with no DAG predecessors (raw-material leaf) shows
            # "RAW_MATERIAL".
            _dag_preds = defaultdict(list)    # consumer_lot -> [producer_lot]
            try:
                _de = _load_csv(["phase3_dag_edges.csv.gz", "phase3_dag_edges.csv"])
                for _p, _c in zip(_de["producer_lot"], _de["consumer_lot"]):
                    _dag_preds[_c].append(_p)
                _placed_set = set(schedule.keys())
                _kit_cache = {}
                def _consumption_kit(lid):
                    cached = _kit_cache.get(lid)
                    if cached is not None:
                        return cached
                    out, seen = [], set()
                    for p in _dag_preds.get(lid, ()):
                        if p == lid or p in seen:
                            continue
                        seen.add(p)
                        if p in _placed_set:
                            out.append(p)                       # real scheduled producer lot
                        else:
                            out.append(f"WIP:{lot_item.get(p, p)}")  # producer from inventory (not scheduled)
                    if not out:
                        out = ["RAW_MATERIAL"]
                    _kit_cache[lid] = out
                    return out
                print(f"  CONSUMPTION (Phase-3 DAG): {len(_dag_preds):,} consumer lots "
                      f"with predecessors, {len(_de):,} edges")
            except Exception as _ce:
                def _consumption_kit(lid):
                    return []
                print(f"  CONSUMPTION DAG build skipped ({_ce})")
            sd_shift["qty"]       = sd_shift["lot_id"].map(lot_qty_map).fillna(0)
            sd_shift["uom"]       = sd_shift["lot_id"].map(lot_uom_map).fillna("")
            sd_shift["item_type"] = sd_shift["lot_id"].map(lot_itype_map).fillna("")

            # Helper: given a timestamp T, return (plant_date, shift_letter, shift_start, shift_end)
            # A: 07:00 -> 15:00 (plant_date = T.date())
            # B: 15:00 -> 23:00 (plant_date = T.date())
            # C: 23:00 -> 07:00 (plant_date = T.date() if T.hour >= 23 else T.date() - 1 day)
            def _shift_of(ts):
                h = ts.hour + ts.minute/60.0 + ts.second/3600.0
                d = ts.normalize()
                if 7 <= h < 15:
                    return d.date(), "A", d + pd.Timedelta(hours=7),  d + pd.Timedelta(hours=15)
                if 15 <= h < 23:
                    return d.date(), "B", d + pd.Timedelta(hours=15), d + pd.Timedelta(hours=23)
                # C-shift: h >= 23 or h < 7
                if h >= 23:
                    return d.date(), "C", d + pd.Timedelta(hours=23), d + pd.Timedelta(days=1, hours=7)
                # h < 7 -> previous day's C-shift
                prev = d - pd.Timedelta(days=1)
                return prev.date(), "C", prev + pd.Timedelta(hours=23), prev + pd.Timedelta(days=1, hours=7)

            ZERO_NS = 0
            # UOMs that must be INTEGER (whole units)
            NOS_UOMS = {"NOS", "NO", "NUMBERS", "EA", "EACH", "PCS", "PIECES"}
            for _, lot in sd_shift.iterrows():
                lid    = lot["lot_id"]
                item   = lot["item"]
                itype  = str(lot.get("item_type", "")) or ""
                mach   = lot["machine"]
                process = str(lot.get("operation", "")) or ""
                dept   = str(lot.get("department", "")) or ""
                # === Consumption_lot_id: which component lots feed THIS lot ===
                # predecessors[lid] are the producer lots (belt, cap strip, ply,
                # carcass, compounds, ...) that this lot consumes — i.e. its full
                # kit. Lets the user trace which lots were consumed to make this one.
                cons_lots = _consumption_kit(lid)   # Phase-3 DAG predecessors
                consumption_lot_id = ";".join(str(p) for p in cons_lots)
                # FG SKU code + description (only for Green Tyres / Carcass)
                if itype in FG_ITYPES:
                    _ic = str(item).strip()
                    fg_sku_code   = fg_skucode_map.get(_ic, "")
                    fg_description = fg_desc_map.get(_ic, "")
                else:
                    fg_sku_code, fg_description = "", ""
                qty    = float(lot["qty"])
                uom    = str(lot["uom"]).strip()
                s      = pd.Timestamp(lot["scheduled_start"])
                f      = pd.Timestamp(lot["scheduled_finish"])
                if pd.isna(s) or pd.isna(f) or f <= s:
                    continue
                dur_ns = (f - s).value
                # === STEP 1: Collect all shift segments for this lot ===
                lot_segs = []
                cur = s
                while cur < f:
                    pd_date, shift_letter, sh_start, sh_end = _shift_of(cur)
                    seg_start = max(cur, sh_start)
                    seg_end   = min(f, sh_end)
                    if seg_end <= seg_start:
                        cur = sh_end
                        continue
                    seg_dur_ns = (seg_end - seg_start).value
                    prorated_qty = qty * (seg_dur_ns / dur_ns) if dur_ns > 0 else 0.0
                    lot_segs.append({
                        "pd_date": pd_date, "shift": shift_letter,
                        "seg_start": seg_start, "seg_end": seg_end,
                        "prorated": prorated_qty,
                    })
                    cur = sh_end
                if not lot_segs:
                    continue
                # === STEP 2: For NOS UOMs, apply LARGEST-REMAINDER ROUNDING ===
                # so segment qtys are integers AND their sum exactly equals lot qty.
                is_nos = uom.upper() in NOS_UOMS
                if is_nos:
                    total_int = int(round(qty))
                    floors  = [int(seg["prorated"]) for seg in lot_segs]
                    sum_fl  = sum(floors)
                    residual = total_int - sum_fl
                    # Sort by remainder DESC, distribute residual to highest remainders
                    idx_remainder = sorted(
                        range(len(lot_segs)),
                        key=lambda i: -(lot_segs[i]["prorated"] - floors[i])
                    )
                    final_qtys = list(floors)
                    for k in range(max(0, min(residual, len(lot_segs)))):
                        final_qtys[idx_remainder[k]] += 1
                    for i, seg in enumerate(lot_segs):
                        seg["final_qty"] = final_qtys[i]
                else:
                    for seg in lot_segs:
                        seg["final_qty"] = round(seg["prorated"], 3)
                # === STEP 3: Append to shift_rows ===
                for seg in lot_segs:
                    shift_rows.append({
                        "date":         str(seg["pd_date"]),
                        "machine":      mach,
                        "shift":        seg["shift"],
                        "item":         item,
                        "item_type":    itype,
                        "process":      process,
                        "department":   dept,
                        "start_time":   seg["seg_start"].strftime("%Y-%m-%d %H:%M:%S"),
                        "end_time":     seg["seg_end"].strftime("%Y-%m-%d %H:%M:%S"),
                        "produce_qty":  seg["final_qty"],
                        "UOM":          uom,
                        "lot_id":       lid,
                        "Consumption_lot_id": consumption_lot_id,
                        "FG_SKU_CODE":  fg_sku_code,
                        "FG_DESCRIPTION": fg_description,
                    })

            if shift_rows:
                shift_df = pd.DataFrame(shift_rows)
                # Drop columns not wanted in the published floor schedule.
                shift_df = shift_df.drop(
                    columns=["Consumption_lot_id", "FG_SKU_CODE", "FG_DESCRIPTION"],
                    errors="ignore")
                shift_df = shift_df.sort_values(
                    ["date","machine","shift","start_time"]).reset_index(drop=True)
                # === ATOMIC WRITE ===
                csv_path = OUTPUTS / _suff("phase5_floor_schedule.csv")
                csv_tmp  = OUTPUTS / (".tmp_" + csv_path.name)
                try:
                    shift_df.to_csv(csv_tmp, index=False)
                    if csv_path.exists():
                        try: csv_path.unlink()
                        except Exception: pass
                    os.replace(csv_tmp, csv_path)
                    print(f"\n  === FLOOR SCHEDULE (shift-wise, publishable) ===")
                    print(f"    Total shift-rows: {len(shift_df):,}")
                    print(f"    Distinct lots:    {shift_df['lot_id'].nunique():,}")
                    print(f"    Distinct items:   {shift_df['item'].nunique():,}")
                    print(f"    Distinct item_types: {shift_df['item_type'].nunique():,}")
                    print(f"    Distinct machines: {shift_df['machine'].nunique():,}")
                    print(f"    Shifts: A 07-15, B 15-23, C 23-07")
                    # === DB output: jkt_floor_endfwd_schedule ===
                    try:
                        import sys as _sys
                        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
                        from db_loader import write_to_db, _resolve_source
                        if _resolve_source(cfg) == "db":
                            res = write_to_db(shift_df, output_key="floor_schedule", cfg=cfg)
                            print(f"    DB write: {res['table']} ({res['rows_written']:,} rows, "
                                  f"{'CREATED' if res['created_or_replaced'] else 'TRUNCATED+INSERT'})")
                    except Exception as _de:
                        print(f"    DB write skipped (floor_schedule): {_de}")
                    # Show item_type coverage (proves end-to-end)
                    itype_counts = shift_df.groupby('item_type').size().sort_values(ascending=False)
                    print(f"    Item-types covered (end-to-end from FINAL MIXING -> GREEN TYRE):")
                    for it, n in itype_counts.head(20).items():
                        print(f"      {str(it)[:35]:35s} : {n:>6,d} shift-rows")
                    print(f"    {csv_path.name} (written)")
                except Exception as _ce:
                    print(f"    floor schedule CSV write FAILED: {_ce}")
                finally:
                    if csv_tmp.exists():
                        try: csv_tmp.unlink()
                        except Exception: pass
    except Exception as _e:
        import traceback
        print(f"    Floor schedule generation FAILED: {_e}")
        traceback.print_exc()

    # ===================================================================
    # SUMMARY XLSX
    # ===================================================================
    try:
        sched_for_sum = sched_df.copy()
        if not sched_for_sum.empty:
            sched_for_sum["scheduled_start"]  = pd.to_datetime(sched_for_sum["scheduled_start"])
            sched_for_sum["scheduled_finish"] = pd.to_datetime(sched_for_sum["scheduled_finish"])
            plan_end = pd.Timestamp(str(horizon)) if horizon is not None else None
            past_h = (sched_for_sum["scheduled_finish"] > plan_end).sum() if plan_end is not None else 0
            overview_rows = [
                ("Total lots placed",        f"{len(sched_for_sum):,}"),
                ("Belt campaigns placed",    f"{placed_belt_campaigns:,}"),
                ("Orphans skipped",          f"{n_orphans_skipped:,}"),
                ("Infeasibilities",          f"{len(infeas):,}"),
                ("Aging violations",         f"{len(aging_v):,}"),
                ("Schedule earliest start",  str(sched_for_sum["scheduled_start"].min())),
                ("Schedule latest finish",   str(sched_for_sum["scheduled_finish"].max())),
                ("Plan horizon (endTime max)", str(plan_end) if plan_end is not None else "(none)"),
                ("Lots PAST_HORIZON",        f"{int(past_h):,}"),
                ("Placement time (s)",       f"{_total:.1f}"),
            ]
            overview = pd.DataFrame(overview_rows, columns=["metric","value"])
            by_dept = (sched_for_sum.groupby("department")
                         .agg(n_lots=("lot_id","count"),
                              first_start=("scheduled_start","min"),
                              last_finish=("scheduled_finish","max"))
                         .reset_index().sort_values("n_lots", ascending=False))
            by_machine = util_df.copy() if not util_df.empty else pd.DataFrame()
            aging_break = pd.DataFrame()
            if aging_v:
                av = pd.DataFrame(aging_v)
                if "excess_h" in av.columns:
                    aging_break = (av.groupby(["item","type"])
                                     .agg(n=("producer_lot","count"),
                                          avg_excess_h=("excess_h","mean"))
                                     .reset_index().sort_values("n", ascending=False))
            past_horizon_break = pd.DataFrame()
            if plan_end is not None:
                ph = sched_for_sum[sched_for_sum["scheduled_finish"] > plan_end]
                if not ph.empty:
                    past_horizon_break = (ph.groupby(["department","machine"])
                                            .size().reset_index(name="n_past_horizon")
                                            .sort_values("n_past_horizon", ascending=False))
            wip_sheet = pd.DataFrame(wip_audit_rows) if wip_audit_rows else pd.DataFrame()
            gt_daily_sheet = pd.DataFrame(gt_daily_rows) if gt_daily_rows else pd.DataFrame()
            floor_sheet = pd.DataFrame(shift_rows) if shift_rows else pd.DataFrame()
            if len(floor_sheet) > 900000:
                floor_sheet = floor_sheet.head(900000)
            sheets = {
                "00_overview": overview,
                "01_by_department": by_dept,
                "03_aging_violations_summary": aging_break,
                "04_past_horizon_breakdown": past_horizon_break,
                "05_carcass_gt_wip_daily": wip_sheet,
                "06_GT_daily_production": gt_daily_sheet,
                "07_floor_schedule_shiftwise": floor_sheet,
            }
            # === ATOMIC WRITE for SUMMARY XLSX ===
            out_xlsx = OUTPUTS/_suff("phase5_summary.xlsx")
            tmp_xlsx = OUTPUTS/(".tmp_" + out_xlsx.name)
            try:
                with pd.ExcelWriter(tmp_xlsx, engine="openpyxl") as w:
                    for name, sh in sheets.items():
                        if isinstance(sh, pd.DataFrame) and not sh.empty:
                            sh.to_excel(w, sheet_name=name[:31], index=False)
                        else:
                            pd.DataFrame({"info": ["(no data)"]}).to_excel(w, sheet_name=name[:31], index=False)
                if out_xlsx.exists():
                    try: out_xlsx.unlink()
                    except Exception: pass
                os.replace(tmp_xlsx, out_xlsx)
                print(f"    {_suff('phase5_summary.xlsx')} (refreshed)")
            finally:
                try:
                    if tmp_xlsx.exists():
                        tmp_xlsx.unlink()
                except Exception:
                    pass
    except Exception as _se:
        print(f"  [WARN] summary xlsx write skipped: {_se}")

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
