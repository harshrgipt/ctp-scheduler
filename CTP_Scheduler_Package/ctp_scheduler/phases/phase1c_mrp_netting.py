"""
phase1c_mrp_netting.py — file-based, time-aware MRP cascade WIP netting (CTP).

Ported from the BTP v6_wave phase1c, adapted to the CTP in-memory ctx flow.

PRINCIPLE
  Opening WIP at an item covers that item AND — via a BOM topological cascade —
  its upstream chain, but ONLY for demand consumed within the item's max-aging
  window (need_by <= plan_start + max_aging). Demand outside that window must be
  produced fresh (the WIP would age out before it is used).

FLOW
  Reads ctx["demand"] (phase1b), nets it against the opening-WIP snapshot, sets
  ctx["demand"] to the netted frame, and writes an audit copy to
  outputs2/phase1_demand_NET_updated.csv.gz.

GRACEFUL NO-OP
  If no opening-WIP file is present (or it has no recognisable rows) the phase is
  a no-op: demand passes through un-netted and day-0 shortfalls surface later as
  phase5 OPENING_WIP_REQUIRED ledger entries.
"""
from __future__ import annotations
import os
from collections import defaultdict, deque
import pandas as pd

import io_utils as io
import common


def _write_net(ctx: dict, net: pd.DataFrame) -> None:
    out = os.path.join(ctx["outputs2_dir"], "phase1_demand_NET_updated.csv.gz")
    net.to_csv(out, index=False, compression={"method": "gzip", "compresslevel": 1})
    print(f"[phase1c] wrote {os.path.basename(out)} ({len(net):,} rows)")


def run(ctx: dict, cfg: dict) -> dict:
    demand = ctx.get("demand")
    if demand is None or len(demand) == 0:
        print("[phase1c] no demand rows; skipping netting")
        return ctx

    # --- opening WIP (graceful no-op when absent) ---
    wip_rel = (cfg.get("inputs") or {}).get("opening_wip")
    wip_path = io.resolve(cfg, wip_rel) if wip_rel else None
    wip = io.read_opening_wip(wip_path) if wip_path else {}
    if not wip:
        who = os.path.basename(wip_path) if wip_path else "not configured"
        print(f"[phase1c] WARN no opening-WIP inventory found ({who}); MRP netting is a "
              f"NO-OP (demand passes through un-netted). Day-0 shortfalls will surface as "
              f"phase5 OPENING_WIP_REQUIRED.")
        _write_net(ctx, demand)   # NET == gross keeps downstream/reporting consistent
        return ctx

    # --- plan_start + per-item max-aging window ---
    plan_start = ctx.get("plan_start") or (ctx.get("plan_params") or {}).get("plan_start")
    plan_start = pd.Timestamp(plan_start)
    default_max_h = float(cfg.get("mrp_default_max_aging_h", 72))
    max_aging_map = {}
    for _, r in ctx["aging_df"].iterrows():
        mx = common.aging_to_hours(r.get("MaxAging"), r.get("MaxAgingUnit"))
        max_aging_map[str(r["ItemCode"]).strip()] = mx if mx is not None else default_max_h

    # --- attach need_by (from block->wave) + in-window flag ---
    d = demand.copy()
    b2w = ctx["block_to_wave"].set_index("block_id")["need_by"]
    d["need_by"] = pd.to_datetime(d["block_id"].map(b2w), errors="coerce")
    d["demand_qty"] = pd.to_numeric(d["demand_qty"], errors="coerce").fillna(0.0)
    d["hours_from_start"] = (d["need_by"] - plan_start).dt.total_seconds() / 3600.0
    d["max_h"] = d["item_code"].map(max_aging_map).fillna(default_max_h)
    d["is_in_window"] = d["hours_from_start"] <= d["max_h"]

    # --- BOM edges: Parent -> [(child, per_unit_qty)] ---
    bom = ctx["bom"]
    par = bom["Parent"].astype(str).str.strip()
    ch = bom["child"].astype(str).str.strip()
    qty = pd.to_numeric(bom["child_quantity"], errors="coerce").fillna(0.0)
    edges = defaultdict(list)
    all_items = set()
    for p, c, q in zip(par, ch, qty):
        if q > 0 and p and c:
            edges[p].append((c, float(q)))
            all_items.add(p); all_items.add(c)
    all_items |= set(d["item_code"].astype(str).unique())

    # --- topological order (finished good -> raw material) ---
    in_deg = defaultdict(int)
    for p, kids in edges.items():
        for c, _ in kids:
            in_deg[c] += 1
    for it in all_items:
        in_deg.setdefault(it, 0)
    queue = deque([it for it in all_items if in_deg[it] == 0])
    topo, local = [], dict(in_deg)
    while queue:
        u = queue.popleft(); topo.append(u)
        for c, _ in edges.get(u, []):
            local[c] -= 1
            if local[c] == 0:
                queue.append(c)
    seen = set(topo)
    for it in all_items:              # cycle safeguard
        if it not in seen:
            topo.append(it)

    # --- in-window demand per item ---
    iw = d[d["is_in_window"]].groupby("item_code")["demand_qty"].sum().to_dict()

    # --- cascade: WIP savings flow down the BOM, in-window only ---
    reduction = defaultdict(float)
    saving = {}
    for item in topo:
        orig_iw = iw.get(item, 0.0)
        if orig_iw <= 0:
            saving[item] = 0.0
            continue
        after = max(0.0, orig_iw - reduction.get(item, 0.0))
        used = min(after, wip.get(item, 0.0))
        net_iw = after - used
        saved = orig_iw - net_iw
        saving[item] = saved
        for c, per_unit in edges.get(item, []):
            reduction[c] += saved * per_unit

    # --- apply per-item ratio to in-window rows only ---
    d["saving"] = d["item_code"].map(pd.Series(saving)).fillna(0.0)
    d["orig_iw"] = d["item_code"].map(pd.Series(iw)).fillna(0.0)
    d["ratio"] = 1.0
    mask = d["is_in_window"] & (d["orig_iw"] > 0)
    d.loc[mask, "ratio"] = (d.loc[mask, "orig_iw"] - d.loc[mask, "saving"]) / d.loc[mask, "orig_iw"]
    d.loc[d["ratio"] < 0, "ratio"] = 0.0
    d["demand_qty"] = (d["demand_qty"] * d["ratio"]).round(4)

    net = d[d["demand_qty"] > 0][list(demand.columns)].reset_index(drop=True)

    gross_q = float(pd.to_numeric(demand["demand_qty"], errors="coerce").fillna(0).sum())
    net_q = float(net["demand_qty"].sum())
    saved_q = gross_q - net_q
    print(f"[phase1c] WIP items: {len(wip):,} | gross {gross_q:,.0f} -> net {net_q:,.0f} "
          f"(saved {saved_q:,.0f}, {100*saved_q/max(gross_q,1):.1f}%) | "
          f"rows {len(demand):,} -> {len(net):,}")

    ctx["demand"] = net
    _write_net(ctx, net)
    return ctx
