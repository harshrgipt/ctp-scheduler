"""
phase1b — demand explosion (MG-agnostic multi-level BOM walk).

For each schedulable SKU, walk its BOM sub-tree from q0 = 1 NOS multiplying
child_quantity along every Parent->child edge to get a per-tyre demand rate for
every intermediate item. The per-tyre rate is cached per SKU, then scaled by each
curing block's qty and consolidated. Length items are normalised to MM.
"""
from __future__ import annotations
import os
from collections import defaultdict, deque
import pandas as pd

import common


def _per_tyre_rate(sub: pd.DataFrame) -> dict:
    """rate[item] = quantity consumed per finished tyre.

    The CTP BOM is already fully *exploded*: child_quantity is the absolute
    per-tyre quantity of the child (verified by mass conservation — a parent's
    children sum to the parent's own quantity, and Parent_qty equals the parent's
    incoming child_quantity). So the per-tyre rate is child_quantity taken
    DIRECTLY (summed over an item's occurrences within the SKU), NOT the product
    along the path — multiplying re-inflates by every ancestor's quantity and was
    the source of the trillion-unit demand blow-up.
    """
    rate = defaultdict(float)
    parents, children = set(), set()
    for p, c, q in zip(sub["Parent"], sub["child"], sub["child_quantity"]):
        if c:
            children.add(c)
        if p:
            parents.add(p)
        if c and q is not None and not pd.isna(q) and float(q) > 0:
            rate[c] += float(q)
    # Roots appear as a Parent but never as a child — the top assemblies
    # (green tyre / carcass / tread / belts / apex): 1 unit per tyre.
    for r in (parents - children):
        if r and r not in rate:
            rate[r] = 1.0
    rate.pop("", None)
    rate.pop(None, None)
    return {k: v for k, v in rate.items() if v > 0}


# UOM classification. LENGTH units carry a value that must be normalised to MM.
# MT is a metric TONNE (mass) — explicitly NOT a length, so it never gets the x1000
# length scale; MTR/M are metres (length) and do. KG is mass; everything else -> NOS.
_LENGTH_UNITS_MM = {"MM"}            # already millimetres, scale 1.0
_LENGTH_UNITS_M = {"M", "MTR"}       # metres, scale 1000.0 -> MM
_MASS_UNITS = {"KG", "MT", "MTON", "TONNE", "TON"}


def _unit_classes(bom: pd.DataFrame) -> tuple[dict, dict]:
    """Build (unit_map, length_scale) in one pass and DETECT ambiguous codes.

    A code appearing with >1 distinct child_Unit is a data gap: first-occurrence-wins
    silently hides it. We collect every distinct unit per code, warn on conflicts, and
    resolve deterministically (first sorted unit) rather than by row order.
    """
    seen = defaultdict(set)          # code -> {UPPER child_Unit, ...}
    for c, u in zip(bom["child"], bom["child_Unit"]):
        if not c:
            continue
        seen[c].add(str(u).strip().upper())

    conflicts = {c: sorted(us) for c, us in seen.items() if len(us) > 1}
    if conflicts:
        import sys
        listing = "; ".join(f"{c}={us}" for c, us in sorted(conflicts.items())[:12])
        msg = (f"[phase1b][WARN] {len(conflicts)} code(s) appear with >1 distinct "
               f"child_Unit (data gap; resolved deterministically, NOT first-win): "
               f"{listing}")
        print(msg, file=sys.stderr)
        print(msg)

    unit_map, lscale = {}, {}
    for c, us in seen.items():
        uu = sorted(us)[0]           # deterministic pick (not row order)
        if uu in _LENGTH_UNITS_MM or uu in _LENGTH_UNITS_M:
            unit_map[c] = "MM"
            lscale[c] = 1000.0 if uu in _LENGTH_UNITS_M else 1.0
        elif uu in _MASS_UNITS:
            unit_map[c] = "KG"       # MT (tonne) demand still tracked in KG-family, NO length scale
            lscale[c] = 1.0
        else:
            unit_map[c] = "NOS"
            lscale[c] = 1.0
    return unit_map, lscale


def run(ctx: dict, cfg: dict) -> dict:
    bom = ctx["bom"]
    drum = ctx["drum"]
    itype_map = ctx["itype_map"]
    unit_map, lscale = _unit_classes(bom)             # UOM + metres->mm scale, conflict-detected

    skus = ctx["slice_skus"]
    bom_by_sku = {s: bom[bom["Super_parent"] == s] for s in skus}
    rate_by_sku = {s: _per_tyre_rate(sub) for s, sub in bom_by_sku.items()}

    prod = drum[(~drum["is_occupancy"]) & (drum["sku"].isin(skus))
                & (~drum["block_id"].isin(ctx["bad_cure_blocks"]))]

    rows = []
    for blk, sku, qty in zip(prod["block_id"], prod["sku"], prod["qty"]):
        for item, rate in rate_by_sku.get(sku, {}).items():
            rows.append((item, itype_map.get(item, "UNKNOWN"), sku, blk,
                         rate * float(qty) * lscale.get(item, 1.0),
                         unit_map.get(item, "NOS")))

    demand = pd.DataFrame(rows, columns=["item_code", "item_type", "sku",
                                         "block_id", "demand_qty", "demand_uom"])
    ctx["demand"] = demand
    demand.to_csv(os.path.join(ctx["outputs2_dir"], "phase1_demand_updated.csv"), index=False)

    consolidated = demand.groupby("item_code")["demand_qty"].sum().reset_index()
    print(f"[phase1b] SKUs={len(skus)} blocks={prod.shape[0]} "
          f"demand rows={len(demand)} distinct items={demand['item_code'].nunique()}")
    return ctx
