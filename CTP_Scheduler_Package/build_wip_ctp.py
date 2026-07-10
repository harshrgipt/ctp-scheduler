"""
build_wip_ctp.py — CTP WIP inventory from the Smart/MES CSV exports, in the exact
`download 1.csv` (BTP) format.

Sources
  o_production            : excel_run_20260709_143509/  (one CSV per schema)
  i_productionconsumption : excel_run_20260709_150102/  (one CSV per schema)
  curing (cured GTs)      : SMARTMIS.dbo.curingpcr + curingtbr  (SQL Server)
  aging / item_type / unit: v6_wave_p2_updated/ctp_inputfiles/

Logic (identical to the BTP inventory_all script)
  mixing  : produced KG - consumed KG      [source_machine = mm]        expired + too_fresh
  stock   : produced qty - consumed qty    [all except mm / tbm stages] expired
  GT      : produced (tbm*stage2) minus cured (curingpcr/tbr)          3-day life
  carcass : produced (tbm*stage1, last 1 day) minus consumed by stage2  1-day life

Two CTP-specific data repairs (verified):
  1. productionID corruption: the export overwrites the FIRST TWO characters with
     \\x14\\x14 for some mixers (Mixer310F/431/432). The consumption export has the
     correct prefix, so we learn machineCode -> 2-char prefix from it and restore.
     Without this, mixing/carcass netting joins at only 19%.
  2. batchWeight is EMPTY, so the compound batch weight (KG) is derived as the sum of
     consumedQuantity of that batch's inputs (median ~230 KG; BTP ref 294.92 KG).

Output: inventory_ctp_wip.csv  (+ .xlsx) with download-1.csv columns.
"""
from __future__ import annotations
import os, re, glob, sys
from datetime import datetime
import pandas as pd

DL = r"C:\Users\91810\Downloads"
PROD_DIR = os.path.join(DL, "excel_run_20260709_143509", "excel_run_20260709_143509")
CONS_DIR = os.path.join(DL, "excel_run_20260709_150102", "excel_run_20260709_150102")
HERE = os.path.dirname(os.path.abspath(__file__))
MASTERS = os.path.join(HERE, "ctp_inputfiles")
# The mixing export `MM_O_production` is control-char corrupted AND has an empty
# batchWeight. `MM_O_productionM` is the clean equivalent: 0 corruption, batchWeight
# 99% populated, and a LiveQty column carrying the MES's own remaining KG.
MM_PRODM = os.path.join(MASTERS, "MM_O_productionM_20260515_0700_to_20260602_0700.csv")
OUT_CSV = os.path.join(HERE, "inventory_ctp_wip.csv")
OUT_XLSX = os.path.join(HERE, "inventory_ctp_wip.xlsx")

# window from the export filenames: 2026-05-15 07:00 -> 2026-06-02 07:00
AS_OF = datetime(2026, 6, 2, 7, 0, 0)
GT_MAX_AGE_DAYS = 3
CARCASS_MAX_AGE_DAYS = 1
CARCASS_LOOKBACK_DAYS = 1
UNIT_TO_HOURS = {"Hours": 1, "Days": 24, "Min": 1 / 60, "Month": 24 * 30}

MM_SM = "mm"
GT_SMS = {"tbmpcrstage2", "tbmtbrstage2"}
CARCASS_SMS = {"tbmpcrstage1", "tbmtbrstage1"}
CONSUMED_BY = GT_SMS                      # stage2 consumes stage1 carcasses
NON_STOCK = {MM_SM} | GT_SMS | CARCASS_SMS

_CTRL = re.compile(r"[\x00-\x1f]")


def _clean(s: pd.Series) -> pd.Series:
    return s.astype(str).str.replace(_CTRL, "", regex=True).str.strip()


def _schema_from(fname: str) -> str:
    """'TBMPCRStage1_O_production_2026...csv' -> 'tbmpcrstage1'"""
    base = os.path.basename(fname)
    return re.split(r"_[IO]_[Pp]roduction", base)[0].lower()


# --------------------------------------------------------------------------- #
# 1. LOAD PRODUCTION (all schemas)
# --------------------------------------------------------------------------- #
# dtandTime schemas use lowercase-ish cols; SyncTime schemas use PascalCase.
def _norm_prod(df: pd.DataFrame, schema: str) -> pd.DataFrame:
    cols = {c.lower(): c for c in df.columns}

    def pick(*names):
        for n in names:
            if n.lower() in cols:
                return cols[n.lower()]
        return None

    pid = pick("productionID", "ProductionID")
    ic = pick("itemCode", "ItemCode")
    inm = pick("ItemName")
    # PRODUCED-QUANTITY COLUMN (verified against how each lot is actually consumed):
    #   ProductionQuantityLength -> the 20 calender/extruder/cutter lines; matches the
    #                               consumption UOM (M or NOS).  Duplex/QuintoplexPCR also
    #                               have TotalQuantity, but that is KG -> netting it against
    #                               a NOS consumption produces garbage. PQL wins.
    #   Weight                   -> AC (its `quantity` is only a blend counter; SUOM=KG).
    #   quantity                 -> TBM stages (1.0 = one carcass / green tyre).
    #                               For MM `quantity` is ALSO only a blend counter and
    #                               batchWeight is empty, so mixing_inventory replaces it
    #                               with the mass-balance weight derived from consumption.
    qty = None
    for cand in ("ProductionQuantityLength", "Weight", "quantity", "TotalQuantity"):
        c = pick(cand)
        if c is not None and not df[c].astype(str).str.strip().eq("").all():
            qty = c
            break
    mch = pick("machineCode", "MachineCode")
    ts = pick("dtandTime", "SyncTime")
    bwt = pick("batchWeight")
    lqt = pick("LiveQty", "liveQty")
    out = pd.DataFrame({
        "production_id": _clean(df[pid]) if pid else "",
        "item_code": _clean(df[ic]) if ic else "",
        "item_name": _clean(df[inm]) if inm else "",
        "quantity": pd.to_numeric(df[qty], errors="coerce") if qty else pd.NA,
        "batch_weight": pd.to_numeric(df[bwt], errors="coerce") if bwt else pd.NA,
        "live_qty": pd.to_numeric(df[lqt], errors="coerce") if lqt else pd.NA,
        "machine_code": df[mch].astype(str).str.strip() if mch else "",
        "event_time": pd.to_datetime(df[ts], errors="coerce") if ts else pd.NaT,
    })
    # keep the RAW (uncleaned) ids so we can repair the 2-char prefix corruption
    out["_raw_pid"] = df[pid].astype(str) if pid else ""
    out["_raw_item"] = df[ic].astype(str) if ic else ""
    out["source_machine"] = schema
    return out


def load_production() -> pd.DataFrame:
    frames = []
    for f in sorted(glob.glob(os.path.join(PROD_DIR, "*_O_[Pp]roduction*.csv"))):
        schema = _schema_from(f)
        if schema == "mm":
            continue                     # corrupted; replaced by MM_O_productionM below
        df = pd.read_csv(f, dtype=str, keep_default_na=False, low_memory=False)
        if df.empty:
            continue
        frames.append(_norm_prod(df, schema))
        print(f"  [{schema}] {len(df):,} produced")
    # Mixing from the CLEAN file (real batchWeight + LiveQty, no control chars)
    dm = pd.read_csv(MM_PRODM, dtype=str, keep_default_na=False, low_memory=False)
    frames.append(_norm_prod(dm, "mm"))
    print(f"  [mm] {len(dm):,} produced  <- MM_O_productionM (clean, batchWeight+LiveQty)")
    prod = pd.concat(frames, ignore_index=True)

    # --- key hygiene (no inference, just removal of unusable rows) ---
    n0 = len(prod)
    blank = prod["production_id"].eq("") | prod["item_code"].eq("")
    prod = prod[~blank]
    n_blank = n0 - len(prod)
    # duplicate lot barcodes double-count WIP; keep the FIRST record per lot
    dup = prod.duplicated(subset=["source_machine", "production_id"], keep="first")
    n_dup = int(dup.sum())
    prod = prod[~dup].reset_index(drop=True)
    print(f"  hygiene: dropped {n_blank:,} blank-key rows, {n_dup:,} duplicate production_ids")
    return prod


def load_consumption() -> pd.DataFrame:
    frames = []
    pats = ["*_I_ProductionConsumption*.csv", "*_I_ProductionConsuption*.csv"]
    files = sorted({f for p in pats for f in glob.glob(os.path.join(CONS_DIR, p))})
    for f in files:
        schema = re.split(r"_I_[Pp]roduction[Cc]onsu", os.path.basename(f))[0].lower()
        df = pd.read_csv(f, dtype=str, keep_default_na=False, low_memory=False)
        if df.empty:
            continue
        cols = {c.lower(): c for c in df.columns}
        # The MES uses TWO different column namings for the same thing:
        #   TBM / MM / AC   : consumptionProductionID + consumedQuantity
        #   all other lines : ConsuptionProductionID (MISSPELLED) + QtyConsume
        # Missing the second form silently skips ~132k consumption rows and
        # OVERSTATES stock WIP (inputs never subtracted).
        cp = cols.get("consumptionproductionid") or cols.get("consuptionproductionid")
        cq = cols.get("consumedquantity") or cols.get("qtyconsume")
        pid = cols.get("productionid")
        mch = cols.get("machinecode")
        if not cp or not cq:
            print(f"  [{schema}] SKIPPED - no consumption key column ({list(df.columns)})")
            continue
        frames.append(pd.DataFrame({
            "consumption_production_id": _clean(df[cp]),
            "consumption_quantity": pd.to_numeric(df[cq], errors="coerce").fillna(0.0),
            "batch_production_id": _clean(df[pid]) if pid else "",
            "machine_code": df[mch].astype(str).str.strip() if mch else "",
            "source_machine": schema,
        }))
        print(f"  [{schema}] {len(df):,} consumption")
    cons = pd.concat(frames, ignore_index=True)
    n0 = len(cons)
    # A consumption row with no lot reference cannot net anything; a negative qty
    # would ADD inventory. Both are removed (no values invented).
    cons = cons[cons["consumption_production_id"].ne("")]
    n_blank = n0 - len(cons)
    neg = cons["consumption_quantity"] < 0
    n_neg = int(neg.sum())
    cons.loc[neg, "consumption_quantity"] = 0.0
    print(f"  hygiene: dropped {n_blank:,} rows with blank consumptionProductionID, "
          f"zeroed {n_neg:,} negative consumedQuantity")
    return cons.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 2. REPAIR the \x14\x14-corrupted productionID prefixes
# --------------------------------------------------------------------------- #
def repair_pids(prod: pd.DataFrame, cons: pd.DataFrame) -> pd.DataFrame:
    """The o_production export overwrites the first 2 chars of productionID with
    control bytes for some mixers. Learn machineCode -> real 2-char prefix from the
    consumption export (which is intact) and restore it."""
    mm_cons = cons[cons["source_machine"] == MM_SM]
    prefix = (mm_cons[mm_cons["batch_production_id"].str.len() > 3]
              .assign(p=lambda d: d["batch_production_id"].str[:2])
              .groupby("machine_code")["p"]
              .agg(lambda s: s.mode().iat[0] if len(s.mode()) else "")
              .to_dict())
    corrupt = prod["_raw_pid"].str.match(r"^[\x00-\x1f]{2}")
    n_before = int(corrupt.sum())
    if n_before:
        fixed = prod.loc[corrupt, "machine_code"].map(prefix).fillna("") + \
                prod.loc[corrupt, "_raw_pid"].str.replace(r"^[\x00-\x1f]+", "", regex=True)
        prod.loc[corrupt, "production_id"] = fixed
    print(f"  repaired {n_before:,} corrupted production_ids  (prefix map: {prefix})")
    return prod.drop(columns=["_raw_pid"])


def drop_corrupted(prod: pd.DataFrame) -> pd.DataFrame:
    """STRICT MODE: the MES overwrites the FIRST TWO characters of productionID and
    itemCode with control bytes on some machines (e.g. 'MB184' -> '\\x14\\x14184').
    Those two characters are LOST — they cannot be recovered by exact match, only
    guessed. We therefore DROP those rows rather than invent an item code."""
    bad_pid = prod["_raw_pid"].str.contains(r"[\x00-\x1f]", regex=True, na=False)
    bad_item = prod["_raw_item"].str.contains(r"[\x00-\x1f]", regex=True, na=False)
    bad = bad_pid | bad_item
    n = int(bad.sum())
    by = prod.loc[bad, "source_machine"].value_counts().to_dict()
    print(f"  DROPPED {n:,} rows with MES control-char corruption (unrecoverable "
          f"leading 2 chars) -> {by}")
    return prod[~bad].drop(columns=["_raw_pid", "_raw_item"]).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# 3. MASTERS from ctp_inputfiles
# --------------------------------------------------------------------------- #
def load_masters():
    ag = pd.read_csv(os.path.join(MASTERS, "jkt_aging_master_pcr.csv"), dtype=str, keep_default_na=False)
    ag["ItemCode"] = _clean(ag["ItemCode"])
    for c in ("MaxAging", "MinAging"):
        ag[c] = pd.to_numeric(ag[c], errors="coerce")
    it = pd.read_csv(os.path.join(MASTERS, "jkt_itemType_master_pcr.csv"), dtype=str, keep_default_na=False)
    item_type_map = dict(zip(_clean(it["ItemCode"]), it["ItemType"].str.strip()))
    bom = pd.read_csv(os.path.join(MASTERS, "jkplanning_CTP_jkt_bom_pcr.csv"), dtype=str,
                      keep_default_na=False, usecols=["child", "child_Unit"])
    bom["child"] = _clean(bom["child"])
    bom["child_Unit"] = bom["child_Unit"].str.strip().replace("MM", "M")
    unit_map = dict(zip(bom["child"], bom["child_Unit"]))
    return ag, item_type_map, unit_map


def resolve_codes(codes: pd.Series, known: set) -> pd.Series:
    """MES item codes sometimes carry a trailing station suffix (e.g. the AC chemical
    station writes 'T638FC_0104'). Keep the code if it already matches a master,
    otherwise fall back to the suffix-stripped form when THAT matches. This is the CTP
    equivalent of BTP's PLCBOMName -> MaterialCode_O hop."""
    stripped = codes.str.replace(r"_\d+$", "", regex=True)
    use_stripped = (~codes.isin(known)) & stripped.isin(known)
    return codes.where(~use_stripped, stripped)


def attach_aging(inv, ag, as_of, with_too_fresh):
    inv = inv.merge(ag[["ItemCode", "MaxAging", "MaxAgingUnit", "MinAging", "MinAgingUnit"]],
                    left_on="itemcode", right_on="ItemCode", how="left")
    inv["produced_time"] = pd.to_datetime(inv["produced_time"], errors="coerce")
    max_h = inv["MaxAging"] * inv["MaxAgingUnit"].map(UNIT_TO_HOURS)
    expiry = inv["produced_time"] + pd.to_timedelta(max_h, unit="h")
    inv["expired"] = expiry.notna() & (expiry < as_of)
    if with_too_fresh:
        min_h = inv["MinAging"] * inv["MinAgingUnit"].map(UNIT_TO_HOURS)
        mat = inv["produced_time"] + pd.to_timedelta(min_h, unit="h")
        inv["too_fresh"] = mat.notna() & (mat > as_of)
    return inv.drop(columns=[c for c in ["ItemCode"] if c in inv.columns])


# --------------------------------------------------------------------------- #
# 4. BUCKETS
# --------------------------------------------------------------------------- #
def consumed_by_lot(cons):
    return cons.groupby("consumption_production_id", as_index=False)["consumption_quantity"].sum()


def mixing_inventory(prod, cons, ag, as_of, known=frozenset()):
    """Mixing WIP = LiveQty, the MES's OWN remaining KG for the compound batch.

    Why not batchWeight - consumed (the BTP formula)? Because consumption that happened
    OUTSIDE the export window is invisible, so that formula reports lots as un-consumed
    (it even yields negative values, e.g. batchWeight 200 - consumed 220). Verified:
    wherever LiveQty > 0, `batchWeight - consumed` agrees with it EXACTLY (median diff
    0.0 KG) — LiveQty is simply the complete, exact figure. No derivation, no assumption.
    """
    lots = prod[prod["source_machine"] == MM_SM].copy()
    lots = lots[lots["live_qty"].notna() & (lots["live_qty"] > 0)]
    inv = lots[["production_id", "item_code", "event_time", "live_qty"]].rename(
        columns={"item_code": "itemcode", "event_time": "produced_time",
                 "live_qty": "inventory"})
    inv["itemcode"] = resolve_codes(inv["itemcode"], known)
    return attach_aging(inv, ag, as_of, with_too_fresh=True)


def stock_inventory(prod, cons, ag, as_of, known=frozenset()):
    lots = prod[~prod["source_machine"].isin(NON_STOCK)].copy()
    lots["quantity"] = pd.to_numeric(lots["quantity"], errors="coerce")
    used = consumed_by_lot(cons)
    m = lots.merge(used, left_on="production_id", right_on="consumption_production_id",
                   how="left").fillna({"consumption_quantity": 0})
    m = m[m["consumption_quantity"] <= m["quantity"]]
    m["inventory"] = m["quantity"] - m["consumption_quantity"]
    inv = m[["production_id", "item_code", "event_time", "inventory"]].rename(
        columns={"item_code": "itemcode", "event_time": "produced_time"})
    inv["itemcode"] = resolve_codes(inv["itemcode"], known)
    return attach_aging(inv, ag, as_of, with_too_fresh=False)


def gt_inventory(prod, cured: set, ag, as_of):
    """Green tyres built in the window and NOT present in the curing table (exact
    barcode match). item_type comes from the producing machine (TBM stage-2 = green
    tyre — a fact, not a code guess). Aging is taken ONLY from the aging master."""
    gt = prod[prod["source_machine"].isin(GT_SMS)].copy()
    gt["barcode"] = gt["production_id"].str.extract(r"(\d+)$")[0]
    gt = gt[gt["barcode"].notna()]
    gt["event_time"] = pd.to_datetime(gt["event_time"], errors="coerce")
    gt = gt[~gt["barcode"].isin(cured)]
    out = gt[["production_id", "event_time", "item_code"]].rename(
        columns={"event_time": "produced_time", "item_code": "itemcode"}).reset_index(drop=True)
    out["inventory"] = 1
    out["item_type"] = "GREEN_TYRE"        # from the machine, not from code matching
    return attach_aging(out, ag, as_of, with_too_fresh=False)


def carcass_inventory(prod, cons, ag, as_of):
    """Carcasses built at TBM stage-1 and NOT consumed by stage-2 (exact production_id
    match). item_type/aging come from the CTP masters by exact ItemCode match."""
    car = prod[prod["source_machine"].isin(CARCASS_SMS)].copy()
    car["event_time"] = pd.to_datetime(car["event_time"], errors="coerce")
    discard = set(cons[cons["source_machine"].isin(CONSUMED_BY)]["consumption_production_id"])
    car = car[~car["production_id"].isin(discard)]
    out = car[["production_id", "event_time", "item_code"]].rename(
        columns={"event_time": "produced_time", "item_code": "itemcode"}).reset_index(drop=True)
    out["inventory"] = 1
    return attach_aging(out, ag, as_of, with_too_fresh=False)


# --------------------------------------------------------------------------- #
# 5. CURING (cured GT barcodes) from SMARTMIS
# --------------------------------------------------------------------------- #
def load_cured(start, end) -> set:
    try:
        import pyodbc
        drv = next(("{" + p + "}") for p in ("ODBC Driver 18 for SQL Server",
                                             "ODBC Driver 17 for SQL Server", "SQL Server")
                   if p in pyodbc.drivers())
        cn = pyodbc.connect(f"DRIVER={drv};SERVER=192.168.230.108,1433;DATABASE=SMARTMIS;"
                            f"UID=Algo;PWD=Algo@2025;TrustServerCertificate=yes;", timeout=30)
        out = set()
        for t in ("curingpcr", "curingtbr"):
            d = pd.read_sql(f"SELECT gtbarCode FROM dbo.{t} WHERE dtandTime >= ? AND dtandTime < ?",
                            cn, params=[start, end])
            out |= set(_clean(d["gtbarCode"]))
            print(f"  [SMARTMIS.{t}] {len(d):,} cured")
        cn.close()
        return out
    except Exception as e:
        print(f"  [curing] WARN could not load ({e}); GT will not be netted by curing")
        return set()


# --------------------------------------------------------------------------- #
def main():
    print("=" * 66)
    print(f"CTP WIP INVENTORY  |  as on {AS_OF:%Y-%m-%d %H:%M}")
    print("=" * 66)
    print("\n[production]")
    prod = load_production()
    print("\n[consumption]")
    cons = load_consumption()
    print("\n[strict: drop MES-corrupted rows]")
    prod = drop_corrupted(prod)
    ag, item_type_map, unit_map = load_masters()
    known = set(ag["ItemCode"]) | set(item_type_map)

    print("\n[curing]  (full export window; a GT not in curing is still WIP)")
    cured = load_cured(AS_OF - pd.Timedelta(days=18), AS_OF)

    print("\n[buckets]")
    res = {
        "mixing": mixing_inventory(prod, cons, ag, AS_OF, known),
        "stock": stock_inventory(prod, cons, ag, AS_OF, known),
        "gt": gt_inventory(prod, cured, ag, AS_OF),
        "carcass": carcass_inventory(prod, cons, ag, AS_OF),
    }
    # item_type / unit: EXACT master lookup only, no fallback, no inference.
    for k, d in res.items():
        if not len(d):
            continue
        if "item_type" not in d.columns:
            d["item_type"] = d["itemcode"].map(item_type_map)
        else:
            d["item_type"] = d["item_type"].fillna(d["itemcode"].map(item_type_map))
        d["unit"] = d["itemcode"].map(unit_map)
        res[k] = d

    # drop expired (BTP behaviour)
    for k, d in list(res.items()):
        if "expired" in d.columns:
            res[k] = d[~d["expired"].fillna(False)].drop(columns=["expired"])

    keep = ["production_id", "produced_time", "itemcode", "item_type", "unit",
            "inventory", "MaxAging", "MaxAgingUnit", "MinAging", "MinAgingUnit"]
    parts = []
    for k, d in res.items():
        d = d.copy()
        for c in keep:
            if c not in d.columns:
                d[c] = pd.NA
        parts.append(d[keep])
        print(f"  {k:8s}: {len(d):,} lots")
    combined = pd.concat(parts, ignore_index=True)
    combined["updatedAt"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    combined.to_csv(OUT_CSV, index=False)
    try:
        combined.to_excel(OUT_XLSX, index=False)
    except PermissionError:
        alt = OUT_XLSX.replace(".xlsx", "_new.xlsx")   # the file is open in Excel
        combined.to_excel(alt, index=False)
        print(f"  {os.path.basename(OUT_XLSX)} is open in Excel -> wrote {os.path.basename(alt)}")
    except Exception as e:
        print(f"  xlsx skipped: {e}")
    print(f"\ncombined : {len(combined):,} rows -> {OUT_CSV}")
    lab = combined["item_type"].astype(str).str.strip()
    print(f"item_type identified: {(lab.ne('') & lab.ne('nan')).mean()*100:.0f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
