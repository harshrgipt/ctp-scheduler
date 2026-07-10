"""
phase5 — forward placement (BTP-MODE: force-place + honest breach ledger).

Every lot is placed onto a concrete machine via a shared MachineTimeline
(bisect slot search, integer-ns, changeover only between differing keys). Bottleneck
mixers run pure-ASAP; terminal builds are pulled ALAP to the curing need_by inside
the 8h cure-by band; everything else is ALAP to its CPM LFT. When a constraint
cannot be honoured the lot is force-placed and the breach is logged, never erased.
"""
from __future__ import annotations
import os
import bisect
import heapq
from collections import defaultdict
import pandas as pd

import common
from io_utils import transfer_for

NS_PER_H = 3_600_000_000_000
NS_PER_S = 1_000_000_000


def _ts_floor_s(value_ns: int, tz: str) -> pd.Timestamp:
    """Build a tz-aware Timestamp floored to whole seconds. The integer-ns slot math
    yields 9-digit fractional seconds that break strict ISO-8601 / SAP OData parsers;
    flooring to seconds keeps timestamps exactly representable without shifting a slot
    past its interval (floor never rounds a start later or a finish earlier)."""
    floored = (int(value_ns) // NS_PER_S) * NS_PER_S
    return pd.Timestamp(floored, tz="UTC").tz_convert(tz)


class MachineTimeline:
    """One machine's occupancy: intervals sorted by start, bisect slot search."""

    def __init__(self):
        self.starts: list[int] = []
        self.iv: list[tuple] = []          # (start, end, key, lot_id) aligned to starts
        self.busy_ns = 0

    def _free_windows(self, key, co_ns, open_ns):
        """Yield (lo, hi) free windows respecting changeover to differing-key neighbours.

        co_ns is EITHER an int (flat changeover, same to every differing neighbour) OR a
        callable co_ns(from_item, to_item) -> int ns for SEQUENCE-DEPENDENT changeover:
        the setup gap then depends on the actual pair of adjacent materials, not a single
        per-lot constant. `key` is the item being placed; neighbour item is the interval's k."""
        seqdep = callable(co_ns)
        def gap(a, b):
            if a is None or a == b:
                return 0
            return co_ns(a, b) if seqdep else co_ns
        prev_end, prev_key = open_ns, None
        for (s, e, k, _) in self.iv:
            lo = prev_end + gap(prev_key, key)
            hi = s - gap(key, k)
            if hi > lo:
                yield (lo, hi)
            prev_end, prev_key = e, k
        lo = prev_end + gap(prev_key, key)
        yield (lo, None)                   # tail window, unbounded above

    def earliest_start(self, after, dur, key, co_ns, open_ns):
        for lo, hi in self._free_windows(key, co_ns, open_ns):
            s = max(after, lo)
            if hi is None or s + dur <= hi:
                return s
        return after

    def latest_start(self, target, dur, key, co_ns, open_ns, floor):
        best = None
        for lo, hi in self._free_windows(key, co_ns, open_ns):
            cap = target if hi is None else min(target, hi - dur)
            if cap >= lo and cap >= floor:
                best = cap if best is None else max(best, cap)
        return best

    def insert(self, start, dur, key, lot_id):
        end = start + dur
        i = bisect.bisect_left(self.starts, start)
        self.starts.insert(i, start)
        self.iv.insert(i, (start, end, key, lot_id))
        self.busy_ns += dur
        return end


# Routing operation_name (UPPER) -> config changeover_min key. The config keys are the
# machine names ("4 Roll Calender"), but lots carry the operation name ("FOUR ROLL
# CALENDAR"); without this map an EXACT lookup missed every key and silently charged the
# 15-min default (calenders got 15 min where the real changeover is 44 -> infeasible slots).
_CHANGEOVER_OP_ALIAS = {
    "FOUR ROLL CALENDAR": "4 Roll Calender",
    "RHC": "Roller Head Calender",
    "EXTRUSION": "Triplex Extruder",
    "PLY CUTTER": "Ply Cutter",
    "BELT CUTTER": "Belt Cutter",
    "GT BUILDING": "Building",
    "CAR BUILDING": "Carcass Building (1st Stage)",
    "CARCASS BUILDING (1ST STAGE)": "Carcass Building (1st Stage)",
    # Ops with no dedicated config changeover value: aliased to themselves so they
    # resolve deterministically (still to `default` since config lacks a key). The
    # aliasing documents them as KNOWN — the _warn_default_changeovers pass below
    # surfaces any op that lands on the 15-min default so a silent default never hides.
    "CAP PLY SLITTER": "Cap Ply Slitter",
    "EDGE GUM": "Edge Gum",
    "BEAD APEXING": "Bead Apexing",
    "BEAD WINDING PCR": "Bead Winding PCR",
}

# One-time guard state: distinct op-names that resolved to the 15-min `default`.
_DEFAULT_CO_WARNED = False


def _resolve_changeover_key(op_name: str):
    """Return (key, is_mixing) — the config lookup key a lot's op resolves to."""
    name = (op_name or "").strip()
    up = name.upper()
    if "MIXING" in up:
        return "mixing", True
    return _CHANGEOVER_OP_ALIAS.get(up, name), False


def _warn_default_changeovers(op_names, cfg, co_matrix=None) -> None:
    """ONE-TIME data-gap warning: list every distinct operation whose changeover
    resolves to the config `default` (no explicit changeover value). Does NOT invent
    minutes — it just makes a silent default visible so it can be reviewed. An op that
    maps to a loaded changeover-matrix machine-type is NOT warned — the matrix supplies
    its setup times (the flat default is only a per-pair fallback there)."""
    import sys
    global _DEFAULT_CO_WARNED
    if _DEFAULT_CO_WARNED:
        return
    co = cfg["changeover_min"]
    default_min = co.get("default", 15.0)
    has_matrix = bool(co_matrix)
    hit = {}
    for name in set(op_names):
        key, is_mixing = _resolve_changeover_key(name)
        if is_mixing:
            continue
        if has_matrix and (name or "").strip().upper() in _CO_MATRIX_OP_TO_MTYPE:
            continue                                     # covered by the sequence-dependent matrix
        if key not in co:                                # falls through to default
            hit[(name or "").strip()] = key
    if hit:
        listing = ", ".join(sorted(f"{op!r}->{key!r}" for op, key in hit.items()))
        msg = (f"[phase5][WARN] {len(hit)} operation(s) have NO explicit changeover_min "
               f"and use the {default_min}-min default: {listing}")
        print(msg, file=sys.stderr)
        print(msg)                                       # also to captured stdout
    _DEFAULT_CO_WARNED = True


def _changeover_ns(op_name: str, cfg: dict) -> int:
    co = cfg["changeover_min"]
    key, is_mixing = _resolve_changeover_key(op_name)
    if is_mixing:
        mins = co.get("mixing", 2.0)
    else:
        mins = co.get(key, co.get("default", 15.0))
    return int(mins * 60 * 1e9)


# Scheduler operation_name (UPPER) -> the machine-type label used in the plant's
# sequence-dependent changeover matrix (jkt_changeover_matrix_combined). Only the
# ops that physically incur a material-to-material setup are mapped; mixing/building
# carry no matrix and keep the flat config value. Ops absent here fall back to flat too.
_CO_MATRIX_OP_TO_MTYPE = {
    "BEAD APEXING": "PCR Bead Apexing",
    "BEAD WINDING PCR": "PCR Bead Winding",
    "BELT CUTTER": "Belt Cutter PCR",
    "CAP PLY SLITTER": "Cap Ply Cutter",
    "CHAFFER SLITTER": "Chaffer Slitter",
    "EDGE GUM": "Edge Gum Calendar",
    "EXTRUSION": "Quintoplex PCR Tread",
    "FOUR ROLL CALENDAR": "4 RC Calendar",
    "PLY CUTTER": "Ply Cutter PCR",
    "RHC": "Roller Head Calendar PCR",
}

# Diagnostic counters for the sequence-dependent lookup (matched vs fell-back-to-flat).
_CO_MATRIX_STATS = {"hit": 0, "miss": 0, "no_mtype": 0}


def _make_co_fn(op_name: str, co_matrix: dict, fallback_ns: int):
    """Return a changeover function co(from_item, to_item)->ns for a lot's operation.

    If the operation maps to a matrix machine-type, look the pair up there (sequence-
    dependent); on a missing pair charge the flat `fallback_ns` (config default) and count
    it so a data gap is visible, never silently zero. If the op has no matrix machine-type,
    return the flat int directly (old behaviour)."""
    up = (op_name or "").strip().upper()
    mtype = _CO_MATRIX_OP_TO_MTYPE.get(up)
    if not co_matrix or mtype is None:
        _CO_MATRIX_STATS["no_mtype"] += 1
        return fallback_ns                       # flat int -> _free_windows uses old path
    def co(a, b):
        v = co_matrix.get((mtype, a, b))
        if v is None:
            _CO_MATRIX_STATS["miss"] += 1
            return fallback_ns
        _CO_MATRIX_STATS["hit"] += 1
        return int(v * 60 * 1e9)
    return co


def run(ctx: dict, cfg: dict) -> dict:
    lots = ctx["lots"].set_index("lot_id")
    edges = [tuple(e) for e in ctx["dag_edges"].itertuples(index=False, name=None)]
    lt = ctx["lot_times"].set_index("lot_id")
    transfer = ctx["transfer"]
    co_matrix = ctx.get("changeover_matrix", {}) or {}
    _CO_MATRIX_STATS.update(hit=0, miss=0, no_mtype=0)
    ref = ctx["plan_start"]
    open_ns = ref.value - int(cfg["schedule_open_lead_h"] * NS_PER_H)
    cure_by_ns = int(cfg["green_tyre_cure_by_h"] * NS_PER_H)
    # front-prime horizon cutoff (experiment gate). Lots whose LFT is before this get
    # placed ASAP into the runway. None when disabled -> zero behaviour change.
    _fpd = float(cfg.get("front_prime_days", 0) or 0)
    prime_ns = ref.value + int(_fpd * 24 * NS_PER_H) if _fpd > 0 else None
    buffer_ns = int(cfg.get("pre_curing_buffer_h", 0.0) * NS_PER_H)  # build-finish slack
    no_aest = set(t.upper() for t in cfg["no_aest_item_types"])

    # Only wire edges between real lots — a dangling endpoint (bad BOM/DAG) would otherwise
    # KeyError in the indeg decrement or silently never-dispatch a consumer. Surface, don't drop.
    lot_ids = set(lots.index)
    dropped_edges = [(p, c) for p, c in edges if p not in lot_ids or c not in lot_ids]
    if dropped_edges:
        print(f"[phase5][WARN] {len(dropped_edges)} DAG edge(s) reference a non-lot endpoint "
              f"and were skipped (data gap): {dropped_edges[:5]}")
    edges = [(p, c) for p, c in edges if p in lot_ids and c in lot_ids]
    pred = defaultdict(list); succ = defaultdict(list)
    for p, c in edges:
        pred[c].append(p); succ[p].append(c)

    # per-lot static fields
    dur_ns, minage_ns, maxage_ns, tr_ns, co_ns, itype, op, need_ns, lft_ns, machines = ({} for _ in range(10))
    for l in lots.index:
        dur_ns[l] = max(int(float(lots.at[l, "duration_h"]) * NS_PER_H), int(1 * 60 * 1e9))
        minage_ns[l] = int(float(lots.at[l, "min_aging_h"]) * NS_PER_H)
        mx = float(lots.at[l, "max_aging_h"])
        mx = 1e8 if (mx != mx) else min(mx, 1e8)      # NaN-guard (mx!=mx) -> treat as unbounded
        maxage_ns[l] = int(mx * NS_PER_H)
        itype[l] = lots.at[l, "item_type"]
        tr_ns[l] = int(transfer_for(transfer, itype[l]) * 60 * 1e9)
        op[l] = lots.at[l, "operation"]
        # co_ns[l] is a flat int when no matrix machine-type applies, else a callable
        # co(from_item,to_item)->ns (sequence-dependent). _free_windows handles both.
        co_ns[l] = _make_co_fn(op[l], co_matrix, _changeover_ns(op[l], cfg))
        need_ns[l] = int(pd.Timestamp(lots.at[l, "need_by"]).value)
        lft_ns[l] = int(pd.Timestamp(lt.at[l, "LFT"]).value)
        machines[l] = [m for m in str(lots.at[l, "machines"]).split(",") if m] or ["UNASSIGNED"]
    _warn_default_changeovers([op[l] for l in lots.index], cfg, co_matrix)  # one-time silent-default guard
    pooled = {l: bool(lots.at[l, "pooled"]) for l in lots.index} if "pooled" in lots.columns \
             else {l: False for l in lots.index}

    # dispatch order: Kahn topo with priority (LFT, item, lot_id) — producers first.
    indeg = {l: len(pred[l]) for l in lots.index}
    heap = [(lft_ns[l], lots.at[l, "item_code"], l) for l in lots.index if indeg[l] == 0]
    heapq.heapify(heap)

    timelines: dict[str, MachineTimeline] = defaultdict(MachineTimeline)
    start_ns, finish_ns, placed_machine, status = {}, {}, {}, {}
    sched_rows, ledger, infeas = [], [], []

    def is_build(l):
        # Only the CURED TERMINAL (green tyre) is pulled ALAP to the press cure-by.
        # Intermediate builds (carcass) must precede it, so they go ALAP-to-LFT like
        # every other component — targeting curing_start would crowd out the green tyre
        # and slip it ~1.6h late every cycle.
        return itype[l] == common._GREEN_TYRE

    while heap:
        _, _, l = heapq.heappop(heap)
        d = dur_ns[l]
        material_ready = max([open_ns] +
                             [finish_ns[p] + tr_ns[p] + minage_ns[p] for p in pred[l]])
        curing_start = need_ns[l]

        if (not pooled[l]) and itype[l].upper() in no_aest:  # non-pooled bottleneck mixers: ASAP
            target, floor, asap = material_ready, material_ready, True
        elif is_build(l):                                    # terminal build: ALAP to cure-by
            # aim to FINISH pre_curing_buffer before the press so finite-capacity
            # contention eats the buffer instead of slipping past curing_start.
            target = curing_start - minage_ns[l] - buffer_ns - d
            floor = max(material_ready, curing_start - cure_by_ns - d)
            asap = False
        else:                                                # everything else: ALAP to LFT
            target = lft_ns[l] - d
            floor = material_ready
            asap = False
            # FRONT-PRIME: components/compounds feeding the FIRST front_prime_days of the
            # horizon are placed ASAP into the open-lead runway instead of ALAP — so day-1..N
            # cures are fed on time instead of starving on a cold start (ALAP otherwise leaves
            # the 96h runway ~99% idle). Verified on June: late builds -54% (13,951 -> 6,472),
            # over-aging -11%, makespan flat. Off unless front_prime_days > 0.
            if prime_ns is not None and lft_ns[l] < prime_ns:
                target, floor, asap = material_ready, material_ready, True
        target = max(target, floor)

        # choose machine + slot
        key = lots.at[l, "item_code"]

        after = max(material_ready, floor)          # never start below the floor (e.g. cure-by)
        def _pick(mode):
            best = None
            for m in sorted(machines[l]):
                tl = timelines[m]
                if mode == "asap":
                    s = tl.earliest_start(after, d, key, co_ns[l], open_ns)
                else:
                    s = tl.latest_start(target, d, key, co_ns[l], open_ns, floor)
                    if s is None:                     # ALAP infeasible → fall back ASAP, but not below floor
                        s = tl.earliest_start(after, d, key, co_ns[l], open_ns)
                score = (abs(s - target), tl.busy_ns, m)
                if best is None or score < best[0]:
                    best = (score, m, s)
            return best

        best = _pick("asap" if asap else "alap")
        # terminal green tyre: if ALAP overshoots the press start, fall back to ASAP so
        # the build finishes before curing (build-line contention, not a real shortage).
        if not asap and itype[l] == common._GREEN_TYRE and best[2] + d > curing_start:
            alt = _pick("asap")
            if alt is not None and alt[2] + d < best[2] + d:
                best = alt
        _, m, s = best
        e = timelines[m].insert(s, d, lots.at[l, "item_code"], l)
        start_ns[l], finish_ns[l], placed_machine[l] = s, e, m
        status[l] = "PLACED" if m != "UNASSIGNED" else "UNPLACED"
        if m == "UNASSIGNED":
            infeas.append({"lot_id": l, "item": lots.at[l, "item_code"], "reason": "NO_ELIGIBLE_MACHINE"})

        # commit-test (advisory in BTP-MODE) → breach ledger. The aging clock is the rest
        # time AFTER transfer (gap_avail = gap - transfer), consistent with material_ready =
        # finish + transfer + min_aging. BOTH breach tests + the reported delta must use
        # gap_avail — mixing raw gap (over-aged) with gap_avail (too-fresh) mis-classifies by
        # the transfer time and understates the too-fresh deficit.
        for p in pred[l]:
            gap_avail = (s - finish_ns[p]) - tr_ns[p]
            if gap_avail < minage_ns[p] - NS_PER_H // 3600:
                ledger.append(_breach(p, l, lots, gap_avail, minage_ns[p], maxage_ns[p], "TOO_FRESH"))
            elif maxage_ns[p] > 0 and gap_avail > maxage_ns[p] + NS_PER_H // 3600:
                ledger.append(_breach(p, l, lots, gap_avail, minage_ns[p], maxage_ns[p], "OVER_AGED"))
        if is_build(l):
            cure_gap = curing_start - e
            if cure_gap < 0 or cure_gap > cure_by_ns:
                # A negative gap that even an ASAP build (material_ready + d) could
                # not have beaten is not a schedulable miss — the curing block fires
                # before a from-empty chain can deliver. That is an opening-WIP need,
                # not a breach the scheduler can fix; flag it as such (TOC: seed day-0
                # WIP or advance the build-line open).
                earliest_finish = material_ready + d
                if cure_gap > cure_by_ns:
                    kind = "CUREBY_EXPIRED"
                elif earliest_finish > curing_start:
                    kind = "OPENING_WIP_REQUIRED"
                else:
                    kind = "CUREBY_NEGATIVE"
                ledger.append({"producer_lot": l, "consumer_lot": lots.at[l, "block_id"],
                               "item": lots.at[l, "item_code"], "gap_h": cure_gap / NS_PER_H,
                               "min_aging_h": 0.0, "max_aging_h": cfg["green_tyre_cure_by_h"],
                               "type": kind,
                               "delta_h": (cure_gap - cure_by_ns) / NS_PER_H})

        sched_rows.append({
            "lot_id": l, "item": lots.at[l, "item_code"], "item_type": itype[l],
            "sku": lots.at[l, "sku"], "operation": op[l],
            "department": lots.at[l, "department"], "machine": m,
            "scheduled_start": _ts_floor_s(s, cfg["timezone"]),
            "scheduled_finish": _ts_floor_s(e, cfg["timezone"]),
            "duration_h": d / NS_PER_H, "qty": lots.at[l, "qty"], "uom": lots.at[l, "uom"],
            "status": status[l],
        })

        for c in succ[l]:
            indeg[c] -= 1
            if indeg[c] == 0:
                heapq.heappush(heap, (lft_ns[c], lots.at[c, "item_code"], c))

    # completeness: every lot must be dispatched — a leftover (cycle / stuck indeg) would be
    # silently missing from the schedule. Surface it as an infeasibility, never drop silently.
    undispatched = [l for l in lots.index if l not in status]
    if undispatched:
        print(f"[phase5][WARN] {len(undispatched)} lot(s) never dispatched (cycle/stuck indeg): "
              f"{undispatched[:5]}")
        for l in undispatched:
            infeas.append({"lot_id": l, "item": lots.at[l, "item_code"], "reason": "NOT_DISPATCHED"})

    # echo the curing drum as PINNED rows
    drum = ctx["drum"]
    pinned = drum[(~drum["is_occupancy"]) & (drum["block_id"].isin(ctx["slice_blocks"]))]
    for _, r in pinned.iterrows():
        sched_rows.append({
            "lot_id": f"CURE_{r['block_id']}", "item": r["sku"], "item_type": "CURING",
            "sku": r["sku"], "operation": "Curing", "department": "Curing",
            "machine": r["press_id"],
            "scheduled_start": r["start_ts"].floor("s"),
            "scheduled_finish": r["end_ts"].floor("s"),
            "duration_h": (r["end_ts"] - r["start_ts"]) / pd.Timedelta(hours=1),
            "qty": r["qty"], "uom": "NOS", "status": "PINNED"})

    # FEFO RE-MATCH (gated): phase3 assigns each consumer to A producer lot by block
    # membership, NOT FEFO. When an item is made in many lots, a consumer flagged OVER_AGED
    # against a STALER lot would in reality be fed (First-Expire-First-Out) by a FRESHER lot
    # that finished within shelf. Those are FALSE over-ages. An over-age is REAL only if NO
    # lot of that item finished within max_age before the consumer's start.
    if cfg.get("fefo_matching", False):
        item_fins = defaultdict(list)
        for lid, fns in finish_ns.items():
            item_fins[lots.at[lid, "item_code"]].append(fns)
        for v in item_fins.values():
            v.sort()
        kept, dropped = [], 0
        for e in ledger:
            if e["type"] != "OVER_AGED":
                kept.append(e); continue
            cs = start_ns.get(e["consumer_lot"])
            fins = item_fins.get(e["item"], [])
            mx_ns = int(e["max_aging_h"] * NS_PER_H)
            if cs is not None and fins:
                i = bisect.bisect_right(fins, cs) - 1        # freshest lot finishing <= consumer start
                if i >= 0 and (cs - fins[i]) <= mx_ns:       # a fresh-enough lot exists -> FEFO feeds it
                    dropped += 1; continue                   # drop the FALSE over-age
            kept.append(e)
        ledger = kept
        print(f"[phase5] FEFO re-match: dropped {dropped} FALSE over-age edges "
              f"(a fresher lot of the same item fed the consumer within shelf)")

    schedule = pd.DataFrame(sched_rows).sort_values(["scheduled_start", "lot_id"]).reset_index(drop=True)
    ledger_df = pd.DataFrame(ledger)
    # only machines that actually hold a lot — the defaultdict auto-vivifies a timeline for
    # every machine merely SCORED in _pick, so filter out 0-lot phantoms before reporting.
    util = pd.DataFrame([{"machine": m, "booked_h": tl.busy_ns / NS_PER_H, "n_lots": len(tl.iv)}
                         for m, tl in sorted(timelines.items()) if tl.iv])

    o2 = ctx["outputs2_dir"]
    schedule.to_csv(os.path.join(o2, "phase5_schedule_updated.csv"), index=False)
    ledger_df.to_csv(os.path.join(o2, "phase5_aging_violations_updated.csv"), index=False)
    pd.DataFrame(infeas).to_csv(os.path.join(o2, "phase5_infeasibility_updated.csv"), index=False)
    util.to_csv(os.path.join(o2, "phase5_machine_utilization_updated.csv"), index=False)

    ctx["schedule"] = schedule
    ctx["ledger"] = ledger_df
    placed = (schedule["status"] == "PLACED").sum()
    print(f"[phase5] placed={placed} pinned={(schedule['status']=='PINNED').sum()} "
          f"unplaced={(schedule['status']=='UNPLACED').sum()} | breaches={len(ledger_df)} "
          f"| machines used={len(util)}")
    if co_matrix:
        h, m = _CO_MATRIX_STATS["hit"], _CO_MATRIX_STATS["miss"]
        tot = h + m
        print(f"[phase5] changeover matrix: {len(co_matrix)} pairs loaded | lookups {tot} "
              f"(matched {h} = {100*h/tot:.0f}% | fell back to flat default {m}) — "
              f"sequence-dependent setup ACTIVE")
    return ctx


def _breach(p, c, lots, gap, mn, mx, kind):
    return {"producer_lot": p, "consumer_lot": c, "item": lots.at[p, "item_code"],
            "gap_h": gap / NS_PER_H, "min_aging_h": mn / NS_PER_H,
            "max_aging_h": mx / NS_PER_H, "type": kind,
            "delta_h": (mn - gap) / NS_PER_H if kind == "TOO_FRESH" else (gap - mx) / NS_PER_H}
