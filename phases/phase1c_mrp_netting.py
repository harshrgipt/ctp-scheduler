#!/usr/bin/env python3
"""
phase1c_mrp_netting.py - TIME-AWARE MRP cascading WIP netting (fast).

PRINCIPLE
  WIP at an item saves the item AND its upstream chain — but ONLY for demand
  consumed within the item's max_aging window. Beyond that window, demand
  must be produced fresh (WIP would age out).

ALGORITHM (vectorized topological cascade)
  1. Build BOM topo order (finished good -> raw materials).
  2. For each item I, compute orig_in_window = sum of demand rows where
     need_by <= plan_start + max_aging[I].
  3. Walk items in topo order:
       after_upstream = orig_in_window[I] - cascaded_reduction_in_window[I]
       wip_used = min(after_upstream, WIP[I])
       total_saved_iw = orig_in_window - (after_upstream - wip_used)
       for each child c: cascaded_reduction_in_window[c] += total_saved_iw * per_unit
  4. Apply per-item ratio to demand DataFrame rows in-window only.
     Out-of-window rows stay at original (must be produced fresh).

PERF: ~4 seconds end-to-end on ~840K demand rows + 3K items.

OUTPUT
  outputs2/phase1_demand_NET_updated.csv.gz - same schema as phase1_demand:
    item_code, sku, block_id, MG, need_by, demand_qty, demand_uom
"""
from __future__ import annotations
import sys, pathlib, yaml, time, os
# Allow `import db_loader` from the same phases/ folder
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import pandas as pd
import numpy as np
from collections import defaultdict, deque

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT/"inputs"
OUTPUTS = ROOT/"outputs2"
OUTPUTS.mkdir(parents=True, exist_ok=True)
PRIOR_OUTPUTS = ROOT/"outputs"

DEFAULT_MAX_H = 72


def _read(p):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(p, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(p)

def _db_or_csv(_k, _cfg=None):
    """DB-aware master loader (auto-inserted by DB-only migration)."""
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_k, _cfg)
    except Exception:
        _fp = (_cfg or {}).get("files", {})
        _fname = _fp.get(_k)
        if _fname is None:
            raise
        return _read(INPUTS / _fname)


def load_cfg(): return yaml.safe_load(open(ROOT/"config.yaml"))


def aging_h(v, u):
    try: v = float(v)
    except (TypeError, ValueError): return 0.0
    u = str(u or "").upper()
    if u.startswith("DAY"): return v*24.0
    if u.startswith("MIN"): return v/60.0
    return v


def main():
    print("\n" + "=" * 65)
    print("  PHASE 1c - Time-Aware MRP Cascade (FAST vectorized)")
    print("=" * 65)
    t0 = time.time()
    cfg = load_cfg()
    fp = cfg["files"]

    # Plan start (DB-first via _db_or_csv)
    plan = _db_or_csv("plan", cfg)
    plan["startTime"] = pd.to_datetime(plan["startTime"], errors="coerce")
    plan_start = plan["startTime"].min()
    if pd.isna(plan_start):
        plan_start = pd.Timestamp("2026-05-01 07:00:00")
    print(f"  plan_start: {plan_start}")

    # === Load demand (Phase 1b output) ===
    csv_gz = OUTPUTS / "phase1_demand_updated.csv.gz"
    csv = OUTPUTS / "phase1_demand_updated.csv"
    # Pick the FRESHEST (Phase 1b writes .csv when small, .gz when large;
    # an old .gz from a prior big run must not shadow today's .csv).
    cands = [p for p in (csv, csv_gz) if p.exists()]
    if not cands:
        print(f"  ERROR: phase1_demand_updated.csv(.gz) not found"); return 2
    src = max(cands, key=lambda p: p.stat().st_mtime)
    print(f"  demand source: {src.name}")
    demand = pd.read_csv(src)
    demand["need_by"] = pd.to_datetime(demand["need_by"], errors="coerce")
    demand["demand_qty"] = pd.to_numeric(demand["demand_qty"], errors="coerce").fillna(0)
    demand["hours_from_start"] = (demand["need_by"] - plan_start).dt.total_seconds() / 3600
    print(f"  [{time.time()-t0:.1f}s] demand rows: {len(demand):,}")

    # === Aging master ===
    aging = _db_or_csv("aging_master", cfg)
    aging["max_h"] = aging.apply(lambda r: aging_h(r.get("MaxAging",""), r.get("MaxAgingUnit","")),
                                  axis=1)
    max_aging_map = dict(zip(aging["ItemCode"].astype(str).str.strip(), aging["max_h"]))
    print(f"  [{time.time()-t0:.1f}s] aging map: {len(max_aging_map):,} items")

    # === BOM edges (parent -> [(child, per_unit_qty)]) ===
    bom = _db_or_csv("bom", cfg)
    def _bom_col(*names):
        for n in names:
            if n in bom.columns:
                return n
        raise KeyError(f"BOM missing any of {names}; has {list(bom.columns)}")
    # DB uses spaces ("child quantity"); CSV uses underscores ("child_quantity").
    _par = _bom_col("Parent", "parent")
    _ch  = _bom_col("child", "Child")
    _qty = _bom_col("child_quantity", "child quantity", "childQuantity")
    bom["__PAR__"] = bom[_par].astype(str).str.strip()
    bom["__CH__"]  = bom[_ch].astype(str).str.strip()
    bom["__QTY__"] = pd.to_numeric(bom[_qty], errors="coerce").fillna(0.0)
    bom_f = bom[(bom["__QTY__"] > 0) & (bom["__PAR__"] != "") & (bom["__CH__"] != "")]
    edges = defaultdict(list)
    all_items = set()
    for par, ch, q in zip(bom_f["__PAR__"], bom_f["__CH__"], bom_f["__QTY__"]):
        edges[par].append((ch, q))
        all_items.add(par); all_items.add(ch)
    print(f"  [{time.time()-t0:.1f}s] BOM edges: {sum(len(v) for v in edges.values()):,}  items: {len(all_items):,}")

    # === WIP (plant's daily 7-AM inventory snapshot) ===
    # Switched from old WIP_simulation_May2026.csv to inventory_combined
    # (plant script writes xlsx daily). load_wip_by_item handles both CSV
    # and DB sources via cfg["data_source"].
    wip_by_item = {}
    try:
        from db_loader import load_wip_by_item
        wip_by_item = load_wip_by_item(cfg)
    except Exception as _e:
        # Fallback: old WIP file if present
        wip_path = OUTPUTS / "WIP_simulation_May2026.csv"
        if wip_path.exists():
            wip = pd.read_csv(wip_path)
            wip_by_item = wip.groupby("itemcode")["availableinventory"].sum().to_dict()
        else:
            print(f"  [WARN] Inventory load failed ({_e}); proceeding with no WIP")
    print(f"  [{time.time()-t0:.1f}s] WIP items: {len(wip_by_item):,}")

    # === Topological order (finished good -> raw material) ===
    in_deg = defaultdict(int)
    for par, kids in edges.items():
        for ch, _ in kids: in_deg[ch] += 1
    for it in all_items:
        if it not in in_deg: in_deg[it] = 0
    queue = deque([it for it in all_items if in_deg[it] == 0])
    topo_order = []
    local_in_deg = dict(in_deg)
    while queue:
        u = queue.popleft()
        topo_order.append(u)
        for ch, _ in edges.get(u, []):
            local_in_deg[ch] -= 1
            if local_in_deg[ch] == 0: queue.append(ch)
    for it in all_items:
        if it not in topo_order: topo_order.append(it)  # cycle safeguard
    print(f"  [{time.time()-t0:.1f}s] topo: {len(topo_order):,}")

    # === In-window vs out-of-window demand per item ===
    orig_total = demand.groupby("item_code")["demand_qty"].sum().to_dict()
    demand["max_h"] = demand["item_code"].map(max_aging_map).fillna(DEFAULT_MAX_H)
    demand["is_in_window"] = demand["hours_from_start"] <= demand["max_h"]
    in_window_demand = demand[demand["is_in_window"]].groupby("item_code")["demand_qty"].sum().to_dict()
    print(f"  [{time.time()-t0:.1f}s] in-window demand computed")

    # === Topological cascade ===
    reduction_in_window = defaultdict(float)
    saving_per_item = {}
    for item in topo_order:
        orig_iw = in_window_demand.get(item, 0.0)
        if orig_iw <= 0:
            saving_per_item[item] = 0.0
            continue
        after_upstream = max(0.0, orig_iw - reduction_in_window.get(item, 0.0))
        wip_q = wip_by_item.get(item, 0.0)
        wip_used = min(after_upstream, wip_q)
        net_iw = after_upstream - wip_used
        total_saved_iw = orig_iw - net_iw
        saving_per_item[item] = total_saved_iw
        for ch, per_unit in edges.get(item, []):
            reduction_in_window[ch] += total_saved_iw * per_unit
    print(f"  [{time.time()-t0:.1f}s] cascade done")

    # === Apply savings to demand DF (in-window rows only) ===
    sav = pd.Series(saving_per_item)
    orig_iw_s = pd.Series(in_window_demand)
    demand["saving"] = demand["item_code"].map(sav).fillna(0.0)
    demand["orig_iw"] = demand["item_code"].map(orig_iw_s).fillna(0.0)
    demand["ratio"] = 1.0
    mask = demand["is_in_window"] & (demand["orig_iw"] > 0)
    demand.loc[mask, "ratio"] = (demand.loc[mask, "orig_iw"] - demand.loc[mask, "saving"]) / demand.loc[mask, "orig_iw"]
    demand.loc[demand["ratio"] < 0, "ratio"] = 0
    demand["demand_qty"] = (demand["demand_qty"] * demand["ratio"]).round(4)
    demand = demand[demand["demand_qty"] > 0]
    demand = demand.drop(columns=["max_h","is_in_window","saving","orig_iw","ratio","hours_from_start"])

    out_path = OUTPUTS / "phase1_demand_NET_updated.csv.gz"
    demand.to_csv(out_path, index=False, compression={"method":"gzip","compresslevel":1})
    print(f"  [{time.time()-t0:.1f}s] saved {out_path.name}  rows={len(demand):,}")

    # === Reporting ===
    itype = _db_or_csv("itemtype_master", cfg)
    itype_map = dict(zip(itype["ItemCode"].astype(str).str.strip(),
                          itype["ItemType"].astype(str).str.strip()))
    orig_df = pd.read_csv(src)
    orig_df["item_type"] = orig_df["item_code"].map(itype_map).fillna("(unknown)")
    demand["item_type"] = demand["item_code"].map(itype_map).fillna("(unknown)")

    try:
        gross_q = pd.to_numeric(orig_df["demand_qty"], errors="coerce").fillna(0).sum()
        net_q   = pd.to_numeric(demand["demand_qty"], errors="coerce").fillna(0).sum()
        saved   = gross_q - net_q
        print(f"\n  MRP NETTING SUMMARY")
        print(f"    gross rows={len(orig_df):,}  ->  net rows={len(demand):,}")
        print(f"    gross qty={gross_q:,.0f}  net qty={net_q:,.0f}  "
              f"saved={saved:,.0f} ({100*saved/max(gross_q,1):.1f}%)")
        by = (demand.groupby("item_type")["demand_qty"].sum()
                    .sort_values(ascending=False).head(10))
        print("    top item_types by NET demand:")
        for k, v in by.items():
            print(f"      {str(k)[:28]:28s} {v:,.0f}")
    except Exception as _re:
        print(f"  (reporting skipped: {_re})")

    print(f"  [{time.time()-t0:.1f}s] PHASE 1c complete  ->  {out_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())