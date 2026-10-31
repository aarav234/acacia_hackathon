"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE.

Half-open intervals [start, end). Rule IDs are stub names; Member 1 publishes
the canonical list.
"""
from __future__ import annotations

from airspace.contracts import Plan, Scenario, ValidationReport, Violation

RULE_IDS = ("R-RWY-OPEN", "R-SEP", "R-GATE-COMPAT", "R-GATE-OCC",
            "R-TAXI-BLOCK", "R-CREW-AVAIL", "R-FUEL-RESERVE", "R-PLAN-STATE")


def _overlap(a0: int, a1: int, b0: int, b1: int) -> bool:
    return a0 < b1 and b0 < a1


def _ev(scenario: Scenario, prefix: str) -> list[str]:
    return [e.evidence_id for e in scenario.provenance if e.field_path.startswith(prefix)]


def compute_metrics(scenario: Scenario, plan: Plan) -> dict[str, float]:
    ops = scenario.operations
    holding = plan.landing_min - scenario.earliest_landing_min
    rwy = next((r for r in ops.runways if r.runway_id == plan.runway_id), None)
    seps = []
    for m in ops.existing_movements:
        if m.runway_id != plan.runway_id:
            continue
        if m.time_min <= plan.landing_min:
            req = ops.separation_min[f"{m.category}>{ops.aircraft_category}"]
            seps.append(plan.landing_min - m.time_min - req)
        else:
            req = ops.separation_min[f"{ops.aircraft_category}>{m.category}"]
            seps.append(m.time_min - plan.landing_min - req)
    sep_margin = min(seps) if seps else 99.0
    spill = sum(1 for m in ops.existing_movements
                if m.runway_id == plan.runway_id and abs(m.time_min - plan.landing_min) < 8)
    f = ops.fuel
    extra_fuel = holding * f.airborne_burn_kg_per_min + plan.taxi_min * f.taxi_burn_kg_per_min
    at_gate = f.snapshot_kg - f.approach_burn_kg - extra_fuel
    missed = sum(c.passengers for c in ops.connections if plan.gate_in_min > c.deadline_min)
    gate = next((g for g in ops.gates if g.gate_id == plan.gate_id), None)
    buf_margins = []
    if gate:
        for w in gate.occupied:
            if w.end_min <= plan.gate_in_min:
                buf_margins.append(plan.gate_in_min - (w.end_min + gate.buffer_min))
            elif w.start_min >= plan.service_end_min:
                buf_margins.append(w.start_min - (plan.service_end_min + gate.buffer_min))
            else:
                buf_margins.append(-1.0)
    return {
        "atc_holding_min": float(holding),
        "atc_landing_delay_min": float(holding),
        "atc_separation_margin_min": float(sep_margin),
        "atc_spillover_proxy": float(spill),
        "pilot_extra_fuel_kg": float(extra_fuel),
        "pilot_fuel_at_gate_kg": float(at_gate),
        "pilot_reserve_margin_kg": float(at_gate - f.min_reserve_kg),
        "pilot_connection_pax_missed": float(missed),
        "pilot_turbulence_proxy": float(holding * (rwy.turbulence_exposure if rwy else 0.0)),
        "ground_turnaround_delay_min": float(max(0, plan.gate_in_min - ops.scheduled_gate_in_min)),
        "ground_crew_overtime_min": float(max(0, plan.service_end_min - ops.crew.overtime_after_min)),
        "ground_gate_change": 0.0 if plan.gate_id == ops.planned_gate_id else 1.0,
        "ground_gate_buffer_margin_min": float(min(buf_margins) if buf_margins else 99.0),
    }


def validate_plan(scenario: Scenario, plan: Plan) -> ValidationReport:
    ops = scenario.operations
    v: list[Violation] = []

    def add(rule, reason, observed, required, units, prefix):
        v.append(Violation(rule_id=rule, reason=reason, observed=str(observed),
                           required=str(required), units=units, evidence_ids=_ev(scenario, prefix)))

    if plan.scenario_id != scenario.scenario_id or plan.state_hash != scenario.state_hash:
        add("R-PLAN-STATE", "plan built for different scenario/state", plan.state_hash,
            scenario.state_hash, "hash", "operations")
    m = compute_metrics(scenario, plan)
    rwy = next((r for r in ops.runways if r.runway_id == plan.runway_id), None)
    if rwy is None or not any(w.start_min <= plan.landing_min < w.end_min for w in rwy.open_windows):
        add("R-RWY-OPEN", "runway not open at landing", plan.landing_min, "inside open window",
            "min", "operations.runways")
    if m["atc_separation_margin_min"] < 0:
        add("R-SEP", "separation from existing movement", m["atc_separation_margin_min"], ">= 0",
            "min margin", "operations.separation")
    gate = next((g for g in ops.gates if g.gate_id == plan.gate_id), None)
    if gate is None or ops.aircraft_category not in gate.compatible_categories:
        add("R-GATE-COMPAT", "gate incompatible with aircraft", plan.gate_id,
            "compatible gate", "id", "operations.gates")
    elif m["ground_gate_buffer_margin_min"] < 0:
        add("R-GATE-OCC", "buffered gate occupancy overlap", m["ground_gate_buffer_margin_min"],
            ">= 0", "min margin", "operations.gates")
    route = next((r for r in ops.taxi_routes if r.taxi_route_id == plan.taxi_route_id), None)
    if route is None or route.runway_id != plan.runway_id or route.gate_id != plan.gate_id:
        add("R-TAXI-BLOCK", "unknown or mismatched taxi route", plan.taxi_route_id,
            "configured route", "id", "operations.taxi_routes")
    elif any(_overlap(plan.landing_min, plan.gate_in_min, b.start_min, b.end_min) for b in route.blocked):
        add("R-TAXI-BLOCK", "taxi route blocked during taxi", plan.landing_min, "no overlap",
            "min", "operations.taxi_routes")
    c = ops.crew.available
    if not (c.start_min <= plan.gate_in_min and plan.service_end_min <= c.end_min):
        add("R-CREW-AVAIL", "ground crew unavailable for service", plan.service_end_min,
            f"<= {c.end_min}", "min", "operations.crew")
    if m["pilot_reserve_margin_kg"] < 0:
        add("R-FUEL-RESERVE", "projected reserve at gate-in below minimum",
            m["pilot_fuel_at_gate_kg"], f">= {ops.fuel.min_reserve_kg}", "kg", "operations.fuel")
    return ValidationReport(plan_id=plan.plan_id, feasible=not v, violations=v, metrics=m)
