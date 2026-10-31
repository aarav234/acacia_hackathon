# Report E — Nash bargaining alternatives (counterfactual research only)

Production remains rule A. The exact rules are in `airspace/engine/research/selectors.py`.
Every rule selects within the same feasible set F, using exact rationals, and returns
the fallback when it has no pick.

## 1. Why strict bargains are rare (diagnosis, `research/diagnosis.py`)

Labels per feasible variant, holdout (119 feasible of 128):

| Label | Count | Meaning |
|---|---|---|
| FALLBACK_EFFICIENT | 117 | no plan weakly improves x₀, so a strict bargain is impossible |
| POSITIVE_BARGAIN | 2 | |
| ROLE_CANNOT_GAIN | pilot 99, ATC 63, ground 50 (variants can have several) | that role's best plan is x₀ |
| CONFLICT | 4 | each role can gain somewhere, never all at once |
| CONSTRAINT_BOUND | 33 | a strict improvement exists only among hard-infeasible plans (counterfactual; rules are not relaxed) |
| GRID_LIMITED | 4 | a strict bargain exists only on the integer-minute grid |
| WEIGHT_SENSITIVE | 10 | a strict bargain appears under some sampled weights |
| WEAK_ONLY | 0 holdout; 2 dev; 1 legacy | a weak improvement but no strict one |

The legacy suite (Report 03) shows the same structure: 31/34 efficient, pilot blocked in 27.

**Mechanism.** The fallback has the earliest landing and, within that slot, the shortest
taxi. The pilot's fuel (rate·d + taxi_rate·τ) is therefore minimal there unless the
turbulence or connection terms dominate, and ATC delay is zero. Strict improvement for all
three is usually impossible **under the model**. This is a genuine structural property of
using the earliest-feasible plan as the disagreement point, not a bug or a weighting
artefact: ±50% weight changes leave it intact (Report D).

## 2. Rules compared

| Rule | Definition | Interpretation |
|---|---|---|
| A strict Nash (production) | g > ε ∀i; max Π g | strict individual rationality |
| A2 scaled strict Nash | g_i > ε_rel·range_i; normalised tie-breaks | same as A, but invariant to role-specific affine maps |
| B weak-IR lexicographic | g ≥ 0 ∀i, ∃ g > 0; max #positive, then Π of the positive gains | handles boundary solutions explicitly; the ordinary Π g is 0 there |
| D1 Kalai–Smorodinsky | g ≥ 0; max min_i g_i / (ideal_i − d_i) | proportional fairness; scale-invariant |
| D2 normalised leximin | g ≥ 0; lexicographic max of sorted normalised gains | egalitarian |
| E utilitarian | max Σ U_i over F | efficiency, no IR |
| E2 utilitarian-IR | max Σ U_i over g ≥ 0 | efficiency with IR |
| C worst-case d | strict Nash with d_i = min_F U_i | counterfactual disagreement point; not independently justified |

## 3. Results (holdout, 119 feasible variants; `results/eval_holdout_summary.json`)

| Rule | = fallback | Pareto-efficient | IR (no role below fallback) | Weight stability | Delay | Spillover | Fuel kg | Missed pax | Turnaround | Overtime | Gate change |
|---|---|---|---|---|---|---|---|---|---|---|---|
| A, A2 | 98.3% | 100% | 100% | 97.9% | 3.02 | 6.46 | 203.6 | 18.56 | 23.62 | 5.06 | 0.655 |
| B, D1, D2, E2 | 98.3% | 100% | 100% | 96.9% | 3.02 | 6.46 | 203.6 | 18.56 | 23.62 | 5.06 | 0.655 |
| E utilitarian | 58.8% | 100% | **60.5%** | 81.3% | 3.28 | 4.53 | 232.9 | 19.87 | 24.92 | 5.14 | 0.378 |
| C worst-case d | 47.9% | 100% | **49.6%** | 82.7% | 3.70 | 3.87 | 255.8 | 23.13 | 25.76 | 5.38 | 0.286 |

* A2 = A in **296/296** feasible variants (all partitions).
* B, D1, D2 and E2 differ from A **only** in the weak-only cases. In dev, both such variants
  are `strat-dev-mia-wake_dense-k1`; in the legacy set it's `suite-den-tight_fuel`. In those
  cases A returns a **Pareto-dominated** fallback, while B, D1, D2 and E2 return a weak
  Pareto improvement.
* E and C buy ATC spillover and gate-change reductions with more fuel, more missed assumed
  passengers and longer turnaround. They make some role worse off than the safe default in
  about 40–50% of variants.

Computation cost: every rule is O(|F|) to O(|F|²) on exact rationals. Per-rule timing was not benchmarked
separately; all 8 rules run inside the evaluation runner for every weight sample.

## 4. Conclusions

* **No alternative is "better" in general.** On this simulation they are mostly identical
  to A. Utilitarian and worst-case-d trade individual rationality for ATC and ground gains.
  Their higher non-fallback rates are not a quality signal (pre-registration H6).
* The one demonstrated weakness of A is boundary solutions (a weakly dominated fallback).
  The lowest-risk fix is to **report** a weak Pareto improvement to the human reviewer, as
  an information flag, without changing selection. B or D1 as a production selector would
  change bargaining semantics and needs approval.
* Contract implications:

  | Change | Contract impact |
  |---|---|
  | Information flag | additive `RunResult` / `Assessment` field or event payload; needs Member 2/3 agreement |
  | Changing the selector | changes `choose_compromise` semantics |
  | Changing the disagreement point | changes `fallback_id` semantics |
