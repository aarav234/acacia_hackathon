# Report F — Expanded validation

## 1. Scenario families

There are 160 configurations: 4 destination tiers (MIA, DEN, SJC, ITO) × 8 families × 5
seeds. Parameter ranges are in `scenario_suite.STRATIFIED_FAMILIES`.

| Family | Description |
|---|---|
| normal | baseline disruption |
| short_delay | small delay |
| long_delay | large delay |
| runway_reduced | runway closure |
| wake_dense | dense, heavy-weighted traffic (wake conflicts) |
| gate_conflict | gate conflicts |
| tight_ground | ground resources restricted |
| tight_fuel | high taxi burn |

* Every configuration is evaluated under both fuel readings, giving **320 variants**.
* **Labelling:**
  - The anchor rows (flights.csv rows 70, 171, 682, 2845) are historical observations, used as context only.
  - Every operating value is either organiser-supplied (workbook) or a synthetic variation.
  - The explicit adversarial cases are unit tests: single-feasible plan, cap probe,
    constructed ties, ε boundary, injected dominated candidate.
  - **No scenario represents an actual historical airport decision.**
* All 160 decision-input fingerprints are distinct, and none overlaps the legacy 40-scenario suite.
* Partitions are fixed by seed index (dev 64 / tuning 32 / holdout 64 configurations).
  The holdout was run once, at commit `37ce5c1`, after `preregistration.md` was committed
  (`results/holdout_provenance.txt`).
* Nothing was tuned on any partition.

## 2. Pre-registered hypotheses (holdout, 128 variants)

| ID | Result | Verdict |
|---|---|---|
| H1 production selection feasible and equal to the oracle | oracle 128/128; validator 119/119 selected plans | **pass** |
| H2 event grid = integer-minute choice | 128/128 | **pass** |
| H3 event grid covers integer-minute Pareto vectors | mean coverage 1.000 | **pass** |
| H4 slot-grid coverage | 0.497 | reported |
| H5 rarity of strict bargains | 2/119 feasible; efficient fallback 117 | reported |
| H6 selector comparison | Report E | reported, no "best" |

## 3. Outcomes and constraint compliance

| Partition | Variants | Human review (no feasible plan) | Fallback | Compromise | Oracle agreement |
|---|---|---|---|---|---|
| dev | 128 | 10 | 114 | 4 | 128/128 |
| tuning | 64 | 5 | 57 | 2 | 64/64 |
| holdout | 128 | 9 | 117 | 2 | 128/128 |

Holdout rejections by rule:

| Rule | Rejections |
|---|---|
| RWY-SEP-LEAD | 3,446 |
| RWY-SEP-FOLLOW | 3,028 |
| FUEL-RESERVE | 2,850 |
| GATE-OCCUPANCY | 696 |
| TAXI-BLOCK | 590 |
| CREW-COVERAGE | 216 |
| RWY-OPEN | 198 |

**Every selected plan passes the production validator in every partition**
(118/118, 59/59, 119/119).

## 4. Fuel-reading sensitivity (paired; organiser clarification still needed)

| Holdout pairs = 64 | Reading A (45 min includes the 30 min reserve) | Reading B (45 min extra) |
|---|---|---|
| Feasible plans (mean) | 8.2 | 25.3 |
| Human review | 8 | 1 |
| FUEL-RESERVE rejections | 2,820 | 30 |
| Strict bargains | 1 | 1 |
| Selected plan's reserve margin, mean (min) | 445 kg (110) | 1,553 kg (540) |
| Selection differs, A vs B | 7 / 64 | |

Dev (10/64 differ) and tuning (3/32 differ) agree. The reading mostly decides **whether
an actionable plan exists**, not which plan is chosen. Production keeps the conservative
reading A.

## 5. Baselines and numerical robustness

* **Baselines:** Report E §3 (raw metrics; holdout).
* **Integer-minute vs slot grid:** Report C §2.
* **Numerics** (`results/weights_numerics.json`, 235 non-holdout scenarios):
  - ε sweep: 0 selection changes
  - 30 binary-float decimal ties, absorbed by ε
  - near-tie gap ≥ 0.54
  - 0/468 affine-transform changes, with constructed counterexamples
* **Determinism:** repeated runs identical; stable seeds; scenario hashes identical across builds.
* **Runtime and memory:** Report C §4.
  - Ingestion: 7.40 s full scan (786,618 rows/s).
  - Peak RSS: 573 MB for the whole benchmark process.

**Tests:** 156 pass under `-W error` with all data. The 11 new research property tests
are listed in Report G.

## 6. What would establish real-world validity (not done; evidence absent)

1. Independent stakeholder rankings or accept/reject judgements over the *same* candidate
   sets, from controllers, pilots and ground staff (class-E evidence for inverse optimisation,
   with held-out scenarios).
2. Historical decision records including the alternatives that were available: actual
   runway/gate assignments, holding instructions, and the constraints in force.
3. Cited operational cost estimates for delay minutes, fuel, missed connections and overtime,
   applicable to the aircraft and airport class.
4. Real airport configuration data: taxi network, gate compatibility, rosters, slot rules.
5. Observed fuel-on-arrival, taxi and turnaround outcomes, to validate the burn and timing models.
6. Expert review of the wake-separation conversion (nm → minutes at an assumed
   groundspeed) and of the fuel-reading interpretation.
7. Organiser clarification of "45 min spare fuel on arrival".

Until these exist, all results are **synthetic-simulation findings under stated
assumptions**. 160 configurations are not 160 independent operational observations.
