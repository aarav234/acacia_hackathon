# Data and model (Member 1)

This is a hackathon decision-support **simulation**. It is not certified ATC, not a
live digital twin, and not a clearance system. Every rule below is a *simulated toy
assumption*, not an FAA, ICAO or Airbus operational standard. No real-airport
performance claim can rest on these inputs. The engine produces a recommendation;
a human must approve it.

## 1. Inputs and what they can and cannot tell us

| Input | Used for | Not used for |
|---|---|---|
| `flights.csv` (2015, 31 columns, 5,819,079 data rows, sha256 `afa4e421…3a83`) | choosing one historical anchor row, shown as context | any decision value |
| `airlines.csv` (14 rows) | joining `AIRLINE` to `IATA_CODE` | — |
| `airports.csv` (322 rows) | joining origin/destination IATA codes; coordinates are display only | resolving numeric airport IDs |
| `configs/sim_*.json` | **every** decision input: runways, separation, gates, taxi, fuel, crew, connections, turbulence, weights | — |

The delay-cause columns (`WEATHER_DELAY`, `LATE_AIRCRAFT_DELAY`, …) are retrospective
allocations of arrival delay. Here they only select the scenario. They are not weather,
forecast, holding, fuel or runway state. They are never added to `ARRIVAL_DELAY`, and
they never imply a runway closure.

The data dictionary files named in the contract (`flights_data_dictionary(1).csv`,
`airlines_…`, `airports_…`) were **not in the workspace**. The column lists were
checked against the actual CSV headers instead: 31, 2 and 7 columns, matching the
counts the contract states.

## 2. Ingestion (`airspace.data.ingest`)

* **Chunked scan.** The file is read with `pandas.read_csv(chunksize=200_000, dtype=str,
  keep_default_na=False)`, so the 592 MB file is never fully loaded. The first
  qualifying row is found in about 1 s.
* **Predicate.** `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20`. Both comparisons are
  strict: 15 and 20 do not qualify. A null cause never qualifies and is never turned into 0.
* **Suitability.** `CANCELLED == 0`, `DIVERTED == 0`, and `SCHEDULED_ARRIVAL`, `WHEELS_ON`,
  `ARRIVAL_TIME` and `ARRIVAL_DELAY` are all present.
* **Row index.** The original zero-based data-row index, header excluded. It does not
  depend on chunking or filtering (tested with chunk sizes 1, 2, 4 and 1000).
* **Default selection.** The first qualifying row in file order. Rows that pass the vector
  prefilter but contain malformed values are skipped and counted
  (`skipped_invalid_candidate_rows`).
* **Explicit `row_index`.** The requested row is checked and either returned or rejected
  with `NonQualifyingRowError(reasons)`. It is never silently replaced.
* **Normalization.** Integer columns become `int` or `None`. HHMM columns stay as their
  original text after validation. Airport IDs are always text.
* **HHMM.** `5 → 00:05`, `0000 → 00:00`, `2400 → 00:00 (+1 day)`. Invalid minutes
  (≥ 60), hours > 24, `24xx` other than 2400, non-digits and more than four digits are
  rejected. HHMM is *local clock text*: shown as context, never subtracted, and never
  turned into UTC. `SCHEDULED_ARRIVAL` is gate-in, not a landing timestamp.
* **Joins.** `AIRLINE → airlines.IATA_CODE` and airports by IATA code.

  | Join status | Meaning |
  |---|---|
  | `resolved` | code found in the lookup |
  | `mapped_explicit` | found through a user-supplied `SOURCE_ID,IATA_CODE` map |
  | `unresolved` | numeric October IDs such as `14057`, or unknown codes |
  | `lookup_unavailable` | the lookup file was not supplied |
  | `missing` | no airport ID in the row |

  The original identifier is always kept. Missing coordinates are `None`, never 0.

### Ingestion provenance entry (`row["__airspace_provenance__"]`)

`namespace="airspace.ingest/v1"`, `mode="historical"`, `source_file`, `source_sha256`
(of the whole file), `row_index`, `row_index_basis`, `selection_method`,
`selection_predicate`, `suitability_rule`, `predicate_values`, `row_hash` (canonical
SHA256 of the 31 normalized values), `skipped_invalid_candidate_rows`,
`joins.{airline,origin_airport,destination_airport}`, `derived_local_clock_times`,
`lookup_files_sha256` and `time_semantics`. The synthetic form, from `synthetic_row()`,
is `{namespace, mode:"synthetic", note}` with no flight values.

## 3. Scenario builder (`airspace.data.scenario.build_scenario`)

* The config is validated strictly. A missing safety section raises
  `MissingSafetyInputError` with the exact path.
* For a historical row, the row hash is recomputed; any edited value is refused.
  `historical_ref` stores the file, sha256, row index, predicate, row hash and
  the three join statuses.
* Provenance has one `Evidence` per simulated section (`sim:operations.*`), the
  planning grid and epoch (`sim:scenario.*`), and the historical row and joins
  (`hist:*`). An unresolved join carries a note starting `UNVERIFIED`. Parsed clocks
  are recorded as `derived:clock.*`, and `derived:leakage_guard` is always present.
* The config `source_ref` uses the **canonical** config hash, not the file bytes.
  Reordering keys or renaming the file therefore leaves `state_hash` unchanged, while
  any value change alters it.
* With no `flights.csv`, use `synthetic_row()`. The scenario is then labelled
  `synthetic`, and validation forbids any historical sha or row index.
* **Leakage guard.** The engine never reads `historical_row`. A test edits
  `ARRIVAL_DELAY`, `ARRIVAL_TIME`, `WHEELS_ON` and `WEATHER_DELAY` and confirms that the
  candidates, utilities, fallback and compromise stay identical.

## 4. Candidate generation (`airspace.engine.candidates`)

* **Slots.** `earliest_landing_min + k·slot_step_min` within the half-open window
  `[earliest, earliest + planning_horizon_min)`.
* **Combinations.** Each slot is paired only with configured taxi routes, not the full
  runway × gate product. In the success scenario, R1→G3 and R2→G4 do not exist.
* **Order and IDs.** Plans are ordered by `(landing, runway, gate, route)`.
  `plan_id = P{landing:04d}_{runway}_{gate}_{route}`.
* **Timing.** `gate_in = landing + taxi_min` (no implicit holding) and
  `service_end = gate_in + service_duration_min`.
* **Cap.** If `slots × routes > candidate_cap`, `CandidateCapExceededError` is raised.
  The list is never truncated.

## 5. Hard constraints (`airspace.engine.constraints`)

All intervals are half-open `[a, b)`. Let L = landing, G = gate-in, E = service end and
B = the gate turnover buffer.

| Rule | Feasible iff | Units |
|---|---|---|
| PLAN-STATE / SLOT / ROUTE / TIMING | the plan is from this state, on the grid, on a configured route, and its timing is consistent | hash / min |
| RWY-OPEN | `[L, L + occupancy)` lies inside one runway open window | min |
| RWY-SEP-LEAD | for each background movement with `t ≤ L` on that runway: `L − t ≥ sep[movement_cat → subject_cat]` | min |
| RWY-SEP-FOLLOW | for each movement with `t ≥ L`: `t − L ≥ sep[subject_cat → movement_cat]`. At `t = L` both directions apply. | min |
| GATE-COMPAT | the subject category is in `compatible_categories` | category |
| GATE-OPEN | `[G, E)` lies inside one gate open window | min |
| GATE-OCCUPANCY | `[G, E + B)` does not overlap any existing `[s, e + B)` | min |
| TAXI-BLOCK | no segment of the route has a blockage overlapping `[L, G)` | min |
| CREW-COVERAGE | some crew has `available ⊇ [G, E)` and no busy window overlapping `[G, E)` (hard) | min |
| FUEL-RESERVE | `snapshot − approach − airborne_rate·(L − earliest) − taxi_rate·taxi_min ≥ min_reserve` (equality passes; taxi burn included) | kg |

**Gate buffer convention.** Every use of a gate, existing or proposed, is treated as
`[start, end + B)`. Two uses conflict iff these extended intervals overlap. The same
B therefore applies whichever use comes first.

Each violation carries the rule ID, observed and required values with units, and
evidence IDs from the scenario provenance. Infeasible plans never become
`Assessment`s: the model validator rejects them, so no preference can override a
safety failure.

## 6. Scoring (`airspace.engine.scoring`)

Let `d = L − earliest_landing_min`. Raw metrics:

| Role | Metric | Formula | Units |
|---|---|---|---|
| ATC | landing_delay_min | `d` | min |
| ATC | spillover_proxy_min | `Σ (W − (t − L))` over same-runway movements with `L ≤ t < L + W`, where W = `spillover_window_min` | min (proxy) |
| Pilot | incremental_fuel_kg | `airborne_rate·d + taxi_rate·taxi_min` | kg |
| Pilot | connection_penalty_pax | `Σ passenger_count` of ASSUMED groups with `G > deadline` | assumed pax |
| Pilot | turbulence_exposure_index | `runway_exposure + holding_exposure_per_min·d` | index (proxy) |
| Ground | turnaround_delay_min | `max(0, E − (scheduled_gate_in + service_duration))` | min |
| Ground | crew_overtime_min | least `max(0, E − max(regular_end, G))` over crews that can cover | min |
| Ground | gate_change_indicator | `1` if gate ≠ assigned gate, else `0` | 0/1 |

From these: `normalized = raw / scale`, `C_i = Σ w·normalized` and `U_i = 1 − C_i`.
C and U are **not** clipped (a test produces C > 1 and U < 0). Weights follow the
contract defaults (ATC 0.6 / 0.4; pilot 0.6 / 0.3 / 0.1; ground 0.5 / 0.3 / 0.2). The
demo scales are 30 min, 10 min, 500 kg, 100 pax, 0.5, 30 min, 30 min and 1. Weights
and scales are frozen in the config, so they enter the hash. They may change only in
labelled sensitivity runs.

**Separation of concerns.**
* Fuel *safety* is the hard FUEL-RESERVE rule; fuel *preference* is `incremental_fuel_kg`.
* Crew *availability* is the hard CREW-COVERAGE rule; *overtime* inside the available
  window is a soft cost.

**No double counting.** ATC delay is measured from the earliest feasible landing.
Ground lateness is measured against the simulated schedule. Historical delay causes are
never added.

**Spillover proxy limits.** The background traffic is fixed and never re-sequenced.
The proxy shows how tightly the inserted landing sits ahead of following movements.
It cannot measure real knock-on delay, network throughput or downstream propagation.

**Connections** are assumed groups. The dataset contains no passenger-level information.

## 7. Fallback and bargaining (`airspace.engine.fallback`, `airspace.engine.bargaining`)

* **Fallback.** The first feasible plan ordered by `(landing_min, gate_in_min, plan_id)`.
  If no plan is feasible, the result is `None`, which means human review with no
  actionable plan. No diversion or hold is invented.
* **Disagreement point.** `d_i = U_i(fallback)`. `fallback_id` must name a feasible
  assessment, otherwise `UnknownPlanError` is raised. Epsilon must be finite and ≥ 0.
* **Eligibility.** A plan is eligible iff `U_i − d_i > ε` for **all** roles
  (ε = 1e-9, strict).
* **Objective.** Maximize `Π(U_i − d_i)`.
* **Ties**, in order: larger minimum gain, smaller `Σ C_i`, earlier landing, earlier
  gate-in, then lexicographic `plan_id`.
* **Exact arithmetic.** Gains, products and sums use `fractions.Fraction` over the float
  utilities, so ties are exact and independent of input order.
* **No eligible plan.** The result is `None` (`no_positive_bargain`): no epsilon is added
  to the product, d is not moved, and agreement is not assumed.
* **Oracle.** `airspace.engine.oracle` checks the result independently. It re-derives
  costs from the published raw metrics and selects by exhaustive pairwise comparison.
  It agrees with the engine on all fixtures and on 60 randomized variants.

This is discrete Nash-product selection *inspired by* Nash bargaining. On a finite,
nonconvex catalogue, classical uniqueness and axiomatic properties are not inherited.
It does not prove truthfulness and does not certify safety. It is, however, Pareto-
optimal over the feasible set (tested).

## 8. Worked example (generated by `PYTHONPATH=src python -m airspace.engine.report`)

<!-- BEGIN GENERATED WORKED EXAMPLE -->
Scenario `demo-success-v1` (source_mode=historical_anchor), state_hash `4073e238ebb0fa6a…`

* candidates: 48; feasible: 23; rejected: 25
* violations by rule: CREW-COVERAGE=2, FUEL-RESERVE=5, GATE-COMPAT=8, GATE-OCCUPANCY=5, RWY-SEP-FOLLOW=3, RWY-SEP-LEAD=6, TAXI-BLOCK=6
* outcome: **compromise_found**

Raw metrics (units) → weighted normalized components, per plan:

| metric | units | fallback<br>`P0010_R1_G2_T2` | atc-best<br>`P0015_R1_G2_T2` | pilot-best<br>`P0015_R2_G3_T4` | ground-best<br>`P0020_R2_G1_T3` | compromise<br>`P0015_R2_G1_T3` |
|---|---|---|---|---|---|---|
| atc.landing_delay_min (w=0.6, scale=30) | min | 0 → 0.000000 | 5 → 0.100000 | 5 → 0.100000 | 10 → 0.200000 | 5 → 0.100000 |
| atc.spillover_proxy_min (w=0.4, scale=10) | min (proxy) | 8 → 0.320000 | 0 → 0.000000 | 3 → 0.120000 | 8 → 0.320000 | 3 → 0.120000 |
| pilot.incremental_fuel_kg (w=0.6, scale=500) | kg | 60 → 0.072000 | 210 → 0.252000 | 195 → 0.234000 | 375 → 0.450000 | 225 → 0.270000 |
| pilot.connection_penalty_pax (w=0.3, scale=100) | assumed passengers | 0 → 0.000000 | 0 → 0.000000 | 0 → 0.000000 | 40 → 0.120000 | 0 → 0.000000 |
| pilot.turbulence_exposure_index (w=0.1, scale=0.5) | index (simulated proxy) | 3 → 0.600000 | 3.25 → 0.650000 | 0.25 → 0.050000 | 0.5 → 0.100000 | 0.25 → 0.050000 |
| ground.turnaround_delay_min (w=0.5, scale=30) | min | 0 → 0.000000 | 0 → 0.000000 | 0 → 0.000000 | 5 → 0.083333 | 0 → 0.000000 |
| ground.crew_overtime_min (w=0.3, scale=30) | min | 4 → 0.040000 | 9 → 0.090000 | 8 → 0.080000 | 0 → 0.000000 | 10 → 0.100000 |
| ground.gate_change_indicator (w=0.2, scale=1) | 0/1 | 1 → 0.200000 | 1 → 0.200000 | 1 → 0.200000 | 0 → 0.000000 | 0 → 0.000000 |
| **atc** C / U | – | C=0.320000, U=0.680000 | C=0.100000, U=0.900000 | C=0.220000, U=0.780000 | C=0.520000, U=0.480000 | C=0.220000, U=0.780000 |
| **pilot** C / U | – | C=0.672000, U=0.328000 | C=0.902000, U=0.098000 | C=0.284000, U=0.716000 | C=0.670000, U=0.330000 | C=0.320000, U=0.680000 |
| **ground** C / U | – | C=0.240000, U=0.760000 | C=0.290000, U=0.710000 | C=0.280000, U=0.720000 | C=0.083333, U=0.916667 | C=0.100000, U=0.900000 |
| gains U_i − d_i (atc, pilot, ground) | – | +0.000000, +0.000000, +0.000000 | +0.220000, -0.230000, -0.050000 | +0.100000, +0.388000, -0.040000 | -0.200000, +0.002000, +0.156667 | +0.100000, +0.352000, +0.140000 |
| Nash product | – | not eligible | not eligible | not eligible | not eligible | 0.004928000 |

d (fallback utilities) = atc=0.680000, pilot=0.328000, ground=0.760000
<!-- END GENERATED WORKED EXAMPLE -->

How to read it: the fallback lands earliest on R1. Its cost comes from the heavy
departure two minutes behind (spillover 8), R1's high turbulence-exposure proxy,
and the change to gate G2. Each role's favourite plan hurts at least one other
role relative to the fallback. Only `P0015_R2_G1_T3` improves all three, with a
product of 0.1 × 0.352 × 0.14 = 0.004928. It is a fourth plan, distinct from each
role's favourite.

## 9. Failure modes and how they surface

| Situation | Behaviour |
|---|---|
| `flights.csv` absent | `FileNotFoundError` from `select_flight`; use `synthetic_row()`. The scenario is labelled synthetic and no historical row is fabricated |
| explicit row does not qualify / out of range | `NonQualifyingRowError` with reasons |
| no qualifying row | `NoQualifyingRowError` |
| missing columns / malformed value | `MissingColumnsError` / the row is rejected |
| numeric airport ID with no map | join `unresolved`, evidence note `UNVERIFIED`, original ID kept |
| missing safety config | `MissingSafetyInputError(paths)` |
| tampered or stale scenario | `ValidationError` (hash mismatch) on load; `StaleStateError` at engine entry |
| stale plan | `StaleStateError` in `assess_candidates`; `PLAN-STATE` violation in `validate_plan` |
| catalogue over cap | `CandidateCapExceededError` (never truncated) |
| no feasible plan | `choose_fallback → None`; reason `no_feasible_plan` |
| no positive bargain | `choose_compromise → None`; reason `no_positive_bargain` |

## 10. Run commands

```bash
PYTHONPATH=src python -m pytest tests/test_contracts.py tests/test_data.py tests/test_engine.py -q
PYTHONPATH=src python -m airspace.data.fixtures                   # rebuild configs/, fixtures/, schemas
PYTHONPATH=src python -m airspace.data.fixtures --refresh-anchor  # re-select anchor from ./flights.csv
PYTHONPATH=src python -m airspace.engine.report fixtures/success_scenario.json
```

Set `AIRSPACE_DATA_DIR` if the CSVs are not at the repository root. The real-data
tests skip, with a visible reason, when `flights.csv` is absent.

## 11. Challenge workbook, validation suite and analysis reports

* `airspace.data.challenge` loads `challenge2_operational_constraints.xlsx` strictly. The values are challenge-supplied planning rules, not measurements.
* `airspace.data.scenario_suite` builds 40 workbook-calibrated scenarios anchored to four genuine `flights.csv` rows.
* `airspace.engine.oracle.independent_oracle` re-implements enumeration, every hard rule, the raw metrics and the selection in exact rationals.
* `airspace.engine.sensitivity` and `airspace.engine.benchmark` produce the experiments.

Reports: `docs/reports/01_bugfix_and_provenance.md`, `02_dataset_assessment.md`,
`03_calibration_and_optimisation.md`, `04_validation.md`; small result files are in `docs/reports/results/`.
The mathematical-assurance pass (reproducibility, model audit, candidate coverage, calibration,
bargaining alternatives, stratified held-out validation, recommendations, handoff) is in
`docs/reports/research/` (Reports A–H and `preregistration.md`).

```bash
PYTHONPATH=src python -m airspace.data.scenario_suite                  # rebuild extract + anchor rows (needs xlsx + flights.csv)
PYTHONPATH=src python -m airspace.engine.sensitivity --out runs/sens   # 11,481 runs, ~4 min, seed 20261031
PYTHONPATH=src python -m airspace.engine.benchmark --out runs/bench --flights flights.csv
```
