# Report I — Candidate-generator improvement: 1-minute landing resolution

Branch: `feat/candidate-generator-improvement` (from `m1/math-assurance`).

## What changed, and what did not

**Changed (configuration only):**
* Production scenario generation uses `slot_step_min = 1`
  (`scenario_suite.PRODUCTION_SLOT_STEP_MIN`), the contract's full integer-minute decision space.
* v2 fixtures put the three demo scenarios on the 1-minute grid.

**Unchanged:**
* `generate_candidates`, `validate_plan`, scoring, fallback and Nash bargaining;
* utility weights and scales, ε, and strict individual rationality;
* Shared Contract v1 and the JSON schemas;
* the v1 fixtures and configs (byte-identical);
* the published 40- and 160-scenario research suites, which are pinned to the 5-minute baseline.
  Their state-hash digests are asserted in tests.

**Generator fix (scenario generation, not the engine):** a crew whose shift, including the
overtime cap, ends before it starts was emitted as an invalid empty window. It is now
omitted and labelled in provenance. This affects only configurations that previously
failed validation. It was found by the new final set (1 of 64 configurations).

## Cap analysis

At 1 minute the catalogue has horizon × routes plans. The 2,000 cap therefore allows a
⌊2000 / routes⌋-minute horizon: 333 min for the demo fixture (6 routes), 133–167 min at
12–15 routes.

* Every fixture and suite scenario (45-minute horizon) fits; the largest is 630 candidates.
* Longer horizons raise `CandidateCapExceededError`, with no truncation. Measured:
  DEN at horizon 180 has 2,160 candidates, which is recorded as a cap error in
  `results/benchmark_candidate_generator.json`.
* Raising the cap was **not** done. If longer horizons are needed, the safe options are the
  exact event grid (below) or a horizon split that is announced to the reviewer.

## Final evaluation (pre-registered; new disjoint set; run once at `f6db702`)

128 variants: 64 new configurations × 2 fuel readings. Source: `results/eval_final_summary.json`.

| | A: 5-min baseline | B: 1-min (production) | C: event grid on B |
|---|---|---|---|
| Candidates | 8,208 | 41,040 | 28,942 |
| Feasible candidates | 1,964 | 8,713 | 3,497 |
| Variants with a feasible plan | 113 | **119** | 119 |
| Pareto-vector coverage of B | **0.512** | 1.000 | **1.000** |
| Strict compromises | 10 | 11 | 11 |
| Oracle agreement | 128/128 | **128/128** | = B in 128/128 |
| Selected plans passing hard constraints | 113/113 | **119/119** | 119/119 |
| Cap errors | 0 | 0 | 0 |

Pre-registered criteria F1–F4 and F6 **pass**.

**F5, descriptive: raw metrics of the selected plan, B − A** (113 variants feasible in both):

| Metric | Mean | Share improved | Share worse |
|---|---|---|---|
| Landing delay | −1.31 min | 25% | 1% |
| Incremental fuel | −50.7 kg | 23% | 3% |
| Turnaround delay | −1.20 min | 23% | 2% |
| Crew overtime | +0.11 min | 4% | 1% |
| Spillover proxy | −0.48 min | 18% | 4% |
| Missed assumed passengers | −1.44 | 4% | 0% |

In addition, **6 variants** with no feasible plan on the 5-minute grid (human review) have
an actionable feasible plan on the 1-minute grid. The fallback changes in 34 of 128.
Medians are 0: most variants are unchanged, and the gains come from the minority where
the earliest feasible minute falls between 5-minute slots.

**These are synthetic-simulation differences under the toy model, not measured aviation savings.**

## Runtime and memory (`results/benchmark_candidate_generator.json`; idle machine)

| Case | 5-min | 1-min |
|---|---|---|
| Demo `success` solve (median of 7) | 8.5 ms (48 candidates) | 40.5 ms (240) |
| Demo `success` heap peak | 0.27 MB | 1.16 MB |
| DEN, 45-min horizon | 32 ms (108) | 162 ms (540) |
| DEN, 90-min horizon | 89 ms (270) | 466 ms (1,350) |
| Stage means, 8 scenarios: validate + score | 15.3 ms | 75.1 ms (event grid 59.6 ms) |

The 5-minute baseline timings reproduce the assurance pass within 1.5%.

## Event grid: guarantees, kept distinct

| Guarantee | Holds? | Evidence |
|---|---|---|
| Exact candidate-set equivalence | **No.** It removes about 29% of candidates and 60% of feasible ones (3,497 vs 8,713) | final set |
| Pareto-frontier (utility-vector) coverage | Yes: 1.000 on every set, under the current monotone metric set | final, holdout, tuning, dev |
| Same selected plans (fallback and compromise) | Yes: 128/128 final, 320/320 stratified, 8/8 benchmark | as above |
| Same aggregate metrics | **Only selection-based ones.** Catalogue counts and shortlist inputs differ | — |

Boundary families verified by the brute-force interval-start check and the full-grid comparison:
* runway windows and occupancy;
* lead/follow wake separation;
* buffered gate occupancy;
* taxi duration and blockages;
* crew windows and busy times;
* the exact fuel-reserve threshold;
* horizon edges.

**Recommendation: keep the event grid as a documented research alternative, not the default.**
* Its guarantee depends on every cost being non-decreasing between events. A future metric
  that breaks that monotonicity would silently void it.
* It changes the catalogue that Member 2's shortlist sees.
* Its measured benefit (about 21% of validate+score time) isn't needed at demo sizes.

Revisit it only if horizons approach the cap.

## Team compatibility (read-only inspection of `feat/agents-negotiation` / UI branch)

* **Member 2:**
  - No hard-coded plan IDs or resolution assumptions.
  - Their `build_shortlist`, run read-only on 1-minute catalogues (up to 107 feasible), returns
    ≤ 12 unique plans containing the fallback and every role optimum, in ≤ 0.4 ms.
  - Their example and tests still import `airspace.engine._stub_fixture`. That was already
    required before this change; they should switch to `load_fixture_scenario(name, version)`.
* **Member 3:** the UI branch carries no UI code yet. Runtime expectations: about 40 ms for the
  demo solve at 1 minute.
* **Plan-ID mapping:** identity for all v1 plans (tested); v2 only adds in-between minutes.
  The demo outcome changes are tabulated in `docs/contract_changes.md`. ATC and the pilot now
  share a best plan, so the v1 "three distinct role preferences" was partly a grid artefact.
* **Teammate action needed (not done here):** decide whether `load_fixture_scenario`'s default
  becomes `"v2"`. It's a one-line change in Member 1 code once agreed.

## Unresolved assumptions

* Fuel reading (A vs B) still needs organiser clarification.
* Weights remain uncalibrated assumptions.
* Every operating value is simulated or organiser-supplied.
