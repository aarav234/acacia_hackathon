# Report 2 — New dataset assessment: `challenge2_operational_constraints.xlsx`

## Verdict

The new file is the **challenge organisers' operational-constraints workbook**,
not an observational dataset. It contains:
* synthetic per-airport planning limits;
* a simplified FAA wake-separation table;
* fuel and crew rules written as text;
* the protocol rules for teams.

It contains **no** flight-level observations, **no** observed decisions or
outcomes, and **no** stakeholder preference labels.

| Role | Supported? | Evidence |
|---|---|---|
| 1. Scenario enrichment | **Yes, partly** | `airport_capacity` joins 322/322 to `airports.csv` on `IATA_CODE`; supplies runway count, spare gates, min turnaround and ops/hour per destination tier |
| 2. Operational parameter calibration | **Yes, as challenge-supplied assumptions** | `wake_separation` (nm / s), `fuel_and_crew` (holding burn 2,400 kg/h, final reserve 30 min, spare 45 min, crew shift 8 h, overtime 1.5×, gate-change notice 20 min) |
| 3. Stakeholder preference calibration | **No** | no rankings, choices, accept/reject decisions or cost trade-offs between metrics |
| 4. Predictive modelling | **No** | no outcome label; the only per-airport numeric column is itself a count derived from `flights.csv` |
| 5. External validation | **No** (consistency check only) | the capacity tiers are a deterministic function of `flights.csv` counts, so they are not independent |

**No model was trained.** There is no existing model to retrain, no supervised
target, and nothing that could be evaluated without circularity (see §6).

## 1. Provenance and format

| Property | Value |
|---|---|
| File | `challenge2_operational_constraints.xlsx`, 27,711 bytes, sha256 `f889ae9e012c36cb…e78211` |
| Location | workspace root (untracked; excluded locally via `.git/info/exclude`) |
| Producer | xlsx core properties: creator `openpyxl`, created 2026-10-31 04:27:29; no title, subject or description |
| Structure | 4 sheets (`airport_capacity`, `wake_separation`, `fuel_and_crew`, `rules_of_play`); no formulas, comments, merged cells or data validation |
| Self-declared status | `airport_capacity`: "SYNTHETIC planning limits … scaled from each airport's traffic in flights.csv. Not real airport data. Teams may replace with sourced figures if they cite them." `wake_separation`: "Simplified from FAA JO 7110.65 wake rules." |
| Licence / usage terms | **None stated.** The only obligation found is `rules_of_play`: "Any limit you change must be stated in your demo." Committed derivative: `fixtures/challenge2_extract.json` (8 KB, values unchanged, 4 airports plus all rule sheets). Confirm with the organisers before redistributing more. |

## 2. Data dictionary

**`airport_capacity`.** The header is on row 3 (row 1 is a note, row 2 is blank).
The unit of observation is one airport (static; there is no time dimension).

| Column | Type | Unit | Notes |
|---|---|---|---|
| IATA_CODE | text | — | 3-letter code; 322 unique; join key |
| AIRPORT, CITY, STATE | text | — | identical to `airports.csv` for all 322 rows |
| delayed_departures_in_flights_csv | int | flights | **Misnomer.** Equals, for 322/322 airports, the per-ORIGIN count of `flights.csv` rows satisfying `WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20`. Not departure delay (0/322 match for `DEPARTURE_DELAY > 0` or `> 15`) |
| size_class | category | — | Non-hub 251, Small hub 43, Medium hub 23, Large hub 5. Perfectly threshold-separable on the column above (≤725 / 784–3,687 / 4,095–12,182 / ≥14,854); **not** separable on total traffic (overlapping ranges), although the two correlate (Spearman 0.983) |
| active_runways | int | runways | 1 / 2 / 3 / 4 by class |
| gates | int | gates | 8 / 30 / 70 / 150 by class |
| max_runway_ops_per_hour | int | ops/h (airport total) | 20 / 36 / 60 / 90 by class |
| min_turnaround_min | int | min | 35 (non-, small, medium hub) / 45 (large hub) |
| spare_gates_at_any_time | int | gates | 2 / 2 / 3 / 3 by class |

**`wake_separation`.** 8 rows; leader/follower ∈ {Super (A380), Heavy, Large, Small}.

| Column | Meaning |
|---|---|
| min_separation_nm | arrival distance separation (nm) |
| min_time_s | same-runway **departures** only (s) |

The row `Any other pair` is a **wildcard**: its empty follower cell is not a
missing value. Large→Large is therefore 3 nm / 60 s through the wildcard.

**`fuel_and_crew`.** 8 rules, all stored as **text**. The loader parses them with
exact patterns and fails closed if the wording changes.

**`rules_of_play`.** 5 protocol statements (input row predicate, "3 turns", fallback, logging, output).

## 3. Quality audit

| Check | Result |
|---|---|
| Missing values | none in `airport_capacity`; one *structural* blank (wildcard follower) |
| Duplicates | 0 duplicate codes, 0 duplicate rows, 0 duplicate wake pairs |
| Identifier validity | 322/322 valid 3-letter codes, all present in `airports.csv`; 0 name/city/state mismatches |
| Impossible values | 0 non-positive limits; 0 rows with spare gates > gates |
| Timestamps / timezones / rollovers | none in the workbook (N/A) |
| Units | mixed and implicit: nm vs seconds (wake); kg/h, minutes and hours as text (fuel/crew); airport-level vs per-runway ops |
| Variation | every limit column is a pure function of `size_class` (4 distinct profiles); no airport-specific information beyond the tier |
| Outliers / skew | the count column is heavy-tailed (1 to 25,274; median 183.5) |
| Coverage gap / selection bias | **All 486,165 October rows use numeric airport IDs** and are absent from the per-airport counts. That is 17,351 predicate rows (4.61%) excluded from tiering, and numeric-ID airports have no profile |
| Definition differences | "delayed departures" (workbook) ≠ departure delay (`flights.csv`). The count is of retrospective *arrival-delay cause* allocations, grouped by origin. Wake rules are FAA *categories*, while flights.csv has no aircraft type |
| Schema stability | single version; the loader validates exact sheet names, headers and text patterns |

Classification of values:
* **Genuine zeros:** none in the workbook.
* **Synthetic defaults:** all capacity limits (self-declared).
* **Missing observations:** aircraft type, taxi times, gate layout, crew rosters,
  trip fuel, flight-crew report time. None are imputed; each is a labelled
  simulation assumption (§5).
* **Invalid records:** none found.

## 4. Point-in-time feature audit

| Variable | Available at decision time? | Use |
|---|---|---|
| Wake table, fuel/crew rules, rules of play | yes (static rules) | parameters |
| active_runways, gates, spare_gates, min_turnaround, ops/hour | yes as *planning* constants | parameters |
| size_class, delayed_departures_in_flights_csv | **no**: full-year 2015 aggregates that include the anchor flight and every later flight | **excluded** from any predictive input; used only for the tier lookup |
| flights.csv outcome fields (ARRIVAL_TIME, ARRIVAL_DELAY, WHEELS_ON, delay causes) | no (retrospective) | context only (existing leakage guard) |

## 5. Integration performed

New code is in Member 1 files only; the existing pipeline and fixtures are unchanged.

| File | Purpose |
|---|---|
| `src/airspace/data/challenge.py` | strict workbook loader (sheet/header/pattern checks, wildcard handling, sha256 provenance); extract writer and reader |
| `src/airspace/data/scenario_suite.py` | workbook-calibrated scenario generator plus the 40-scenario validation suite |
| `src/airspace/data/scenario.py` | additive `scenario_from_config(row, config)`; `build_scenario` is unchanged in behaviour |
| `fixtures/challenge2_extract.json` | derived extract (4 airports + rule sheets) for CI without the xlsx or openpyxl |
| `fixtures/challenge_anchor_rows.json` | 4 genuine qualifying `flights.csv` rows (data rows 70→MIA, 171→DEN, 682→SJC, 2845→ITO), each the first qualifying completed row for its destination tier |

**Mapping into Shared Contract v1, without any schema change:**

| Workbook | Contract field | Additional assumption (labelled in scenario provenance) |
|---|---|---|
| Holding burn 2,400 kg/h (narrowbody) | `fuel.airborne_burn_kg_per_min = 40` | subject aircraft is "Large" (A320/737 class); type is not in flights.csv |
| Final reserve 30 min | `fuel.min_reserve_kg = 1200` | — |
| Spare on arrival 45 min | `fuel.snapshot_fuel_kg = 1800` (at the earliest landing), `approach_burn_kg = 0` | **ambiguous:** whether the 45 min *includes* the 30 min reserve. The default is the conservative reading (includes it → 15 min usable holding); the alternative (3,000 kg) is the `generous_fuel` category and a sensitivity input |
| Wake nm / s | `separation` minutes = ceil(max(nm / groundspeed, s / 60)) | 140 kt approach groundspeed; one conservative matrix for arrivals and departures |
| min_turnaround_min | `service.service_duration_min` | — |
| active_runways | number of runways | closures are simulated per category |
| spare_gates_at_any_time | candidate gates = assigned + spare | the other gates are not modelled |
| max_runway_ops_per_hour × utilisation | seeded, self-separated background movements | utilisation (0.3–0.95) is simulated |
| Ground crew shift 8 h | regular end = shift start + 480 min | shift timing and a 60-min hard overtime cap are simulated |
| Gate change ≥ 20 min before arrival | earliest landing = minute 20 after the decision | holds **by construction**, not enforced as a rule |

**Not representable without new contract fields or invented data (proposed, not applied):**
* Contingency fuel (5% of trip fuel): trip fuel is unknown, and deriving fuel from DISTANCE is forbidden.
* Flight-crew 13 h duty: report time is unknown.
* The 1.5× overtime cost multiplier: there is no regular-time cost term to scale.
* A gate-change-notice hard rule for decision times after minute 0: this would need
  a new `GATE-CHANGE-NOTICE` rule ID.

**Protocol note for Members 2/3.** `rules_of_play` says one "turn" is each agent
sending one message and asks for consensus "within 3 turns". The contract's 2
rounds / 6 messages satisfies this, and the existing disclosure of how "turn" is
interpreted should cite this sheet.

## 6. Why no predictive model

1. There is no trained component in the system to retrain.
2. There is no supervised target. The only candidate, `delayed_departures_in_flights_csv`,
   is an exact deterministic count over `flights.csv`, so "predicting" it would
   rediscover the predicate.
3. The workbook adds no flight-level features to `flights.csv`. A delay model on
   `flights.csv` would predict retrospective delays, which the engine never uses as
   decision inputs (the leakage guard), and its accuracy would say nothing about
   bargaining quality.
4. All hard constraints remain deterministic and authoritative; the workbook simply
   sets some of their parameters.

## 7. Tests added (`tests/test_data.py`)

* Loader round trip; 8 fail-closed cases: header change, duplicate code, numeric ID
  in the IATA column, missing integer, spare > gates, wildcard with a follower,
  unknown category, changed or missing fuel text.
* Numeric October IDs never mapped to a profile.
* Wake wildcard and conversion arithmetic.
* Real workbook equals the committed extract (sha256-checked).
* **Real-data:** the workbook count column equals the per-origin predicate count for all 322 airports.
* Committed anchor rows are genuine (row hash, predicate, destination).
* **Real-data:** the anchor rows reproduce from `flights.csv`.
