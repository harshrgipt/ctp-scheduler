"""
common.py — shared normalisation + cycle-time math for the CTP PCR scheduler.

Pure, deterministic helpers used across phases: item typing (with the documented
GT/CAP fixes), aging-unit normalisation, the routing index, and op_duration_min.
"""
from __future__ import annotations
import math
import re
import pandas as pd

# Spelling/encoding variants → canonical item-type (B7-style canonicalisation).
_TYPE_ALIASES = {
    "synethic rubber": "Synthetic rubber",
    "zince oxide": "Zinc oxide",
    "bead wire": "Bead Wire",
}
_GREEN_TYRE = "GREEN_TYRE"


def canon_item_type(code: str, raw_type: str | None, descr: str | None = None) -> str:
    """Resolve an item code to its controlled type, applying the documented fixes.

    GT* → GREEN_TYRE (B11); CAP* → Cap Strip; alias map for spelling drift;
    else the itemtype-master value; else a description-based fallback.
    """
    c = (code or "").strip()
    if c.upper().startswith("GT ") or re.match(r"^GT[\s\-]?\d", c.upper()):
        return _GREEN_TYRE
    if c.upper().startswith("CAP"):
        return "Cap Strip"
    t = (raw_type or "").strip()
    if t:
        return _TYPE_ALIASES.get(t.lower(), t)
    d = (descr or "").strip()
    return _TYPE_ALIASES.get(d.lower(), d) if d else "UNKNOWN"


def aging_to_hours(value, unit) -> float | None:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    u = str(unit or "Hours").strip().lower()
    v = float(value)
    if u.startswith("day"):
        return v * 24.0
    if u.startswith("min"):
        return v / 60.0
    return v  # hours (default)


def build_itemtype_map(itemtype_df: pd.DataFrame, bom_df: pd.DataFrame) -> dict:
    """code -> canonical item-type, covering BOM codes the master omits."""
    raw = dict(zip(itemtype_df["ItemCode"], itemtype_df["ItemType"]))
    descr = {}
    if "child" in bom_df.columns and "child_description" in bom_df.columns:
        descr = dict(zip(bom_df["child"], bom_df["child_description"]))
    codes = set(raw) | set(bom_df["child"].unique()) | set(bom_df["Parent"].unique())
    return {c: canon_item_type(c, raw.get(c), descr.get(c)) for c in codes}


def build_aging_map(aging_df: pd.DataFrame, itype_map: dict, buffer_h: dict,
                    green_cure_by_h: float, planning_max: dict | None = None,
                    green_min_age_h: float = 0.0) -> dict:
    """code -> (min_h, max_h). GREEN_TYRE forced to [green_min_age_h, cure_by]; blanks backfilled.

    planning_max (item-type -> hours) tightens the max-aging ceiling to the plant's
    planning limit (e.g. silica Final Compound ages out at 48h, not the 120h master
    value) so lot consolidation and the EXPIRED test use the real shelf life.
    green_min_age_h enforces the green-tyre rest period before curing (0 = none).
    """
    import sys
    planning_max = {k.upper(): float(v) for k, v in (planning_max or {}).items()}
    out: dict[str, tuple[float, float]] = {}
    for _, r in aging_df.iterrows():
        code = r["ItemCode"]
        mn = aging_to_hours(r["MinAging"], r.get("MinAgingUnit"))
        mx = aging_to_hours(r["MaxAging"], r.get("MaxAgingUnit"))
        out[code] = (mn if mn is not None else 0.0, mx)
    # GREEN_TYRE cure-by + backfill missing max from buffer-master type default.
    inverted = []
    suffix_recovered = []
    for code, itype in itype_map.items():
        if itype == _GREEN_TYRE:
            out[code] = (float(green_min_age_h), green_cure_by_h)
            continue
        mn, mx = out.get(code, (0.0, None))
        if mx is None:
            # NAMING RECONCILIATION: the aging master may carry the base code (e.g. "CAP 66")
            # while the BOM adds a suffix ("CAP 66 - CAPSTRIP", "CAP 66-MOTHERROLL"). Before
            # falling back to the coarse type default, try the base code so the item's REAL
            # per-item shelf life (24h) is used instead of the buffer default (4h).
            for base in (str(code).split(" - ")[0].strip(), str(code).rsplit("-", 1)[0].strip()):
                if base != code and base in out:
                    mn, mx = out[base]
                    suffix_recovered.append((code, base))
                    break
        if mx is None:
            mx = buffer_h.get(itype.lower())          # plant buffer as fallback ceiling
        pm = planning_max.get(itype.upper())
        if pm is not None:                            # cap to the real planning shelf life
            mx = pm if mx is None else min(mx, pm)
        mn = mn if mn is not None else 0.0
        mx = mx if mx is not None else 1e9
        if mx < mn:                                   # backfill/cap must never invert the window
            inverted.append((code, round(mn, 1), round(mx, 1)))
            mx = mn                                    # clamp: a 0-width window, not a negative one
        out[code] = (mn, mx)
    # also normalise any aging-master code not seen in itype_map (None max -> unbounded)
    for code, (mn, mx) in list(out.items()):
        if mx is None:
            out[code] = (mn if mn is not None else 0.0, 1e9)
    if inverted:
        print(f"[aging][WARN] {len(inverted)} code(s) had MaxAging < MinAging after backfill/cap "
              f"(clamped to min): {inverted[:8]}", file=sys.stderr)
    if suffix_recovered:
        print(f"[aging] {len(suffix_recovered)} item(s) matched aging by BASE code (suffix stripped) "
              f"instead of the coarse type default: {suffix_recovered[:6]}", file=sys.stderr)
    return out


def build_routing_index(routing_df: pd.DataFrame) -> dict:
    """routed_product -> primary operation meta (machines parsed to a list).

    Keeps the SMALLEST operation_seq per routed_product (sort first, so "first" is the true
    lead op regardless of CSV row order). Warns if a routed_product has >1 DISTINCT operation
    being collapsed to one — that item would need multi-op sequencing the index can't express.
    """
    if "operation_seq" in routing_df.columns:
        _seq = pd.to_numeric(routing_df["operation_seq"], errors="coerce")
        routing_df = routing_df.assign(_seq=_seq).sort_values("_seq", kind="stable")
    multi = (routing_df.groupby("routed_product")["operation_name"].nunique())
    multi = [rp for rp, n in multi.items() if rp and n > 1]
    if multi:
        import sys
        print(f"[routing][WARN] {len(multi)} routed_product(s) have >1 distinct operation; only "
              f"the lowest-seq op is used (multi-op routing not modelled): {multi[:8]}", file=sys.stderr)

    def _parse_machines(cell):
        return [m.strip() for m in str(cell).split(",") if m.strip() and m.strip().lower() != "nan"]

    # Department+operation machine POOL: the union of every machine any product uses for a
    # given (department, operation_name). Used as a fallback when a routed_product's own
    # `machines` cell is blank — the plant runs shared lines (all master compounds on the
    # same Banbury), so a blank cell is a data-entry omission, not "no machine exists".
    # This infers eligibility from other rows; it never edits the input data.
    dept_pool: dict[tuple, set] = {}
    for _, r in routing_df.iterrows():
        key = (str(r.get("department", "")), str(r.get("operation_name", "")))
        for m in _parse_machines(r.get("machines")):
            dept_pool.setdefault(key, set()).add(m)

    idx: dict[str, dict] = {}
    _blank_fallback = []
    for _, r in routing_df.iterrows():
        rp = r["routed_product"]
        if not rp or rp in idx:
            continue
        machines = _parse_machines(r.get("machines"))
        if not machines:
            key = (str(r.get("department", "")), str(r.get("operation_name", "")))
            pooled = sorted(dept_pool.get(key, set()))
            if pooled:
                machines = pooled
                _blank_fallback.append(rp)
        idx[rp] = {
            "operation_name": r.get("operation_name", ""),
            "department": r.get("department", ""),
            "machines": machines,
            "proc_time": r.get("proc_time"),
            "proc_time_UOM": r.get("proc_time_UOM", ""),
            "batch_size": r.get("batch_size"),
            "efficiency": r.get("efficiency"),
        }
    if _blank_fallback:
        import sys
        print(f"[routing][WARN] {len(_blank_fallback)} routed_product(s) had a BLANK machine cell; "
              f"eligibility inferred from the shared department+operation machine pool (data gap, "
              f"not silently dropped): {_blank_fallback[:8]}", file=sys.stderr)
    return idx


# Length-rate proc UOMs whose duration must be timed on the length the machine
# physically FEEDS, not the developed output length wound at the builder.
_LENGTH_RATE_UOMS = {"MPM", "M/MIN", "MTR/MIN", "MM/MIN"}
# Length child_Units in the BOM (metres/millimetres). MT is a tonne (mass) -> excluded.
_BOM_LENGTH_UNITS = {"M", "MTR", "MM"}


def _bom_len_m(qty, unit) -> float | None:
    """A BOM child_quantity in metres, or None if the unit is not a length."""
    if qty is None or (isinstance(qty, float) and math.isnan(qty)):
        return None
    u = str(unit or "").strip().upper()
    if u in ("M", "MTR"):
        return float(qty)
    if u == "MM":
        return float(qty) / 1000.0
    return None


def build_length_input_factor(routing_df: pd.DataFrame, bom_df: pd.DataFrame,
                              min_ratio: float = 2.0) -> dict:
    """routed_product -> (fed_len_m / developed_len_m) for length-rate slitter/cutter ops.

    THE CAP-PLY-SLITTER FIX (confirmed root cause): a length-rate op (M/MIN etc.) is
    timed on its routed_product's DEMAND quantity — the developed strip length wound at
    the builder (e.g. CAP 66 - CAPSTRIP = 25.761 m/tyre). But the machine physically
    feeds the smaller SHEET/mother-roll named in the routing's input_components_from_bom
    (CAP 66-MOTHERROLL = 0.1668 m/tyre). Timing on the developed length inflates the op
    ~154x -> phantom bottleneck. This returns a per-routed-product scale factor (<1) to
    multiply the duration qty by, so the op is timed on the length actually processed.

    Guarded to fire ONLY where a length op's routed_product has a BOM child that is
    (a) named in input_components_from_bom, (b) a length item (M/MTR/MM), and
    (c) smaller than the routed_product's own developed length by >= min_ratio.
    This excludes mixers, calenders and extruders (compound KG inputs -> no length
    child) and any op where input length == output length (ratio 1) — only the genuine
    developed-output-vs-fed-sheet gap is corrected.

    NB: the factor is a DURATION basis only; it does NOT change the demand quantity the
    builder consumes (still the full developed strip).
    """
    # Pair developed and fed lengths WITHIN the same SKU (Super_parent) — mixing a max
    # developed length from one SKU with a min fed length from another produces a ratio that
    # belongs to no real SKU and can over-shorten. Build per-(SKU, code) developed and
    # per-(SKU, parent, child) fed lengths.
    sp_col = "Super_parent" if "Super_parent" in bom_df.columns else None
    dev_sku: dict[tuple, float] = {}
    fed_sku: dict[tuple, float] = {}
    for sp, p, c, q, u in zip(bom_df[sp_col] if sp_col else bom_df["child"] * 0,
                              bom_df["Parent"], bom_df["child"],
                              bom_df["child_quantity"], bom_df["child_Unit"]):
        lm = _bom_len_m(q, u)
        if not (lm and lm > 0):
            continue
        sku, parent, child = str(sp).strip(), str(p).strip(), str(c).strip()
        kd = (sku, child); dev_sku[kd] = max(lm, dev_sku.get(kd, 0.0))
        kf = (sku, parent, child); fed_sku[kf] = min(lm, fed_sku.get(kf, lm))
    skus = {k[0] for k in dev_sku}

    out: dict[str, float] = {}
    seen = set()
    for r in routing_df.itertuples():
        rp = str(getattr(r, "routed_product", "")).strip()
        pu = str(getattr(r, "proc_time_UOM", "")).strip().upper()
        if not rp or rp in seen or pu not in _LENGTH_RATE_UOMS:
            continue
        seen.add(rp)
        inp_names = [x.strip() for x in re.split(r"[,;]", str(getattr(r, "input_components_from_bom", "") or "")) if x.strip()]
        ratios = []
        for sku in skus:                             # per-SKU fed/dev, paired in-SKU
            dv = dev_sku.get((sku, rp))
            if not dv:
                continue
            fv = None
            for name in inp_names:
                il = fed_sku.get((sku, rp, name))
                if il and il > 0 and (fv is None or il < fv):
                    fv = il
            if fv and dv / fv >= min_ratio:          # genuine developed>>fed gap in this SKU
                ratios.append(fv / dv)
        if ratios:
            out[rp] = max(ratios)                    # conservative: least shortening; never
                                                     # under-times any SKU below its fed length
    return out


def op_duration_min(qty: float, qty_uom: str, proc_uom: str,
                    proc_time: float, batch_size: float, eff: float = 1.0) -> float:
    """Per-lot duration in minutes — the documented UOM-dispatched cycle-time math.

    qty_uom is the item's demand unit (MM/KG/NOS); proc_uom selects the formula.
    Length quantities (MM) convert to metres for rate/per-unit-minute families.
    """
    e = eff if (eff and eff > 0) else 1.0
    pu = (proc_uom or "").upper().strip()
    qu = (qty_uom or "").upper().strip()
    pt = float(proc_time) if proc_time and proc_time > 0 else 0.0
    bs = float(batch_size) if batch_size and batch_size > 0 else 0.0
    if pt == 0:
        return 1.0
    metres = qty / 1000.0 if qu == "MM" else qty

    if pu == "MM/MIN":                                        # line-speed in mm/min: time on the RAW mm length
        return max(qty / pt / e, 1.0)                         # raw MM qty, NOT metres (metres/pt under-times 1000x)
    if pu in ("MPM", "M/MIN", "MTR/MIN"):                     # line-speed in metres/min
        return max(metres / pt / e, 1.0)
    if "BATCH" in pu:                                          # per-BATCH family
        # SEC/BATCH = seconds per batch (÷60 → min); MIN/BATCH and a bare BATCH tag = minutes/batch.
        per_batch_min = pt / 60.0 if "SEC" in pu else pt
        if bs > 0:
            return max(math.ceil(qty / bs) * per_batch_min / e, 1.0)
        return max(qty * per_batch_min / e, 1.0)              # per-piece
    if pu == "SEC":
        if bs > 0:
            return max((qty / bs) * pt / 60.0 / e, 1.0)
        return max(qty * pt / 60.0 / e, 1.0)
    if pu in ("MIN", "MINS"):                                 # per-unit minutes
        return max(metres * pt / e, 1.0)
    if pu in ("RPM", "REV/MIN", "PC/HR", "PCS/HR", "PCH"):    # bead-line throughput
        # The "RPM" UOM tag on Bead Apex/Bundle ops is a master-data mislabel: the
        # value is a throughput in PIECES PER HOUR (200/128.89 = 1.55 min/bead;
        # 1000 beads -> 7.76 h, the plausible CTP bead-line band). The old fallback
        # read it as minutes-per-piece and produced 36-year schedules. Confirm the
        # UOM with the routing master-data owner; re-tag "RPM" -> "PC/HR".
        return max(qty / pt * 60.0 / e, 1.0)                  # pieces/hour -> minutes
    if pu in ("CUTS/MIN", "NOS/MIN", "NO/MIN", "PC/MIN", "PCS/MIN"):
        # Per-MINUTE throughput tags on the cutters (belt/ply: CUTS/MIN) and the bead
        # apexer (NOS/MIN): proc_time is PIECES PRODUCED PER MINUTE, so duration =
        # qty / rate. Read as minutes-per-piece (the old fallback) it inflated the belt
        # cutter ~216x and the bead apexer ~26x — overloading those machines >10x and
        # blowing the makespan to 650+ days. NB: "R/MIN" (bead winding, revolutions/min
        # against a KG demand) is deliberately NOT handled here — it needs a
        # revs-per-bead / KG-per-rev factor confirmed by the CTP bead room before
        # encoding; until then it stays caught by the phase2 runaway cap.
        return max(qty / pt / e, 1.0)                         # pieces/minute -> minutes
    return max(qty * pt / e, 1.0)                             # fallback per-piece min
