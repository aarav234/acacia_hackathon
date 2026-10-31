# Shared Contract v1 — canonical implementation reference

Owner: Member 1. Canonical code: `src/airspace/contracts.py`. JSON Schemas:
`fixtures/schemas/*.schema.json` (regenerate with `PYTHONPATH=src python -m airspace.contracts fixtures/schemas`).
Proposed changes and interpretations go in `docs/contract_changes.md`, not silently into code.

## Exact imports for Members 2 and 3

```python
from airspace.contracts import (
    Role, ROLES, ProviderMode, SCHEMA_VERSION, EPSILON_DEFAULT,
    Scenario, OperationalConfig, SimulationConfig, Plan, Evidence, Violation,
    ValidationReport, Assessment, Objection, AgentMessage, LogEvent, RunResult,
    StaleStateError, ContractError, ensure_sealed, canonical_hash,
    compute_state_hash,      # accepts a Scenario or its model_dump() dict
    RULE_IDS, METRIC_ROLE_PREFIXES,
)
from airspace.data.ingest import select_flight            # (csv_path, *, row_index=None) -> dict
from airspace.data.scenario import build_scenario         # (row, config_path) -> Scenario
from airspace.data.scenario import synthetic_row, load_scenario
from airspace.engine.candidates import generate_candidates  # (scenario) -> list[Plan]
from airspace.engine.constraints import validate_plan       # (scenario, plan) -> ValidationReport
from airspace.engine.scoring import assess_candidates       # feasible only
from airspace.engine.scoring import evaluate_candidates     # (.assessments, .rejected) for audit logs
from airspace.engine.fallback import choose_fallback         # -> Plan | None
from airspace.engine.bargaining import choose_compromise     # (assessments, fallback_id, *, epsilon) -> Plan | None
from airspace.engine.bargaining import pareto_front, role_best, gains   # shortlist helpers
from airspace.engine.pipeline import solve_scenario          # composition of the APIs above

# Challenge-calibrated suite and independent checks (Member 1 analysis; no contract change)
from airspace.data.scenario_suite import build_suite, suite_specs   # 40 scenarios, deterministic
from airspace.engine.oracle import independent_oracle                # exact re-implementation for evaluation

# Fixtures (no flights.csv needed)
from airspace.data.fixtures import (
    load_fixture_scenario,      # "success" | "no_feasible" | "no_positive_gain"
    load_example_assessments,   # list[Assessment] for the success scenario
    load_example_result,        # RunResult — MOCK example, not a run
    config_path, build_config, config_dict,   # config builders for tests/sensitivity runs
)
```

`airspace` currently resolves as a PEP 420 namespace package (Member 3 owns
`src/airspace/__init__.py`). Run with `PYTHONPATH=src` until the packaging PR lands.

## Global rules

* All models: `extra='forbid'`, frozen, explicit validators. Safety numbers are
  strict: `"5"`, `True`, `NaN`, `inf` are rejected; an int is accepted where a
  float is declared. Parse external input with `Model.model_validate_json`.
* Enums/literals serialize as their string values.
* Minutes are integers relative to the labelled simulation epoch
  (`simulation_epoch_utc`, minute 0). Fuel is kg. Log timestamps are ISO 8601 UTC
  (`Z` or `+00:00`) — anything else is rejected.
* Intervals are half-open `[start, end)`: `[0,5)` and `[5,9)` do **not** overlap.
* `Scenario.state_hash` = SHA256 of the canonical JSON (sorted keys, no
  whitespace) of every Scenario field except `state_hash`. It is re-verified on
  every load; engine entry points also call `ensure_sealed` to catch in-place
  mutation of dict/list fields. ID-keyed operational collections are sorted
  by ID during validation, so input key/list order never changes the hash.
  Config file name and build time are not part of the hash.

## Canonical models (fields exactly as Shared Contract v1)

| Model | Fields | Extra invariants enforced |
|---|---|---|
| `Scenario` | schema_version, scenario_id, state_hash, source_mode, historical_row, historical_ref, simulation_epoch_utc, earliest_landing_min, planning_horizon_min, slot_step_min, operations, provenance | hash matches; provenance has unique IDs incl. all `REQUIRED_EVIDENCE_IDS`; `historical_anchor` needs `source_file, source_sha256, row_index, selection_predicate, row_hash` and the row hash must match; `synthetic` needs `historical_ref.mode='synthetic'`, empty `historical_row`, no fake sha/row index |
| `Plan` | plan_id, scenario_id, state_hash, landing_min, runway_id, gate_id, taxi_route_id, taxi_min, gate_in_min, service_end_min | `gate_in = landing + taxi`; `service_end > gate_in`. `plan_id = P{landing:04d}_{runway}_{gate}_{route}` (stable) |
| `Evidence` | evidence_id, kind ∈ historical/derived/simulated, field_path, source_ref, note | id format `namespace:path` |
| `Violation` | rule_id, reason, observed, required, units, evidence_ids | |
| `ValidationReport` | plan_id, feasible, violations, metrics | `feasible == (violations == [])`; finite metrics; every metric key starts with `atc_`, `pilot_` or `ground_` (`METRIC_ROLE_PREFIXES`) |
| `Assessment` | plan, validation, utilities, components | keys exactly atc/pilot/ground; finite; validation must be feasible; `utilities[r] == 1 - components[r]['cost']` |
| `Objection` | code, plan_id, rule_id, evidence_ids, reason | |
| `AgentMessage` | scenario_id, state_hash, role, round, action, plan_id, ranked_plan_ids, objections, rationale, evidence_ids | round 1 = propose/reject, round 2 = accept/reject; propose/accept need plan_id; ≤3 unique ranked IDs; rationale ≤120 words; no extra (numeric) fields. `check_known(scenario_id, state_hash, known_ids)` rejects stale/unknown IDs |
| `LogEvent` | sequence, timestamp_utc, scenario_id, state_hash, round (0–2), actor, event_type, payload | UTC timestamp |
| `RunResult` | schema_version (defaults to `"1.0"`), scenario_id, state_hash, status, reason_code, selected_plan_id, fallback_plan_id, rounds_completed, agent_message_count, provider_mode, events, elapsed_ms, human_approval_required | `human_approval_required` is literally `True`; consensus ⇒ selected set, 2 rounds, 6 messages; fallback ⇒ selected == fallback; human_review ⇒ selected None; strictly increasing event sequence; events share the result's hash |

`components[role]` keys: `raw.<metric>`, `normalized.<metric>`, `weighted.<metric>`, `cost`.

## OperationalConfig (all values SIMULATED unless independently evidenced)

| Section | Model / fields |
|---|---|
| `subject` | `SubjectAircraft`: flight_label, wake_category, runway_occupancy_min (>0), assigned_gate_id, scheduled_gate_in_min |
| `separation` | `SeparationConfig`: categories, rules[`SeparationRule`: leader_category, follower_category, min_separation_min ≥0] — complete matrix required |
| `runways` | `Runway`: runway_id, open_windows[`TimeWindow`: start_min, end_min] (disjoint) |
| `runway_movements` | `RunwayMovement`: movement_id, runway_id, time_min, wake_category, kind ∈ arrival/departure (fixed background) |
| `gates` | `Gate`: gate_id, compatible_categories, open_windows, turnover_buffer_min ≥0 |
| `gate_occupancy` | `GateOccupancy`: occupancy_id, gate_id, start_min, end_min (before buffer) |
| `taxi_routes` | `TaxiRoute`: route_id, runway_id, gate_id, taxi_min (>0), segment_ids — only listed runway/gate pairs are connected |
| `taxi_blockages` | `TaxiBlockage`: blockage_id, segment_id, start_min, end_min |
| `service` | `ServiceConfig`: service_duration_min (>0) |
| `fuel` | `FuelConfig`: snapshot_fuel_kg, approach_burn_kg, airborne_burn_kg_per_min, taxi_burn_kg_per_min, min_reserve_kg |
| `crew` | `CrewShift`: crew_id, available (hard window), regular_end_min (overtime after), busy windows |
| `connections` | `ConnectionGroup`: group_id, passenger_count (ASSUMED), deadline_gate_in_min |
| `turbulence` | `TurbulenceConfig`: runway_exposure[`RunwayExposure`: runway_id, exposure_index] (every runway once), holding_exposure_per_min |
| `utility` | `UtilityConfig`: atc/pilot/ground → metrics[`MetricSpec`: metric, weight ≥0, scale >0], spillover_window_min. Metric set per role fixed by `ROLE_METRICS`; weights sum to 1 (±1e-9) |
| `planning` | `PlanningConfig`: candidate_cap (default 2000 — the only defaulted field) |

Safety-critical sections have no defaults: a missing section raises
`MissingSafetyInputError` naming the path (e.g. `operations.fuel.min_reserve_kg`).

`SimulationConfig` (the file in `configs/`) = schema_version, config_version,
scenario_id, simulation_epoch_utc, earliest_landing_min, planning_horizon_min,
slot_step_min, operations, assumptions[`Assumption`: field_path, note].

## Evidence IDs

Every scenario contains `sim:scenario.planning`, `sim:scenario.epoch` and
`sim:operations.<section>` for each section above. Historical scenarios add
`hist:row`, `hist:join.airline|origin_airport|destination_airport`,
`derived:clock.<HHMM column>`; synthetic ones add `sim:scenario.synthetic_anchor`.
All carry `derived:leakage_guard`. Every `Violation.evidence_ids` entry is one of these.

## Rule IDs (`airspace.engine.constraints.RULES`)

Published as `airspace.contracts.RULE_IDS` (the engine refuses to import if they drift):
`PLAN-STATE, PLAN-SLOT, PLAN-ROUTE, PLAN-TIMING, RWY-OPEN, RWY-SEP-LEAD,
RWY-SEP-FOLLOW, GATE-COMPAT, GATE-OPEN, GATE-OCCUPANCY, TAXI-BLOCK,
CREW-COVERAGE, FUEL-RESERVE` — semantics in `docs/data_and_model.md`.

## Validation metric keys

`atc_min_lead_separation_margin_min`, `atc_min_follow_separation_margin_min` (min, present only
when a background movement precedes/follows on that runway), `pilot_fuel_at_gate_in_kg`,
`pilot_reserve_margin_kg` (kg), `pilot_holding_min` (min), `ground_covering_crew_count`.

## Fixtures

| File | Content |
|---|---|
| `fixtures/historical_anchor_row.json` | one genuine `flights.csv` row (data row 70, AA1057 DFW→MIA, WEATHER_DELAY=102) + ingestion provenance |
| `fixtures/success_scenario.json` | historical anchor + simulated ops; 48 candidates, 23 feasible; three different role-preferred plans and a fourth Nash compromise |
| `fixtures/no_feasible_scenario.json` | SYNTHETIC; every plan fails FUEL-RESERVE ⇒ human_review |
| `fixtures/no_positive_gain_scenario.json` | SYNTHETIC; fallback exists, no plan improves all three roles ⇒ `no_positive_bargain` |
| `fixtures/example_assessments.json` | feasible assessments of the success scenario |
| `fixtures/example_result.json` | **MOCK** `RunResult` illustrating the schema (`reason_code=example_fixture_mock_not_a_run`); not negotiation evidence |
| `configs/sim_*_v1.json` | the versioned simulation configs the scenarios were built from |
