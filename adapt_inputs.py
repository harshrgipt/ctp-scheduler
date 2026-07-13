"""
adapt_inputs.py — the ONLY file in this project that knows CTP's data is different.

The v6 phase files in `phases/` are BYTE-IDENTICAL copies of
`v6_wave_p2/phases/*.py`. Not one line of the algorithm is changed. To keep it that
way, we convert CTP's inputs into exactly the CSVs v6 expects, with exactly v6's
column names, and drop them in `inputs/`. v6 then runs unmodified.

Every conversion below is a DATA reshape, never a rule change. Each one is listed in
MEMORY.md with the reason. Nothing here is guessed: every mapping was verified against
the real files.

    CTP file                              ->  v6 file (what the phases open)
    -----------------------------------------------------------------------
    jkt_bom_pcr 13 (1).xlsx               ->  bom.csv                (columns already match)
    jkt_routing_pcr 14.xlsx               ->  routing.csv            (+ MIN/BATCH -> SEC/BATCH)
    CTP_PCR_Curing_Schedule ... .xlsx     ->  plan.csv               (Shift Schedule -> plan cols)
    jkt_itemType_master_pcr.csv           ->  itemtype_master.csv    (columns already match)
    jkt_aging_master_pcr.csv              ->  aging_master.csv       (columns already match)
    MOQ (3).xlsx                          ->  mpq.csv                (v6's 5-column schema)
    jkt_buffer_master_pcr.csv             ->  buffer_master.csv
    opening_wip.csv                       ->  inventory.csv          (v6 inventory schema)
    (config planning_max_aging_h)         ->  planning_max_aging.csv
    jkt_changeover_matrix_combined.xlsx   ->  changeover_matrix.csv  (CTP-ONLY — see MEMORY.md)

Usage:  python adapt_inputs.py
"""
from __future__ import annotations
import os
import re
import sys
import math
import pandas as pd
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "inputs")
CFG = yaml.safe_load(open(os.path.join(HERE, "config.yaml"), encoding="utf-8"))
SRC = os.path.join(HERE, CFG["ctp_inputs_dir"])


def _p(name: str) -> str:
    return os.path.join(SRC, CFG["ctp_files"][name])


def _read(name: str) -> pd.DataFrame:
    p = _p(name)
    if p.lower().endswith((".xlsx", ".xls")):
        return pd.read_excel(p, sheet_name=0, dtype=str, engine="openpyxl").fillna("")
    return pd.read_csv(p, dtype=str, keep_default_na=False)


def _w(df: pd.DataFrame, name: str) -> None:
    path = os.path.join(OUT, name)
    df.to_csv(path, index=False)
    print(f"  {name:<26} {len(df):>7,} rows")


# --------------------------------------------------------------------------- BOM
def bom() -> None:
    """CTP's BOM already carries v6's column names. Straight through."""
    df = _read("bom")
    need = ["Super_parent", "Equipment", "grand_parent", "Parent", "Parent_qty",
            "Parent_unit", "child", "child_quantity", "child_Unit"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"BOM is missing v6 columns: {missing}")
    _w(df, "bom.csv")


# ----------------------------------------------------------------------- ROUTING
def routing() -> None:
    """CTP's routing already carries v6's column names, with ONE data difference.

    v6's `op_duration_min` (phase2:196-213) handles these proc_time_UOMs:
        MM/MIN  M/MIN  MTR/MIN  NOS/MIN  SEC/BATCH  SEC  MIN  *anything*/MIN
    It has NO `MIN/BATCH` branch, and an unmatched UOM falls through to `qty * pt`
    — which for a 690 KG batch at 15 min/batch yields 10,350 min instead of 15.6.

    CTP's routing is 72% `MIN/BATCH` (10,599 of 14,693 rows); v6's TBR routing uses
    `SEC/BATCH`. They mean the same thing in different units, so we convert the UNIT,
    not the rule:  MIN/BATCH @ pt  ==  SEC/BATCH @ pt*60.
    v6's SEC/BATCH branch then computes  ceil(qty/batch_size) * (pt*60) / 60  minutes
    = exactly the intended minutes-per-batch. The algorithm is untouched.
    """
    df = _read("routing")
    # the sheet loads as all-strings; rebuild the two columns as plain objects so the
    # converted numbers can be written back without a dtype clash
    df["proc_time"] = df["proc_time"].astype(object)
    df["proc_time_UOM"] = df["proc_time_UOM"].astype(object)
    u = df["proc_time_UOM"].astype(str).str.strip().str.upper()
    m = (u == "MIN/BATCH").to_numpy()
    n = int(m.sum())
    if n:
        pt = pd.to_numeric(df["proc_time"], errors="coerce") * 60.0
        df.loc[m, "proc_time"] = pt[m].to_numpy(dtype=object)
        df.loc[m, "proc_time_UOM"] = "SEC/BATCH"
        print(f"  [routing] MIN/BATCH -> SEC/BATCH (x60) on {n:,} rows "
              f"— v6 has no MIN/BATCH branch; same rule, converted unit")
    _w(df, "routing.csv")


# -------------------------------------------------------------------------- PLAN
def plan() -> None:
    """The curing drum -> v6's plan schema.

    v6 (`phase1_5.parse_plan_blocks`, `phase0.parse_plan_to_curing`) reads:
        skuCode, qty, startTime, endTime, pressNo, shift, date
    and drops rows whose skuCode is CHANGEOVER / MOULD_CLEANING, and qty <= 0.

    The CTP export puts a title banner + a summary line above the header, so the real
    header is not on row 0. Find it instead of hard-coding a skiprows.
    """
    p = _p("plan")
    probe = pd.read_excel(p, sheet_name="Shift Schedule", header=None, nrows=10,
                          engine="openpyxl")
    hdr = 0
    for i in range(len(probe)):
        cells = {str(v).strip() for v in probe.iloc[i].tolist()}
        if {"SKUCode", "StartTime", "Machine"} <= cells:
            hdr = i
            break
    raw = pd.read_excel(p, sheet_name="Shift Schedule", header=hdr, engine="openpyxl")
    raw.columns = [str(c).strip() for c in raw.columns]

    out = pd.DataFrame({
        "plan_id":   "CTP_PCR",
        "skuCode":   raw["SKUCode"].astype(str).str.strip(),
        "date":      pd.to_datetime(raw["Date"], errors="coerce"),
        "pressNo":   raw["Machine"].astype(str).str.strip(),
        "shift":     raw["Shift"].astype(str).str.strip(),
        "startTime": pd.to_datetime(raw["StartTime"], errors="coerce"),
        "endTime":   pd.to_datetime(raw["EndTime"], errors="coerce"),
        "qty":       pd.to_numeric(raw["Qty"], errors="coerce").fillna(0),
        "cycleTime": pd.to_numeric(raw.get("CycleTime_min"), errors="coerce"),
        "remarks":   raw.get("Remarks", "").astype(str).str.strip(),
    })
    # v6 filters CHANGEOVER itself (phase1_5:64-69). Keep every row so its own
    # occupancy handling sees them — do not pre-filter here.
    _w(out, "plan.csv")


# --------------------------------------------------------------------- ITEM TYPE
def itemtype() -> None:
    """CTP's itemtype master + the ONE gap it has: it does not type the green tyre.

    v6 keys three critical behaviours on the RAW ItemType string:
        BUILDING_ITYPES  = {"GREEN TYRES", "CARCASS"}          phase5_v2:708
        AGE_OVERRIDE_H   = {"GREEN TYRES": (0.0, 72.0), ...}   phase5_v2:869
        PLANT_ITEM_TYPE_CAPS / TARGET_WIP_HOURS                phase2 / phase5
    and it builds its map from master rows ONLY (`build_itype_map`, phase2:126-128) —
    it never canonicalises, never guesses from a code prefix.

    CTP's master types 2,862 codes but **not one green tyre** (verified: 0 of the 226
    GT codes are present). Left alone, `lot_item_type` is "" for every green tyre, so:
      - it is NOT in BUILDING_ITYPES  -> it skips `place_building_campaigns` and falls
        into the ordinary JIT picker (a different algorithm),
      - AGE_OVERRIDE_H never fires    -> its aging silently becomes
        (DEFAULT_MIN_AGE_H, DEFAULT_MAX_AGE_H) = (8 h, 8760 h) instead of (0, 72),
      - the short-life resync becomes a no-op.
    The plan would still "succeed" and be nonsense.

    The green tyre is identified DETERMINISTICALLY from the routing, not guessed:
    it is a `routed_product` whose `department` is BUILDING (226 codes; they are the
    same GT-prefixed codes the BOM uses as `Parent`). We type them `Green Tyres` —
    v6's exact string. This is a DATA GAP FILL, not a rule change.
    """
    df = _read("itemtype")
    df = df.rename(columns={c: c.strip() for c in df.columns})
    df = df[["ItemCode", "ItemType"]].copy()

    rt = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str, keep_default_na=False)
    building = sorted({str(r).strip() for r, d in
                       zip(rt["routed_product"], rt["department"])
                       if "BUILD" in str(d).upper() and str(r).strip()})
    have = set(df["ItemCode"].astype(str).str.strip())
    add = [c for c in building if c not in have]
    if add:
        df = pd.concat([df, pd.DataFrame({"ItemCode": add, "ItemType": "Green Tyres"})],
                       ignore_index=True)
        print(f"  [itemtype] typed {len(add)} green tyres as 'Green Tyres' "
              f"(routing dept=BUILDING; CTP's master does not type them)")
    _w(df, "itemtype_master.csv")


# ------------------------------------------------------------------------- AGING
def aging() -> None:
    """CTP's aging master already carries v6's columns.

    NOTE what v6 does with a green tyre: there is NO green-tyre row in this master
    (0 of 2,658). v6 does not need one — `phase5_v2:869` hard-codes
        AGE_OVERRIDE_H = {"GREEN TYRES": (0.0, 72.0), "CARCASS": (0.0, 24.0)}
    keyed on the raw ItemType string. CTP's itemtype master must therefore type its
    green tyres as literally "GREEN TYRES" for that override to fire — see itemtype()
    and MEMORY.md.

    *** NO-INFORMATION ROWS ARE DROPPED, NOT KEPT BLANK. This is load-bearing. ***

    789 of CTP's 2,658 rows carry an ItemCode and FOUR EMPTY CELLS. v6's input contract
    is: an item is either FULLY SPECIFIED, or ABSENT from the master. Never
    present-but-blank. (Proof: v6's own aging_masterV4.csv has 7,213 rows and ZERO blanks
    in Min/MaxAging — which is why the parser below is not guarded against one.)

    A present-but-blank row does NOT behave like an absent one; it detonates:
        v6:107  aging_hours -> `float(v)`  -> float(NaN) is NaN, NOT an exception,
                                              so its `except` never fires
        v6:463  mn_age, mx_age = aging_map.get(item, (0.0, 72.0))   -> gets (NaN, NaN)
        v6:480  int(mx_age // wave_dur_h)  -> ValueError: cannot convert float NaN to integer

    An ItemCode with four empty cells carries NO INFORMATION — it is indistinguishable
    from an item the master never listed. So we DROP it, and v6's OWN documented default
    for an unlisted item fires:  `aging_map.get(item, (0.0, 72.0))`.
    Dropping a data-free row is DECLINING TO INVENT DATA. Writing a number in would be
    inventing it, and we do not.

    !! THIS IS A REAL BEHAVIOURAL DEFAULT, NOT A NO-OP. It is reported loudly at the end
       of this function and recorded in MEMORY.md. To override it, populate the aging
       master — do not patch the phases.
       (v6's phase5 additionally hard-overrides CARCASS -> (0,24) and GREEN TYRES -> (0,72)
       at placement via AGE_OVERRIDE_H, regardless of the master.)
    """
    df = _read("aging")
    need = ["ItemCode", "MaxAging", "MinAging", "MaxAgingUnit", "MinAgingUnit"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"aging master is missing v6 columns: {missing}")
    df = df[need].copy()

    # NB: `_read` uses keep_default_na=False, so a blank cell arrives here as "" (an empty
    # STRING), not NaN. It only BECOMES NaN later, when v6's phases read our CSV back with
    # pandas' default settings. So test for both — `.isna()` alone silently matches nothing.
    def _blank(s: pd.Series) -> pd.Series:
        return s.isna() | (s.astype(str).str.strip() == "")

    n0 = len(df)
    no_data = _blank(df["MaxAging"]) & _blank(df["MinAging"])
    if no_data.any():
        dropped = df.loc[no_data, "ItemCode"].astype(str).str.strip()
        df = df.loc[~no_data]
        print(f"  [aging] DROPPED {n0 - len(df)} of {n0} rows that had an ItemCode but NO "
              f"aging values.\n"
              f"          These items now take v6's OWN default for an unlisted item: "
              f"(min=0.0h, max=72.0h)  [v6 phase2:463]\n"
              f"          No aging value was invented. To override, populate the aging master.")
        try:
            it = pd.read_csv(os.path.join(OUT, "itemtype_master.csv"), dtype=str)
            tm = dict(zip(it["ItemCode"].astype(str).str.strip(),
                          it["ItemType"].astype(str).str.strip()))
            counts = dropped.map(lambda c: tm.get(c, "<untyped>")).value_counts()
            for t, n in counts.items():
                print(f"            {n:5d}  {t}")
        except Exception:
            pass

    # A HALF-blank row is NOT information-free — it means the master says something we
    # cannot read. Refuse rather than guess which half to trust.
    half = _blank(df["MaxAging"]) ^ _blank(df["MinAging"])
    if half.any():
        bad = df.loc[half, "ItemCode"].head(10).tolist()
        sys.exit(f"aging master has {int(half.sum())} rows with only ONE of Min/MaxAging "
                 f"filled, e.g. {bad}. That is not a no-information row and I will not "
                 f"guess the missing half. Fill or clear them.")

    _w(df, "aging_master.csv")


# --------------------------------------------------------------------------- MPQ
def mpq() -> None:
    """MOQ (3).xlsx -> v6's mpq schema.

    v6 (`phase2.build_mpq_map`, :131-146) expects:
        Item Type, Minimum Run Qty, Maximum Run Qty, UOM, Fraction Allowed
    CTP's file has:
        Item Type, Minimum Order Qty, Maximum Order Qty, UOM     (no Fraction Allowed)

    Two facts, both verified in the file, both user-confirmed:
      1. `Maximum Order Qty` is BLANK for every one of the 12 real item types.
         "CTP has no max MPQ."

         *** THE COLUMN IS OMITTED, NOT WRITTEN BLANK. This is load-bearing. ***

         v6 supports a missing max — but ONLY as an ABSENT COLUMN, which is how its own
         earlier master `mpq_v2.csv` is written (4 cols, no max). Its reader is built for
         exactly that:  `try: mx = float(r["Maximum Run Qty"]) except (..., KeyError): mx = None`
         The `KeyError` in that except list IS the "no max" path.

         A BLANK column does NOT take that path, and fails silently-then-loudly:
             pandas reads blank            -> NaN
             float(NaN)                    -> NaN   (no exception! mx = NaN, not None)
             v6:461 `mpq_max <= 0`         -> False (NaN comparisons are always False)
             v6:575 `if mpq_max`           -> TRUE  (NaN is truthy)
             int(ceil(qty / NaN))          -> ValueError: cannot convert float NaN to integer
         v6's own mpq_v3.csv has the max populated on all 15 rows, so v6 never meets a NaN
         here and has no guard against one.

         With the column absent: mpq_max = None -> v6's MHE split never fires ->
         n_subs = n_dur (v6:597-599), i.e. lots split by the 8h duration cap alone.
         That is v6's own designed no-max behaviour, not a CTP special case.

      2. There is no `Fraction Allowed` column. v6's reader defaults it to "NO"
         (`r.get("Fraction Allowed","NO")`), which selects v6's else-branch
         `lot_qty_full = max(raw_qty, mpq_min)` (:567-568) — a plain floor, no
         rounding up to whole MPQ multiples. We write the column explicitly as NO so
         the behaviour is visible rather than implied.

    The compound rows read "3 Batches" (text). v6 does `float(...)` -> ValueError ->
    mn = None -> no floor at all for compounds. The batch SIZE lives in the routing
    (`batch_size`), which is what v6 rounds compounds to anyway, so we leave these as
    text and let v6 drop them. We do NOT invent a KG number.
    """
    p = _p("mpq")
    raw = pd.read_excel(p, sheet_name="PCR", header=0, engine="openpyxl")
    rows = []
    for _, r in raw.iterrows():
        it = str(r.iloc[0]).strip()
        if not it or it.lower() in ("nan",):
            continue
        if it.lower().startswith(("for mixers", "machine code")):
            break                                    # the mixer table / footer
        rows.append({
            "Item Type":         it,
            "Minimum Run Qty":   r.get("Minimum Order Qty", ""),
            # NO `Maximum Run Qty` COLUMN — see docstring. It is OMITTED, not blanked.
            "UOM":               str(r.get("UOM", "")).strip(),
            "Fraction Allowed":  "NO",               # no such column in CTP's file
        })
    _w(pd.DataFrame(rows), "mpq.csv")


# ------------------------------------------------------------------------ BUFFER
def buffer() -> None:
    df = _read("buffer")
    _w(df, "buffer_master.csv")


# --------------------------------------------------------------- PLANNING MAXAGE
def planning_max_aging() -> None:
    """v6 reads this as a MASTER FILE (phase4:340-347), keyed by ItemType.
    CTP carries the same policy in config; write it out in v6's schema."""
    pol = CFG.get("planning_max_aging_h") or {}
    df = pd.DataFrame([{"ItemType": k, "PlanningMaxAging": v, "PlanningMaxUnit": "HRS",
                        "Notes": "from ctp_v6/config.yaml"} for k, v in pol.items()])
    _w(df, "planning_max_aging.csv")


# --------------------------------------------------------------------- INVENTORY
def inventory() -> None:
    """opening_wip.csv -> v6's inventory schema (phase1c / phase2 WIP).

    *** `unit` BACKFILL — EXACT MATCH ONLY, FROM THE OTHER MASTERS. NO FUZZY. ***

    WHY THIS MATTERS. v6 converts length stock to MM (db_loader:286, phase5:1466):
        _f = inv["unit"].astype(str).str.upper().str.strip().map(_L2MM).fillna(1.0)
    A BLANK unit -> astype(str) -> "nan" -> .map() misses -> NaN -> **.fillna(1.0)** ->
    factor 1.0, i.e. **MM**. If the real unit is **M**, that stock is credited at
    **1/1000th**. v6 hit this exact bug once; its own comment (db_loader:280-285) says the
    1/1000th credit "drove the cap-strip / ply over-ageing".
    NOTE `fillna(1.0)` is CORRECT for NOS/KG items — only LENGTH items are at risk.

    The unit is a **LABEL, not a quantity**, and the same item states its unit in the other
    masters. So we resolve a blank by EXACT itemcode lookup, in this priority order:
        1. a sibling WIP row      — same itemcode, non-blank unit
        2. bom.child_Unit         — exact `child` match
        3. routing.batch_UNIT     — exact `routed_product` match
        4. routing.proc_time_UOM  — M/MIN -> M, MM/MIN -> MM, NOS/MIN -> NOS
           (a rate's numerator IS the item's unit: 65 M/MIN means the item is measured in M)
    A source is used ONLY if it gives ONE unambiguous unit for that itemcode. This is a
    LOOKUP, not a guess — no value is invented, and nothing is fuzzy-matched.

    Anything still unresolved is LEFT BLANK, so v6's own `fillna(1.0)` default applies.
    On the current data that is harmless: all 2,041 blank-unit LENGTH rows are on items
    that NO BOM ever raises, so the scheduler never looks them up (dead WIP). The backfill
    is what protects us the moment one of them enters a BOM.
    """
    df = _read("inventory")
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n in cols:
                return cols[n]
        return None

    ic = pick("itemcode", "item_code", "material_code_o")
    qc = pick("inventory", "qty", "quantity", "livequantity")
    if not ic or not qc:
        sys.exit(f"opening_wip: cannot find itemcode/qty columns in {list(df.columns)}")
    out = pd.DataFrame({
        "production_id": df.get(pick("production_id") or ic, ""),
        "produced_time": df.get(pick("produced_time", "producedtime") or ic, ""),
        "itemcode":      df[ic].astype(str).str.strip(),
        "item_type":     df.get(pick("item_type", "itemtype") or ic, ""),
        "unit":          df.get(pick("unit", "uom") or ic, ""),
        "inventory":     pd.to_numeric(df[qc], errors="coerce").fillna(0.0),
    })
    out = out[out["inventory"] > 0]

    # ---- unit backfill: EXACT itemcode match, from the other masters (see docstring) ----
    out["unit"] = out["unit"].astype(str).str.strip()
    blank = out["unit"].isin(("", "nan", "NaN", "None"))
    n_blank = int(blank.sum())
    if n_blank:
        def _one(df_, key, val, xform=lambda v: v):
            """itemcode -> unit, but ONLY where the source is unambiguous for that code."""
            if key not in df_.columns or val not in df_.columns:
                return {}
            d = df_[[key, val]].dropna()
            d = d.assign(_k=d[key].astype(str).str.strip(),
                         _v=d[val].astype(str).str.strip().str.upper().map(xform))
            d = d[d["_v"].notna() & (d["_v"] != "")]
            g = d.groupby("_k")["_v"].agg(lambda s: set(s))
            return {k: next(iter(v)) for k, v in g.items() if len(v) == 1}

        def _rate_to_unit(u):
            """A rate's NUMERATOR is the item's unit: 65 M/MIN => the item is in M."""
            return {"M/MIN": "M", "MTR/MIN": "M", "MM/MIN": "MM", "NOS/MIN": "NOS",
                    "CM/MIN": "CM"}.get(u)

        bom_df = pd.read_csv(os.path.join(OUT, "bom.csv"), dtype=str)
        rt_df  = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str)
        sib = out.loc[~blank, ["itemcode", "unit"]]
        sib = sib.assign(_v=sib["unit"].str.upper())
        sibg = sib.groupby("itemcode")["_v"].agg(lambda s: set(s))
        sources = [
            ("WIP sibling row",      {k: next(iter(v)) for k, v in sibg.items() if len(v) == 1}),
            ("bom.child_Unit",       _one(bom_df, "child", "child_Unit")),
            ("routing.batch_UNIT",   _one(rt_df, "routed_product", "batch_UNIT")),
            ("routing.proc_time_UOM", _one(rt_df, "routed_product", "proc_time_UOM",
                                           _rate_to_unit)),
        ]
        filled, hits = out["unit"].copy(), {}
        for name, m in sources:
            if not m:
                continue
            still = filled.isin(("", "nan", "NaN", "None"))
            got = out.loc[still, "itemcode"].map(m)
            n = int(got.notna().sum())
            if n:
                filled.loc[still & got.notna().reindex(filled.index, fill_value=False)] = \
                    got[got.notna()]
                hits[name] = n
        out["unit"] = filled
        left = int(out["unit"].isin(("", "nan", "NaN", "None")).sum())
        print(f"  [inventory] unit was BLANK on {n_blank:,} rows. Backfilled by EXACT itemcode "
              f"match (no fuzzy):")
        for name, n in hits.items():
            print(f"                {n:6,} rows  <- {name}")
        print(f"              {left:6,} rows still blank -> v6's own fillna(1.0) default. "
              f"(Harmless: none are on items any BOM raises.)")

    _w(out, "inventory.csv")


# ------------------------------------------------------------- CHANGEOVER MATRIX
def changeover() -> None:
    """CTP-ONLY. v6 has NO changeover matrix — it derives setup minutes from machine
    NAMES (`compute_changeover_min`, phase5_v2:139-170: WBC/LTBC/HTBC/FISCHER/DUPLEX/
    TRIPLEX/…). NONE of CTP's 123 machine ids match any of those branches, so under
    v6's own rule every CTP machine would fall to DEFAULT_CHANGEOVER_MIN = 15 (mixing
    = 2).

    The user supplied a real plant matrix (`jkt_changeover_matrix_combined (1).xlsx`) and
    instructed it be used: "we don't have [it] in the btp v6 but we will use it in the ctp".

    *** KEYED ON (LINE, from_item, to_item). The LINE comes from the user's machine list. ***

    The matrix is (machine, from_MaterialCode_O, to_MaterialCode_O) -> minutes. Its `machine`
    column holds 22 machine *LINE NAMES* spanning BOTH plants — "4 RC Calendar",
    "Belt Cutter PCR", "Quadraplex TBR SW" — while CTP's routing holds numeric machine *IDs*
    ("901", "1103", "3409"). The two sets have **ZERO string overlap**.

    The bridge is `machine_to_changeover_line` in config.yaml, built from the user-supplied
    CTP machine list (`pcr machines.png`). It is DATA, not a guess, and it is editable.

    A machine with no mapping, or an item pair the matrix does not carry for that line, falls
    through to v6's own `compute_changeover_min` (mixing = 2 min, else 15 min) — unchanged.
    That is the honest outcome for Building (3401-3411) and the mixers, which the matrix simply
    does not cover.

    Keying on the line also RESOLVES the 23 item pairs that carry different times on different
    lines (e.g. `EG01 -> EG02` = 10 min on the Cap Ply Cutter, 17 on the Edge Gum Calendar,
    24 on the PCR Roller Head). With the line known we take the RIGHT one instead of guessing.
    """
    p = _p("changeover_matrix")
    if not os.path.exists(p):
        print("  changeover_matrix       (absent — v6's compute_changeover_min will be used)")
        return
    df = pd.read_excel(p, sheet_name=0, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    _w(df, "changeover_matrix.csv")

    need = ["machine", "from_MaterialCode_O", "to_MaterialCode_O", "changeover_time_min"]
    missing = [c for c in need if c not in df.columns]
    if missing:
        sys.exit(f"changeover matrix is missing columns {missing}; got {list(df.columns)}")

    m2l = {str(k).strip(): str(v).strip()
           for k, v in (CFG.get("machine_to_changeover_line") or {}).items()}
    if not m2l:
        sys.exit("config.yaml has no `machine_to_changeover_line`. Without it the matrix's "
                 "line NAMES cannot be joined to the routing's machine IDs and the whole "
                 "matrix would be silently ignored.")

    d = df.copy()
    d["_l"] = d["machine"].astype(str).str.strip()
    d["_f"] = d["from_MaterialCode_O"].astype(str).str.strip()
    d["_t"] = d["to_MaterialCode_O"].astype(str).str.strip()
    d["_m"] = pd.to_numeric(d["changeover_time_min"], errors="coerce")
    d = d[(d["_f"] != "") & (d["_t"] != "") & d["_m"].notna()]
    d = d[~d["_f"].str.lower().isin(("nan",)) & ~d["_t"].str.lower().isin(("nan",))]

    lines_we_use = set(m2l.values())
    unknown = sorted(lines_we_use - set(d["_l"]))
    if unknown:
        sys.exit(f"config.machine_to_changeover_line points at line name(s) that do NOT exist "
                 f"in the matrix: {unknown}. Matrix has: {sorted(set(d['_l']))}")

    keep = d[d["_l"].isin(lines_we_use)]
    lut = (keep.groupby(["_l", "_f", "_t"])["_m"].max().reset_index())
    lut.columns = ["line", "from_item", "to_item", "changeover_min"]
    _w(lut, "changeover_lookup.csv")

    rt_df = pd.read_csv(os.path.join(OUT, "routing.csv"), dtype=str)
    rmach = set()
    for m in rt_df["machines"].dropna():
        for x in re.split(r"[,;/|]", str(m)):
            x = x.strip().strip('"').strip()
            if x and x.upper() not in ("NAN", "NONE"):
                rmach.add(x)
    mapped = sorted(rmach & set(m2l))
    unmapped = sorted(rmach - set(m2l))

    print(f"  [changeover] joined via config.machine_to_changeover_line (from the plant's "
          f"machine list) — the matrix's line NAMES have zero overlap with the routing's IDs.")
    print(f"               {len(lut):,} (line, from_item, to_item) rules on "
          f"{lut['line'].nunique()} lines: {', '.join(sorted(set(lut['line'])))}")
    print(f"               routing machines MAPPED   : {len(mapped)}  {mapped}")
    print(f"               routing machines UNMAPPED : {len(unmapped)} -> v6's own rule "
          f"(mixing 2 min / default 15 min). Includes Building + curing: the matrix has no "
          f"line for them.")


# --------------------------------------------------------------- MG ASSIGNMENT
def mg_assignment() -> None:
    """Synthesise phase1a's output — we do NOT bypass v6's MG filter, we satisfy it.

    v6's phase1b (:364-383) and phase3 both REQUIRE `outputs/mg_assignment.csv` and abort
    without it ("Run phase1a first"). The filter is (phase1b:296-297):
        mask = (sub["__EQ__"] == mg) | (sub["__EQ__"] == "")

    Deleting that filter is NOT safe in general. In v6's BOM, `Equipment` denormalises
    2-4 MUTUALLY EXCLUSIVE bills per SKU (95 of 250 SKUs), so dropping the mask UNIONS
    alternative bills and inflates per-tyre demand up to 2.74x.

    CTP's BOM is SINGLE-VARIANT — verified: `Equipment` is 'TBM PCR' on all 42,411 rows,
    and every one of the 228 SKUs has exactly 1 distinct value. So we write the trivial
    assignment (each SKU -> its own single Equipment) and let v6's filter run UNCHANGED.
    With one variant it keeps every row — correct by construction, and the algorithm is
    untouched. phase1a itself is not needed: its only job was to CHOOSE among variants,
    and CTP has nothing to choose between.

    RE-CHECK THIS if the BOM is ever re-issued with more than one Equipment per SKU.
    """
    b = pd.read_csv(os.path.join(OUT, "bom.csv"), dtype=str, keep_default_na=False)
    b["EQ"] = b["Equipment"].astype(str).str.strip().str.upper()
    v = b.groupby("Super_parent")["EQ"].nunique()
    multi = v[v > 1]
    if len(multi):
        sys.exit(
            f"REFUSING TO PROCEED: {len(multi)} SKUs have >1 Equipment variant "
            f"({list(multi.index[:3])}). v6's MG filter picks ONE variant per SKU; without "
            f"phase1a there is no rule to choose. Omitting it would union mutually-exclusive "
            f"bills and inflate demand. Ask the user for the variant-selection rule.")
    mg = (b[b["Super_parent"].str.strip() != ""]
          .groupby("Super_parent")["EQ"].first().reset_index())
    mg.columns = ["sku", "MG"]
    prior = os.path.join(HERE, "outputs")
    os.makedirs(prior, exist_ok=True)
    mg.to_csv(os.path.join(prior, "mg_assignment.csv"), index=False)
    print(f"  outputs/mg_assignment.csv   {len(mg):>7,} rows  "
          f"(single-variant BOM: {sorted(set(b['EQ']))} — v6's MG filter is a no-op)")


def main() -> int:
    os.makedirs(OUT, exist_ok=True)
    print(f"CTP inputs  : {SRC}")
    print(f"v6 inputs   : {OUT}")
    print("-" * 62)
    bom()
    routing()
    plan()
    itemtype()
    aging()
    mpq()
    buffer()
    planning_max_aging()
    inventory()
    changeover()
    mg_assignment()
    print("-" * 62)
    print("NOT PROVIDED by CTP (v6 degrades gracefully — see MEMORY.md):")
    for f, why in [
        ("mg_preference.csv",      "MG excluded by user instruction"),
        ("belt_wire_mapping.csv",  "CTP has no belt-wire campaign data"),
        ("building_cycle_times.csv", "no per-machine cycle times; routing duration is used"),
        ("building_changeover.csv", "no per-machine building setup table"),
    ]:
        print(f"  {f:<26} {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
