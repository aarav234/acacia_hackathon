# Report H — Handoff for Members 2 and 3

## Confirmed: unchanged and safe to integrate

* Shared Contract v1 and the public engine APIs are **unchanged**:
  `generate_candidates`, `validate_plan`, `assess_candidates`, `choose_fallback`,
  `choose_compromise`, `solve_scenario`.
* The engine equals an independent exact oracle in every tested run, and every selected
  plan passes the hard-constraint validator.

## How to read engine outcomes in the UI and negotiation

* **`no_positive_bargain` → fallback is the normal outcome**, not a failure.
  - The earliest feasible plan is usually Pareto-efficient: 117/119 holdout feasible variants.
  - Show *which role cannot gain* (diagnosis labels `ROLE_CANNOT_GAIN:<role>`); usually it's the pilot.
* **Gains are relative to the fallback of that scenario.** Never compare gains or Nash
  products across scenarios or weight settings. Compare raw metrics instead: delay min,
  fuel kg, turnaround min, overtime min, gate change.
* **Utilities can be negative.** U = 1 − C is not clipped.
* Show the **implied trade-off rates** as assumptions (Report B §2), e.g. "1 missed
  connection ≡ 2.5 kg fuel (design assumption)".

## Suggested UI and evaluation metrics

* Outcome counts: human review / fallback / compromise.
* Feasibility rejections by rule.
* Selected plan's raw metrics next to the fallback's.
* Per-role gains.
* Diagnosis labels.
* Selection stability under weight probes.
* The fuel-reading case (A/B), shown as a labelled toggle.

Available research APIs (read-only, Member 1):

```python
from airspace.data.scenario_suite import stratified_specs, build_suite_scenario, with_fuel_reading
from airspace.engine.research.diagnosis import diagnose            # labels + per-role max gains
from airspace.engine.research.selectors import Table, SELECTORS     # counterfactual rules (not production)
from airspace.engine.oracle import independent_oracle
```

## Proposed integration changes (need approval; see Report G)

1. `slot_step_min = 1` in the simulation configs: demo plan IDs and outcomes change.
   Hard-coded IDs in agents, tests and the UI would need updating.
2. An additive "weak Pareto improvement available" flag for the human reviewer: a contract
   addition coordinated with Member 2 (`RunResult`/events) and Member 3 (UI).
3. Optional rule A2 (scale-consistent tolerance): internal to the engine, no interface change.

## Limitations to state in the demo

* Synthetic simulation with organiser-supplied limits.
* Weights are uncalibrated assumptions.
* There is no real-world validation (Report F §6).
* Not certified, and not operational ATC.

## Items needing organiser clarification

* Does "45 min spare fuel on arrival" include the 30-min final reserve?
* Does a "turn" equal one message per agent? The rules-of-play reading is 2 rounds / 6
  messages ≤ 3 turns.
* Are the workbook limits per airport or per tier? Every limit is a pure function of size class.
