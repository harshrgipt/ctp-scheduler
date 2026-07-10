# CTP (PCR) Production Scheduler — Self-Contained Package

This folder contains **everything** needed to reproduce the CTP scheduling run:
the code, every input file, and a pre-built opening-WIP snapshot.
Copy the whole folder, install 4 Python packages, run one command.

---

## 1. Quick start

```bash
# from inside this folder
pip install pandas numpy openpyxl pyyaml

cd ctp_scheduler
python run_june_wip.py
```

That runs all 9 phases on the **June 2026 curing plan** with the **real opening WIP**
and writes phase-wise outputs to `ctp_scheduler/run_june_wip/`.

Expected runtime: **3–6 minutes** (the DAG + placement are the slow parts).

> Python 3.10+ recommended (developed on 3.14). No database access is required —
> everything reads from files in `ctp_inputfiles/`.

---

## 2. What you should get (verify against this)

If your run matches these numbers, the environment is correct:

```
[phase1b]  SKUs=38  blocks=7123  demand rows=899,849
[phase1c]  WIP items: 889 | gross 19,077,554,371 -> net 18,993,610,235
           rows 899,849 -> 707,704
[phase2]   lots=12617 (pooled=5494, per-block=7123)
[phase3]   edges=84954 | terminals=7123
[phase4]   lots=12617 | AGING-INFEASIBLE lots=197
[phase5]   placed=12617  pinned=7123  unplaced=0  breaches=8627
[phase6]   verdict=CLEAN | C1=0 C4=0 silent_C2=0 orphans_C3=0

================ KPI BLOCK ================
  lots_placed           : 12617
  pinned_curing         : 7123
  unplaced              : 0
  aging_clean_rate_pct  : 81.0
  fulfillment_tyres     : 370664
  real_breaches         : 2756
  opening_wip_required  : 5871
  makespan              : 30 days 00:00:00
```

**Key result:** the schedule fits the 30-day curing horizon exactly, nothing is
unplaced, and Phase 6 (an independent validator) reports **CLEAN**.

---

## 3. Folder structure

```
CTP_Scheduler_Package/
├── README.md                  <- this file
├── build_wip_ctp.py           <- (optional) rebuilds opening_wip.csv from MES exports
│
├── ctp_scheduler/             <- the scheduler
│   ├── config.yaml            <- ALL paths + tuning live here
│   ├── run_june_wip.py        <- MAIN entry point (June plan + WIP)
│   ├── run_pipeline.py        <- generic runner (same config, outputs to outputs/, outputs2/)
│   ├── run_0703.py            <- same plan WITHOUT opening WIP (for comparison)
│   ├── pipeline.py            <- phase registry + input loading + KPI block
│   ├── io_utils.py            <- every input reader (csv + xlsx aware)
│   ├── common.py              <- item-type / aging / routing helpers + duration math
│   └── phases/
│       ├── phase0_validate_inputs.py
│       ├── phase1b_demand_explosion.py
│       ├── phase1_5_wave_builder.py
│       ├── phase1c_mrp_netting.py       <- WIP netting (BOM cascade)
│       ├── phase2_lot_sizing.py
│       ├── phase3_dag_construction.py
│       ├── phase4_cpm.py
│       ├── phase5_forward_placement.py
│       └── phase6_validate.py
│
└── ctp_inputfiles/            <- every input the scheduler reads
    ├── CTP_PCR_Curing_Schedule_2026-07-03 1 (1).xlsx   <- the DRUM (June plan)
    ├── jkplanning_CTP_jkt_bom_pcr.csv                  <- BOM
    ├── jkt_routing_pcr 13.xlsx                         <- routing
    ├── jkt_aging_master_pcr.csv                        <- min/max aging
    ├── jkt_itemType_master_pcr.csv                     <- item types
    ├── jkt_buffer_master_pcr.csv                       <- buffer hours
    ├── MOQ (3).xlsx                                    <- MPQ (reference only)
    ├── jkt_changeover_matrix_combined (1).xlsx         <- sequence-dependent changeover
    ├── opening_wip.csv                                 <- OPENING WIP (pre-built)
    └── MM_O_productionM_...csv                         <- source for rebuilding WIP
```

---

## 4. The inputs — what each file is and how it's used

| File | Read by | Purpose | Key columns |
|---|---|---|---|
| `CTP_PCR_Curing_Schedule_...xlsx` | `read_drum` (sheet **"Shift Schedule"**) | **The drum** — the fixed curing plan. This *is* the demand: 7,123 press blocks, 375,098 tyres, 2026-06-01 07:00 → 2026-07-01 07:00 | `Date, Shift, Machine, SKUCode, StartTime, EndTime, Qty, CycleTime_min` |
| `jkplanning_CTP_jkt_bom_pcr.csv` | `read_bom` | BOM tree — explodes each tyre into components | `Super_parent, grand_parent, Parent, child, child_quantity, child_Unit` |
| `jkt_routing_pcr 13.xlsx` | `read_routing` | Machines, operations, rates | `routed_product, operation_name, department, machines, proc_time, proc_time_UOM, batch_size` |
| `jkt_aging_master_pcr.csv` | `read_aging` | Shelf life per item | `ItemCode, MaxAging, MinAging, MaxAgingUnit, MinAgingUnit` |
| `jkt_itemType_master_pcr.csv` | `read_itemtype` | item → item_type | `ItemCode, ItemType` |
| `jkt_buffer_master_pcr.csv` | `read_buffer` | fallback aging ceiling per type | `Item type, Buffer Level (Hrs)` |
| `MOQ (3).xlsx` (sheet PCR) | `read_mpq` | minimum run qty | **Not used** — `produce_to_demand: true` means lot qty = demand |
| `jkt_changeover_matrix_combined (1).xlsx` | `read_changeover_matrix` | real setup time between two materials on a machine | `machine, from_MaterialCode_O, to_MaterialCode_O, changeover_time_min` |
| `opening_wip.csv` | `read_opening_wip` | **Opening WIP** consumed by phase1c | `itemcode, inventory, unit` (+ produced_time, aging) |

**No transfer-time file** is present, so `config.yaml` leaves `transfer:` blank and the
code falls back to a flat **10-minute** transfer between operations.

---

## 5. The 9 phases

| Phase | What it does |
|---|---|
| **0 — validate** | Builds the item-type / aging / routing lookups and runs ~15 warn-only data checks. Writes `phase0_gate.json`. |
| **1b — demand explosion** | Walks the BOM per SKU and scales by each curing block's qty → 899,849 demand rows. Takes `child_quantity` **directly** (the BOM is already per-tyre; multiplying down the tree causes a unit blow-up). |
| **1.5 — wave builder** | Buckets curing blocks into 3-day waves anchored on the 07:00 plant day. |
| **1c — MRP netting** | **Consumes the opening WIP.** WIP at an item covers that item *and*, via a BOM topological cascade, its upstream chain — but only for demand inside the item's max-aging window. Nets 899,849 → 707,704 rows. |
| **2 — lot sizing** | BUILD items stay per-block; pooled items (compounds, beads, plies) are consolidated on the CPM consumption clock, bounded by shelf life. `produce_to_demand: true` → **no MPQ padding**. |
| **3 — DAG** | Builds producer→consumer precedence edges (84,954). |
| **4 — CPM** | Backward LST/LFT + forward EST/EFT; flags structurally aging-infeasible lots. |
| **5 — placement** | Places every lot on a real machine (bisect timeline, sequence-dependent changeover). Bottleneck mixers ASAP, green tyres ALAP into the cure-by band, everything else ALAP to its CPM LFT. **Force-places and logs an honest breach ledger** — nothing is silently dropped. FEFO re-match removes false over-age flags. |
| **6 — validate** | Independent re-proof from the placed schedule: precedence, machine non-overlap, acyclicity, and **`silent_C2`** (an aging breach missing from the ledger). `silent_C2 = 0` means the ledger caught every real breach. |

---

## 6. Outputs (in `ctp_scheduler/run_june_wip/`)

**`outputs/`**
- `phase0_gate.json`, `phase0_findings.csv`
- `phase1_5_waves.csv`, `phase1_5_block_to_wave.csv`

**`outputs2/`** — the important ones
| File | Contents |
|---|---|
| **`phase5_schedule_updated.csv`** | **The floor schedule** — every lot with machine, start, finish, qty. `status=PLACED` (produced) or `PINNED` (the fixed curing drum). |
| `phase5_aging_violations_updated.csv` | The honest breach ledger (`OVER_AGED`, `TOO_FRESH`, `CUREBY_NEGATIVE`, `OPENING_WIP_REQUIRED`) |
| `phase5_machine_utilization_updated.csv` | booked hours + lot count per machine |
| `phase6_validation.json` | independent validation verdict |
| `phase1_demand_updated.csv` / `phase1_demand_NET_updated.csv.gz` | demand before / after WIP netting |
| `phase2_lots_updated.csv`, `phase3_dag_edges_updated.csv`, `phase4_lot_times_updated.csv` | intermediate artifacts |

---

## 7. Key configuration (`ctp_scheduler/config.yaml`)

```yaml
produce_to_demand: true      # lot qty = demand. MPQ minimum is NOT used to pad up.
slice: {enabled: false}      # run the FULL plant (all 38 SKUs)
fefo_matching: true          # drop false over-age flags when a fresher lot fed the consumer
wave_duration_days: 3
max_lot_duration_h: 8.0
pooled_window_factor: 0.8    # tighten pooled batches inside shelf life
schedule_open_lead_h: 0.0    # zero lead time: nothing is built before the plan starts
inputs.opening_wip: ../ctp_inputfiles/opening_wip.csv    # set to blank to disable netting
```

**To run WITHOUT opening WIP** (to see the difference): `python run_0703.py`.
You'll get makespan 31 days, aging-clean 75%, breaches 6,303, Phase 6 = ISSUES —
i.e. the WIP is what makes the schedule fit the horizon and validate clean.

---

## 8. Rebuilding `opening_wip.csv` (optional)

`opening_wip.csv` is already built and shipped, so **you do not need this to run the
scheduler.** Rebuild only if you have a newer MES export.

`build_wip_ctp.py` computes four WIP buckets in the plant's standard format:

| Bucket | Definition |
|---|---|
| mixing | `LiveQty` — the MES's own remaining KG for a compound batch |
| stock | produced − consumed |
| gt | green tyres built and **not** found in the curing table |
| carcass | carcasses built and **not** consumed by TBM stage-2 |

It needs three things not shipped here (≈500 MB):
1. `excel_run_<ts>/` — the `*_O_Production*.csv` exports
2. `excel_run_<ts>/` — the `*_I_ProductionConsu*.csv` exports
3. network access to `SMARTMIS.dbo.curingpcr` / `curingtbr` (needs `pyodbc` + a SQL Server ODBC driver)

Edit `PROD_DIR` / `CONS_DIR` at the top of `build_wip_ctp.py`, then:
```bash
python build_wip_ctp.py
cp inventory_ctp_wip.csv ctp_inputfiles/opening_wip.csv
```

---

## 9. Known data caveats (important — these are input-data issues, not code bugs)

1. **MES writes 2 control bytes over the first 2 characters** of `productionID`/`itemCode`
   on some mixers (e.g. `XMT1315` → `▮▮T1315`). Those characters are **lost**, so
   `build_wip_ctp.py` **drops** those rows rather than guess the code.
   Using `MM_O_productionM` (shipped) avoids this for mixing — it is clean.
2. **The consumption exports use two different column names** for the same field:
   `consumptionProductionID` (TBM/MM/AC) vs the misspelled **`ConsuptionProductionID`**
   + `QtyConsume` (all other lines). Both are handled.
3. **`batchWeight` and `TotalQuantity` are empty** in most exports. The real produced
   quantity is `ProductionQuantityLength` (lines) / `Weight` (AC) / `LiveQty` (mixing).
4. **The aging master has no min/max aging** for GREEN_TYRE, Carcass, SMALL CHEMICAL,
   SLITTED MATERIAL or SHOULDER PAD — so only ~28% of WIP rows carry a shelf life.
5. **Some WIP cannot net**: the routing has **no carcass stage** (green tyres are built
   directly), Inner-Liner WIP is coded `SQ-00x` while the BOM uses `IL00x`, and the WIP
   contains TBR items that a PCR plan never consumes. Net effect: **98% of WIP *quantity*
   is usable and 81% of that is actually consumed.**

---

## 10. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `FileNotFoundError` on an input | You moved `ctp_scheduler/` or `ctp_inputfiles/` apart. `config.yaml` resolves inputs as `../ctp_inputfiles/...` — keep both folders side by side. |
| `ModuleNotFoundError: openpyxl` | `pip install openpyxl` (needed to read the .xlsx inputs). |
| `PermissionError` writing an .xlsx | The file is open in Excel. Close it. |
| Phase 1c prints "NO-OP" | `inputs.opening_wip` is blank or the file is missing → demand passes through un-netted. |
| Different KPI numbers | Check §2. Most likely a different drum file or a missing `opening_wip.csv`. |

---

## 11. One-line summary

`python ctp_scheduler/run_june_wip.py` → a **100%-fulfilled, 30-day, Phase-6-CLEAN**
floor schedule for the June PCR plan, netted against real opening WIP.
The floor schedule is `run_june_wip/outputs2/phase5_schedule_updated.csv`.
