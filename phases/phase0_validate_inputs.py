#!/usr/bin/env python3
"""
phase0_validate_inputs.py — sanity-check the updated 7 input files.

Validates:
  - File existence & encoding
  - BOM schema, dedup, encoding, unit sanity
  - Routing schema, operation coverage, UOM standardisation (MM/MIN canonical)
  - Aging Master coverage
  - ItemType Master coverage
  - MPQ ↔ ItemType label match
  - Plan parsing (tz-safe)
  - Cross-file integrity (BOM↔Routing, BOM↔ItemType, MPQ↔ItemType)
  - Belt-wire mapping + FRC campaign constants self-consistency  (NEW)
  - Month horizon from plan_params

Outputs phase0_data_validation.xlsx for planner inspection.
"""
from __future__ import annotations
import sys, pathlib, re, yaml
import pandas as pd

ROOT = pathlib.Path(__file__).resolve().parent.parent
INPUTS, OUTPUTS = ROOT/"inputs", ROOT/"outputs"


def _read(path):
    """Tolerant CSV reader — python engine + skip bad lines + dual encoding."""
    for enc in ("utf-8", "latin-1"):
        try:
            return pd.read_csv(path, encoding=enc, engine="python",
                               on_bad_lines="skip", dtype=str, quoting=0)
        except UnicodeDecodeError:
            continue
    raise IOError(f"Could not parse {path}")

# ============================================================
# DB-AWARE MASTER LOADER (auto-inserted by DB-only migration)
# Routes `load master` calls to MySQL when cfg["data_source"]=="db".
# Falls back to the existing local CSV read otherwise.
# ============================================================
def _db_or_csv(_key, _cfg=None):
    try:
        import sys as _s, pathlib as _p
        _s.path.insert(0, str(_p.Path(__file__).resolve().parent))
        from db_loader import load_input
        return load_input(_key, _cfg)
    except Exception as _e:
        # Fall back to CSV when DB or db_loader is unavailable
        _fp = (_cfg or {}).get("files", {})
        _fname = _fp.get(_key)
        if _fname is None:
            raise
        return _read(INPUTS / _fname)


def load_config():
    with open(ROOT/"config.yaml") as f:
        return yaml.safe_load(f)


def load_inputs(cfg):
    f = cfg["files"]
    bom = _db_or_csv("bom", cfg).rename(columns={
        "Super_parent":"Super parent", "grand_parent":"grand parent",
        "Parent_qty":"Parent qty", "Parent_unit":"Parent unit",
        "child_quantity":"child quantity", "child_Unit":"child Unit",
        "child_description":"child description"})
    out = {
        "bom":             bom,
        "routing":         _db_or_csv("routing", cfg),
        "aging_master":    _db_or_csv("aging_master", cfg),
        "itemtype_master": _db_or_csv("itemtype_master", cfg),
        "mpq":             _db_or_csv("mpq", cfg),
        "plan":            _db_or_csv("plan", cfg),
        # plan_params dropped — horizon is derived from plan.startTime.min() /
        # plan.endTime.max() inside each phase that needs it.
    }
    try:
        out["belt_wire_mapping"] = _db_or_csv("belt_wire_mapping", cfg)
    except Exception:
        out["belt_wire_mapping"] = pd.DataFrame(columns=["belt_item","wire_type"])
    return out


def save_xlsx(phase, name, sheets):
    """Write Excel — robust to OneDrive sync locks via tmp-then-copy."""
    import tempfile, shutil, os
    OUTPUTS.mkdir(exist_ok=True)
    dest = OUTPUTS / f"phase{phase}_{name}.xlsx"
    tmp_path = OUTPUTS / f".tmp_phase{phase}_{name}_updated.xlsx"
    try:
        with pd.ExcelWriter(tmp_path, engine="openpyxl") as w:
            for k, df in sheets.items():
                clean = re.sub(r"[\\/?*\[\]:]", "_", k)[:31]
                df.to_excel(w, sheet_name=clean, index=False)
        if dest.exists():
            try: dest.unlink()
            except Exception: pass
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.exists():
            try: tmp_path.unlink()
            except Exception: pass
    return dest


def parse_plan_to_curing(plan, selected_skus):
    df = plan.copy()
    df["SKUCode"] = df["skuCode"].astype(str).str.strip()
    if selected_skus:
        df = df[df["SKUCode"].isin(selected_skus)]
    df = df[df["SKUCode"] != "CHANGEOVER"]
    df["Qty"] = pd.to_numeric(df["qty"], errors="coerce").fillna(0)
    df = df[df["Qty"] > 0]
    if df.empty:
        return pd.DataFrame(columns=["SKUCode","Qty","effective_start","effective_end","block_id"])
    def parse_ts(s):
        r = pd.to_datetime(s, errors="coerce")
        if hasattr(r,"dt") and r.dt.tz is not None:
            return r.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        return r
    start = parse_ts(df["startTime"])
    end   = parse_ts(df["endTime"])
    if start.isna().any():
        ds = parse_ts(df["date"]).dt.strftime("%Y-%m-%d")
        start = start.fillna(pd.to_datetime(ds + " " + df["startTime"].astype(str), errors="coerce"))
    if end.isna().any():
        ds = parse_ts(df["date"]).dt.strftime("%Y-%m-%d")
        end = end.fillna(pd.to_datetime(ds + " " + df["endTime"].astype(str), errors="coerce"))
    overnight = end < start
    end = end.where(~overnight, end + pd.Timedelta(days=1))
    out = pd.DataFrame({
        "SKUCode": df["SKUCode"].values,
        "Qty":     df["Qty"].astype(int).values,
        "effective_start": start.values,
        "effective_end":   end.values,
        "block_id": [f"B{i:04d}" for i in range(len(df))],
    })
    return out.dropna(subset=["effective_start","effective_end"]).reset_index(drop=True)


def main():
    print("\n" + "=" * 65)
    print("  PHASE 0 — Input Data Validation (updated BOM + Routing)")
    print("=" * 65)
    cfg = load_config()
    inputs = load_inputs(cfg)
    sheets = {}
    summary = []
    def s(label, value): summary.append(f"  {label:45s}  {value}")

    # 1. Row counts
    counts = []
    for k, df in inputs.items():
        counts.append({"file": k, "rows": len(df),
                       "columns": ", ".join(list(df.columns)[:5]) + ("..." if len(df.columns)>5 else "")})
        s(f"{k} rows", f"{len(df):,}")
    sheets["00_row_counts"] = pd.DataFrame(counts)

    # 2. Plan parsing
    cur = parse_plan_to_curing(inputs["plan"], cfg.get("selected_skus", []))
    s("Curing blocks parsed", f"{len(cur):,}")
    s("Distinct SKUs in scope", f"{cur['SKUCode'].nunique() if not cur.empty else 0}")
    s("Total tyres in plan", f"{int(cur['Qty'].sum()) if not cur.empty else 0:,}")
    sheets["01_curing_blocks"] = cur

    # 3. BOM UOM
    bom = inputs["bom"]
    bom_p_uom = bom["Parent unit"].astype(str).str.strip().str.upper().value_counts()
    bom_c_uom = bom["child Unit"].astype(str).str.strip().str.upper().value_counts()
    sheets["02_bom_parent_uom"] = bom_p_uom.reset_index()
    sheets["03_bom_child_uom"]  = bom_c_uom.reset_index()
    s("BOM Parent_unit distinct", f"{len(bom_p_uom)}: {sorted(bom_p_uom.index.tolist())}")
    s("BOM child_Unit distinct", f"{len(bom_c_uom)}: {sorted(bom_c_uom.index.tolist())}")
    expected = {"NOS","KG","MM","MT","KL","M","G"}
    bad_uoms = sorted(set(bom_p_uom.index.tolist() + bom_c_uom.index.tolist()) - expected)
    if bad_uoms:
        s("WARN Unexpected BOM UOMs", str(bad_uoms))

    # 4. Encoding check
    moji_bom = bom[bom["child"].astype(str).str.contains("Â", na=False) |
                   bom["Parent"].astype(str).str.contains("Â", na=False)]
    moji_rt  = inputs["routing"][inputs["routing"]["routed_product"].astype(str).str.contains("Â", na=False)]
    s("BOM mojibake rows", f"{len(moji_bom):,}")
    s("Routing mojibake rows", f"{len(moji_rt):,}")
    sheets["04_bom_mojibake"] = moji_bom[["Super parent","Parent","child"]].drop_duplicates().head(100)

    # 5. BOM dedup diagnostic
    bom_dedup1 = bom.drop_duplicates(subset=["Super parent","Parent","child"])
    bom_dedup2 = bom_dedup1.drop_duplicates(subset=["Super parent","child"], keep="first")
    s("BOM raw rows", f"{len(bom):,}")
    s("BOM after (SKU,Parent,child) dedup", f"{len(bom_dedup1):,}")
    s("BOM after (SKU,child) dedup", f"{len(bom_dedup2):,}")

    # 6. BOM unit sanity
    bom["_q"] = pd.to_numeric(bom["child quantity"], errors="coerce")
    suspect_kg = bom[(bom["Parent unit"].str.upper().str.strip()=="NOS") &
                     (bom["child Unit"].str.upper().str.strip()=="KG") &
                     (bom["_q"] > 50)]
    suspect_mm = bom[(bom["Parent unit"].str.upper().str.strip()=="NOS") &
                     (bom["child Unit"].str.upper().str.strip()=="MM") &
                     (bom["_q"] > 100000)]
    sheets["05_suspect_kg_unit_bug"] = suspect_kg[["Super parent","Parent","child","child quantity","child Unit"]].drop_duplicates().head(50)
    sheets["06_suspect_mm_unit_bug"] = suspect_mm[["Super parent","Parent","child","child quantity","child Unit"]].drop_duplicates().head(50)
    s("Suspect KG unit-error rows", f"{len(suspect_kg):,}")
    s("Suspect MM > 100k per tyre rows", f"{len(suspect_mm):,}")

    # 7. Routing UOM
    rt = inputs["routing"]
    rt_uom = rt["proc_time_UOM"].astype(str).str.strip().str.upper().value_counts()
    sheets["07_routing_uom"] = rt_uom.reset_index()
    s("Routing UOM distinct", f"{sorted(rt_uom.index.tolist())}")
    s("Operations distinct", f"{rt['operation_name'].nunique()}")

    # 8. Operation coverage
    op_counts = rt["operation_name"].value_counts().reset_index()
    op_counts.columns = ["operation_name", "n_rows"]
    sheets["08_operations"] = op_counts
    expected_ops = {"Master Mixing","Final Mixing","FOUR ROLL CALENDAR","TRC","FULL WIDTH SLITTER",
                    "Cap Strip Slitter","MIDLAND BIAS CUTTER","PLY CUTTER","Belt Cutter","Cameroon Slitter",
                    "extrusion","CHAFER","BEAD WINDING PCR","AUTO AND MANUAL FILLERING","CAR BUILDING",
                    "GT BUILDING","curing"}
    found_ops = set(rt["operation_name"].astype(str).unique())
    missing_ops = sorted(expected_ops - found_ops)
    s("Operations in routing", f"{len(found_ops)}")
    if missing_ops:
        s("WARN Missing operations", str(missing_ops))

    # 9. BOM ↔ Routing linkage
    skus = set(cur["SKUCode"].unique()) if not cur.empty else set(rt["finished_product"].astype(str).unique())
    rt_sku = rt[rt["finished_product"].astype(str).isin(skus)]
    bom_sku = bom_dedup2[bom_dedup2["Super parent"].astype(str).isin(skus)]
    routed = set(rt_sku["routed_product"].astype(str).str.strip())
    bom_parents = set(bom_sku["Parent"].astype(str).str.strip())
    missing_routing = sorted(bom_parents - routed)
    sheets["09_bom_parents_no_routing"] = pd.DataFrame({"item_code": missing_routing})
    s("BOM parents without routing", f"{len(missing_routing):,}")

    # 10. ItemType
    itype = inputs["itemtype_master"]
    itype_codes = set(itype["ItemCode"].astype(str).str.strip())
    bom_codes = set(bom["Super parent"].astype(str)) | set(bom["Parent"].astype(str)) | set(bom["child"].astype(str))
    missing_itype = sorted(bom_codes - itype_codes)
    sheets["10_missing_itemtype"] = pd.DataFrame({"item_code": missing_itype})
    s("BOM codes missing from ItemType Master", f"{len(missing_itype):,}")
    skus_missing_itype = sorted(skus - itype_codes)
    s("Selected SKUs missing ItemType", f"{len(skus_missing_itype):,}")

    # 11. MPQ ↔ ItemType
    mpq = inputs["mpq"]
    mpq_types = set(mpq["Item Type"].astype(str).str.strip())
    itype_types = {str(t).strip() for t in itype["ItemType"].dropna().unique()}
    itype_types_upper = {t.upper() for t in itype_types}
    mpq_unmatched = sorted([t for t in mpq_types if t.upper() not in itype_types_upper])
    sheets["11_mpq_unmatched"] = pd.DataFrame({"mpq_label": mpq_unmatched})
    s("MPQ labels not in ItemType", f"{len(mpq_unmatched):,}")

    # 12. Aging coverage
    aging = inputs["aging_master"]
    aging_codes = set(aging["ItemCode"].astype(str).str.strip())
    chain_items = set(rt_sku["routed_product"].astype(str))
    no_aging = sorted(chain_items - aging_codes)
    sheets["12_chain_items_no_aging"] = pd.DataFrame({"item_code": no_aging})
    s("Chain items missing aging entry", f"{len(no_aging):,}")

    # 13. Routing completeness
    rt_q = pd.DataFrame({
        "column": ["proc_time","proc_time_UOM","batch_size","batch_UNIT","machines","efficiency"],
        "null_count": [int(rt_sku[c].isna().sum()) for c in
                       ["proc_time","proc_time_UOM","batch_size","batch_UNIT","machines","efficiency"]],
        "total_rows": [len(rt_sku)]*6,
    })
    sheets["13_routing_completeness"] = rt_q

    # 13b. Belt-wire mapping + FRC constants  (NEW)
    bw = inputs["belt_wire_mapping"]
    bw_n = len(bw)
    s("belt_wire_mapping rows", f"{bw_n:,}")
    if bw_n:
        req_cols = {"belt_item","wire_type"}
        missing = req_cols - set(bw.columns)
        if missing:
            s("WARN belt_wire_mapping missing cols", str(sorted(missing)))
        belt_items = set(bw["belt_item"].astype(str).str.strip()) if "belt_item" in bw.columns else set()
        bom_children = set(bom["child"].astype(str).str.strip())
        belt_not_in_bom = sorted(belt_items - bom_children)
        sheets["14a_belt_not_in_bom"] = pd.DataFrame({"belt_item": belt_not_in_bom})
        s("Belt items not in BOM as child", f"{len(belt_not_in_bom):,}")
        wires = bw["wire_type"].astype(str).str.strip() if "wire_type" in bw.columns else pd.Series(dtype=str)
        bad_wires = bw[wires.isin(["", "nan", "None"])] if len(wires) else pd.DataFrame()
        sheets["14b_blank_wire_types"] = bad_wires
        s("Mapping rows with blank wire_type", f"{len(bad_wires):,}")
        n_wires = wires.nunique() if len(wires) else 0
        s("Distinct wire types", f"{n_wires}")
        # group: how many belts per wire campaign
        if n_wires:
            grp = bw.groupby("wire_type")["belt_item"].apply(lambda x: ", ".join(sorted(x))).reset_index()
            grp.columns = ["wire_type", "belt_items"]
            sheets["14c_wires_to_belts"] = grp
    else:
        s("WARN belt_wire_mapping", "MISSING — FRC falls back to per-lot sizing")

    # FRC constants self-consistency
    fmax = float(cfg.get("frc_campaign_max_m", 0) or 0)
    lmax = float(cfg.get("frc_belt_lot_max_mm", 0) or 0)
    lmin = float(cfg.get("frc_belt_lot_min_mm", 0) or 0)
    cool = float(cfg.get("frc_cooldown_min", 0) or 0)
    pad  = bool(cfg.get("frc_pad_to_floor", False))
    fill = str(cfg.get("frc_cooldown_filler", "")).strip()
    s("FRC campaign max (m)", f"{fmax:,.0f}")
    s("FRC belt lot max (mm)", f"{lmax:,.0f}")
    s("FRC belt lot min (mm)", f"{lmin:,.0f}")
    s("FRC cool-down (min)", f"{cool:,.0f}")
    s("FRC pad-to-floor", str(pad))
    s("FRC cool-down filler", fill or "(unset)")
    frc_issues = []
    if lmin > lmax:
        frc_issues.append(f"frc_belt_lot_min_mm ({lmin:.0f}) > frc_belt_lot_max_mm ({lmax:.0f})")
    if lmax > fmax * 1000:
        frc_issues.append(f"frc_belt_lot_max_mm ({lmax:.0f}) > frc_campaign_max_m*1000 ({fmax*1000:.0f})")
    if fmax <= 0 or lmax <= 0 or lmin <= 0:
        frc_issues.append("one of FRC limits is <= 0")
    sheets["14d_frc_constants_issues"] = pd.DataFrame({"issue": frc_issues}) if frc_issues else pd.DataFrame({"issue": ["(none)"]})
    if frc_issues:
        s("WARN FRC constant issues", str(frc_issues))

    # 14. Month horizon — DERIVED FROM PLAN (max endTime across all blocks),
    # NOT from plan_params.planEndDate. The plan's last block-finish IS the
    # true horizon; plan_params can be stale or slightly earlier (off by hours).
    horizon = None
    plan_df = inputs.get("plan", pd.DataFrame())
    if not plan_df.empty and "endTime" in plan_df.columns:
        et = pd.to_datetime(plan_df["endTime"], errors="coerce")
        if et.notna().any():
            horizon = et.max()
    s("Month horizon (from plan max endTime)", str(horizon) if horizon is not None else "(unknown)")

    sheets["20_SUMMARY"] = pd.DataFrame({"check": [line.strip() for line in summary]})
    out = save_xlsx(0, "data_validation", sheets)
    print(f"\n[OK] Phase 0 output: {out}")
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
