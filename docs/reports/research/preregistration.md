# Pre-registration: Member 1 mathematical assurance evaluation

This was written and committed **before** the `holdout` partition was evaluated.
The git history of this file is the evidence for that ordering.

## Partitions (fixed by seed index in `airspace.data.scenario_suite.stratified_specs`)

* 160 configurations: 4 destination tiers × 8 families × k ∈ {0..4}.
* `dev` (k = 0, 1; 64 configurations): exploration and debugging.
* `tuning` (k = 2; 32): confirmation that the definitions behave as intended; no parameter is fitted.
* `holdout` (k = 3, 4; 64): evaluated **once**, after every definition below is frozen.
* Each configuration is run under both fuel readings (A: the 45-min spare includes the
  30-min reserve; B: it does not), giving 128 / 64 / 128 evaluated variants.
* The original 40-scenario suite and the 3 fixtures were inspected heavily in earlier
  work and count as development data only.

## Frozen definitions (nothing is tuned)

* **Production engine:** unchanged; contract weights and scales; ε = 1e-9.
* **Grids:**
  * slot grid (production, 5 min)
  * integer-minute refinement (`refine(s, 1)`)
  * event grid (`event_candidates`)
* **Selectors** (`airspace.engine.research.selectors`): A, A2 (ε_rel = 1e-9), B, D1, D2, E, E2, C.
  When a selector has no pick, the recommendation is the fallback (production semantics).
* **Weight-stability probe:** per variant, 30 Dirichlet(50·w) and 30 Dirichlet(1) joint samples;
  seed `20261101 + crc32(scenario_id)`.

## Hypotheses and pass criteria (holdout)

| ID | Statement | Pass if |
|---|---|---|
| H1 | The production selection is feasible and equals the independent oracle's | 100% of variants |
| H2 | The event grid gives the same fallback and Nash choice as the full integer-minute grid | 100% of variants without a cap error |
| H3 | The event grid covers every integer-minute Pareto utility vector | mean coverage = 1.0 |
| H4 | The slot grid misses integer-minute Pareto vectors | reported (no threshold; descriptive) |
| H5 | Strict positive bargains are rare because the fallback is efficient or a role cannot gain | label frequencies reported; no threshold |
| H6 | Selector comparison (Pareto share, IR share, raw metrics, weight stability) | descriptive; **no selector is declared "best" by consensus rate** |

Failures of H1–H3 are reported as found and are not re-run with changed definitions.

## Addendum — candidate-generator change (written before the `final` set is evaluated)

The decision to make 1-minute resolution the production profile used dev, tuning **and the
already-disclosed holdout**. The holdout is therefore no longer pristine for this decision.
A new set, `final_evaluation_specs()`, was generated and has not been inspected:

* 64 configurations: 4 tiers × 8 families × 2 seeds; seed base 9,100,000.
* Disjoint fingerprints from every earlier suite.
* Both fuel readings, giving 128 variants.

Grids compared:
* **A:** 5-minute baseline (`slot_step_min = 5`)
* **B:** 1-minute production profile
* **C:** event grid on B

Pass criteria:

| ID | Criterion |
|---|---|
| F1 | B selection = the independent oracle on the 1-minute scenario, in 100% of variants |
| F2 | B and C selections pass the hard-constraint validator, in 100% of variants |
| F3 | C fallback and compromise = B, in 100% of variants |
| F4 | C covers 100% of B's Pareto utility vectors |
| F5 | A's Pareto coverage and the raw-metric differences A → B (descriptive; no threshold) |
| F6 | no cap errors at the 45-minute horizon |

Run once; failures are reported, not re-run with changed definitions.
