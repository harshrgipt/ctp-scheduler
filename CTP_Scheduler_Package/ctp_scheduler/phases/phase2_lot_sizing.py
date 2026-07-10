"""
phase2 — lot sizing with time-phased demand consolidation (two-pass).

Two grains, chosen by item role:

  * BUILD items (green tyre, carcass — department "Building") keep PER-BLOCK grain:
    each curing block gets its own build lot. These are genuinely per-block.

  * POOLED items (beads, plies, belts, calandered rolls, compounds, chemicals) are
    CONSOLIDATED across blocks into time-phased batches. MPQ is applied ONCE per
    bucket, not once per block — this kills the per-block MPQ over-production that
    otherwise floors e.g. 69 needed beads up to 1000 per block (14.5x).

TWO-PASS bucketing (arvind-rao's fix for multi-level pooling drift): a pooled item
is not consumed at curing — it is consumed at its parent's build, offset by the
whole downstream chain. Bucketing on the curing need_by therefore lets a batch span
more than its shelf life and over-age its latest consumer. So pass 1 builds
per-event provisional lots and runs a CPM backward pass to harvest each producer's
true latest-consume instant  consume_at = LST[consumer] - transfer - min_aging;
pass 2 buckets on consume_at with window W = MaxAging - MinAging - safety, which
bounds every bucket to the real shelf life by construction.

consume_at is a BUCKETING key only — it bounds each batch's span here in phase2. It is
carried onto the emitted lot for traceability but phase5 does NOT place the producer to
consume_at; phase5 places pooled lots ALAP to their CPM LFT (and builds/mixers to
need_by / material-ready). Do not read the `consume_at` column as a placement target.

Each lot carries `pooled` and `member_blocks` (the curing blocks it serves) so
phase3 can wire the producer->build precedence edges across the consolidation.
"""
from __future__ import annotations
import os
import json
import math
from collections import defaultdict, deque
import pandas as pd

import common
from io_utils import transfer_for
import phase3_dag_construction as p3

HOUR = pd.Timedelta(hours=1)


def _apply_mpq(qty: float, item_type: str, demand_uom: str, mpq: dict,
               produce_to_demand: bool = True) -> float:
    """Lot quantity for a (consolidated) demand block.

    CTP default is produce_to_demand=True: production tracks demand and the MPQ is
    NOT used to pad qty upward — a single-SKU slice must not be inflated 393x by a
    300 m calender MPQ or a 675 kg compound batch when its shared demand is tiny.
    (Set produce_to_demand=False for plant-wide runs to honor MPQ batch sizes.)
    """
    if produce_to_demand:
        return math.ceil(qty) if demand_uom == "NOS" else qty
    floor = mpq.get(item_type.upper())
    if not floor:
        return math.ceil(qty) if demand_uom == "NOS" else qty
    fqty, fuom = floor
    if fuom != demand_uom:                       # unit mismatch → skip the floor
        return math.ceil(qty) if demand_uom == "NOS" else qty
    out = max(qty, fqty)
    if item_type.upper() in ("MASTER COMPOUND", "FINAL COMPOUND"):
        out = math.ceil(out / fqty) * fqty       # whole multiples of the batch floor
    return math.ceil(out) if demand_uom == "NOS" else out


def _is_build_item(meta: dict, itype: str) -> bool:
    dept = str(meta.get("department", "")).lower()
    op = str(meta.get("operation_name", "")).lower()
    return itype == common._GREEN_TYRE or "building" in dept or "build" in op


def _round_to_batch(qty: float, meta: dict, itype: str) -> float:
    """Compounds are mixed in whole Banbury/Final-Mix batches, not fractional KG.
    Round the (demand-tracking) lot UP to an integer number of routing batches —
    the leftover of the last batch is normal mixing-room carryover, not the per-block
    MPQ over-production that produce_to_demand removed."""
    bs = meta.get("batch_size")
    if itype.upper() in ("MASTER COMPOUND", "FINAL COMPOUND") and bs and bs > 0:
        return math.ceil(qty / bs) * bs
    return qty


def _is_curing(meta: dict) -> bool:
    """The finished-tyre 'Curing' op IS the pinned drum — never a production lot.
    The green tyre is the real terminal build; it gets cure-by checked against the
    pinned curing block directly, so the finished SKU must not be scheduled."""
    dept = str(meta.get("department", "")).lower()
    op = str(meta.get("operation_name", "")).lower()
    return "curing" in dept or "curing" in op


# Plant-confirmed yield: on the belt cutter ONE cut = ONE tyre = 2 belts (B1+B2 in a
# single stroke). The lot qty is in belt PIECES, so the cut-time basis is qty/2. Halves
# the belt-cutter hours (815h -> ~408h). See memory ctp-beltcutter-beadapex-rates.
_PIECES_PER_CUT = {"BELT CUTTER": 2}

# Plant-known throughput overrides (pieces/min per machine) for ops whose routing
# proc_time_UOM is an unusable mislabel. Bead Apexing / Bead Winding are tagged
# "RPM" (12.33 / 128.89) which op_duration_min reads as ~0.5-4.9 min/bead; the real
# line runs 15 NOS/min. Without this override the bead line falsely saturates at
# ~300% utilisation and stretches the makespan to 300+ days (the two ops are the
# ONLY operations >100% util). Timing at 15 NOS/min drops them to ~30-42% util and
# no operation exceeds 100%. See memory ctp-beltcutter-beadapex-rates.
_OP_NOS_PER_MIN = {"BEAD APEXING": 15.0, "BEAD WINDING": 15.0}
# The routing "RPM" proc_time_UOM is the plant's bead-line mislabel: it appears on
# Bead Apexing, Bead Winding AND apex rows whose operation_name is blank/"Unknown"
# (e.g. AP 0012/AP0009/AP 0022 — same signature: RPM, batch 200 NOS). All are timed
# at the known 15 NOS/min line rate. Catching by UOM (not op-name) fixes the
# "Unknown"-op apex runaways (~3,300 h each) that the op-name map alone missed.
_RPM_NOS_PER_MIN = 15.0


def _dur_h(qty, uom, meta, eff, item=None, len_factor=None):
    """Per-lot duration in hours. For length-rate slitter/cutter ops whose routed_product
    is timed on the developed output length but physically feeds a smaller sheet, scale
    the DURATION qty by the fed/developed length ratio (the cap-ply-slitter fix). The
    lot's demand qty is unchanged — only the time basis is corrected."""
    dqty = qty
    if len_factor and item is not None:
        f = len_factor.get(item)
        if f and f > 0:
            dqty = qty * f
    op = str(meta.get("operation_name", "")).strip().upper()
    puom = str(meta.get("proc_time_UOM", "")).strip().upper()
    # Plant-rate override for RPM-mislabelled bead ops: qty pieces at N NOS/min.
    # Match by op-name first, then by the RPM UOM (catches blank/"Unknown"-op apex rows).
    rate = _OP_NOS_PER_MIN.get(op)
    if rate is None and puom == "RPM":
        rate = _RPM_NOS_PER_MIN
    if rate and rate > 0:
        return max(dqty / rate, 1.0) / 60.0               # minutes -> hours
    ppc = _PIECES_PER_CUT.get(op)
    if ppc and ppc > 1:                                   # N pieces per cut -> time basis = qty/N
        dqty = dqty / ppc
    return common.op_duration_min(dqty, uom, meta["proc_time_UOM"],
                                  meta["proc_time"], meta["batch_size"], eff) / 60.0


def _bucket_events(events: list, window_h: float) -> list:
    """Greedy forward bucketing on the key (events[i][0]); carries full tuples."""
    events = sorted(events, key=lambda e: e[0])
    buckets, cur, anchor = [], [], None
    span = pd.Timedelta(hours=window_h)
    for ev in events:
        if anchor is None or ev[0] <= anchor + span:
            cur.append(ev)
            if anchor is None:
                anchor = ev[0]
        else:
            buckets.append(cur)
            cur, anchor = [ev], ev[0]
    if cur:
        buckets.append(cur)
    return buckets


def _compute_bucket_key(ctx, cfg, grp, routing_idx, aging_map):
    """Pass 1: per-event CPM to get consume_at[(item, block)] = the latest instant a
    producer batch may FINISH and still age-clean feed that block's consumer."""
    eff = cfg["efficiency_override"]
    buffer_h = float(cfg.get("pre_curing_buffer_h", 0.0))
    transfer = ctx["transfer"]
    ref = ctx["plan_start"]
    lenf = ctx.get("len_input_factor")
    block_sku = dict(zip(ctx["block_to_wave"]["block_id"], ctx["block_to_wave"]["sku"]))

    serving = {}        # (item, block) -> event lot id
    meta_l = {}         # lot -> timing meta
    lid = 0
    for r in grp.itertuples():
        item = r.item_code
        if item not in routing_idx:
            continue
        m = routing_idx[item]
        if _is_curing(m):                 # curing = pinned drum, not a scheduled lot
            continue
        L = lid; lid += 1
        serving[(item, r.block_id)] = L
        mn, mx = aging_map.get(item, (0.0, 1e9))
        meta_l[L] = {
            "item": item,
            "dur": _dur_h(r.demand_qty, r.demand_uom, m, eff, item, lenf),
            "mn": mn,
            "tr": transfer_for(transfer, r.item_type) / 60.0,
            "need": (r.need_by - ref) / HOUR,
        }

    pc, pg = p3._sku_edges(ctx["bom"], ctx["slice_skus"])
    succ = defaultdict(list); indeg = defaultdict(int)
    for blk in ctx["slice_blocks"]:
        s = block_sku.get(blk)
        if s is None:
            continue
        for parent, child in pc.get(s, ()):
            cl, pl = serving.get((child, blk)), serving.get((parent, blk))
            if cl is not None and pl is not None:
                succ[cl].append(pl); indeg[pl] += 1
        for parent, gp in pg.get(s, ()):
            pl, gl = serving.get((parent, blk)), serving.get((gp, blk))
            if pl is not None and gl is not None:
                succ[pl].append(gl); indeg[gl] += 1

    # Kahn topo, then backward LST pass.
    nodes = list(meta_l)
    ind = dict(indeg)
    q = deque([n for n in nodes if ind.get(n, 0) == 0])
    order = []
    while q:
        n = q.popleft(); order.append(n)
        for c in succ.get(n, ()):
            ind[c] -= 1
            if ind[c] == 0:
                q.append(c)
    seen = set(order)
    order += [n for n in nodes if n not in seen]

    LST = {}
    for l in reversed(order):
        ml = meta_l[l]
        if not succ.get(l):
            lft = ml["need"] - ml["mn"] - buffer_h
        else:
            lft = min(LST[c] - ml["mn"] - ml["tr"] for c in succ[l])
        LST[l] = lft - ml["dur"]

    key = {}
    for (item, blk), L in serving.items():
        if not succ.get(L):
            continue
        ml = meta_l[L]
        consume_h = min(LST[c] for c in succ[L]) - ml["tr"] - ml["mn"]
        key[(item, blk)] = ref + consume_h * HOUR
    return key


def run(ctx: dict, cfg: dict) -> dict:
    demand = ctx["demand"]
    routing_idx = ctx["routing_idx"]
    mpq = ctx["mpq"]
    aging_map = ctx["aging_map"]
    b2w = ctx["block_to_wave"].set_index("block_id")
    eff = cfg["efficiency_override"]
    max_h = cfg["max_lot_duration_h"]
    max_sub = int(cfg.get("max_sub_lots", 50))
    safety = float(cfg.get("bucket_safety_h", 2.0))
    p2d = bool(cfg.get("produce_to_demand", True))   # CTP: produce to demand, MPQ not a max
    lenf = ctx.get("len_input_factor")               # slitter/cutter fed-length duration fix

    # consolidate multi-SKU demand on a (block, item) first.
    grp = (demand[demand["item_code"].isin(routing_idx)]
           .groupby(["block_id", "item_code", "item_type", "demand_uom"], as_index=False)
           .agg(demand_qty=("demand_qty", "sum"),
                sku=("sku", lambda s: ",".join(sorted(set(s))))))
    grp["need_by"] = grp["block_id"].map(b2w["need_by"])

    # PASS 1: harvest the consumption-clock bucket key (consume_at per item/block).
    bucket_key = _compute_bucket_key(ctx, cfg, grp, routing_idx, aging_map)

    lots, flagged = [], []

    def emit(item, itype, uom, sku, need_by, qty, members, pooled, sub_idx, consume_at=None):
        meta = routing_idx[item]
        mn, mx = aging_map.get(item, (0.0, 1e9))
        dur_h = _dur_h(qty, uom, meta, eff, item, lenf)
        lots.append({
            "item_code": item, "item_type": itype, "sku": sku,
            "block_id": members[0], "qty": qty, "uom": uom,
            "need_by": need_by, "consume_at": consume_at if consume_at is not None else need_by,
            "duration_h": dur_h,
            "operation": meta["operation_name"], "department": meta["department"],
            "machines": ",".join(meta["machines"]),
            "min_aging_h": mn, "max_aging_h": mx if mx is not None else 1e9,
            "pooled": pooled, "member_blocks": json.dumps(members), "sub": sub_idx,
        })

    for (item, itype, uom), sub in grp.groupby(["item_code", "item_type", "demand_uom"]):
        meta = routing_idx[item]
        if _is_curing(meta):              # finished-tyre curing = pinned drum, skip
            continue
        sku0 = sub["sku"].iloc[0]

        if _is_build_item(meta, itype):
            # per-block grain: one lot per curing block (split by 8h cap).
            for r in sub.itertuples():
                qty = _apply_mpq(r.demand_qty, itype, uom, mpq, p2d)
                dur_h = _dur_h(qty, uom, meta, eff, item, lenf)
                n = max(1, math.ceil(dur_h / max_h)) if dur_h > max_h else 1
                if n > max_sub:
                    flagged.append({"item_code": item, "qty": qty, "dur_h": round(dur_h, 1), "n_sub_raw": n})
                    n = max_sub
                for k in range(n):
                    emit(item, itype, uom, sku0, r.need_by, qty / n, [r.block_id], False, k)
            continue

        # pooled grain: bucket on the CONSUMPTION clock (consume_at), window = shelf life.
        # window_factor (<1) tightens buckets below the raw shelf life so multi-level
        # pooling drift (a batch placed ALAP for its earliest consumer aging out for
        # its latest) cannot exceed max-aging — i.e. release fresher, more-frequent
        # batches on the rope, the TOC-correct response to the over-age signal.
        mn, mx = aging_map.get(item, (0.0, 1e9))
        mx = mx if mx is not None else 1e9
        wf = float(cfg.get("pooled_window_factor", 1.0))
        window = max(1.0, (min(mx, 1e6) - mn) * wf - safety)
        # events: (bucket_key, block, qty, real_need_by)
        events = [(bucket_key.get((item, r.block_id), r.need_by), r.block_id,
                   float(r.demand_qty), r.need_by) for r in sub.itertuples()]
        # window clamp: never shrink a bucket below the time demand takes to accrue ONE
        # production batch — else a tight wf makes 1 full Banbury batch per block (mass
        # over-production of e.g. a 355 kg compound for a few kg of need) and defeats
        # consolidation. Extend up to one-batch demand span, but cap at the shelf-life
        # window so aging stays bounded (no over-age reintroduced).
        bs = meta.get("batch_size")
        if bs and bs > 0 and itype.upper() in ("MASTER COMPOUND", "FINAL COMPOUND"):
            total_dem = sum(e[2] for e in events)
            if total_dem > 0:
                span_h = max((max(e[0] for e in events) - min(e[0] for e in events)) / HOUR, 1.0)
                one_batch_span = bs * span_h / total_dem     # time to accrue one batch of demand
                hard_cap = max(1.0, (min(mx, 1e6) - mn) - safety)   # full shelf life − safety
                window = min(max(window, one_batch_span), hard_cap)
        for bucket in _bucket_events(events, window):
            bd = sum(e[2] for e in bucket)
            members = [e[1] for e in bucket]                 # blocks (consume_at order)
            total = _apply_mpq(bd, itype, uom, mpq, p2d)     # produce-to-demand (CTP)
            total = _round_to_batch(total, meta, itype)      # whole Banbury batches for compounds
            dur_total = _dur_h(total, uom, meta, eff, item, lenf)
            n = max(1, math.ceil(dur_total / max_h)) if dur_total > max_h else 1
            if n > max_sub:
                flagged.append({"item_code": item, "qty": total, "dur_h": round(dur_total, 1), "n_sub_raw": n})
                n = max_sub
            n = min(n, len(members))
            size = math.ceil(len(members) / n)
            # size=ceil(len/n) can yield FEWER than n non-empty chunks (e.g. len=4,n=3 ->
            # [2,2,0]); dividing total by n would then under-produce (emit total*non_empty/n).
            # Split total across the ACTUAL non-empty chunks so qty (and duration) are conserved.
            chunks = [members[k * size:(k + 1) * size] for k in range(n)]
            chunks = [c for c in chunks if c]
            qty_sub = total / len(chunks)
            for k, chunk in enumerate(chunks):
                need_k = min(b2w.loc[bclk, "need_by"] for bclk in chunk)
                # binding consumption deadline for this batch = earliest member's
                # consume_at. NOTE: this is used HERE only to FORM the bucket (window W)
                # so a batch cannot span past its shelf life. phase5 does NOT place the
                # producer to consume_at — it places pooled lots ALAP to their CPM LFT
                # (and builds/mixers to need_by/material-ready). consume_at is carried on
                # the lot for traceability but is not phase5's placement target.
                consume_k = min(bucket_key.get((item, bclk), b2w.loc[bclk, "need_by"])
                                for bclk in chunk)
                emit(item, itype, uom, sku0, need_k, qty_sub, chunk, True, k, consume_k)

    lots_df = pd.DataFrame(lots).sort_values(["pooled", "block_id", "item_code", "sub"]).reset_index(drop=True)
    lots_df.insert(0, "lot_id", [f"L{i:06d}" for i in range(len(lots_df))])
    ctx["lots"] = lots_df
    lots_df.to_csv(os.path.join(ctx["outputs2_dir"], "phase2_lots_updated.csv"), index=False)

    fdf = (pd.DataFrame(flagged).sort_values("dur_h", ascending=False).drop_duplicates("item_code")
           if flagged else pd.DataFrame(columns=["item_code", "qty", "dur_h", "n_sub_raw"]))
    fdf.to_csv(os.path.join(ctx["outputs2_dir"], "phase2_runaway_lots.csv"), index=False)  # always rewrite (clears stale)
    if flagged:
        print(f"[phase2] WARNING capped {len(fdf)} runaway item(s) at n_sub={max_sub}; "
              f"worst={fdf.iloc[0]['item_code']} dur_h={fdf.iloc[0]['dur_h']}")

    n_pooled = int(lots_df["pooled"].sum())
    print(f"[phase2] lots={len(lots_df)} (pooled={n_pooled}, per-block={len(lots_df)-n_pooled}) "
          f"| items={lots_df['item_code'].nunique()} "
          f"| total duration_h={lots_df['duration_h'].sum():.1f} "
          f"| max lot dur_h={lots_df['duration_h'].max():.2f}")
    return ctx
