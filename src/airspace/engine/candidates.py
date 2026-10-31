"""Deterministic candidate catalogue: landing slots x configured taxi routes."""

from __future__ import annotations

from airspace.contracts import Plan, Scenario, ensure_sealed, make_plan_id
from airspace.engine.errors import CandidateCapExceededError


def candidate_count(scenario: Scenario) -> int:
    return len(scenario.slots) * len(scenario.operations.taxi_routes)


def generate_candidates(scenario: Scenario) -> list[Plan]:
    """Enumerate every representable plan.

    Slots are earliest_landing_min + k*slot_step_min inside the half-open horizon
    [earliest, earliest + planning_horizon_min). Each slot is combined only with
    configured runway->gate taxi routes (not the runway x gate Cartesian product).
    Order: (landing_min, runway_id, gate_id, route_id). Feasibility is NOT checked
    here; see constraints.validate_plan.
    """
    ensure_sealed(scenario)
    ops = scenario.operations
    count = candidate_count(scenario)
    if count > ops.planning.candidate_cap:
        raise CandidateCapExceededError(count, ops.planning.candidate_cap)
    routes = sorted(ops.taxi_routes, key=lambda r: (r.runway_id, r.gate_id, r.route_id))
    service = ops.service.service_duration_min
    plans: list[Plan] = []
    for landing in scenario.slots:
        for route in routes:
            gate_in = landing + route.taxi_min
            plans.append(
                Plan(
                    plan_id=make_plan_id(landing, route.runway_id, route.gate_id, route.route_id),
                    scenario_id=scenario.scenario_id,
                    state_hash=scenario.state_hash,
                    landing_min=landing,
                    runway_id=route.runway_id,
                    gate_id=route.gate_id,
                    taxi_route_id=route.route_id,
                    taxi_min=route.taxi_min,
                    gate_in_min=gate_in,
                    service_end_min=gate_in + service,
                )
            )
    return plans
