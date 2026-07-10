"""
phase3 — DAG construction (producer -> consumer precedence + degrees).

Precedence comes from the SKU's BOM edges: child -> Parent (a component feeds its
assembly) and Parent -> grand_parent (carcass/tread feed the green tyre). After
phase2's consolidation a producer lot may be POOLED (one batch serving many curing
blocks) while its consumers stay per-block builds, so edges are resolved through a
(item, block) -> serving-lot map built from each lot's member_blocks: for every
block, wire the lots serving the child to the lots serving its parent. Pooled lots
shared across blocks collapse to a single deduped edge.
"""
from __future__ import annotations
import os
import json
from collections import defaultdict
import pandas as pd

import common


def _sku_edges(bom: pd.DataFrame, skus) -> tuple[dict, dict]:
    """sku -> set of (parent, child) and (parent, grand_parent) edges."""
    pc, pg = defaultdict(set), defaultdict(set)
    sub = bom[bom["Super_parent"].isin(skus)]
    for s, p, c, g in zip(sub["Super_parent"], sub["Parent"], sub["child"], sub["grand_parent"]):
        if p and c:
            pc[s].add((p, c))
        if p and g and g != p:
            pg[s].add((p, g))
    return pc, pg


def run(ctx: dict, cfg: dict) -> dict:
    lots = ctx["lots"]
    pc, pg = _sku_edges(ctx["bom"], ctx["slice_skus"])
    b2w = ctx["block_to_wave"]
    block_sku = dict(zip(b2w["block_id"], b2w["sku"]))

    # (item_code, block) -> [lot_id]: which lot(s) serve this item for this block.
    serving = defaultdict(list)
    for row in lots.itertuples():
        for blk in json.loads(row.member_blocks):
            serving[(row.item_code, blk)].append(row.lot_id)

    edges = set()
    for blk in ctx["slice_blocks"]:
        s = block_sku.get(blk)
        if s is None:
            continue
        for parent, child in pc.get(s, ()):                 # child produces -> parent consumes
            for cl in serving.get((child, blk), ()):
                for pl in serving.get((parent, blk), ()):
                    edges.add((cl, pl))
        for parent, gp in pg.get(s, ()):                    # parent -> green tyre
            for pl in serving.get((parent, blk), ()):
                for gl in serving.get((gp, blk), ()):
                    edges.add((pl, gl))
    edges = sorted(edges)

    n_pred, n_succ = defaultdict(int), defaultdict(int)
    for prod, cons in edges:
        n_pred[cons] += 1
        n_succ[prod] += 1

    deg = [{
        "lot_id": lid,
        "n_predecessors": n_pred[lid],
        "n_successors": n_succ[lid],
        "is_terminal": itype == common._GREEN_TYRE,
        "is_root": n_pred[lid] == 0,
    } for lid, itype in zip(lots["lot_id"], lots["item_type"])]
    degrees = pd.DataFrame(deg)

    ctx["dag_edges"] = pd.DataFrame(edges, columns=["producer_lot", "consumer_lot"])
    ctx["degrees"] = degrees
    ctx["dag_edges"].to_csv(os.path.join(ctx["outputs2_dir"], "phase3_dag_edges_updated.csv"), index=False)
    degrees.to_csv(os.path.join(ctx["outputs2_dir"], "phase3_lot_degrees_updated.csv"), index=False)

    print(f"[phase3] edges={len(edges)} | terminals={int(degrees['is_terminal'].sum())} "
          f"| roots={int(degrees['is_root'].sum())}")
    return ctx
