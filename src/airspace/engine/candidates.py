"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE."""
from __future__ import annotations

from airspace.contracts import Plan, Scenario


def generate_candidates(scenario: Scenario) -> list[Plan]:
    ops = scenario.operations
    plans: list[Plan] = []
    end = scenario.earliest_landing_min + scenario.planning_horizon_min
    for landing in range(scenario.earliest_landing_min, end + 1, scenario.slot_step_min):
        for route in ops.taxi_routes:
            gate_in = landing + route.taxi_min
            plans.append(Plan(
                plan_id=f"P{landing:03d}-{route.runway_id}-{route.gate_id}-{route.taxi_route_id}",
                scenario_id=scenario.scenario_id, state_hash=scenario.state_hash,
                landing_min=landing, runway_id=route.runway_id, gate_id=route.gate_id,
                taxi_route_id=route.taxi_route_id, taxi_min=route.taxi_min,
                gate_in_min=gate_in, service_end_min=gate_in + ops.service_duration_min,
            ))
    if len(plans) > ops.catalogue_cap:
        raise ValueError(f"catalogue {len(plans)} exceeds cap {ops.catalogue_cap}; refusing to truncate")
    return plans
