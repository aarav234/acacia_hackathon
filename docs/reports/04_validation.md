# Report 4 — Complete validation (Member 1)

Environment:
* Python 3.13.9 on macOS-15.7.3 arm64 (Apple Silicon), pydantic 2.12.4, pandas 2.3.3
* Python 3.11.13 cross-check: pydantic 2.14.2, pandas 3.1.0
* Seeds: suite `20261031 + 101·i`; sensitivity `20261031`

## 1. Test suite

Command: `PYTHONPATH=src python -m pytest tests/test_contracts.py tests/test_data.py tests/test_engine.py -W error -q`

| Configuration | Result |
|---|---|
| All data present (flights.csv, workbook, openpyxl) | **145 passed** (contracts 25, data 69, engine 51) |
| No datasets (`AIRSPACE_DATA_DIR` → empty dir) | 139 passed, 6 skipped: 4 need `flights.csv`, 2 need the original workbook |
| Python 3.11, no openpyxl installed | 133 passed, 12 skipped (the workbook-reading tests skip via `importorskip`) |
| Published fix commit `709eab2` alone | 113 passed (real data), 110 + 3 skipped (no CSVs) |

There are no failing tests and no suppressed warnings. Previously: 103 passed
normally, and 3 failed under `-W error` (the reader leak).

**New tests, 42 in total:**
* 10 reader-closure tests
* 6 provenance-only tests
* 16 workbook / anchor tests: loader round trip, 9 fail-closed cases, numeric IDs,
  wake conversion, the real-workbook extract and count-definition checks, and
  anchor reproduction
* 10 suite / oracle / sensitivity tests: determinism, workbook values, outcome
  coverage, rule-by-rule oracle, oracle under reweighting, simplex, re-sealing
  and a fresh disagreement point, affine invariance, exact selection, and a
  single feasible plan

## 2. Scenarios evaluated

There are 43 distinct operational configurations. Alternative plans within a
scenario are *not* counted as scenarios.

* **3 committed fixtures:** success, no-feasible and no-positive-gain.
* **40 workbook-calibrated scenarios:** 4 destination tiers × 10 categories. The
  tiers are MIA medium, DEN large, SJC small and ITO non-hub, each anchored to a
  genuine `flights.csv` row (rows 70, 171, 682, 2845). The anchor rows are
  context only; every operating value is simulated or challenge-supplied, and
  labelled as such.
* **Plus 1 targeted single-feasible-plan test case.**

| Category | Outcomes (4 tiers) |
|---|---|
| baseline, on_time, dense_traffic, crew_restricted, generous_fuel | no_positive_bargain ×4 each |
| large_delay (60 min behind schedule) | no_positive_bargain 3, no_feasible 1 |
| runway_closure | no_positive_bargain 3, no_feasible 1 |
| gate_restricted | no_positive_bargain 2, no_feasible 2 |
| tight_fuel (taxi burn 30 kg/min) | **compromise 1** (MIA), no_positive_bargain 3 |
| all_runways_closed | no_feasible ×4 (human review) |
| fixtures | compromise 1, no_feasible 1, no_positive_bargain 1 |

Totals:
* 2,703 candidate plans, of which 455 are feasible and 2,248 rejected.
* Violations by rule:

  | Rule | Violations |
  |---|---|
  | FUEL-RESERVE | 1,660 |
  | RWY-SEP-LEAD | 987 |
  | RWY-SEP-FOLLOW | 930 |
  | RWY-OPEN | 322 |
  | TAXI-BLOCK | 212 |
  | GATE-OCCUPANCY | 164 |
  | GATE-COMPAT | 20 |
  | CREW-COVERAGE | 4 |

* Rates at contract weights:
  * no feasible plan → human review: 9 of 43
  * fallback with `no_positive_bargain`: 32 of 43
  * Nash compromise: 2 of 43
* At contract weights the eligible set never holds more than one plan. Sets of 2–9
  eligible plans occur only under alternative weights, in 3 scenarios.

## 3. Independent oracle

`independent_oracle` re-implements slot enumeration, the 9 operational rules (the 4 `PLAN-*` structural
checks only apply to externally supplied plans),
the 8 raw metrics, the utilities (exact rationals), the fallback, the eligibility
test, the Nash selection with every tie-break, and the Pareto set. It uses no
engine module.

| Check | Result |
|---|---|
| Per-plan violated-rule sets, 43 scenarios | **2,703 / 2,703 identical** |
| Feasible set, fallback, compromise | 43 / 43 identical |
| Same under every sensitivity configuration | **11,481 / 11,481 runs identical** |
| Compromise ∈ Pareto set | 2 / 2 at baseline (the property test also passes) |
| Float vs exact utility | max 4.2e-16; no selection affected |

## 4. Nash vs baselines (contract weights, 34 feasible scenarios, raw metrics, means)

| Method | Delay min | Spillover proxy | Fuel kg | Missed pax | Turnaround min | Overtime min | Gate change | Pareto | = fallback |
|---|---|---|---|---|---|---|---|---|---|
| Fallback (earliest feasible) | 1.47 | 7.50 | 142.6 | 9.41 | 17.21 | 3.68 | 0.68 | 31/34 | 34/34 |
| **Nash** | 1.62 | 7.03 | 151.0 | 9.41 | 17.32 | 3.85 | 0.62 | 33/34 | 32/34 |
| Utilitarian (min Σ C_i) | 1.76 | 6.21 | 164.0 | 10.15 | 17.97 | 3.94 | 0.44 | 34/34 | 21/34 |
| ATC-best | 3.24 | 4.53 | 213.1 | 13.74 | 18.88 | 4.18 | 0.59 | 34/34 | 20/34 |
| Pilot-best | 1.62 | 7.24 | 150.1 | 9.85 | 17.41 | 3.85 | 0.68 | 34/34 | 27/34 |
| Ground-best | 2.21 | 6.53 | 205.3 | 16.38 | 19.77 | 3.77 | 0.09 | 34/34 | 14/34 |

How to read this:
* Nash moves away from the fallback only when *all three* roles gain, so it stays
  closest to the fallback in raw terms.
* The utilitarian sum is always Pareto-efficient, but it trades between roles: in
  13 of 34 scenarios it moves away from the fallback, and in 10 of those some role ends up
  worse off than at the fallback.
* The one Nash result that isn't Pareto-efficient is `suite-den-tight_fuel`
  (weak dominance, Report 3 §5).

These are comparisons inside a **synthetic simulation**. They are not evidence of
real-world performance, and the spread is variation across simulated scenarios,
not statistical uncertainty about real outcomes.

## 5. Sensitivity summary (details in Report 3)

* Across 267 weight/scale configurations × 43 scenarios, selections match the
  baseline in 94–100% of feasible runs per family.
* ±10–20% one-at-a-time perturbations never change a selection.
* Positive-bargain frequency stays between 1% and 8% across families.
* Scaling all of one role's metric scales together changed 0/297 selections.
* Role-specific affine transforms changed 0/680 selections on the suite, although
  constructed cases show the tie-breaks and ε are not scale-invariant.

## 6. Runtime and robustness (`docs/reports/results/benchmark.json`)

Engine scaling at DEN (alternative fuel reading, so many plans are feasible):

| Horizon min | Candidates | Feasible | Engine median ms | Oracle median ms | Deterministic ×3 |
|---|---|---|---|---|---|
| 15 | 30 | 8 | 8.7 | 0.8 | yes |
| 45 | 108 | 37 | 32.9 | 3.4 | yes |
| 90 | 270 | 58 | 91.7 | 8.3 | yes |
| 180 | 432 | 42 | 181.0 | 15.5 | yes |
| 360 | 936 | 40 | 545.7 | 48.0 | yes |

Notes:
* Engine time is roughly linear at about 0.3–0.6 ms per candidate. It is dominated
  by validating contract objects; extrapolated to the 2,000 cap, that's ≈ 1.2 s.
* Across the 11,481 sensitivity runs: median 11.0 ms, p95 38.4 ms, max 108 ms.

Ingestion:
* First qualifying row: 384 ms.
* Full chunked scan to the last data row (5,819,078): 7.45 s, about 781k rows/s.
* Peak process RSS 622 MB. That is measured across the whole benchmark process,
  not isolated to ingestion.

Tolerance boundaries are covered by tests:
* fuel reserve equality (passes) and 1 kg short (fails)
* separation gap = requirement, in both directions
* touching half-open intervals for gate buffers, taxi blockage, runway windows and crew busy windows
* ε exactly equal to a gain → ineligible

## 7. Remaining risks

* **Fallback structure.** The earliest-feasible fallback is Pareto-efficient in 31/34
  calibrated scenarios, so Nash rarely activates (Report 3 §5). This is correct
  under the contract, but it must be presented honestly in the demo.
* **Weak-dominance gap:** 1/34 recommendations are weakly Pareto-dominated.
* **Fuel-reading ambiguity.** The workbook's "45 min spare on arrival" sentence is
  ambiguous, and the outcome mix depends on how it is read.
* **Coefficients are unvalidated design assumptions.** For example, the implied
  rate of 1 missed passenger ≡ 2.5 kg fuel.
* **Tie-breaks and ε** are scale-dependent.
* **Data gaps.** October numeric airport IDs have no capacity profile. Aircraft type,
  trip fuel and crew rosters are assumptions.
* **Dependency.** `openpyxl` is needed only to read the original xlsx. CI uses the
  committed extract. Member 3 should add it as an optional extra.

## 8. Handoff for Member 3 (dashboard and evaluation)

```python
from airspace.data.scenario_suite import build_suite      # [(ScenarioSpec, Scenario)] x 40, deterministic
from airspace.engine.pipeline import solve_scenario       # summary(): counts, fallback, compromise, gains
from airspace.engine.oracle import independent_oracle     # per-plan rule sets + exact selection for audit panels
from airspace.engine.sensitivity import weight_families, reweighted
```

* Show the raw metrics next to the utilities.
* Label suite scenarios "challenge-calibrated simulation, anchored to flights.csv row N (context only)".
* Present "no positive bargain → fallback" as a normal outcome.
* Cite `rules_of_play` for the "turn" interpretation (2 rounds / 6 messages ≤ 3 turns).
* Never present the system as certified or as operational ATC.
