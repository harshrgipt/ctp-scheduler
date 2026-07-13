#!/usr/bin/env python3
"""
phase2_lot_sizing.py — WAVE-AWARE lot sizing (V6_wave).

NEW APPROACH (vs old span_h sizer):
  1. Each curing block is tagged with a wave_id (Phase 1.5).
  2. For each item I:
       max_carry_waves = floor(max_aging[I] / wave_duration_days)
       Walk demand wave-by-wave, group consecutive waves up to
       (max_carry_waves + 1) waves where the resulting span ≤ max_aging.
       Each group becomes ONE lot (subject to MPQ + lot-size cap).
  3. MPQ floor applied (overproduce surplus when raw < mpq_min).
  4. LOT-SIZE CAP: if lot duration > max_lot_duration_h, split into N sub-lots
     of ≤ max_lot_duration_h each. Sub-lots share first/last_need.
  5. FRC special sizer (BELT items): wire campaigns per wave (8000 m mandatory).
     NON-BELT FRC items (Ply, Cap-strip carrier, etc.) sized as regular items —
     they will be placed during 6h+ cool-down windows by Phase 5.

OUTPUT
  outputs2/phase2_lots_updated.csv
  outputs2/phase2_lot_blocks_updated.csv.gz
  outputs2/phase2_lot_skus_updated.csv
  outputs2/phase2_lots_updated.xlsx
"""
from __future__ import annotations
import sys, pathlib, yaml, math, re, os
import pandas as pd
from collections import defaultdict
import shutil

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS = ROOT/"inputs"
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

# --- staging helpers ---
def _ensure_writable(p):
    """OneDrive sync conflicts can leave a directory at a file's path (with the
    actual file trapped inside). If that's the case, nuke the directory before
    pandas writes — otherwise to_csv silently writes to the wrong path and
    downstream phases read stale data from a previous run."""
    p = pathlib.Path(p)
    if p.is_dir():
        print(f"  WARNING: {p} exists as a directory (OneDrive sync conflict). "
              f"Removing before write.")
        shutil.rmtree(p, ignore_errors=True)


def _stage_csv(df, name, big_threshold_rows=200000):
    is_big = len(df) > big_threshold_rows
    write_name = _suff(name + ".gz" if is_big else name)
    p = OUTPUTS / write_name
    _ensure_writable(p)
    if is_big:
        # Use compression level 1 (fastest) to avoid timeouts on big block files
        df.to_csv(p, index=False, compression={"method":"gzip","compresslevel":1})
    else:
        df.to_csv(p, index=False)
    return p


def _stage_xlsx_sheets(sheets, name):
    p = OUTPUTS / _suff(name)
    _ensure_writable(p)
    with pd.ExcelWriter(p, engine="openpyxl") as w:
        for k, df in sheets.items():
            clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
            df.to_excel(w, sheet_name=clean, index=False)
    return p


def _db_or_csv(_k, _cfg=None):
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_k, _cfg)
    except Exception:
        _fp = (_cfg or {}).get('files', {})
        return _read(INPUTS / _fp[_k])


def _read(p):
    for enc in ("utf-8","latin-1"):
        try: return pd.read_csv(p, encoding=enc, engine="python",
                                on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError: continue
    raise IOError(p)


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


def build_itype_map(it):
    return {str(r["ItemCode"]).strip(): str(r["ItemType"]).strip()
            for _, r in it.iterrows() if pd.notna(r.get("ItemCode"))}


def build_mpq_map(mpq):
    """Returns: itype -> (mpq_min, mpq_max, uom, fraction_allowed)
    mpq_max is the MHE capacity — used to SPLIT lots into MHE-sized sub-lots."""
    out = {}
    for _, r in mpq.iterrows():
        t = str(r["Item Type"]).strip()
        if not t: continue
        try: mn = float(r["Minimum Run Qty"])
        except (TypeError, ValueError): mn = None
        try: mx = float(r["Maximum Run Qty"])
        except (TypeError, ValueError, KeyError): mx = None
        uom = str(r.get("UOM", "")).strip().upper()
        frac_str = str(r.get("Fraction Allowed", "NO")).strip().upper()
        fraction_allowed = frac_str in ("YES","Y","TRUE","1")
        out[t] = (mn, mx, uom, fraction_allowed)
    return out


def build_belt_wire_map(bw_df):
    out = {}
    if bw_df is None or bw_df.empty: return out
    for _, r in bw_df.iterrows():
        b = str(r.get("belt_item", "")).strip()
        w = str(r.get("wire_type", "")).strip()
        if b and w:
            out[b] = w
    return out


def parse_machines(s: str):
    if not s: return []
    out = []
    for tok in str(s).split(","):
        m = tok.strip().strip("'\"")
        if m and m.upper() not in ("NAN", "NONE", ""):
            out.append(m)
    return out


def build_routing_meta(rt):
    out = {}
    for _, r in rt.iterrows():
        rp = str(r["routed_product"]).strip()
        if not rp or rp in out: continue
        try: pt = float(r["proc_time"])
        except (TypeError, ValueError): pt = 0.0
        try: bs = float(r["batch_size"])
        except (TypeError, ValueError): bs = 0.0
        # ═══ CTP DEVIATION #1 of 2 — EFFICIENCY FROM THE ROUTING ═══════════════════
        # v6 hardcoded `eff = 1.0` ("100% efficiency override (per user spec)") and
        # IGNORED this column. CTP's routing states efficiency = 0.96, and the user
        # instructed: "use the efficiency from the routing as well".
        # `eff` DIVIDES in op_duration_min, so 0.96 makes every operation take
        # 1/0.96 = +4.17% longer. That is the intended meaning of efficiency.
        # NaN-safe: a blank must fall back to 1.0, NOT become NaN (see MEMORY.md,
        # "present-but-blank" — float(NaN) does not raise, and NaN <= 0 is False).
        try: eff = float(r["efficiency"])
        except (TypeError, ValueError, KeyError): eff = 1.0
        if not (eff == eff) or eff <= 0: eff = 1.0        # NaN or non-positive -> 1.0
        # ═══ CTP DEVIATION #2 of 2 — TRANSFER TIME FROM THE ROUTING ════════════════
        # v6 NEVER reads transfer_time_min (zero references in all 9 phases). The user
        # instructed: "there is a transfer time column in that, make change in the code
        # according, always use that". It is a mandatory lag between a producer
        # finishing and its consumer being able to start — the same SHAPE as min_aging,
        # so it is added to min_aging at the single funnel in phase4 (get_min_aging) and
        # phase5 (_ages_ns). It is carried down the pipeline as a lot column below.
        # NOT added to max_aging: that is a shelf-life CEILING, not a lag.
        try: tt = float(r["transfer_time_min"])
        except (TypeError, ValueError, KeyError): tt = 0.0
        if not (tt == tt) or tt < 0: tt = 0.0             # NaN or negative -> 0.0
        machines = parse_machines(str(r["machines"]))
        out[rp] = {
            "proc_time":  pt,
            "proc_uom":   str(r["proc_time_UOM"]).strip().upper(),
            "batch_size": bs,
            "batch_uom":  str(r["batch_UNIT"]).strip().upper(),
            "efficiency": max(0.01, eff),
            "transfer_time_min": tt,
            "machines":   machines,
            "operation":  str(r["operation_name"]).strip(),
            "department": str(r["department"]).strip(),
            "equipment":  str(r["Equipment"]).strip(),
        }
    return out


def op_duration_min(qty, meta, item_code=None, lot_uom=None):
    """Compute operation duration in minutes for `qty` of `item_code`.
    Assumes lot_qty UOM matches routing proc_time UOM (e.g., NOS lot + NOS/MIN rate).
    Item-code argument kept for future routing-derived overrides; currently unused.
    """
    pt, u, bs, eff = meta["proc_time"], meta["proc_uom"], meta["batch_size"], meta["efficiency"]
    try: qty = float(qty)
    except (TypeError, ValueError): return 0.0
    if not (qty == qty) or qty <= 0: return 0.0
    if pt <= 0: return 0.0
    if u in ("MM/MIN", "M/MIN", "MTR/MIN"):       return (qty / pt) / eff
    if u == "NOS/MIN":                            return (qty / pt) / eff
    if u == "SEC/BATCH" and bs > 0:               return math.ceil(qty / bs) * pt / 60.0 / eff
    if u == "SEC" and bs > 0:                     return (qty / bs) * pt / 60.0 / eff
    if u == "SEC":                                return (qty * pt) / 60.0 / eff
    if u == "MIN":                                return (qty * pt) / eff
    if u.endswith("/MIN"):                        return (qty / pt) / eff
    return (qty * pt) / eff


LOT_COLUMNS = [
    "lot_id","item_code","item_type","operation","department","equipment",
    "raw_demand","lot_qty","lot_uom","surplus",
    "duration_min","duration_hours",
    "first_need","last_need","n_blocks","n_skus",
    "min_aging_h","max_aging_h",
    "transfer_time_h",                      # CTP DEVIATION — routing transfer_time_min / 60
    "mpq_min","mpq_uom","fraction_allowed",
    "is_belt","machines","n_machines",
    "wire_type","campaign_id","is_campaign_start","is_campaign_end",
    # NEW: wave attrs
    "wave_ids","wave_first","wave_last",
    # NEW: MHE attrs (Phase 5 uses these for per-MHE material flow)
    "batch_id","mhe_index","mhe_total",
    # lot -> SKU set: written to phase2_lot_skus.csv so Phase 3 can match
    # producer<->consumer by SKU (must be regenerated each run, else GT/Carcass
    # sub-lots renumber and the DAG loses their edges).
    "skus_set",
]


def _empty_camp_fields():
    return {"wire_type":"", "campaign_id":"",
            "is_campaign_start": False, "is_campaign_end": False}


WAVE_DURATION_DAYS = 3.0

# === FIX #2 v3 (REVERTED to safe scope) SUB-WAVE BUCKETING ===
# Items with max_aging shorter than the wave (72h) need finer-grained batches.
# Lesson learned: sub-bucketing FC items (24h buckets) created 39% MORE FC lots,
# adding 12h of changeover overhead on mixer 0203 -> FC supply slipped 2 days
# -> cascaded through FRC -> cutters -> building -> horizon overrun.
#
# Reverted to: only short-aging items (max_aging < 48h) get sub-bucketed.
# FC items (max_aging 72-96h) keep their original 72h wave bucket — fewer
# mixer changeovers, smoother FC supply.
SHORT_AGING_THRESHOLD_H  = 48.0   # apply 12h sub-bucketing for items < 48h
SUB_WAVE_BUCKET_H_SHORT  = 12.0   # bucket size for items with max_aging < 48h
SUB_WAVE_BUCKET_H_MEDIUM = 24.0   # bucket size (daily) for all other non-mixing items
SUB_WAVE_SAFETY_H        = 8.0    # safety inside max_aging when checking span

# === HYBRID WAVE GRANULARITY (mixing=3-day, others=1-day) ===
# Plant decision: keep MIXING (compounds) batched at the 3-day wave for Banbury
# campaign efficiency (and because sub-bucketing FINAL COMPOUND historically slipped
# the mixer -> horizon overrun). Pace EVERYTHING ELSE to DAILY (24h) buckets so
# short-life components are produced fresh against the smooth daily curing and the
# building runs smaller, more frequent, multi-SKU days. Costs more changeovers
# downstream (within budget), buys component freshness + smoother building.
MIXING_ITYPES = {"MASTER COMPOUND", "FINAL COMPOUND"}


def size_lots_waveaware(demand, wave_map, rt_meta, aging_map, itype_map, mpq_map, cfg,
                          skip_items=None):
    """Wave-bucketed lot sizing — FAST numpy-array implementation."""
    import numpy as np
    skip_items = skip_items or set()
    max_dur_min = cfg["max_lot_duration_h"] * 60.0
    wave_dur_h = WAVE_DURATION_DAYS * 24

    lots = []
    block_assignments = []

    # Attach wave_id and sort once
    demand = demand.copy()
    demand["wave_id"] = demand["block_id"].map(wave_map).fillna("W99")
    demand = demand.sort_values(["item_code","need_by"]).reset_index(drop=True)

    # ---------------- WIP SUBTRACTION (V6_wave) ----------------
    # Load plant's daily 7-AM inventory snapshot. Switched from old
    # WIP_simulation_May2026.csv to inventory_combined (via db_loader).
    # FIFO: earliest-need demand rows are covered first.
    wip_by_item = {}
    try:
        import sys as _sys
        _sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        from db_loader import load_wip_by_item
        wip_by_item = load_wip_by_item(cfg)
    except Exception as _e:
        # Fallback: legacy WIP file if present
        wip_path = OUTPUTS / "WIP_simulation_May2026.csv"
        if wip_path.exists():
            wip = pd.read_csv(wip_path)
            for _, r in wip.iterrows():
                item = str(r["itemcode"]).strip()
                try:
                    q = float(r["availableinventory"])
                except Exception:
                    continue
                wip_by_item[item] = wip_by_item.get(item, 0.0) + q
        else:
            print(f"  [WARN] Inventory load failed ({_e}); proceeding with no WIP")
    if wip_by_item:
        print(f"  WIP loaded: {len(wip_by_item):,} items, {sum(wip_by_item.values()):,.0f} units (pre-cap)")

        # FIX 3: ENFORCE PLANT-WIDE FLOOR CAPS per item-type (plant physical limits)
        # Plant cannot store more than 4,000 Carcass NOS or 10,000 GT NOS at once.
        # Cap individual item-types at the plant total, allocated PROPORTIONALLY to
        # each item's current WIP share.
        PLANT_ITEM_TYPE_CAPS = {
            "Carcass":     4000.0,    # plant floor space hard cap
            "Green Tyres": 10000.0,   # plant floor space hard cap
        }
        try:
            _itype = _db_or_csv("itemtype_master", cfg)
            itype_map = dict(zip(_itype["ItemCode"].astype(str).str.strip(),
                                  _itype["ItemType"].astype(str).str.strip()))
            for itype_name, cap_total in PLANT_ITEM_TYPE_CAPS.items():
                # Find all WIP items of this item-type
                items_of_type = {it: q for it, q in wip_by_item.items()
                                 if itype_map.get(it, "") == itype_name}
                if not items_of_type:
                    continue
                cur_total = sum(items_of_type.values())
                if cur_total <= cap_total:
                    print(f"  PLANT_CAP[{itype_name}]: WIP {cur_total:,.0f} <= cap {cap_total:,.0f}  (no clip)")
                    continue
                # Clip proportionally
                scale = cap_total / cur_total
                clipped = 0.0
                for it, q in items_of_type.items():
                    new_q = q * scale
                    wip_by_item[it] = new_q
                    clipped += (q - new_q)
                print(f"  PLANT_CAP[{itype_name}]: clipped {cur_total:,.0f} -> {cap_total:,.0f}  (removed {clipped:,.0f} phantom WIP)")
        except Exception as e:
            print(f"  PLANT_CAP step skipped (no itemtype_master / error): {e}")

        # FIX 4: Cap MASTER COMPOUND WIP at <= 50% of its CASCADED demand.
        # MC was previously set to 10x demand (effectively infinite). After Phase 1c
        # MRP cascade, MC WIP showed 575M vs cascaded demand 4.85M (118x over) which
        # nullifies all MC production -> upstream feeders break. Now cap MC per-item
        # at half of its remaining demand in phase1_demand_NET.
        try:
            net_path = OUTPUTS / "phase1_demand_NET_updated.csv.gz"
            if net_path.exists():
                _net = pd.read_csv(net_path)
                mc_items = {it for it, t in itype_map.items() if t == "MASTER COMPOUND"}
                mc_demand_by_item = _net[_net["item_code"].isin(mc_items)].groupby("item_code")["demand_qty"].sum().to_dict()
                clipped_mc = 0.0
                n_clipped = 0
                for it in list(wip_by_item.keys()):
                    if itype_map.get(it, "") != "MASTER COMPOUND":
                        continue
                    demand_qty = mc_demand_by_item.get(it, 0.0)
                    mc_cap = max(0.0, demand_qty * 0.5)
                    if wip_by_item[it] > mc_cap:
                        clipped_mc += (wip_by_item[it] - mc_cap)
                        wip_by_item[it] = mc_cap
                        n_clipped += 1
                if n_clipped > 0:
                    print(f"  PLANT_CAP[MASTER COMPOUND]: capped {n_clipped} items at 50% of cascaded demand "
                          f"(removed {clipped_mc:,.0f} phantom WIP)")
        except Exception as e:
            print(f"  MC WIP cap skipped: {e}")
        print(f"  WIP after plant caps: {len(wip_by_item):,} items, {sum(wip_by_item.values()):,.0f} units")

    # Check if NET demand file exists - if so, MRP cascade already handled WIP
    _skip_wip_sub = (OUTPUTS / "phase1_demand_NET_updated.csv.gz").exists()
    if wip_by_item and not _skip_wip_sub:
        import numpy as np
        orig_by_item = demand.groupby("item_code")["demand_qty"].sum().to_dict()
        # Vectorized FIFO subtraction:
        # Within each item (already sorted by need_by), compute cumulative qty
        # and subtract WIP. Rows whose cumsum <= wip are fully covered (=0).
        # The "straddling" row gets the residual.
        demand_qty_arr = demand["demand_qty"].fillna(0.0).to_numpy(dtype=float)
        item_arr = demand["item_code"].to_numpy()
        cumsum = pd.Series(demand_qty_arr).groupby(item_arr).cumsum().to_numpy()
        wip_arr = np.array([wip_by_item.get(it, 0.0) for it in item_arr], dtype=float)
        # Remaining demand BEFORE this row was needed at all
        prior_cum = cumsum - demand_qty_arr
        # WIP used by THIS row = max(0, min(demand_qty, wip - prior_cum))
        wip_for_row = np.maximum(0.0, np.minimum(demand_qty_arr, wip_arr - prior_cum))
        new_qty_arr = demand_qty_arr - wip_for_row
        n_zeroed = int(((demand_qty_arr > 0) & (new_qty_arr == 0)).sum())
        demand["demand_qty"] = new_qty_arr
        print(f"  After WIP subtraction: {n_zeroed:,} demand rows fully covered")
        n_pos = int((demand["demand_qty"] > 0).sum())
        print(f"  Remaining demand rows with qty > 0: {n_pos:,}")

        new_by_item = demand.groupby("item_code")["demand_qty"].sum().to_dict()
        audit_rows = []
        all_items = set(orig_by_item) | set(wip_by_item)
        for item in all_items:
            orig = orig_by_item.get(item, 0.0)
            new = new_by_item.get(item, 0.0)
            wip_avail = wip_by_item.get(item, 0.0)
            wip_used = max(0.0, min(orig, wip_avail))
            audit_rows.append({
                "item_code": item,
                "original_demand_qty": round(orig, 3),
                "wip_available": round(wip_avail, 3),
                "wip_used": round(wip_used, 3),
                "new_demand_qty": round(new, 3),
            })
        audit_df = pd.DataFrame(audit_rows).sort_values("wip_used", ascending=False)
        audit_path = OUTPUTS / _suff("phase2_wip_subtraction_audit.csv")
        _ensure_writable(audit_path)
        audit_df.to_csv(audit_path, index=False)
        print(f"  Wrote audit -> {audit_path.name}")

        demand = demand[demand["demand_qty"] > 0].reset_index(drop=True)
    # ---------------- END WIP SUBTRACTION ----------------
    # Convert critical columns to numpy arrays once
    all_items = demand["item_code"].values
    all_need_by = pd.to_datetime(demand["need_by"]).values.astype("datetime64[ns]")
    all_qty = pd.to_numeric(demand["demand_qty"], errors="coerce").fillna(0).values.astype(float)
    all_wave = demand["wave_id"].values
    all_block = demand["block_id"].values
    all_sku = demand["sku"].astype(str).values
    all_uom = demand["demand_uom"].astype(str).values

    # Find item boundaries
    item_change = np.r_[True, all_items[1:] != all_items[:-1]]
    item_starts = np.where(item_change)[0]
    item_ends   = np.r_[item_starts[1:], len(all_items)]

    for k in range(len(item_starts)):
        item = all_items[item_starts[k]]
        if item in skip_items: continue
        meta = rt_meta.get(item)
        if not meta: continue
        a, b = item_starts[k], item_ends[k]
        # Slice arrays for this item
        need_by = all_need_by[a:b]
        qty     = all_qty[a:b]
        wave_id = all_wave[a:b]
        block_id = all_block[a:b]
        sku_arr = all_sku[a:b]
        d_uom = all_uom[a]
        # Filter zero qty
        m = qty > 0
        if not m.any(): continue
        need_by = need_by[m]; qty = qty[m]; wave_id = wave_id[m]
        block_id = block_id[m]; sku_arr = sku_arr[m]

        itype = itype_map.get(item, "")
        # New 4-tuple: (min, max, uom, fraction_allowed). Backward compatible 3-tuple supported.
        mpq_info = mpq_map.get(itype, (None, None, "", False))
        if len(mpq_info) == 3:
            mpq_min, mpq_uom, fraction_allowed = mpq_info
            mpq_max = None
        else:
            mpq_min, mpq_max, mpq_uom, fraction_allowed = mpq_info
        if mpq_min is not None and mpq_min <= 0: mpq_min = None
        if mpq_max is not None and mpq_max <= 0: mpq_max = None

        mn_age, mx_age = aging_map.get(item, (0.0, 72.0))
        # === WAVE-CARRY (smooths production load by combining adjacent waves) ===
        # Items with max_aging significantly longer than wave_duration can absorb
        # later-wave demand into earlier-wave batches. This SHIFTS production load
        # backward in time, reducing late-wave concentration (W08/W09/W10 spike).
        #
        # Plant rule: producer batch span must fit within max_aging window so
        # late consumers don't see expired material (enforced by span check below).
        #   max_aging >= 3 * wave_duration_h (>=216h): combine up to 2 forward waves
        #   max_aging >= 2 * wave_duration_h (>=144h): combine up to 1 forward wave
        #   max_aging  <  2 * wave_duration_h (<144h): no carry (Carcass, FC 72-96h)
        if mx_age >= 3 * wave_dur_h:
            WAVE_CARRY_CAP = 2
        elif mx_age >= 2 * wave_dur_h:
            WAVE_CARRY_CAP = 1
        else:
            WAVE_CARRY_CAP = 0
        max_carry = min(WAVE_CARRY_CAP, max(0, int(mx_age // wave_dur_h)))
        max_window_ns = np.timedelta64(int(mx_age * 3600 * 1e9), "ns")

        # Build wave_ids_in_order (preserve chronological)
        seen_waves = []
        seen_set = set()
        for w in wave_id:
            if w not in seen_set:
                seen_waves.append(w); seen_set.add(w)
        wave_ids_in_order = seen_waves
        # Indices per wave
        wave_demand_idx = {}
        for wid in wave_ids_in_order:
            wave_demand_idx[wid] = np.where(wave_id == wid)[0]

        # Walk waves; build buckets of up to (max_carry+1) consecutive waves
        # while staying inside max_aging window
        i = 0
        i = 0
        seq = 1
        machines_str = ", ".join(meta["machines"])
        n_machines = len(meta["machines"])
        op_name = meta["operation"]
        dept = meta["department"]
        equip = meta["equipment"]

        # === FIX #2 v3 SUB-WAVE BUCKETING (short-aging only) ===
        # Only sub-bucket items with max_aging < 48h (Carcass, Cap Strip, R.Steel Belt).
        # FC items (72-96h) stay at wave granularity to keep mixer changeovers low.
        if mx_age > 0 and mx_age < SHORT_AGING_THRESHOLD_H:
            sub_wave_bucket_h = SUB_WAVE_BUCKET_H_SHORT     # 12h
            sub_wave_bucket_ns = np.timedelta64(int(sub_wave_bucket_h * 3600 * 1e9), "ns")
            use_sub_wave = True
        else:
            sub_wave_bucket_ns = None
            use_sub_wave = False

        while i < len(wave_ids_in_order):
            # Bucket waves [i..j) — extend while (j-i) <= max_carry and within aging window
            bucket_idx = np.array(wave_demand_idx[wave_ids_in_order[i]])
            first_need_arr = need_by[bucket_idx]
            first_need = first_need_arr[0]
            bucket_waves = [wave_ids_in_order[i]]
            j = i + 1
            while j < len(wave_ids_in_order) and (j - i) <= max_carry:
                wid = wave_ids_in_order[j]
                idx = wave_demand_idx[wid]
                last_in_wave = need_by[idx[-1]]
                if (last_in_wave - first_need) > max_window_ns:
                    break
                bucket_waves.append(wid)
                bucket_idx = np.concatenate([bucket_idx, idx])
                j += 1

            # FIX #2: For short-aging items, split bucket_idx into sub-buckets
            # by need_by time so each sub-bucket fits in (max_aging - safety).
            if use_sub_wave and len(bucket_idx) > 0:
                # bucket_idx is already chronologically ordered (demand was sorted by need_by).
                sub_buckets = []
                cur_sub = [bucket_idx[0]]
                cur_start_ns = need_by[bucket_idx[0]]
                for k_idx in bucket_idx[1:]:
                    if (need_by[k_idx] - cur_start_ns) > sub_wave_bucket_ns:
                        sub_buckets.append(np.array(cur_sub))
                        cur_sub = [k_idx]
                        cur_start_ns = need_by[k_idx]
                    else:
                        cur_sub.append(k_idx)
                if cur_sub:
                    sub_buckets.append(np.array(cur_sub))
            else:
                sub_buckets = [bucket_idx]

            # Inner loop: each sub-bucket becomes one batch.
            for sub_bucket_idx in sub_buckets:
                bucket_idx = sub_bucket_idx
                # Aggregate bucket
                qb = qty[bucket_idx]
                nb = need_by[bucket_idx]
                raw_qty = float(qb.sum())
                first_n = nb[0]
                last_n  = nb[-1]
                sku_set = set(sku_arr[bucket_idx].tolist())

                # MPQ floor (round up to MPQ-min if Fraction Allowed)
                if fraction_allowed and mpq_min:
                    lot_qty_full = math.ceil(max(raw_qty, mpq_min) / mpq_min) * mpq_min
                elif mpq_min:
                    lot_qty_full = max(raw_qty, mpq_min)
                else:
                    lot_qty_full = raw_qty

                # SPLIT by lot duration cap (max_dur_min)
                dur_full_min = op_duration_min(lot_qty_full, meta, item_code=item, lot_uom=d_uom)
                n_dur  = max(1, int(math.ceil(dur_full_min / max_dur_min)))
                mhe_total_batch = max(1, int(math.ceil(lot_qty_full / mpq_max))) if mpq_max else 1
                # === MHE-aligned sub-lots (CARCASS + GREEN TYRES ONLY) ===
                # CRITICAL: only Carcass and Green Tyres should split per-MHE because their
                # MHE cart = 20 NOS literally exists in the plant. For mixers (FC/MC), FRC
                # (mother rolls), Tread, Belt, etc. — production is BATCH (not MHE) so
                # over-splitting causes changeover explosion and saturates upstream machines.
                #
                # Past disaster: N_SUB_MAX=40 globally caused FRC to balloon from 750 lots
                # to 5,081 lots (97% util, finished 7/11 — +41 days past horizon).
                #
                # Items where MHE-per-sub-lot makes physical sense:
                #   Carcass, Green Tyres -> N_SUB_MAX_MHE (up to 40 per batch)
                # All other items keep the batch-level cap (N_SUB_MAX_DEFAULT = 6).
                N_SUB_MAX_DEFAULT = 6
                N_SUB_MAX_MHE     = 20   # enough for typical Carcass/GT batches (50-200 NOS)
                MHE_ALIGNED_ITYPES = ("Carcass", "Green Tyres")
                if itype in MHE_ALIGNED_ITYPES and mpq_max:
                    # 1 sub-lot per MHE — matches plant reality
                    n_subs = max(n_dur, min(mhe_total_batch, N_SUB_MAX_MHE))
                elif mpq_max:
                    # Other items with MPQ — keep original batch cap to avoid changeover explosion
                    n_subs = max(n_dur, min(mhe_total_batch, N_SUB_MAX_DEFAULT))
                else:
                    # No MPQ — split only by duration cap
                    n_subs = n_dur
                mhe_per_sub = max(1, int(math.ceil(mhe_total_batch / n_subs))) if mpq_max else 1
                sub_qty = lot_qty_full / n_subs
                sub_dur = op_duration_min(sub_qty, meta, item_code=item, lot_uom=d_uom)
                sub_raw = raw_qty / n_subs
                wave_str = ", ".join(bucket_waves)

                sub_ids = []
                batch_id = f"{item}_B{seq:04d}"
                # === DE-PULSE DEADLINES ===
                # The bucket spans the whole wave (e.g. 3 days of smooth demand). Stamping
                # every sub-lot with the bucket's earliest need (first_n = wave start) piles
                # 3 days of deadlines onto one day -> 60k/day spikes the line can't build.
                # Instead, spread each sub-lot's need_by across the demand it actually covers:
                # sub-lot s serves the qty slice [s*sub_raw, (s+1)*sub_raw], so its deadline is
                # the need_by at that cumulative-demand position. Lot SIZE/COUNT is unchanged
                # (no changeover explosion); only the deadlines fan out to the real curing days,
                # which also lands each lot closer to consumption (fresher).
                _cum = np.cumsum(qb) if len(qb) else np.array([0.0])
                _tot = float(_cum[-1]) if len(_cum) else 0.0
                def _need_at(frac_qty):
                    if _tot <= 0 or len(nb) == 0:
                        return first_n
                    pos = int(np.searchsorted(_cum, min(frac_qty, _tot)))
                    return nb[min(pos, len(nb) - 1)]
                for s in range(n_subs):
                    sub_id = f"{item}_L{seq:04d}"
                    seq += 1
                    sub_ids.append(sub_id)
                    _fn_s = _need_at(s * sub_raw)
                    _ln_s = _need_at(min((s + 1) * sub_raw, _tot))
                    lots.append({
                        "lot_id":          sub_id,
                        "item_code":       item,
                        "item_type":       itype,
                        "operation":       op_name,
                        "department":      dept,
                        "equipment":       equip,
                        "raw_demand":      round(sub_raw, 2),
                        "lot_qty":         round(sub_qty, 2),
                        "lot_uom":         d_uom,
                        "surplus":         round(sub_qty - sub_raw, 2),
                        "duration_min":    round(sub_dur, 2),
                        "duration_hours":  round(sub_dur/60, 3),
                        "first_need":      pd.Timestamp(_fn_s),
                        "last_need":       pd.Timestamp(_ln_s),
                        "n_blocks":        len(bucket_idx),
                        "n_skus":          len(sku_set),
                        "min_aging_h":     mn_age,
                        "max_aging_h":     mx_age,
                        "transfer_time_h": meta.get("transfer_time_min", 0.0) / 60.0,
                        "mpq_min":         mpq_min,
                        "mpq_uom":         mpq_uom,
                        "fraction_allowed": fraction_allowed,
                        "is_belt":         False,
                        "machines":        machines_str,
                        "n_machines":      n_machines,
                        "wire_type":       "",
                        "campaign_id":     "",
                        "is_campaign_start": False,
                        "is_campaign_end":   False,
                        "wave_ids":        wave_str,
                        "wave_first":      bucket_waves[0],
                        "wave_last":       bucket_waves[-1],
                        "batch_id":        batch_id,
                        "mhe_index":       s,
                        "mhe_total":       mhe_per_sub,
                        "skus_set":        ", ".join(sorted(str(x) for x in sku_set)),
                    })

                # Block-assignments: map only this sub-bucket's demand rows to sub-lots.
                # (FIX #2: each sub-bucket's batch feeds only its own demand window,
                # not the entire wave's demand.)
                n_sub = max(1, len(sub_ids))
                for sub_id in sub_ids:
                    for idx in bucket_idx:
                        block_assignments.append({
                            "lot_id":     sub_id,
                            "item_code":  item,
                            "block_id":   block_id[idx],
                            "sku":        sku_arr[idx],
                            "need_by":    pd.Timestamp(need_by[idx]),
                            "demand_qty": float(qty[idx]) / n_sub,
                        })

            i = j

    return pd.DataFrame(lots), pd.DataFrame(block_assignments)


def size_wire_campaigns_per_wave(demand, wave_map, rt_meta, aging_map, itype_map,
                                  belt_to_wire, cfg):
    """FRC wire-campaign sizing PER WAVE.

    For each wave, for each wire, produce 8000m mandatory campaigns (with pad).
    Belt lots within campaigns split by 200k-400k mm.
    Aging window: each lot's first_need to last_need bounded by max_aging.
    """
    LOT_MAX_MM  = float(cfg.get("frc_belt_lot_max_mm", 400000))
    LOT_MIN_MM  = float(cfg.get("frc_belt_lot_min_mm", 200000))
    CAMP_MAX_MM = float(cfg.get("frc_campaign_max_m", 8000)) * 1000.0
    PAD         = bool(cfg.get("frc_pad_to_floor", True))
    max_dur_min = cfg["max_lot_duration_h"] * 60.0

    if not belt_to_wire:
        return pd.DataFrame(columns=LOT_COLUMNS), pd.DataFrame()

    belt_items = set(belt_to_wire.keys())
    bd = demand[demand["item_code"].isin(belt_items)].copy()
    if bd.empty:
        return pd.DataFrame(columns=LOT_COLUMNS), pd.DataFrame()
    bd["wire_type"] = bd["item_code"].map(belt_to_wire)
    bd["demand_qty"] = pd.to_numeric(bd["demand_qty"], errors="coerce").fillna(0.0)
    bd = bd[bd["demand_qty"] > 0]
    if bd.empty:
        return pd.DataFrame(columns=LOT_COLUMNS), pd.DataFrame()
    bd["wave_id"] = bd["block_id"].map(wave_map).fillna("W99")

    lots_out = []
    block_assignments = []
    seq_per_item = defaultdict(int)

    # Per (wave, wire), build belt lots, then pack into campaigns of 8000m
    waves_sorted = sorted(bd["wave_id"].unique())
    for wave in waves_sorted:
        wave_demand = bd[bd["wave_id"] == wave]
        for wire, wg in wave_demand.groupby("wire_type", sort=False):
            wg = wg.sort_values("need_by").reset_index(drop=True)

            # Step 1: per-item raw lots within this wave (single item per lot)
            raw_lots = []
            for item, g in wg.groupby("item_code"):
                g = g.sort_values("need_by").reset_index(drop=True)
                meta = rt_meta.get(item)
                if not meta: continue
                mn_age, mx_age = aging_map.get(item, (0.0, 72.0))
                # All demand in one wave (≤3 days) is within max_aging (typically 96h)
                remain = g["demand_qty"].astype(float).values.copy()
                need_arr = g["need_by"].values
                blk_arr = g["block_id"].values
                sku_arr = g["sku"].values
                N = len(g)
                i = 0
                while i < N:
                    if remain[i] <= 1e-6: i += 1; continue
                    qty = 0.0
                    claimed = []
                    sku_set = set()
                    first = need_arr[i]; last = first
                    j = i
                    while j < N:
                        if remain[j] <= 1e-6: j += 1; continue
                        space = LOT_MAX_MM - qty
                        if space <= 1e-6: break
                        take = min(remain[j], space)
                        qty += take
                        claimed.append((blk_arr[j], sku_arr[j], need_arr[j], take))
                        sku_set.add(sku_arr[j])
                        last = need_arr[j]
                        remain[j] -= take
                        if remain[j] <= 1e-6: j += 1
                        if qty >= LOT_MAX_MM - 1e-6: break
                    if qty > 0:
                        # MPQ floor for belt
                        if qty < LOT_MIN_MM:
                            qty = LOT_MIN_MM
                        raw_lots.append({
                            "item": item, "wire": wire,
                            "raw": sum(c[3] for c in claimed), "qty": qty,
                            "claimed": claimed,
                            "first_need": first, "last_need": last,
                            "sku_set": sku_set,
                        })
                    i = j if j > i else i + 1

            if not raw_lots: continue

            # Step 2: pack into campaigns within this wave (each ≤ 8000m)
            raw_lots.sort(key=lambda x: x["first_need"])
            camp_seq = 1
            camp_buffer = []
            camp_qty = 0.0

            def emit_campaign():
                nonlocal camp_seq, camp_buffer, camp_qty
                if not camp_buffer: return
                cid = f"CAMP_{wire}_{wave}_{camp_seq:04d}"
                # Pad last lot if PAD enabled and campaign short
                if PAD and camp_qty < CAMP_MAX_MM:
                    deficit = CAMP_MAX_MM - camp_qty
                    last_lot = camp_buffer[-1]
                    add = min(deficit, LOT_MAX_MM - last_lot["qty"])
                    if add > 0:
                        last_lot["qty"] += add
                        camp_qty += add

                for k, L in enumerate(camp_buffer):
                    item = L["item"]
                    seq_per_item[item] += 1
                    lot_id = f"{item}_L{seq_per_item[item]:04d}"
                    meta = rt_meta[item]
                    itype = itype_map.get(item, "")
                    mn_age, mx_age = aging_map.get(item, (0.0, 72.0))
                    dur = op_duration_min(L["qty"], meta, item_code=item, lot_uom=meta.get("batch_uom","MM"))
                    row = {
                        "lot_id":          lot_id,
                        "item_code":       item,
                        "item_type":       itype,
                        "operation":       meta["operation"],
                        "department":      meta["department"],
                        "equipment":       meta["equipment"],
                        "raw_demand":      round(L["raw"], 2),
                        "lot_qty":         round(L["qty"], 2),
                        "lot_uom":         "MM",
                        "surplus":         round(L["qty"] - L["raw"], 2),
                        "duration_min":    round(dur, 2),
                        "duration_hours":  round(dur/60, 3),
                        "first_need":      L["first_need"],
                        "last_need":       L["last_need"],
                        "n_blocks":        len(L["claimed"]),
                        "n_skus":          len(L["sku_set"]),
                        "min_aging_h":     mn_age,
                        "max_aging_h":     mx_age,
                        "transfer_time_h": meta.get("transfer_time_min", 0.0) / 60.0,
                        "mpq_min":         LOT_MIN_MM,
                        "mpq_uom":         "MM",
                        "fraction_allowed": False,
                        "is_belt":         True,
                        "machines":        ", ".join(meta["machines"]),
                        "n_machines":      len(meta["machines"]),
                        "wire_type":       wire,
                        "campaign_id":     cid,
                        "is_campaign_start": (k == 0),
                        "is_campaign_end":   (k == len(camp_buffer) - 1),
                        "wave_ids":        wave,
                        "wave_first":      wave,
                        "wave_last":       wave,
                        # Belt lots are already MHE-sized (400m mother-roll each)
                        "batch_id":        lot_id,
                        "mhe_index":       0,
                        "mhe_total":       1,
                        "skus_set":        ", ".join(sorted(str(x) for x in L["sku_set"])),
                    }
                    lots_out.append(row)
                    for blk, sku, need, q in L["claimed"]:
                        block_assignments.append({
                            "lot_id": lot_id, "item_code": item,
                            "block_id": blk, "sku": sku, "need_by": need,
                            "demand_qty": float(q),
                        })
                camp_seq += 1
                camp_buffer = []
                camp_qty = 0.0

            for L in raw_lots:
                if camp_qty + L["qty"] > CAMP_MAX_MM and camp_buffer:
                    emit_campaign()
                camp_buffer.append(L)
                camp_qty += L["qty"]
            emit_campaign()

    return pd.DataFrame(lots_out, columns=LOT_COLUMNS), pd.DataFrame(block_assignments)


def main():
    print("\n" + "=" * 65)
    print("  PHASE 2 — Wave-aware Lot Sizing (V6_wave)")
    print("=" * 65)
    cfg = load_cfg(); fp = cfg["files"]

    # MRP NETTING: prefer Phase 1c's cascaded NET demand if available.
    # Phase 1c walks BOM tree per-block with cascading WIP subtraction (downstream-first).
    # If NET demand exists, use it AND skip the per-item WIP subtraction below
    # (the cascade has already done the work).
    net_gz = OUTPUTS / "phase1_demand_NET_updated.csv.gz"
    csv_gz = OUTPUTS / _suff("phase1_demand.csv.gz")
    if net_gz.exists():
        csv_in = net_gz
        skip_legacy_wip_subtract = True
        print(f"  Loading NET demand (post-MRP-cascade): {csv_in.name}")
    else:
        csv_in = csv_gz if csv_gz.exists() else OUTPUTS / _suff("phase1_demand.csv")
        skip_legacy_wip_subtract = False
    if not csv_in.exists():
        print(f"  ERROR: {csv_in} not found"); return 2
    demand = pd.read_csv(csv_in)
    demand["need_by"]    = pd.to_datetime(demand["need_by"], errors="coerce")
    demand["demand_qty"] = pd.to_numeric(demand["demand_qty"], errors="coerce")
    print(f"  Demand rows: {len(demand):,}  (skip_legacy_wip_subtract={skip_legacy_wip_subtract})")

    # Wave map
    wave_csv = PRIOR_OUTPUTS / "phase1_5_block_to_wave.csv"
    if not wave_csv.exists():
        print(f"  ERROR: {wave_csv} missing — run Phase 1.5 first"); return 2
    wmap = pd.read_csv(wave_csv)
    wave_map = dict(zip(wmap["block_id"], wmap["wave_id"]))
    print(f"  Waves loaded: {len(set(wave_map.values()))}")

    rt    = _db_or_csv("routing", cfg)
    am    = _db_or_csv("aging_master", cfg)
    itype = _db_or_csv("itemtype_master", cfg)
    mpq   = _db_or_csv("mpq", cfg)
    bw_name = fp.get("belt_wire_mapping")
    try:
        bw_df = _db_or_csv("belt_wire_mapping", cfg)
    except Exception:
        bw_df = pd.DataFrame()

    aging_map = build_aging_map(am)
    itype_map = build_itype_map(itype)
    mpq_map   = build_mpq_map(mpq)
    rt_meta   = build_routing_meta(rt)
    belt_to_wire = build_belt_wire_map(bw_df)

    print(f"  Belt mapping: {len(belt_to_wire)} items, {len(set(belt_to_wire.values()))} wires")
    print(f"  Wave duration: {WAVE_DURATION_DAYS} days")

    # Belt sizing (FRC) — per wave
    print(f"\n  Sizing FRC wire campaigns per wave...")
    belt_lots, belt_blocks = size_wire_campaigns_per_wave(
        demand, wave_map, rt_meta, aging_map, itype_map, belt_to_wire, cfg)
    print(f"  Belt lots: {len(belt_lots):,}")
    if not belt_lots.empty:
        print(f"  Belt campaigns: {belt_lots['campaign_id'].nunique()}")

    # Non-belt sizing — wave-aware with carry-over and lot-size cap
    print(f"\n  Sizing non-belt lots (wave + carry-over + lot-size cap)...")
    skip = set(belt_to_wire.keys()) if belt_to_wire else set()
    # V1 fix: MASTER COMPOUND is treated as always available — never produced
    # in this scheduler. Add every MC item to the skip set so no MC lots are
    # created and MC never appears on the floor schedule.
    mc_skip = {it for it, t in itype_map.items() if str(t).strip().upper() == "MASTER COMPOUND"}
    if mc_skip:
        skip |= mc_skip
        print(f"  Skipping {len(mc_skip):,} MASTER COMPOUND items (assumed available)")
    other_lots, other_blocks = size_lots_waveaware(
        demand, wave_map, rt_meta, aging_map, itype_map, mpq_map, cfg, skip_items=skip)
    print(f"  Non-belt lots: {len(other_lots):,}")

    if belt_lots.empty:
        belt_lots = pd.DataFrame(columns=LOT_COLUMNS)
    if not other_lots.empty:
        for c in LOT_COLUMNS:
            if c not in other_lots.columns:
                if c in ("wire_type","campaign_id","wave_ids","wave_first","wave_last","batch_id"):
                    other_lots[c] = ""
                elif c in ("mhe_index","mhe_total"):
                    other_lots[c] = 0 if c == "mhe_index" else 1
                else:
                    other_lots[c] = False
        other_lots = other_lots[LOT_COLUMNS]
    if not belt_lots.empty:
        belt_lots = belt_lots[LOT_COLUMNS]

    lots = pd.concat([belt_lots, other_lots], ignore_index=True)
    if belt_blocks.empty: belt_blocks = pd.DataFrame()
    if other_blocks.empty: other_blocks = pd.DataFrame()
    block_assignments = pd.concat([belt_blocks, other_blocks], ignore_index=True)

    print(f"\n  TOTAL lots: {len(lots):,}")
    tot_h = lots["duration_hours"].sum()
    print(f"  Total work hours: {tot_h:,.0f} h")
    print(f"  Lots > 8h: {(lots['duration_min'] > 480).sum()}")

    OUTPUTS.mkdir(parents=True, exist_ok=True)
    out_csv_path = OUTPUTS / _suff("phase2_lots.csv")
    _ensure_writable(out_csv_path)
    out_csv = _stage_csv(lots, "phase2_lots.csv")
    print(f"  Wrote: {out_csv}")

    if block_assignments is not None and not block_assignments.empty:
        b_csv = _stage_csv(block_assignments, "phase2_lot_blocks.csv")
        print(f"  Wrote: {b_csv}")

    if not lots.empty and "skus_set" in lots.columns:
        skus = lots[["lot_id","skus_set"]].copy()
        skus_csv = OUTPUTS / _suff("phase2_lot_skus.csv")
        skus.to_csv(skus_csv, index=False)
        print(f"  Wrote: {skus_csv}")

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
