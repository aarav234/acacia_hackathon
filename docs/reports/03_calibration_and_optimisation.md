# Report 3 — Mathematical calibration and Nash optimisation (Member 1)

**Bottom line.** Neither `flights.csv` nor the new challenge workbook contains
independent evidence of stakeholder preferences or of trade-offs between
metrics. **No utility coefficient was estimated, and no production coefficient
was changed.** The contract weights remain *design assumptions*. Their effect is
quantified by sensitivity analysis over 11,481 engine runs, each verified by an
independent oracle.

All numbers below come from:
* `PYTHONPATH=src python -m airspace.engine.sensitivity --out <dir>`
  (seed 20261031; Python 3.13.9, macOS-15.7.3 arm64)
* the role-wide scale check and the constructed counter-examples in §5.

The summary is committed as `docs/reports/results/sensitivity_summary.json`. The
per-run file (9.7 MB) regenerates from the same command and seed.

## 1. Implemented formulas (from the source, not the brief)

From `scoring.py`, with `d = landing_min − earliest_landing_min`:

| Role | Raw metric r_j | Formula | Unit |
|---|---|---|---|
| ATC | landing_delay_min | d | min |
| ATC | spillover_proxy_min | Σ over same-runway background movements with L ≤ t < L+W of (W − (t − L)) | min (proxy) |
| Pilot | incremental_fuel_kg | airborne_rate·d + taxi_rate·taxi_min | kg |
| Pilot | connection_penalty_pax | Σ passengers of ASSUMED groups with gate_in > deadline | pax |
| Pilot | turbulence_exposure_index | runway_exposure + holding_exposure_per_min·d | index |
| Ground | turnaround_delay_min | max(0, service_end − (scheduled_gate_in + service)) | min |
| Ground | crew_overtime_min | min over covering crews of max(0, service_end − max(regular_end, gate_in)) | min |
| Ground | gate_change_indicator | 1[gate ≠ assigned] | 0/1 |

The cost and utility are:

**C_i(x) = Σ_j w_ij · r_ij(x) / s_ij**  and  **U_i = 1 − C_i**

There is no clipping. The scales s_ij are **fixed** per configuration and do not
depend on the candidate set. The brief's conceptual α/β/γ map exactly onto
`CONTRACT_DEFAULT_WEIGHTS`:

| | Weights |
|---|---|
| α (ATC) | (0.6, 0.4) |
| β (pilot) | (0.6, 0.3, 0.1) |
| γ (ground) | (0.5, 0.3, 0.2) |

Demo scales: 30 min, 10 min, 500 kg, 100 pax, 0.5, 30 min, 30 min, 1.

**Bargaining**, as implemented in `fallback.py` / `bargaining.py` and verified
against the definition:
* x₀ = argmin over F(s) of (landing_min, gate_in_min, plan_id)
* d_i = U_i(x₀)
* g_i = U_i − d_i
* B = {x : g_i(x) > ε for all i}, with ε = 1e-9
* x* = argmax over B of Π g_i, with tie-breaks: larger min g, smaller Σ C_i,
  earlier landing, earlier gate-in, plan_id
* Computed exactly with `fractions.Fraction` over the float utilities.
* If B = ∅, the result is `None` (the fallback is recommended).

## 2. Parameter space and identifiability

θ = (α, β, γ), with each vector on its simplex and non-negative (enforced by
`UtilityConfig`; the sampled families are tested to stay on the simplex).

1. **Weights and scales are confounded.** Only c_ij = w_ij / s_ij enters the cost.
   Any (w, s) pair with the same ratios gives identical utilities.
2. **Only the direction of c_i matters to the Nash choice.** Multiplying every c_ij
   of one role by k > 0 maps g_i to k·g_i, so the product is multiplied by k and
   the argmax is unchanged, apart from ε and the tie-breaks (§5). Measured: scaling
   all of one role's scales by ×0.5, ×2 and ×10 changed **0 of 297** selections
   (the 33 scenarios with a feasible plan among the success fixture and the 40 suite scenarios, × 3 roles × 3 factors).
3. The identifiable parameters are therefore **1 ratio for ATC, 2 for pilot and
   2 for ground**. Those ratios are the implied exchange rates in the table below.
   The Σw = 1 normalisation and the absolute scales carry no extra decision
   information.

| Implied exchange rate (contract weights, demo scales) | Value |
|---|---|
| ATC: 1 min spillover proxy ≡ | 2 min landing delay |
| Pilot: 1 assumed missed passenger ≡ | 2.5 kg incremental fuel |
| Pilot: 1 turbulence-index unit ≡ | 166.7 kg fuel (≈ 4.2 min of holding at the workbook's 40 kg/min) |
| Ground: 1 gate change ≡ | 12 min turnaround delay ≡ 20 min crew overtime |
| Ground: 1 min turnaround delay ≡ | 1.67 min crew overtime |

Exposing these rates is the honest way to show what the weights assume. For
example, a passenger missing a connection is worth only 2.5 kg of fuel. **That
trade-off is not supported by any supplied data** and should be reviewed as a
design choice. I have not substituted any outside cost figures.

## 3. Evidence available for calibration (C4–C5)

| Method | Applicable? | Why |
|---|---|---|
| 1. Operational cost calibration | **No** (partial within-metric facts only) | The workbook gives holding burn (kg/min), reserve and turnaround minima, and "overtime costs 1.5× after 8 h". These are *physical or within-metric* rules: they set fuel burn and when overtime starts, not the cross-metric rates above. 1.5× needs a regular-time cost term the model doesn't have. No fuel price, delay cost or passenger cost is supplied, and none was invented. |
| 2. Inverse optimisation | **No** | There are no observed decisions, rankings or accept/reject labels. Historical arrivals in `flights.csv` are outcomes of unknown decisions with unknown alternatives and constraints, so treating them as optimal choices would be confounded, and they are banned as decision inputs anyway. |
| 3. Sensitivity and robustness | **Yes**, done (§4) | — |

Self-referential objectives (C6) were **not** used. No θ was chosen to maximise
Nash products, consensus or bargain frequency, or to reproduce the demo plan.

## 4. Sensitivity results

Setup:
* 43 scenarios: 3 fixtures plus 40 workbook-calibrated suite scenarios. They are
  distinct operational configurations, not alternative plans of one scenario.
  34 of them have at least one feasible plan.
* For every configuration the scenario is re-sealed and the full engine re-run,
  so the disagreement utilities are recomputed per θ.
* **Oracle agreement: 11,481 / 11,481 runs**, on feasible set, fallback and compromise.

Configuration families:

| Family | Configs | Description |
|---|---|---|
| baseline | 1 | contract weights |
| ofat_pm10_pm20 | 32 | each weight ×0.8 / ×0.9 / ×1.1 / ×1.2, then renormalised |
| dirichlet_local_k50 | 100 | Dirichlet(50·w_baseline) per role, jointly |
| dirichlet_uniform_k1 | 100 | Dirichlet(1,…,1) per role, jointly (uniform on each simplex) |
| simplex_vertices | 18 | every role puts all weight on one metric |
| scale_ofat_x0.5_x2 | 16 | each scale ×0.5 / ×2 |

Results:

| Family | Positive-bargain rate (feasible runs) | Selection = baseline selection | Scenarios ever bargaining |
|---|---|---|---|
| baseline | 0.059 (2/34) | — | 2 |
| ofat_pm10_pm20 | 0.059 | 1.000 | 2 |
| dirichlet_local_k50 | 0.050 | 0.989 | 3 |
| dirichlet_uniform_k1 | 0.081 | 0.945 | 6 |
| simplex_vertices | 0.011 | 0.936 | 6 |
| scale_ofat_x0.5_x2 | 0.052 | 0.993 | 2 |

**Stability.**
* Small perturbations (±10–20%) never change a selection.
* Selections are weight-sensitive in only **6 of 34** feasible scenarios. As runs
  differing from baseline / runs:

  | Scenario | Differing runs |
  |---|---|
  | success | 69/267 |
  | suite-mia-tight_fuel | 67/267 |
  | suite-den-tight_fuel | 53/267 |
  | suite-mia-generous_fuel | 31/267 |
  | suite-mia-large_delay | 30/267 |
  | suite-sjc-crew_restricted | 17/267 |

  Almost all changes come from the uniform-Dirichlet and vertex probes, which are
  deliberately far from baseline.
* When the selected plan differs from the fallback, the change in raw metrics is
  small. Mean over feasible baseline runs: +0.15 min landing delay, +8.4 kg fuel,
  +0.12 min turnaround, +0.18 min overtime. These are averages over *synthetic
  scenarios*, not estimates of real-world effects.

## 5. Disagreement point, ε and invariance (C8)

**The earliest-feasible fallback is usually Pareto-efficient.**
* It is non-dominated in **31 of 34** feasible scenarios. In those, no plan can
  have g_i > 0 for every role, so a strict bargain is impossible by construction.
* The **pilot** is the role that most often cannot gain (27/34). Within the
  earliest feasible slot, the fallback has the smallest gate-in, i.e. the
  shortest taxi, so the least taxi fuel. In the suite, any later slot adds
  ≥ 5 min × 40 kg/min = 200 kg of holding, which exceeds the largest possible
  taxi saving (9 min × 15 kg/min = 135 kg). The pilot can therefore only gain
  through the turbulence or connection terms.
* The **ATC** fallback is ATC-optimal in 19/34 scenarios (zero delay, low spillover).

Roles unable to gain over the fallback (count of scenarios):

| Roles that cannot gain | Scenarios |
|---|---|
| none | 3 |
| pilot only | 6 |
| ATC only | 3 |
| ATC + pilot | 8 |
| ground + pilot | 5 |
| ATC + ground | 1 |
| all three | 8 |

**A weakly dominated recommendation.** In `suite-den-tight_fuel` a plan weakly
dominates the fallback: it is better for some roles and equal for one. Strict
positivity rejects it, so the system recommends a Pareto-dominated plan (1/34).
This is contract-faithful, but a reviewer should be told.

**Counterfactuals.** These are labelled analyses only and nothing was changed:
* **ε.** With ε ∈ {0, 1e-6, 1e-3, 1e-2}, the scenarios with a bargain stay at 2 / 34.
  ε is not the binding issue.
* **Worst-case disagreement point** (d_i = min over F of U_i): 28/34 scenarios bargain.
  But this d is not individually rational relative to a safe default plan, and it
  reached the utilitarian choice in only 19/34. It shows how strongly the fallback
  definition drives outcomes; it is not a recommendation (see C6).

**Affine invariance.**

| Transform | Effect |
|---|---|
| Common U → aU + b (a > 0) for all roles, ε = 0 | invariant (tested on all suite scenarios) |
| Role-specific a_i on the product | invariant |
| Role-specific a_i on the min-gain and Σ C_i tie-breaks | **not invariant** |
| Absolute ε under role-specific scaling | **not invariant** |

Measured on the suite: 0 of 680 role-specific random transforms changed a
selection, because those tie-breaks almost never activate there. Constructed
exact counter-examples:
* Two plans with equal product 1/64: gains (½, ½, 1/16) and (¼, ¼, ¼). The second
  wins on min-gain. After scaling ground utility ×8, both products are ⅛ and the
  **first** wins.
* A gain of 5e-10 is ineligible at ε = 1e-9. After ×10 scaling it becomes eligible.

**Floating point.** Over 2,703 candidate plans, float utilities differ from exact
rational recomputation by at most **4.2e-16**. Raw metrics differ in 351 values
(all turbulence `0.05·d`, at most 3.7e-16). No selection differs.

## 6. Recommendations (for approval; nothing applied)

1. **Keep the contract weights and scales.** There is no evidence to replace them.
   Publish the implied exchange rates (§2) with every result as an explicit
   design assumption. Re-examine the passenger-connection rate (1 pax ≡ 2.5 kg
   fuel), which looks low; any new figure needs a cited source.
2. **Disclose the structural fallback effect.** With challenge-calibrated limits,
   Nash produces a compromise in 2 of 34 feasible scenarios. "No positive
   bargain → fallback" will be the common, correct outcome, and the dashboard
   should present it as such.
3. **Optional additive information flag:** report a *weak* Pareto improvement over
   the fallback, as in `suite-den-tight_fuel`, to the human reviewer without
   changing selection. This needs a contract addition, so it requires approval.
4. **Optional:** document, or replace with a relative tolerance, the absolute ε
   and the scale-dependent tie-breaks. This is a bargaining-rule change and
   needs team approval.
5. **Ask the organisers** whether "45 min spare fuel on arrival" includes the
   30-min final reserve. Under the conservative default (it includes the reserve)
   FUEL-RESERVE is the most frequent violation (1,660 of 4,299 violations). The
   `generous_fuel` category (alternative reading plus lower traffic) has 21–64
   feasible plans per scenario, against at most 20 in every other category.
