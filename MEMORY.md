# ctp_v6 — MEMORY

**Read this before changing anything.** Everything here was verified against the real
files. Nothing is assumed.

---

## THE RULE

`phases/*.py` are **byte-identical copies** of `v6_wave_p2/phases/*.py`. The v6 algorithm
is not modified — not the bucketing, not the aging, not the placement, not the changeover
logic, not one constant.

Where CTP's data does not fit v6's expected shape, the fix goes in **`adapt_inputs.py`**
(a data reshape), never in a phase file. Every reshape is listed below with its reason.

If a future change cannot be expressed as a data reshape, **stop and ask the user.**
Do not edit a phase.

Verify at any time:
```
diff v6_wave_p2/phases/phase2_lot_sizing.py  ctp_v6/phases/phase2_lot_sizing.py
```

---

## CTP FACTS (user-confirmed — do not re-litigate)

### MPQ — there is NO MAXIMUM
`ctp_inputfiles/MOQ (3).xlsx`, sheet `PCR`:
- columns are `Item Type | Minimum Order Qty | Maximum Order Qty | UOM`
- **`Maximum Order Qty` is blank for all 12 real item types** (verified).
- there is **no `Fraction Allowed` column** (v6's file has one).

What that means **inside v6's own algorithm** (`phase2.build_mpq_map` :131-146, sizer :557-600):
- `mpq_max = None` → `mhe_total_batch = 1` → **the MHE split never fires** →
  `n_subs = n_dur` (duration cap only, v6:599).
- `Fraction Allowed` absent → v6's reader defaults it `"NO"` → the **else-branch**
  `lot_qty_full = max(raw_qty, mpq_min)` (v6:567-568) — a plain floor, **no rounding up to
  whole MPQ multiples**.
- The compound rows read `"3 Batches"` (text). v6 does `float(...)` → `ValueError` →
  `mn = None` → **no MPQ floor for compounds at all**. That is fine: v6 rounds compounds to
  the routing's `batch_size`, which is the real Banbury batch. **Do not invent a KG number.**

### Changeover — CTP has a plant matrix; v6 has none
`ctp_inputfiles/jkt_changeover_matrix_combined (1).xlsx` (193,928 pairs, 86–88% hit rate).

v6 has **no changeover matrix anywhere**. It derives setup minutes from **machine names**
(`compute_changeover_min`, phase5_v2:139-170): `WBC / LTBC / HTBC / FISCHER / DUPLEX /
TRIPLEX / QUADRAPLEX / DUAL`, else `DEFAULT_CHANGEOVER_MIN = 15`, mixing = 2.

**None of CTP's 123 machine ids match any of those branches** (verified). Under v6's own
rule, every CTP machine falls to a flat 15 min.

**This is the ONE deliberate algorithm deviation in the project**, made on the user's
explicit instruction ("changeover file i have provided use that in ctp"). It is applied
through a single documented hook, not by editing v6's function. See `adapt_inputs.changeover()`.

### Transfer time — from the routing
`routing.transfer_time_min`, per `routed_product`, 98% populated (GT 90 min, Tread 15,
Bead Apex 7, Master Compound 4). User instruction.

**v6's own material floor has NO transfer term** (`earliest_ns`, phase5_v2:978-1002 =
`first_drum_ready + min_aging`). Adding transfer is therefore a CTP addition — flagged, not
hidden.

### Green tyre aging — v6's own override
`phase5_v2:869`:
```python
AGE_OVERRIDE_H = {"GREEN TYRES": (0.0, 72.0), "CARCASS": (0.0, 24.0)}
```
Keyed on the **raw ItemType string**. There is **no green-tyre row in CTP's aging master**
(0 of 2,658), so this override is the only thing that types it — exactly as in v6.

**Requirement:** CTP's itemtype master must type green tyres as literally `GREEN TYRES` for
the override to fire. If it does not, the green tyre falls to
`(DEFAULT_MIN_AGE_H, DEFAULT_MAX_AGE_H) = (8, 8760)` and the plan is nonsense. **Check this
after every itemtype-master update.**

`CARCASS` is irrelevant: **CTP has zero carcass lots** (PCR builds in one stage).

### Machines
- **Ply cutter: TWO machines, `1103` + `1104`.** The routing lists only `1103`; the plant runs
  both (MES `Fisher_O_Production` cuts the same `PBP*` codes and carries 60.8% of real ply
  output). Rate **15 CUTS/MIN = 15 NOS/MIN per machine**; a tyre needs 2 plies.
- **Bead apexing / winding: 15 NOS/min.** The routing's 5.0 NOS/MIN for apexing is wrong.
- **Belt cutter: 1 cut = 1 tyre = 2 belts.**
- **Bead winding is ONE machine (`1201`)** and the routing's rate is correct — at 15 NOS/min
  it would need 768.6 h in a 720 h month (infeasible). Do not "fix" it.
- Drum uses **86 presses**, matching the plant list.

## ⚠️ THE #1 BUG CLASS: "PRESENT-BUT-BLANK" (read this before touching any input)

**Every NaN hazard in this project is one bug, wearing different hats. Learn it once.**

v6 parses numbers like this, everywhere:
```python
try:    v = float(row["Col"])
except (TypeError, ValueError, KeyError):    v = None
```
That is written for a **MISSING column/row**. But **`float(NaN)` returns `NaN` — it does NOT
raise.** So a cell that is **present but blank** yields `NaN`, never `None`. And `NaN` then
defeats *every* downstream guard, because **all NaN comparisons are False**:

| v6 guard | with NaN | result |
|---|---|---|
| `if x is not None and x <= 0:` | `NaN <= 0` → **False** | NaN survives the sanitiser |
| `if x:` | **True** (NaN is truthy) | takes the "value present" branch |
| `if pt <= 0: return 0.0` | **False** | NaN duration propagates |
| `max(0.0, NaN - 8.0)` | **0.0** | window silently collapses to zero |
| `int(math.ceil(a / x))` | 💥 | `ValueError: cannot convert float NaN to integer` |

**v6's OWN masters contain ZERO blanks. That is its input contract, and why it has no guard.
CTP's masters are full of them.** So the two legal ways to express "no value" *in v6's terms*:

1. **Column absent entirely** → `KeyError` fires → `None`. (v6's own `mpq_v2.csv` does this.)
2. **Row absent entirely** → v6's `.get(key, default)` default fires.

**A BLANK CELL IS NEITHER. It is the one thing v6 cannot survive.**

⚠️ Also note `adapt_inputs._read()` uses `keep_default_na=False`, so blanks arrive in the
adapter as **empty strings `""`**, NOT NaN. They only *become* NaN when v6 reads our CSV back.
So `.isna()` alone silently matches nothing — always test `s.isna() | (s.astype(str).str.strip()=="")`.

### Resolved by the adapter (no data invented)
| input | blanks | resolution |
|---|---|---|
| `mpq.Maximum Run Qty` | all (CTP has no max MPQ) | **OMIT THE COLUMN** → v6's `KeyError` path → `mpq_max=None` → MHE split correctly never fires. Writing it blank crashes phase2:575. |
| `aging_master` | 789 of 2,658 rows, all 4 cells empty | **DROP THE ROW** → v6's own default `aging_map.get(item,(0.0,72.0))` fires. Affects 449 SMALL CHEMICAL, **238 Carcass**, 64 Bead Bundle, 17 SLITTED, 13 PRE CUT ROLL, 8 Apex. Latent crash sites this also defuses: phase2:480, **phase5:878**, and a *silent* one at phase3:374 (`max(0.0, NaN-8.0)` = 0.0 → every producer edge severed → orphan lots). |

---

## 🛑 BLOCKER — 3 routed_products cannot be scheduled (DATA GAP, needs the user)

These are **incomplete master records**, not just a missing number: no `proc_time`, no
`proc_time_UOM`, **no `machines`**, and no `batch_size` for the compound. Each has exactly ONE
operation, so there is no other row to fall back on. They crash at **phase2:574**.

| routed_product | ItemType | dept | NET demand | what peers run at |
|---|---|---|---|---|
| `GT 2568 HT2` | Green Tyres | BUILDING | **19,036** | 187 peers, median **65 SEC** |
| `GT 1482 UHL` | Green Tyres | BUILDING | **14,670** | 187 peers, median **65 SEC** |
| `TCA313`      | FINAL COMPOUND | FINAL MIXING | 239 | 3,654 peers, median **130.26 SEC/BATCH** |

`TBA827` and `MTCM313` are **MASTER COMPOUND**, which v6 skips outright (phase2:928) — harmless.

**DO NOT "FIX" THIS BY COERCING THE NaN AWAY.** `phase4_cpm.py:368` does
`pd.to_numeric(...).fillna(0)` — so a NaN duration that survives phase2 becomes a **ZERO-HOUR
LOT** (33,706 green tyres built in zero time). **The crash is the only thing currently
preventing a silently-wrong schedule.** Let it crash until the data is supplied.

**Also do not delete the routing rows.** `phase1b:225 if ch not in routed: continue` would then
drop the items from demand entirely — trading a loud crash for the **silent omission of two
green tyres from the curing feed**. There is no default here. A rate must be supplied.

---

## ✅ FIXED — `db_loader.py` was MISSING, and WIP netting was silently doing NOTHING

`db_loader.py` lives in **`v6_wave_p2/phases/db_loader.py`** — it is part of the phases folder
and must be copied with them. It was not, at first. Every phase does
`from db_loader import load_input` **inside a try/except with a CSV fallback**, so nothing
crashed — phase1c just printed one line and moved on:

```
[WARN] Inventory load failed (No module named 'db_loader'); proceeding with no WIP
```

**Consequence: the entire 37,948-row opening WIP was ignored and the plan over-produced by 12%.**

| | rows | qty |
|---|---|---|
| without `db_loader` | 402,367 | (no netting) |
| **with `db_loader`** | **272,018** | saved **3,503,690 units (12.0%)**, 852 WIP items |

**Lesson: a phase that prints a WARN and continues is more dangerous than one that crashes.**

---

## ✅ RESOLVED — inventory `unit` blank → backfilled by EXACT match from the other masters

**User decision:** *"use this correct unit as there in other file, so if not given use the other
files also."* Implemented in `adapt_inputs.inventory()`. **Exact itemcode match only. No fuzzy.**
Priority: (1) sibling WIP row → (2) `bom.child_Unit` → (3) `routing.batch_UNIT` →
(4) `routing.proc_time_UOM` (a rate's numerator IS the unit: `65 M/MIN` ⇒ the item is in **M**).
A source is used only if it yields ONE unambiguous unit for that code. **269 rows** backfilled
from `bom.child_Unit`. **This is a LOOKUP, not a guess.**

**Residual 1000× risk = 0.** All **1,020** WIP rows on demanded LENGTH items now carry `unit=M`
and convert ×1000 correctly. The 19,515 still-blank rows fall to v6's own `fillna(1.0)`, which is
**correct for NOS/KG** — and every remaining blank LENGTH row is on an item **no BOM ever raises**
(dead WIP, never looked up).

⚠️ **I initially over-stated this hazard's severity.** The mechanism below is real, but on the
current data its impact was already nil. Record it honestly.

### (the mechanism, for reference)

`db_loader.load_wip_by_item` / `phase5:1466`:
```python
_f = _inv["unit"].astype(str).str.upper().str.strip().map(_L2MM).fillna(1.0)
```
NaN → `astype(str)` → `"nan"` → `.map()` miss → NaN → **`.fillna(1.0)` → factor 1.0 (= MM)**.
But the real unit is **`M` (×1000)**. So 131,356 M of stock is read as 131,356 MM.

**2,041 rows / 144,501 qty** on length item-types. **100% of their non-blank siblings say `M`:**
Tread (414 `M`), SideWall (302), SLITTED MATERIAL (192), CALANDARED ROLL (104), PRE CUT ROLL (20).

v6 hit this exact bug once and fixed it — its own comment (db_loader:280-285) says the 1/1000th
credit *"drove the cap-strip / ply over-ageing"*. **We are currently re-introducing it.**
`fillna(1.0)` is CORRECT for NOS/KG items and only wrong for length items.

**Needs a user decision** (memory: *"no assumption in the inventory data, exact match only"*).

## ⚠️ OPEN — MPQ `"3 Batches "` is text → the compound lot-size floor VANISHES silently

`MOQ (3).xlsx` says `Minimum Run Qty = "3 Batches "` with `UOM = KG` for MASTER + FINAL
COMPOUND. v6: `float("3 Batches ")` → ValueError → **caught** → `mn = None` → **no MPQ floor at
all**. A stated constraint disappears with zero diagnostics. MASTER COMPOUND is skipped anyway,
but **FINAL COMPOUND (43 demand items) is real**. The KG-per-batch weight is **nowhere in the
CTP masters**. Needs the user.

## ✅ RESOLVED BY USER — the ONLY 3 deliberate deviations from v6

The user was shown the conflict (*"use transfer time + efficiency from routing"* vs *"no change
in algorithm"* — **these cannot both hold**) and chose: **use them.** They then also authorised
the changeover matrix: *"yes wire the changeover matrix, we don't have [it] in the btp v6 but we
will use it in the ctp."* So 3 phase files are no longer byte-identical.
**These are the ONLY intended differences. Anything else is a bug.**

| file | status |
|---|---|
| `db_loader.py`, `phase0`, `phase1b`, `phase1_5`, `phase1c`, `phase3`, `phase6` | **IDENTICAL to v6** |
| `phase2_lot_sizing.py` | **MODIFIED** (+26/-2) — reads `efficiency` + `transfer_time_min` |
| `phase4_cpm.py` | **MODIFIED** (+31/-1) — transfer lag in CPM |
| `phase5_forward_placement_v2.py` | **MODIFIED** (+72/-0) — transfer lag + changeover matrix |

### DEVIATION 1 — `efficiency` from the routing
v6 phase2:179-180 hardcoded `eff = 1.0` (`# 100% efficiency override (per user spec)`) and
ignored the column. CTP's routing says **0.96 on all 1,510 routed products**. `eff` **divides**
in `op_duration_min`, so every operation now takes **1/0.96 = +4.17% longer**. NaN-safe: a blank
falls back to 1.0 (never NaN — see the present-but-blank rule above).

### DEVIATION 2 — `transfer_time_min` from the routing
v6 has **ZERO references** to it in all 9 phases. It is a **mandatory lag** between a producer
finishing and its consumer starting — **the same shape as `min_aging`**. So it is **ADDED TO
`min_aging` at the single funnel each phase already routes every gap through**:
- `phase4_cpm.get_min_aging()` → inherited by the backward LFT pass, the forward EST pass, and
  the terminal green-tyre→curing anchor.
- `phase5_v2._ages_ns()` → inherited by every `p_min_ns` placement gap (~:997, ~:1243, ~:1300).
  Applied **after** `AGE_OVERRIDE_H`, so a GT with min=0 and a 90-min transfer gets a **1.5h
  floor** before it may be cured.

Carried down the pipeline as a `transfer_time_h` column on `phase2_lots` — the phases do not
re-read the routing.

⚠️ **NEVER add transfer to `get_max_aging` / `mx`.** `max_aging` is a shelf-life **CEILING**,
not a lag. Adding transfer there would **EXTEND shelf life** — the opposite of the truth.

Observed lags: **90 min × 226 green tyres** (building→curing), 15 min × 226, 10 min × 584
(final mixing), 7 min × 131, 4 min × 113 (master mixing), 0 min × 228.

### DEVIATION 3 — the CTP plant changeover matrix (`jkt_changeover_matrix_combined (1).xlsx`)
**v6 has NO changeover matrix.** Its `compute_changeover_min` (phase5:139-170) derives setup
minutes from machine **NAMES** — `WBC` / `LTBC` / `HTBC` / `FISCHER` / `DUPLEX` / `TRIPLEX`.
**Zero of CTP's 123 numeric machine IDs match any branch**, so under v6's own rule every CTP
machine would fall to a flat `DEFAULT_CHANGEOVER_MIN = 15` (or 2 for mixing). The matrix was
being written to `inputs/` and then **never read**.

**⚠️ THE JOIN IS THE WHOLE PROBLEM.** The matrix is keyed on a machine **LINE NAME**
(`"4 RC Calendar"`, 22 of them, spanning **both plants**); the routing is keyed on a machine
**ID** (`"901"`, `"3409"`). **ZERO string overlap.** No bridge exists in the routing, the BOM,
`MM_O_productionM` (machineName/machineCode), or the changeover workbook (one sheet only).

The bridge is **`machine_to_changeover_line` in `config.yaml`**, built from the user's own
plant machine list (`pcr machines.png`). **It is data, not a guess, and it is editable.**
`adapt_inputs.changeover()` **hard-fails** if it names a line the matrix does not contain.

Result: **167,921 `(line, from_item, to_item)` rules on 11 lines**; 12 routing machine IDs
mapped (`901, 1101, 1001, 1002, 1103, 1104, 801, 601, 2401, 2402, 1201, 201, 202`).
Keying on the line also **resolves** the 23 item pairs that carry different times on different
lines (`EG01→EG02` = 10 min on Cap Ply Cutter, 17 on Edge Gum Calendar, 24 on PCR Roller Head) —
with the line known we take the right one instead of guessing.

**111 machines stay unmapped and fall back to v6's rule — this is correct, not a gap:**
92 curing presses (the pinned echo — changeover is meaningless there), 11 GT-building machines,
6 mixers (dept contains `MIX` → v6 gives 2 min), 2 cap-strip slitters. **The matrix simply has
no line for them.** Building therefore still gets a flat 15 min — if the plant has real building
changeover data, that is the next thing to ask for.

Subtlety worth keeping: the routing runs op **`EDGE GUM` on machine `601`**, and the plant
machine list confirms **601 (Cap Ply Cutter) outputs "Cap ply, Edge gum"**. Consistent — 601
correctly maps to the matrix's `Cap Ply Cutter` line. (The list's `1102 Edge gum calender` does
not appear in the CTP routing at all.)

### Latent, not firing today — but will the moment demand touches them
- **`batch_size` blank on 25 routed_products whose UOM is `SEC`** → `if u=="SEC" and bs>0`
  → `NaN > 0` is False → skips the per-BATCH branch → falls to the per-UNIT branch →
  duration inflated by exactly `batch_size`× (measured: **20× on `1325226716123KRMT0`**).
  Silent, no crash. 0 demand today. **Highest-risk latent item in the set.**
- 13 exact-duplicate BOM lines → `B617` demand doubles on 4 SKUs.
- `child_quantity = 0` rows → `FILLIPER2` and `SWRS30` end up with no producer.
- 4 demand items have no `itemtype_master` row (`CAP 66 - CAPSTRIP`, `CAP 66-MOTHERROLL`,
  `MT1511`, `MT1512`) → `itype=""` → no MPQ floor. This IS v6's documented default path.

### Proven BENIGN (audited — do not spend time here again)
`bom.child_quantity` (the only correctly NaN-guarded reader in the codebase), all of `plan.csv`
(`to_numeric(errors="coerce").fillna(0)`), `inventory.inventory`, `buffer_master` (declared in
config but **never loaded by any phase** — dead file), `routing.operation_seq` (never read).

---

## FIVE THINGS THAT SILENTLY DESTROY A v6 RUN — all checked, all clear

v6's phase5 keys critical behaviour on **exact strings**. A mismatch does not crash; it
produces a plausible, wrong plan. Re-check these after ANY master-data update.

| # | v6 expects | CTP reality | status |
|---|---|---|---|
| 1 | `TERMINAL_DEPT_TOKENS = ("BUILD","TBM","CURING")` — a department containing none of these is an **orphan** and is silently never placed (`cascade_orphans`, phase5_v2:629-659, applied :1907) | CTP departments include `BUILDING` and `CURING` | **SAFE** |
| 2 | `BUILDING_ITYPES = {"GREEN TYRES","CARCASS"}` (phase5_v2:708, matched after `.upper()`) — also the key for `AGE_OVERRIDE_H` (:869) | **CTP's itemtype master types 2,862 codes but NOT ONE green tyre** (0 of 226) | **WAS FATAL — FIXED in `adapt_inputs.itemtype()`** |
| 3 | `phase2_lots.csv` must HAVE the columns `is_belt, campaign_id, wire_type, is_campaign_start, is_campaign_end` (unguarded, phase5_v2:407,513-516) — values may be all-False/empty | v6's own phase2 emits them | **SAFE** (v6's phase2 is unmodified) |
| 4 | `aging_master` is unguarded (phase5_v2:343) | supplied | **SAFE** |
| 5 | `outputs2/WIP_simulation_May2026.csv` — if present, phase5 does a **SECOND** WIP netting on top of phase1c and **double-counts** (phase5_v2:411-482) | CTP never writes that file | **SAFE — never create it** |

### #2 in detail — the one that was broken
The green tyre is identified **deterministically from the routing**, not guessed: it is a
`routed_product` whose `department` is `BUILDING` (226 codes — the same GT-prefixed codes
the BOM uses as `Parent`). `adapt_inputs.itemtype()` types them **`Green Tyres`**, v6's exact
string.

Left unfixed, `lot_item_type` is `""` for every green tyre and v6 silently:
- skips `place_building_campaigns` and sends GTs through the ordinary JIT picker (a
  *different algorithm*),
- never fires `AGE_OVERRIDE_H`, so GT aging becomes `(8 h, 8760 h)` instead of **`(0, 72)`**,
- turns the short-life resync into a no-op.

The plan would still "succeed" — and be nonsense. **v6 never canonicalises item types
(`build_itype_map`, phase2:126-128 reads master rows only), so the master must be right.**

`CARCASS` is irrelevant: PCR builds in one stage. CTP has **0 carcass lots**.

---

## DATA RESHAPES IN `adapt_inputs.py` (the ONLY place CTP differs)

| # | Reshape | Why |
|---|---|---|
| 1 | `MIN/BATCH` → `SEC/BATCH`, `proc_time × 60` | v6's `op_duration_min` (phase2:196-213) has **no `MIN/BATCH` branch**; an unmatched UOM falls through to `qty * pt` (a 690 kg batch at 15 min/batch → 10,350 min instead of 15.6). **72% of CTP's routing is `MIN/BATCH`** (10,599 of 14,693 rows); v6's TBR routing uses `SEC/BATCH`. Same rule, converted unit — v6's `SEC/BATCH` branch then yields exactly the intended minutes. |
| 2 | Curing xlsx → v6 `plan.csv` | The CTP export has a title banner + summary line above the header, so the header is not on row 0. Found by probing for `SKUCode/StartTime/Machine`, not by a hard-coded skiprows. |
| 3 | `MOQ (3).xlsx` → v6 `mpq.csv` | v6 wants `Minimum/Maximum Run Qty` + `Fraction Allowed`. Max written blank (CTP has none); `Fraction Allowed` written `NO` explicitly so the behaviour is visible rather than implied. |
| 4 | `opening_wip.csv` → v6 `inventory.csv` | Column names only. |
| 5 | `planning_max_aging_h` (config) → `planning_max_aging.csv` | v6 reads this as a **master file** (phase4:340-347), not config. |
| 6 | changeover matrix → `changeover_matrix.csv` | CTP-only. See above. |
| 7 | **type the 226 green tyres as `Green Tyres`** | CTP's itemtype master does not type them at all. Identified from the routing (`department == BUILDING`), not guessed. Without this the whole plan is silently wrong — see the hazard table above. |

BOM, routing, itemtype and aging masters **already carry v6's exact column names** — they pass
through untouched.

---

## WHAT v6 DOES WHEN CTP'S DATA IS ABSENT (v6's own graceful fallbacks — not changes)

| missing input | v6's behaviour |
|---|---|
| `mg_preference.csv` | phase1a not run (MG excluded by user instruction) |
| `belt_wire_mapping.csv` | `belt_to_wire = {}` → `size_wire_campaigns_per_wave` never fires → every belt lot goes through the ordinary wave-aware sizer |
| `building_cycle_times.csv` | `machine_cycle_sec = {}` → `dur_m_ns = dur_ns` (the routing duration) |
| `building_changeover.csv` | falls back to `BUILDING_CO_SAME_MIN = 40` / `DIFF = 60` |

---

## v6 QUIRKS — port them, do not "fix" them

1. **Six different aging maps, four different defaults.** A code missing from the aging master
   gets `(0.0, 72.0)` in phase2, `(8, 40)` in phase4, `(8, 8760)` in phase5, `72` in phase1c.
   That is drift, not design — but it **is** v6, so it stays.
2. **`_wave_anchor` assumes 3-day waves after the balance pass has split them.**
   `T0 + (wi-1) × 3 days` (phase5_v2:931), but a post-split wave is **not** 3 days wide. Every
   split pushes later waves' anchors past their true start. Real v6 behaviour.
3. **`SUB_WAVE_BUCKET_H_MEDIUM` (24 h), `SUB_WAVE_SAFETY_H` (8 h), `MIXING_ITYPES`** are
   **defined and never referenced** in v6's phase2. There is no 24 h bucket path and no safety
   margin. Do not implement them.
4. **`N_SUB_MAX_DEFAULT = 6`, `N_SUB_MAX_MHE = 20`** — the surrounding comments say 40. The
   comments are stale; the values are 6 and 20.
5. **v6 never reads `buffer_master`** — the file is declared in its config and never loaded.
6. **v6 skips MASTER COMPOUND entirely** (phase2:928-931) — assumed always on hand, never
   produced. CTP's 4 master mixers therefore do not appear in the schedule.
7. **v6's phase0 builds nothing.** It is a skippable report; no phase reads its output. Every
   lookup is rebuilt independently inside phases 1c/2/3/4/5.

---

## RUNNING

```
python adapt_inputs.py      # CTP masters -> inputs/*.csv in v6's schema
python run_pipeline.py      # v6's phases, unmodified
```

Phase order (v6's, minus phase1a):
```
phase0 -> phase1b -> phase1_5 -> phase1c -> phase2 -> phase3 -> phase4 -> phase5 -> phase6
```
