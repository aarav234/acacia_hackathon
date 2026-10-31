# Report B — Mathematical model audit

## 1. Utility model as implemented (`engine/scoring.py`)

For role i and metric j:

**C_i(x) = Σ_j w_ij · r_ij(x) / s_ij**  and  **U_i(x) = 1 − C_i(x)**

There is no clipping, and the scales s_ij are fixed per configuration, not dependent on the
candidate set. Let d = L − earliest_landing_min.

| Role | Raw metric r_ij | Formula | Unit | Source | w (contract) | s (demo) | Observable at decision time? | Comparable across scenarios? |
|---|---|---|---|---|---|---|---|---|
| ATC | landing_delay_min | d | min | derived from the plan | 0.6 | 30 | yes (model output) | as raw minutes, yes |
| ATC | spillover_proxy_min | Σ (W − (t−L)) over same-runway background movements in [L, L+W) | min (proxy) | simulated background traffic | 0.4 | 10 | yes (model output) | only within the same traffic model |
| Pilot | incremental_fuel_kg | rate·d + taxi_rate·τ | kg | workbook holding burn (suite) or simulated (fixtures); taxi burn simulated | 0.6 | 500 | yes (model output) | yes, in kg |
| Pilot | connection_penalty_pax | Σ assumed pax with G > deadline | pax | **assumed** (no passenger data) | 0.3 | 100 | yes (model output) | no (assumed groups) |
| Pilot | turbulence_exposure_index | runway exposure + 0.05·d | index | **simulated proxy** | 0.1 | 0.5 | yes (model output) | no |
| Ground | turnaround_delay_min | max(0, E − (scheduled G + service)) | min | workbook turnaround minimum (suite); simulated schedule | 0.5 | 30 | yes (model output) | yes, in minutes |
| Ground | crew_overtime_min | min over covering crews of max(0, E − max(regular_end, G)) | min | workbook 8 h shift (suite); simulated rosters | 0.3 | 30 | yes (model output) | yes, in minutes |
| Ground | gate_change_indicator | 1[gate ≠ assigned] | 0/1 | config | 0.2 | 1 | yes | yes |

*Observable* means computable from decision-time inputs. **None of these quantities is a
measured outcome.** They are model predictions from simulated or organiser-supplied inputs.

**Utility contribution:** w_ij · r_ij / s_ij, published per plan as `weighted.<metric>`.

**Cross-scenario comparability.**
* Utilities share fixed scales, so U values are on a common scale across scenarios.
* **Gains g_i are not comparable across scenarios**, because d (the fallback) differs.
* Nash products are not comparable across scenarios or across weight vectors.
* Raw metrics are the only valid cross-configuration comparison.

## 2. Implied marginal trade-offs (rates in original units)

Within a role, the marginal rate of substitution between metrics j and k is
(w_ij / s_ij) / (w_ik / s_ik), in units of k per unit of j.

| Role | Implied equivalence (contract weights, demo scales) |
|---|---|
| ATC | 1 min of spillover proxy ≡ **2 min** of landing delay |
| Pilot | 1 assumed missed passenger ≡ **2.5 kg** of fuel ≡ 0.0625 min of holding at the workbook's 40 kg/min |
| Pilot | 1 turbulence-index unit ≡ **166.7 kg** of fuel (≈ 4.2 min of holding) |
| Pilot | 1 min of holding (suite) costs 0.048 (fuel) + 0.010 (turbulence) = 0.058 utility, so 1 missed passenger ≈ 0.05 min of holding |
| Ground | 1 gate change ≡ **12 min** of turnaround delay ≡ **20 min** of crew overtime |
| Ground | 1 min of turnaround delay ≡ 1.67 min of crew overtime |

**Flags:**
* The passenger rate (1 pax ≡ 2.5 kg of fuel) is unsupported by any supplied data and looks
  implausible as an airline trade-off.
* The turbulence rate depends entirely on an invented index.
* No rate is evidence-based (Report D).

## 3. Feasibility (`constraints.py`), verified independently

F(s) is the set of plans passing all 13 rules (4 structural + 9 operational), with
half-open intervals throughout. The rule semantics are in `docs/data_and_model.md` §5.

`independent_oracle` re-implements the 9 operational rules. It agrees rule-by-rule on
2,703 plans (legacy scenarios) and on feasible sets in 11,481 + 320 runs. Weights never
change feasibility: `test_weights_never_change_physical_feasibility`.

## 4. Bargaining (`fallback.py`, `bargaining.py`), verified against the definition

| Element | Definition |
|---|---|
| Fallback | x₀ = argmin_{x ∈ F} (landing_min, gate_in_min, plan_id) |
| Disagreement point | d = U(x₀) |
| Gains | g = U(x) − d |
| Bargaining set | B = {x ∈ F : g_i(x) > 1e-9 for all i} |
| Compromise | x* = argmax_{x ∈ B} Π g_i |
| Tie-breaks, in order | larger min g, smaller Σ C, earlier landing, earlier gate-in, plan_id |
| Arithmetic | exact `Fraction` over the float utilities |
| If B = ∅ | result is None → fallback recommended (no feasible plan → human review) |

Verification:
* A research re-implementation (`research.selectors.strict_nash`) equals production on all
  43 legacy scenarios.
* The oracle selector equals production on all 11,481 + 320 runs.
* Adding a strictly dominated candidate never changes the choice; reordering candidates never
  changes it (property tests).
* Global optimality within B holds by exhaustive pairwise comparison in the oracle.

**Pareto relations, kept distinct:**

| Relation | Definition |
|---|---|
| Strict all-role improvement over x₀ | g_i > 0 for all i (the bargaining condition) |
| Weak Pareto improvement | g ≥ 0 for all i, > 0 for some i |
| Pareto-efficient x | no weak improvement over x exists in F |

If x₀ is Pareto-efficient, then B = ∅ with ε = 0 as well: no plan improves all roles
(confirmed: in 31/31 legacy scenarios with an efficient fallback, the strict pool is empty).
The converse fails. `suite-den-tight_fuel` has an inefficient fallback (a weak improvement
exists) but no strict improvement.

**Fallback as disagreement point.**
* It is defensible as a safety default: the earliest feasible plan, with no preference input.
* It isn't an independently observed no-agreement outcome, and it embeds the pilot's and
  ATC's preferences. Its landing time is earliest, which minimises delay. Within that slot
  it has the shortest taxi, which minimises taxi fuel. So it is efficient in 31/34 legacy and
  117/119 holdout feasible variants.
* This is why strict bargains are rare (Report E).

## 5. Numerical properties

| Property | Finding |
|---|---|
| Positive affine map U' = aU + b (a > 0, common to all roles) | g' = a·g; selection invariant for ε = 0 (tested) |
| Role-specific a_i | the product ordering is invariant, but the **min-gain and Σ C tie-breaks and the absolute ε are not**. Counterexamples: equal products 1/64 flip after ground ×8; gain 5e-10 becomes eligible after ×10 (`test_scaled_nash_is_invariant…`) |
| Scale-consistent variant A2 | ε_i = ε_rel·range_i and normalised tie-breaks; invariant in the counterexamples; identical to A on all evaluated variants |
| Observed impact | 0 changes in 468 random role-specific transforms (tie-breaks almost never activate) |
| Float vs exact | max \|U_float − U_exact\| = 4.2e-16; 30 exact-decimal ties appear as ±1e-17 noise; ε = 1e-9 absorbs them; 0 selection changes |
| ε sweep (0 … 1e-2) | 0 selection changes across 235 scenarios; 8 bargains at every ε |
| Near-ties | over 28 tables with ≥ 2 eligible plans, the smallest relative gap between the best two products is 0.54; no exact ties |
| Determinism | repeated runs identical; seeds stable across processes (crc32, not `hash`) |
