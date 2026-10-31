# Report D — Coefficient calibration and uncertainty

**Answer:** no coefficient can be legitimately learned from the available evidence. The
weights remain **design assumptions**. Recommendations are nevertheless robust to large
preference uncertainty, and that robustness is not an artefact of a small candidate set.

## 1. Identifiability

* **Weights and scales are confounded.** Only c_ij = w_ij / s_ij enters U.
* **Only the direction of c_i matters** to the Nash argmax. Scaling a role's c by k > 0 maps
  g_i to k·g_i. Measured: 0/297 selection changes under role-wide scale ×{0.5, 2, 10}.
* Identifiable parameters: 1 ratio (ATC) + 2 (pilot) + 2 (ground) = **5**. Weight vectors
  equal up to per-role positive scaling of c form equivalence classes; the Σw = 1
  normalisation picks one representative. The implied rates are in Report B §2.

## 2. Evidence audit (class A–E)

| Quantity | Class |
|---|---|
| flights.csv delays, times, delay causes, airports, airline | A (directly observed, retrospective) |
| Parsed HHMM clocks, joins, workbook "delayed departures" (= predicate count) | B (derived) |
| Wake separation, holding burn, final reserve, spare fuel, turnaround minimum, runways, spare gates, ops/hour, 8 h shift, overtime 1.5×, gate-change notice | C (organiser-defined) |
| Background traffic, taxi routes/times, crews, connections, turbulence exposure, scales, weights, groundspeed, taxi burn | D (synthetic assumption) |
| Stakeholder rankings, accept/reject decisions, cost of delay / fuel / passenger disruption | **E — none available** |

## 3. Calibration routes

* **Cost-based calibration: blocked.** No vetted operational cost source is in the repository,
  and none was invented. The workbook's 1.5× overtime multiplier and 40 kg/min burn are
  within-metric facts; they don't fix cross-metric rates. To unblock, the team must approve a
  cited source for delay, fuel and passenger-disruption costs (for example an industry
  cost-of-delay reference). Even then, stakeholder priorities are not costs, and the result
  would be a labelled cost-reference vector, not "true preferences".
* **Inverse optimisation: blocked.** There are no independent reference decisions or rankings.
  Historical flights.csv outcomes are not chosen alternatives: the decision set, constraints
  and objectives behind them are unobserved, and using them would also leak outcome data into
  decisions.
* **Circular objectives were not used.** No θ was chosen to maximise Nash products, consensus
  or bargain frequency, or to reproduce the demo.

## 4. Sensitivity (reproduced and extended)

| Experiment | Configurations | Selection = baseline | Source |
|---|---|---|---|
| ±10/20% one-at-a-time | 32 × 43 | 100% | `../results/sensitivity_summary.json` (reproduced bit-identically) |
| Dirichlet κ = 50 / κ = 1 | 100 + 100 joint × 43 | 98.9% / 94.5% | same |
| Simplex vertices | 18 × 43 | 93.6% | same |
| Single-scale ×0.5 / ×2 | 16 × 43 | 99.3% | same |
| **±50% one-at-a-time** | 16 per scenario; 75 scenarios (43 legacy + 32 dev variants), 63 feasible on the slot grid | slot **99.1%**, integer-minute **98.9%** | `results/weights_numerics.json` |
| **Joint log-uniform scales ×[0.5, 2]** | 60 per scenario, same scenarios | slot **97.6%**, integer-minute **97.3%** | same |
| ε ∈ {0 … 1e-2} | 7 × 235 | 100% | same |
| Selector weight probe (30 κ = 50 + 30 κ = 1 per variant) | holdout | strict Nash 97.9% | `results/eval_holdout_summary.json` |

**Robustness vs small candidate set.** With 5× more candidates (integer-minute grid),
stability falls by only 0.2–0.3 points. The robustness comes mainly from the fallback
structure and feasibility, not from catalogue size.

**Decision regions.** Varying one role's weights over its simplex (21-point or 231-point
grids), with the others at baseline, gives at most **2 distinct selections** per role per
scenario. The baseline region's share of the simplex for the weight-sensitive cases:

| Scenario | ATC | Pilot | Ground |
|---|---|---|---|
| success | 71% | 90% | 79% |
| suite-mia-tight_fuel | 95% | 86% | 84% |
| suite-den-tight_fuel | — | **36%** | — |

`suite-den-tight_fuel` is the only case where the baseline choice holds in a minority of
the pilot simplex.

**Uncertainty statement.** These are frequencies over synthetic scenarios and hypothetical
weight distributions. They are not confidence intervals on real stakeholder preferences,
which are unidentified.

## 5. Recommendations

1. Keep the contract weights and scales. Label them as assumptions and publish the implied
   rates (Report B §2) with every result.
2. Review the passenger-connection rate (1 pax ≡ 2.5 kg of fuel). Any change needs a cited
   cost source and team approval.
3. To make calibration possible later, collect class-E evidence (Report F §6).
