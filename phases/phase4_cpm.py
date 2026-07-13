#!/usr/bin/env python3
"""
phase4_cpm.py — Critical Path Method backward + forward pass on the lot DAG.

Locked-in design (signed off):
  • T0 = (first curing block) − pre_curing_buffer_h
  • Infeasibility = revise curing (defer block to later in month horizon, up to 5 iterations)
  • Critical = slack ≤ 0
  • Aging = both min_aging (gap floor) AND max_aging (gap ceiling) enforced

PASSES
  1. Anchor terminal lots → LFT = block.need_by − min_aging[item] (or pre_curing_buffer_h)
  2. Backward LST propagation (reverse-topo)
  3. Forward EST propagation (topo)
  4. Slack & critical-path identification
  5. Aging audit (TOO_FRESH / EXPIRED)
  6. Curing-revision loop if any terminal LFT < T0 (up to 5 iters)

OUTPUTS to outputs2/
  phase4_lot_times_updated.csv         — per-lot EST/EFT/LST/LFT/slack/is_critical
  phase4_aging_violations_updated.csv  — producer-consumer pairs violating min/max aging
  phase4_curing_revisions_updated.csv  — curing blocks deferred during revision loop
"""
from __future__ import annotations
import sys, pathlib, yaml, re
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
    if u.startswith("DAY"):  return v*24.0
    if u.startswith("MIN"):  return v/60.0
    return v


def load_lots():
    for name in ("phase2_lots_v2.csv","phase2_lots.csv","phase2_lots.csv.gz"):
        p = OUTPUTS/_suff(name)
        if p.exists() and p.is_file():
            df = pd.read_csv(p, low_memory=False)
            print(f"  loaded {p.name}  rows={len(df):,}", flush=True)
            return df
    raise FileNotFoundError("no phase2_lots*.csv in outputs2/")


def load_edges():
    # Prefer .gz first (more reliable on OneDrive than the .csv ghost-file state)
    for name in ("phase3_dag_edges.csv.gz","phase3_dag_edges.csv"):
        p = OUTPUTS/_suff(name)
        if p.exists() and p.is_file():
            df = pd.read_csv(p, low_memory=False)
            print(f"  loaded {p.name}  rows={len(df):,}", flush=True)
            return df
    raise FileNotFoundError("no phase3_dag_edges.csv in outputs2/")


def load_lot_blocks():
    for name in ("phase2_lot_blocks.csv.gz","phase2_lot_blocks.csv"):
        p = OUTPUTS/_suff(name)
        if p.exists() and p.is_file():
            df = pd.read_csv(p, low_memory=False)
            print(f"  loaded {p.name}  rows={len(df):,}", flush=True)
            return df
    raise FileNotFoundError("no phase2_lot_blocks.csv in outputs2/")


def build_aging_map(am):
    out = {}
    for _, r in am.iterrows():
        code = str(r["ItemCode"]).strip()
        if code:
            out[code] = (aging_hours(r["MinAging"], r["MinAgingUnit"]),
                         aging_hours(r["MaxAging"], r["MaxAgingUnit"]))
    return out


def load_planning_max_aging_map(itype_or_path, policy_or_path):
    """V6_wave: load PLANNING max_aging policy per item-type.

    Accepts either DataFrames (preferred — from db_loader) or paths.

    Returns dict[item_code -> planning_max_aging_h]. Items whose ItemType is not
    listed in the policy file are NOT in the dict (callers should fall back to
    chemistry max from aging_master).
    """
    # Resolve policy
    if isinstance(policy_or_path, pd.DataFrame):
        pol = policy_or_path
        if pol is None or len(pol) == 0:
            print("    NOTE: planning_max_aging policy empty - AEST will use chemistry max_aging only")
            return {}
    else:
        if not policy_or_path.exists():
            print(f"    NOTE: {policy_or_path.name} not found - AEST will use chemistry max_aging only")
            return {}
        pol = pd.read_csv(policy_or_path)
    pol = pol.copy()
    pol["ItemType"] = pol["ItemType"].astype(str).str.strip()
    pol["__h__"] = pol.apply(
        lambda r: aging_hours(r["PlanningMaxAging"], r.get("PlanningMaxUnit", "HRS")),
        axis=1)
    type_to_h = dict(zip(pol["ItemType"], pol["__h__"]))
    print(f"    loaded planning policy: {len(type_to_h)} item-types")

    # Resolve itemtype master
    if isinstance(itype_or_path, pd.DataFrame):
        it = itype_or_path
        if it is None or len(it) == 0:
            print("    WARNING: itemtype_master empty - cannot map item-type-policy")
            return {}
    else:
        if not itype_or_path.exists():
            print(f"    WARNING: {itype_or_path.name} missing - cannot map item-type-policy")
            return {}
        it = pd.read_csv(itype_or_path)
    if "ItemCode" not in it.columns or "ItemType" not in it.columns:
        print(f"    WARNING: itemtype_master schema unexpected - skipping AEST")
        return {}
    code_to_h = {}
    for _, r in it.iterrows():
        code = str(r["ItemCode"]).strip()
        itype = str(r.get("ItemType","")).strip()
        if code and itype in type_to_h:
            code_to_h[code] = type_to_h[itype]
    print(f"    mapped planning max_aging to {len(code_to_h):,} items")
    return code_to_h


def get_effective_max_aging(item, aging_map, planning_max_map, default_h):
    """Effective max_aging = min(chemistry, planning) for AEST and Phase 5 floor.
    Falls back to chemistry if no planning policy for this item.
    """
    chem_max = aging_map.get(item, (default_h, default_h*5))[1]
    plan_max = planning_max_map.get(item)
    if plan_max is None:
        return chem_max
    return min(chem_max, plan_max)


# ═══ CTP DEVIATION — TRANSFER TIME FROM THE ROUTING ════════════════════════════════
# v6 never reads `transfer_time_min`. The user instructed that it always be used.
# It is a MANDATORY LAG between a producer finishing and its consumer starting — exactly
# the same shape as min_aging — so it is ADDED TO MIN_AGING at this single funnel.
# Every call site of get_min_aging() therefore inherits it: the backward LFT pass, the
# forward EST pass, and the terminal (green-tyre -> curing) anchor.
#
# It is deliberately NOT added to get_max_aging(): max_aging is a shelf-life CEILING
# (how long material may sit), not a lag. Adding transfer there would EXTEND shelf life.
#
# Populated in main() from phase2's `transfer_time_h` lot column. Empty dict = v6 behaviour.
TRANSFER_H: dict[str, float] = {}


def get_min_aging(item, aging_map, default_h):
    return (aging_map.get(item, (default_h, default_h*5))[0]
            + TRANSFER_H.get(item, 0.0))


def get_max_aging(item, aging_map, default_h):
    return aging_map.get(item, (default_h, default_h*5))[1]


def topo_order(lot_ids, successors):
    """Kahn topological sort. Returns list in topo order; cycles handled by BFS fallback."""
    in_deg = {l: 0 for l in lot_ids}
    for u, succs in successors.items():
        for v in succs:
            if v in in_deg: in_deg[v] += 1
    q = deque([l for l, d in in_deg.items() if d == 0])
    order = []
    while q:
        u = q.popleft(); order.append(u)
        for v in successors.get(u, ()):
            in_deg[v] -= 1
            if in_deg[v] == 0: q.append(v)
    if len(order) < len(lot_ids):
        seen = set(order)
        for l in lot_ids:
            if l not in seen: order.append(l)
    return order


def run_cpm(lot_ids, duration_h, successors, predecessors,
            lot_item, terminal_anchors, aging_map, T0, pre_curing_buffer_h):
    """
    Returns: est, eft, lst, lft (all dicts keyed by lot_id, in pandas.Timestamp).
    """
    DEFAULT_MIN_AGE = pre_curing_buffer_h
    DEFAULT_MAX_AGE = 365 * 24.0  # 1 year - effectively no upper bound for unknown items
    BIG = pd.Timestamp("2099-12-31")

    order = topo_order(lot_ids, successors)
    rev_order = list(reversed(order))

    # -- Backward pass: LFT, LST --
    lft = {l: BIG for l in lot_ids}
    for lot_id, deadline in terminal_anchors.items():
        if deadline is not None and lot_id in lft:
            lft[lot_id] = min(lft[lot_id], deadline)

    # === F2 PER-ITEM MAX_AGING LFT ===
    # Backward LFT propagation: producer u's LFT = MIN over consumers v of
    #   (v_start - min_aging_u). Standard CPM.
    # PLUS: track the EARLIEST and LATEST consumer of u so we can detect
    # multi-consumer-span fan-out (consumer spread > max_aging - safety).
    consumer_earliest_start = {}  # u -> earliest v_start across successors
    consumer_latest_start   = {}  # u -> latest v_start across successors
    for u in rev_order:
        # If u has any successors, propagate from them
        for v in successors.get(u, ()):
            item_u = lot_item.get(u, "")
            min_age_u = get_min_aging(item_u, aging_map, DEFAULT_MIN_AGE)
            # FIX 3: ALSO enforce max_aging — u must NOT finish more than max_aging
            # before v starts, else u expires. This caps u's LFT range so CPM
            # doesn't give producers infinite earliness.
            max_age_u = get_max_aging(item_u, aging_map, DEFAULT_MAX_AGE)
            # u must finish >= min_aging_u BEFORE v starts (existing constraint)
            v_start = lft[v] - pd.Timedelta(hours=duration_h.get(v, 0))
            u_deadline = v_start - pd.Timedelta(hours=min_age_u)
            if u_deadline < lft[u]:
                lft[u] = u_deadline
            # F2: track consumer span for u
            prev_e = consumer_earliest_start.get(u)
            if prev_e is None or v_start < prev_e:
                consumer_earliest_start[u] = v_start
            prev_l = consumer_latest_start.get(u)
            if prev_l is None or v_start > prev_l:
                consumer_latest_start[u] = v_start

    # FIX 3 continued: also propagate the max_aging EST floor backward.
    # For each producer u, EST_u_earliest = v_start - max_aging_u (else u expires).
    # We capture this as an "EST floor" that the forward pass will respect.
    est_floor_from_aging = {}
    for u in rev_order:
        for v in successors.get(u, ()):
            item_u = lot_item.get(u, "")
            max_age_u = get_max_aging(item_u, aging_map, DEFAULT_MAX_AGE)
            v_start_lft = lft[v] - pd.Timedelta(hours=duration_h.get(v, 0))
            # u_earliest_start = v_start - max_aging - dur_u
            u_earliest = v_start_lft - pd.Timedelta(hours=max_age_u) - pd.Timedelta(hours=duration_h.get(u, 0))
            prev = est_floor_from_aging.get(u)
            if prev is None or u_earliest > prev:
                est_floor_from_aging[u] = u_earliest

    lst = {l: lft[l] - pd.Timedelta(hours=duration_h.get(l, 0)) for l in lot_ids}

    # -- Forward pass: EST, EFT --
    # FIX 3: Initialize EST from max_aging floor where it's later than T0.
    # Producer must not start more than max_aging before consumer needs it.
    est = {l: T0 for l in lot_ids}
    for l, floor in est_floor_from_aging.items():
        if floor > est.get(l, T0):
            est[l] = floor
    for u in order:
        for v in successors.get(u, ()):
            item_u = lot_item.get(u, "")
            min_age_u = get_min_aging(item_u, aging_map, DEFAULT_MIN_AGE)
            u_finish = est[u] + pd.Timedelta(hours=duration_h.get(u, 0))
            v_earliest = u_finish + pd.Timedelta(hours=min_age_u)
            if v_earliest > est[v]:
                est[v] = v_earliest

    eft = {l: est[l] + pd.Timedelta(hours=duration_h.get(l, 0)) for l in lot_ids}
    return est, eft, lst, lft


def find_critical_chains(critical_set, predecessors, successors, max_chains=10):
    """Trace chains of critical lots from roots (no critical pred) to terminals (no critical succ)."""
    chains = []
    critical_roots = [l for l in critical_set
                      if not any(p in critical_set for p in predecessors.get(l, ()))]
    for root in critical_roots[:max_chains*5]:
        chain = [root]
        cur = root
        while True:
            nxt = next((s for s in successors.get(cur, ()) if s in critical_set), None)
            if nxt is None: break
            chain.append(nxt); cur = nxt
            if len(chain) > 50: break  # safety
        if len(chain) > 1: chains.append(chain)
        if len(chains) >= max_chains: break
    return chains


def main():
    print("\n" + "=" * 65)
    print("  PHASE 4 - CPM Backward + Forward Pass")
    print("=" * 65)
    cfg = load_cfg()
    pre_curing_buffer_h = float(cfg.get("pre_curing_buffer_h", 72))
    print(f"\n  pre_curing_buffer_h: {pre_curing_buffer_h}")
    print("\n  Loading inputs...")
    lots = load_lots()
    edges = load_edges()
    lot_blocks = load_lot_blocks()

    # ═══ CTP DEVIATION — load the routing transfer time carried down by phase2 ═══
    if "transfer_time_h" in lots.columns:
        _t = (lots[["item_code", "transfer_time_h"]]
              .assign(transfer_time_h=lambda d: pd.to_numeric(d["transfer_time_h"],
                                                              errors="coerce").fillna(0.0))
              .groupby("item_code")["transfer_time_h"].max())
        TRANSFER_H.update({str(k).strip(): float(v) for k, v in _t.items() if v > 0})
        print(f"  TRANSFER TIME (routing): {len(TRANSFER_H):,} items carry a transfer lag "
              f"(max {max(TRANSFER_H.values()) if TRANSFER_H else 0:.2f}h). "
              f"Added to min_aging on every producer->consumer edge.")
    else:
        print("  [WARN] phase2 lots have no `transfer_time_h` column — transfer time NOT "
              "applied. Re-run phase2.")

    am = _db_or_csv("aging_master", cfg)
    aging_map = build_aging_map(am)
    # plan_params dropped — horizon comes from plan.endTime.max() below.

    # V6_wave: load planning max_aging policy (per item-type) -> per-item dict
    # DB-first via _db_or_csv; falls back to CSV paths inside the helper.
    try:
        _policy_df = _db_or_csv("planning_max_aging", cfg)
    except Exception:
        _policy_df = INPUTS / "planning_max_aging.csv"
    try:
        _itype_df = _db_or_csv("itemtype_master", cfg)
    except Exception:
        _itype_df = INPUTS / cfg["files"]["itemtype_master"]
    planning_max_map = load_planning_max_aging_map(_itype_df, _policy_df)

    # -- Determine T0 = first curing block - pre_curing_buffer_h --
    lot_blocks["need_by"] = pd.to_datetime(lot_blocks["need_by"], errors="coerce")
    first_curing = lot_blocks["need_by"].min()
    T0 = first_curing - pd.Timedelta(hours=pre_curing_buffer_h)
    print(f"  First curing block: {first_curing}")
    print(f"  T0 (plan start):    {T0}")

    # Month horizon — DERIVED FROM PLAN (max endTime across all blocks),
    # NOT from plan_params.planEndDate. The plan's last block-finish IS the
    # true horizon; plan_params can be stale or slightly earlier (off by hours).
    horizon = None
    plan = _db_or_csv("plan", cfg)
    if "endTime" in plan.columns:
        et = pd.to_datetime(plan["endTime"], errors="coerce")
        if et.notna().any():
            horizon = et.max()
    print(f"  Month horizon (plan max endTime):  {horizon}")

    # -- Build DAG structures --
    lots["duration_h"] = pd.to_numeric(lots["duration_hours"], errors="coerce").fillna(0)
    lot_ids = lots["lot_id"].tolist()
    lot_set = set(lot_ids)
    duration_h = dict(zip(lots["lot_id"], lots["duration_h"]))
    lot_item   = dict(zip(lots["lot_id"], lots["item_code"]))

    print(f"  Lots: {len(lot_ids):,}   Edges: {len(edges):,}")
    successors   = defaultdict(list)
    predecessors = defaultdict(list)
    for u, v in zip(edges["producer_lot"], edges["consumer_lot"]):
        if u in lot_set and v in lot_set:
            successors[u].append(v)
            predecessors[v].append(u)

    # -- Anchor terminal lots --
    # A terminal lot has no successors. Its deadline = min(need_by of its served blocks) - pre_curing_buffer_h
    terminal_ids = [l for l in lot_ids if not successors.get(l)]
    print(f"  Terminal lots (anchors): {len(terminal_ids):,}")

    terminal_anchors = {}
    lot_block_grp = lot_blocks.groupby("lot_id")
    for tid in terminal_ids:
        try:
            g = lot_block_grp.get_group(tid)
            earliest_curing = g["need_by"].min()
            # use min_aging of the terminal lot's item if available, else pre_curing_buffer_h
            item = lot_item.get(tid, "")
            min_age = get_min_aging(item, aging_map, pre_curing_buffer_h)
            terminal_anchors[tid] = earliest_curing - pd.Timedelta(hours=min_age)
        except KeyError:
            # No block info - pin to end of month
            terminal_anchors[tid] = horizon if horizon is not None else (T0 + pd.Timedelta(days=30))

    # -- CPM loop with curing-revision (up to 5 iterations) --
    cur_revisions = []
    revised_anchors = dict(terminal_anchors)
    MAX_ITERS = 5
    for iteration in range(1, MAX_ITERS+1):
        print(f"\n  CPM iteration {iteration}...")
        est, eft, lst, lft = run_cpm(lot_ids, duration_h, successors, predecessors,
                                     lot_item, revised_anchors, aging_map,
                                     T0, pre_curing_buffer_h)
        # Find infeasible terminals: LFT < T0 means we'd need to start before T0
        infeas = [(t, lft[t]) for t in terminal_ids if lft[t] < T0]
        # Also check: any lot's LFT < EFT (lst < est) means slack < 0
        n_critical = sum(1 for l in lot_ids if (lst[l] - est[l]).total_seconds() <= 0)
        n_neg = sum(1 for l in lot_ids if (lst[l] - est[l]).total_seconds() < 0)
        print(f"    Critical lots (slack<=0): {n_critical:,}   Negative-slack lots: {n_neg:,}")
        print(f"    Infeasible terminals (LFT<T0): {len(infeas):,}")
        if not infeas:
            break
        if horizon is None:
            print(f"    No month horizon - cannot defer. Stopping.")
            break
        # Defer infeasible terminals - push their anchor forward by deficit
        deferred = 0
        for tid, old_lft in infeas:
            deficit = (T0 - old_lft).total_seconds() / 3600.0  # hours
            new_anchor = revised_anchors[tid] + pd.Timedelta(hours=deficit + 1)  # +1h buffer
            if new_anchor > horizon:
                # cap at horizon
                new_anchor = horizon
            if new_anchor > revised_anchors[tid]:
                cur_revisions.append({
                    "iteration": iteration,
                    "lot_id":   tid,
                    "old_anchor": revised_anchors[tid],
                    "new_anchor": new_anchor,
                    "deferred_h": round(deficit, 1),
                })
                revised_anchors[tid] = new_anchor
                deferred += 1
        print(f"    Deferred {deferred:,} terminal anchors")
        if deferred == 0: break

    # === FEEDER LFT DE-BUNCH (production de-pulse) ===
    # The DAG over-links each feeder lot to ALL GTs in its wave, so the backward
    # CPM pass collapses every feeder lot's LFT to the wave-START (3-day steps),
    # discarding phase-2's smooth daily deadlines -> feeder production bursts ->
    # kit-readiness lumps -> building dips. Re-anchor each binding feeder lot's LFT
    # to its OWN de-pulsed last_need (demand-accurate, smooth) so feeder production
    # tracks the level daily demand. Only moves LFT *later* (de-bunch) and stays
    # feasible (>= EFT). Mixing/FRC are intentionally excluded.
    # NOTE: de-bunch spreads feeder production (tread daily) but removes the buffer
    # the system relies on -> starves early days + more expiry + bigger tail
    # (92.8% / tail 50.7k / 516 aging viol). The clean fix is a phase-3 DAG
    # quantity-allocation redesign, not this LFT override. Gated OFF.
    FEEDER_LFT_DEBUNCH = False
    FEEDER_TYPES = {"Tread", "SideWall", "Inner Liner", "Rubberized Ply",
                    "Cap Strip", "Pre Cap Strip"}
    if FEEDER_LFT_DEBUNCH and "item_type" in lots.columns and "last_need" in lots.columns:
        _it  = dict(zip(lots["lot_id"], lots["item_type"].astype(str)))
        _ln  = dict(zip(lots["lot_id"], pd.to_datetime(lots["last_need"], errors="coerce")))
        _mna = dict(zip(lots["lot_id"], pd.to_numeric(lots.get("min_aging_h", 0), errors="coerce").fillna(0)))
        _n_rb = 0
        for lid in lot_ids:
            if _it.get(lid) in FEEDER_TYPES:
                ln = _ln.get(lid)
                if ln is not None and not pd.isna(ln):
                    new_lft = ln - pd.Timedelta(hours=float(_mna.get(lid, 0)))
                    if new_lft >= eft[lid] and new_lft > lft[lid]:
                        lft[lid] = new_lft
                        lst[lid] = lft[lid] - pd.Timedelta(hours=duration_h.get(lid, 0))
                        _n_rb += 1
        print(f"  FEEDER LFT de-bunch: re-anchored {_n_rb:,} feeder lots to de-pulsed last_need")

    # -- Slack + critical set --
    slack_h = {l: (lst[l] - est[l]).total_seconds()/3600.0 for l in lot_ids}
    critical_set = {l for l, s in slack_h.items() if s <= 0}
    print(f"\n  Final: {len(critical_set):,} critical lots, "
          f"{sum(1 for s in slack_h.values() if s < 0):,} negative-slack")

    # -- Aging audit --
    print("  Auditing aging violations...")
    violations = []
    for u, v in zip(edges["producer_lot"], edges["consumer_lot"]):
        if u not in lot_set or v not in lot_set: continue
        item_u = lot_item.get(u, "")
        mn = get_min_aging(item_u, aging_map, pre_curing_buffer_h)
        mx = get_max_aging(item_u, aging_map, 365*24)
        gap_h = (est[v] - eft[u]).total_seconds()/3600.0
        if gap_h < mn - 0.01:
            violations.append({"producer_lot": u, "consumer_lot": v, "item": item_u,
                               "gap_h": round(gap_h,2), "min_aging_h": mn,
                               "type": "TOO_FRESH", "deficit_h": round(mn-gap_h,2)})
        elif gap_h > mx + 0.01:
            violations.append({"producer_lot": u, "consumer_lot": v, "item": item_u,
                               "gap_h": round(gap_h,2), "max_aging_h": mx,
                               "type": "EXPIRED", "excess_h": round(gap_h-mx,2)})

    print(f"  Aging violations: {len(violations):,}")

    # -- Critical-path tracing --
    chains = find_critical_chains(critical_set, predecessors, successors, max_chains=10)
    print(f"  Critical chains found: {len(chains)} (longest = {max((len(c) for c in chains), default=0)} lots)")

    # -- Build outputs --
    print("\n  Saving outputs...")
    depth_map = {l: 0 for l in lot_ids}
    # depth = longest path from root
    for u in topo_order(lot_ids, successors):
        for v in successors.get(u, ()):
            if depth_map[u] + 1 > depth_map[v]: depth_map[v] = depth_map[u] + 1

    # V6_wave: AEST = aging-aware Earliest Start (= LST - planning_window)
    # planning_window_h = effective_max_aging - min_aging.
    # Effective max_aging = min(chemistry, planning_policy).
    DEFAULT_MIN = pre_curing_buffer_h
    aest = {}
    plan_window_h = {}
    eff_max_age_h = {}
    for l in lot_ids:
        item = lot_item.get(l, "")
        mn = get_min_aging(item, aging_map, DEFAULT_MIN)
        emax = get_effective_max_aging(item, aging_map, planning_max_map, DEFAULT_MIN)
        win_h = max(0.0, emax - mn)
        plan_window_h[l] = win_h
        eff_max_age_h[l] = emax
        aest[l] = lst[l] - pd.Timedelta(hours=win_h)

    times = pd.DataFrame({
        "lot_id":       lot_ids,
        "item_code":    [lot_item.get(l,"") for l in lot_ids],
        "duration_h":   [round(duration_h.get(l,0),3) for l in lot_ids],
        "depth":        [depth_map[l] for l in lot_ids],
        "EST":          [est[l]  for l in lot_ids],
        "EFT":          [eft[l]  for l in lot_ids],
        "LST":          [lst[l]  for l in lot_ids],
        "LFT":          [lft[l]  for l in lot_ids],
        "AEST":         [aest[l] for l in lot_ids],
        "planning_window_h": [round(plan_window_h[l], 1) for l in lot_ids],
        "effective_max_age_h": [round(eff_max_age_h[l], 1) for l in lot_ids],
        "min_age_h":    [round(get_min_aging(lot_item.get(l,""), aging_map, DEFAULT_MIN), 1) for l in lot_ids],
        "slack_h":      [round(slack_h[l],2) for l in lot_ids],
        "is_critical":  [l in critical_set for l in lot_ids],
        "n_pred":       [len(predecessors.get(l,())) for l in lot_ids],
        "n_succ":       [len(successors.get(l,())) for l in lot_ids],
    })
    times.to_csv(OUTPUTS/_suff("phase4_lot_times.csv"), index=False)
    print(f"    {_suff('phase4_lot_times.csv')} ({len(times):,})")

    # V6_wave: per-item-type AEST window summary (sanity check)
    try:
        it_df = _db_or_csv("itemtype_master", cfg)
        code_to_type = dict(zip(it_df["ItemCode"].astype(str).str.strip(),
                                  it_df["ItemType"].astype(str).str.strip()))
        t = times.copy()
        t["item_type"] = t["item_code"].map(code_to_type).fillna("(unknown)")
        ws = t.groupby("item_type").agg(
            n_lots=("lot_id", "count"),
            avg_window_h=("planning_window_h", "mean"),
            median_window_h=("planning_window_h", "median"),
            max_aging_h=("effective_max_age_h", "max"),
        ).reset_index().sort_values("n_lots", ascending=False)
        ws.to_csv(OUTPUTS / _suff("phase4_aest_window_by_itype.csv"), index=False)
        print(f"    {_suff('phase4_aest_window_by_itype.csv')} ({len(ws)})")
    except Exception as _e:
        print(f"    (aest window summary skipped: {_e})")

    if violations:
        pd.DataFrame(violations).to_csv(OUTPUTS / _suff("phase4_aging_violations.csv"), index=False)
        print(f"    {_suff('phase4_aging_violations.csv')} ({len(violations)})")

    print(f"\n[OK] Phase 4 done")
    print(f"     Files written to {OUTPUTS}")
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
