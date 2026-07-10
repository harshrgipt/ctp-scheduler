"""
phase4 — CPM time windows over the lot DAG.

Backward pass (LST/LFT) anchors sink lots at need_by - min_aging and propagates
latest-start through aging+transfer gaps; forward pass (EST/EFT) propagates
earliest material-ready. slack = LST - EST; critical when slack <= 0. No machines
are committed here. Inverted windows (LST < EST) predict the phase5 aging breaches.
"""
from __future__ import annotations
import os
from collections import defaultdict, deque
import pandas as pd

import common
from io_utils import transfer_for

HOUR = pd.Timedelta(hours=1)


def _topo(nodes, edges):
    succ = defaultdict(list); indeg = defaultdict(int)
    for p, c in edges:
        succ[p].append(c); indeg[c] += 1
    q = deque([n for n in nodes if indeg[n] == 0])
    order = []
    while q:
        n = q.popleft(); order.append(n)
        for c in succ[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    seen = set(order)                                    # O(n): build once, not per-node
    order += [n for n in nodes if n not in seen]         # append any cycle remnants
    return order


def run(ctx: dict, cfg: dict) -> dict:
    lots = ctx["lots"].set_index("lot_id")
    edges = [tuple(e) for e in ctx["dag_edges"].itertuples(index=False, name=None)]
    transfer = ctx["transfer"]
    ref = ctx["plan_start"]
    sched_open_h = -cfg["schedule_open_lead_h"]
    buffer_h = float(cfg.get("pre_curing_buffer_h", 0.0))   # slack: finish builds before press

    nodes = list(lots.index)
    succ = defaultdict(list); pred = defaultdict(list)
    for p, c in edges:
        succ[p].append(c); pred[c].append(p)

    dur = {l: float(lots.at[l, "duration_h"]) for l in nodes}
    minage = {l: float(lots.at[l, "min_aging_h"]) for l in nodes}
    maxage = {l: float(lots.at[l, "max_aging_h"]) for l in nodes}
    tr_h = {l: transfer_for(transfer, lots.at[l, "item_type"]) / 60.0 for l in nodes}
    need_h = {l: (lots.at[l, "need_by"] - ref) / HOUR for l in nodes}

    order = _topo(nodes, edges)

    # Backward: LST/LFT (process consumers before producers).
    # LFT (min-aging, freshness ceiling) = must finish by this or the EARLIEST consumer is
    # too fresh. EAF (max-aging, over-age floor) = must finish AFTER this or the LATEST
    # consumer over-ages. A lot is AGING-INFEASIBLE when EAF > LFT: no finish time keeps
    # every consumer within shelf -> its consumer span exceeds shelf (structural, Class-II).
    LFT, LST, EAF = {}, {}, {}
    for l in reversed(order):
        mxa = maxage[l] if maxage[l] == maxage[l] and maxage[l] < 1e7 else 1e9
        if not succ[l]:
            LFT[l] = need_h[l] - minage[l] - buffer_h   # pull whole chain earlier by buffer
            EAF[l] = need_h[l] - mxa                     # earliest finish before consumer over-ages
        else:
            LFT[l] = min(LST[c] - minage[l] - tr_h[l] for c in succ[l])
            EAF[l] = max(LST[c] - tr_h[l] - mxa for c in succ[l])   # binds on LATEST consumer
        LST[l] = LFT[l] - dur[l]

    # Forward: EST/EFT (process producers before consumers).
    EST, EFT = {}, {}
    for l in order:
        if not pred[l]:
            EST[l] = sched_open_h
        else:
            EST[l] = max([sched_open_h] +
                         [EFT[p] + minage[p] + tr_h[p] for p in pred[l]])
        EFT[l] = EST[l] + dur[l]

    rows, inverted, ageinfeas = [], [], []
    for l in nodes:
        slack = LST[l] - EST[l]
        age_window = LFT[l] - EAF[l]          # >0 = an aging-valid finish window exists
        rows.append({
            "lot_id": l, "item_code": lots.at[l, "item_code"],
            "EST": ref + EST[l] * HOUR, "EFT": ref + EFT[l] * HOUR,
            "LST": ref + LST[l] * HOUR, "LFT": ref + LFT[l] * HOUR,
            "EAF_h": round(EAF[l], 1),        # earliest aging-valid finish, hours from ref (over-age floor)
            "age_window_h": age_window,
            "aging_infeasible": age_window < -1e-6,
            "slack_h": slack, "is_critical": slack <= 0,
            "need_by": lots.at[l, "need_by"],
        })
        if slack < 0:
            inverted.append({"lot_id": l, "item_code": lots.at[l, "item_code"],
                             "slack_h": slack, "inversion_h": -slack})
        if age_window < -1e-6:
            ageinfeas.append({"lot_id": l, "item_code": lots.at[l, "item_code"],
                              "item_type": lots.at[l, "item_type"],
                              "max_aging_h": maxage[l], "age_window_h": round(age_window, 1),
                              "consumer_span_over_shelf_h": round(-age_window, 1)})

    lot_times = pd.DataFrame(rows)
    ctx["lot_times"] = lot_times
    lot_times.to_csv(os.path.join(ctx["outputs2_dir"], "phase4_lot_times_updated.csv"), index=False)
    pd.DataFrame(inverted).to_csv(
        os.path.join(ctx["outputs2_dir"], "phase4_inverted_windows_updated.csv"), index=False)
    pd.DataFrame(ageinfeas).to_csv(
        os.path.join(ctx["outputs2_dir"], "phase4_aging_feasibility_updated.csv"), index=False)

    print(f"[phase4] lots={len(lot_times)} critical={int(lot_times['is_critical'].sum())} "
          f"| inverted windows={len(inverted)} | AGING-INFEASIBLE lots={len(ageinfeas)} "
          f"(consumer span > shelf -> structural, no placement can avoid over-age)")
    return ctx
