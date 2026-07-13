# ctp_v6 — CTP (PCR) Production Scheduler


A finite-capacity production scheduler for **JK Tyre CTP Chennai (PCR)**.

It runs the **`v6_wave_p2` algorithm** — the scheduler already proven at BTP Banmore — against
CTP's data. The phase files are **byte-identical copies** of v6's, except for **three
deliberate, documented deviations** (see below). All CTP-specific data adaptation is isolated in
a single file, `adapt_inputs.py`, so the algorithm itself never has to know it is running on a
different plant.

---

## Quick start

```bash
git clone <this-repo>
cd ctp_v6

python -m venv .venv
# Windows:  .venv\Scripts\activate
# macOS/Linux:  source .venv/bin/activate

pip install -r requirements.txt

python run_all.py
```

That's it. Python **3.10+**. Everything needed is bundled — **no database, no network, no
external paths.** Every path resolves relative to the script, so it runs from any directory.

**⚠️ Out of the box this stops at phase 2 with a `ValueError`. That is expected and correct —
see [Known blocker](#-known-blocker) below.**

### Running parts of it

```bash
python adapt_inputs.py            # rebuild inputs/ from ctp_inputfiles/ only
python run_pipeline.py            # run the phases (assumes inputs/ exists)
python run_pipeline.py --from phase4
python run_pipeline.py --only phase5
python run_pipeline.py --skip phase0

python make_data_issues_report.py # -> CTP_DATA_ISSUES_FOR_PLANT.xlsx
```

---

## How it works

```
ctp_inputfiles/          the plant's raw masters (BOM, routing, curing plan, WIP, …)
        │
        │   adapt_inputs.py     <-- THE ONLY PLACE CTP DIFFERS FROM v6
        ▼
inputs/                  the same data, in v6's exact schema   (generated, git-ignored)
        │
        │   run_pipeline.py     <-- runs v6's phases, in v6's order
        ▼
outputs/  outputs2/      lots, DAG, CPM windows, the schedule  (generated, git-ignored)
```

`inputs/`, `outputs/` and `outputs2/` are **derived** — they are rebuilt on every run and are
not committed. Only `ctp_inputfiles/` is source data.

### The phases

| # | phase | what it does |
|---|---|---|
| 0 | `phase0_validate_inputs` | Validates the inputs. Report only — safe to `--skip`. |
| 1b | `phase1b_demand_explosion` | Curing plan → BOM explosion → per-item demand |
| 1.5 | `phase1_5_wave_builder` | 3-day waves, plus the load-balance pass |
| 1c | `phase1c_mrp_netting` | MRP netting — consumes opening WIP down the BOM |
| 2 | `phase2_lot_sizing` | Lot sizing, wave-aware bucketing, MPQ floor |
| 3 | `phase3_dag_construction` | Producer→consumer DAG + the aging edge filter |
| 4 | `phase4_cpm` | CPM time windows (EST / EFT / LST / LFT) |
| 5 | `phase5_forward_placement_v2` | Finite-capacity forward placement onto machines |
| 6 | `phase6_curing_revision` | Curing revision |

**`phase1a` (machine-group assignment) is not run** — CTP has no MG model. But it is not
*bypassed*: `adapt_inputs.py` synthesises its output, so v6's MG filter still runs **exactly as
written**. CTP's BOM is single-variant (`TBM PCR` on all 42,411 rows), which makes that filter a
no-op — correct by construction. The adapter **refuses to run** if a multi-variant BOM ever
appears, because then the filter would stop being a no-op and silently inflate demand.

---

## The three deviations from v6

Everything else is **byte-identical** to `v6_wave_p2/phases/`. Verify with `diff`, don't take
my word for it.

| # | what | why |
|---|---|---|
| 1 | **Efficiency from the routing** | v6 hardcodes `eff = 1.0`. CTP's routing states **0.96**. `eff` divides, so every operation takes **+4.17%** longer. |
| 2 | **Transfer time from the routing** | v6 never reads `transfer_time_min`. It is a mandatory producer→consumer lag — the same shape as `min_aging` — so it is **added to `min_aging`**. Green tyres get a **90-minute** building→curing floor. |
| 3 | **The plant changeover matrix** | v6 has none: its `compute_changeover_min` is machine-**name**-based (`WBC`/`LTBC`/`FISCHER`…) and matches **0 of CTP's 123 machine IDs**, so everything would fall to a flat 15 min. |

Deviations 1 and 2 touch `phase2`, `phase4`, `phase5`. Deviation 3 touches `phase5`.

> **On deviation 3 — the join is the hard part.** The matrix is keyed on a machine **line name**
> (`"4 RC Calendar"`); the routing is keyed on a machine **ID** (`"901"`). **Zero string
> overlap**, and no bridge exists in any input file. The bridge is
> **`machine_to_changeover_line` in `config.yaml`**, built from the plant's own machine list
> (`ctp_inputfiles/pcr machines.png`). **Review that table.** `adapt_inputs.py` hard-fails if it
> names a line the matrix does not contain, so it cannot rot silently.
>
> Curing, GT-building and the mixers have **no line in the matrix** and correctly fall back to
> v6's rule — so **building still uses a flat 15 min changeover.**

---



---

## Files

| file | |
|---|---|
| `run_all.py` | **start here** — adapt inputs, then run every phase |
| `adapt_inputs.py` | the only place CTP differs from v6. Every reshape is documented in-line. |
| `run_pipeline.py` | runs the phases in v6's order, with preflight guards |
| `config.yaml` | v6's tuning values + CTP paths + the changeover machine bridge |
| `MEMORY.md` | **the important one.** Every CTP fact, hazard, and design decision. |
| `make_data_issues_report.py` | writes the plant-team data-gap workbook |
| `phases/` | v6's algorithm. Only 3 files differ, all marked `CTP DEVIATION`. |
| `ctp_inputfiles/` | the plant's raw masters (bundled — this repo is self-contained) |

### Inputs

| file | what |
|---|---|
| `jkt_bom_pcr 13 (1).xlsx` | bill of materials |
| `jkt_routing_pcr 14.xlsx` | routing — operations, machines, cycle times, transfer, efficiency |
| `CTP_PCR_Curing_Schedule_2026-07-03 (1).xlsx` | the curing plan (the drum) |
| `opening_wip.csv` | **opening WIP** — 39,510 rows. Netted in phase 1c. |
| `jkt_itemType_master_pcr.csv` | item types |
| `jkt_aging_master_pcr.csv` | min/max aging |
| `MOQ (3).xlsx` | MPQ — **minimum only; CTP has no maximum** |
| `jkt_buffer_master_pcr.csv` | buffer levels |
| `jkt_changeover_matrix_combined (1).xlsx` | the plant changeover matrix |
| `pcr machines.png` | the plant machine list — the changeover machine bridge |

> **The opening WIP matters more than it looks.** `db_loader.py` (in `phases/`) loads it. If
> that file is ever missing, every phase falls back **silently** — phase 1c prints one WARN line
> and carries on with **no WIP at all**, over-producing by ~12%. A phase that warns and
> continues is more dangerous than one that crashes.
