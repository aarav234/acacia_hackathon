"""Hard-constraint validation under the SIMULATED toy safety model.

Every interval is half-open [start, end). Rule catalogue (see RULES):

PLAN-STATE        plan belongs to this scenario/state hash
PLAN-SLOT         landing_min lies on the configured slot grid
PLAN-ROUTE        taxi route exists and matches runway/gate/taxi_min
PLAN-TIMING       gate_in = landing + taxi; service_end = gate_in + service duration
RWY-OPEN          runway occupancy [landing, landing+occupancy) inside one open window
RWY-SEP-LEAD      background movement at/before landing: landing - t >= sep[movement, subject]
RWY-SEP-FOLLOW    background movement at/after landing:  t - landing >= sep[subject, movement]
GATE-COMPAT       gate accepts the subject wake category
GATE-OPEN         gate use [gate_in, service_end) inside one gate open window
GATE-OCCUPANCY    buffered uses do not overlap (convention below)
TAXI-BLOCK        no route segment blocked during taxi [landing, gate_in)
CREW-COVERAGE     some crew available (hard) and not busy for [gate_in, service_end)
FUEL-RESERVE      fuel on board at gate-in >= min_reserve_kg (includes taxi burn)

Gate buffer convention: every use of a gate - existing occupancy AND the
proposed plan - occupies [start, end + turnover_buffer_min). Two uses conflict
iff those buffered intervals overlap, so the same buffer separates a proposed
use from an earlier existing use and an existing use from an earlier proposed one.

A rule here is a simulated assumption, not an FAA/ICAO/Airbus operational standard.
No preference or utility can override a violation.
"""

from __future__ import annotations

from dataclasses import dataclass

from airspace.contracts import (
    RULE_IDS,
    SCENARIO_PLANNING_EVIDENCE_ID,
    Plan,
    Scenario,
    ValidationReport,
    Violation,
    ensure_sealed,
    overlaps,
    section_evidence_id as ev,
)


@dataclass(frozen=True)
class Rule:
    rule_id: str
    description: str
    evidence_ids: tuple[str, ...]


RULES: dict[str, Rule] = {
    r.rule_id: r
    for r in (
        Rule("PLAN-STATE", "plan scenario/state hash matches", (SCENARIO_PLANNING_EVIDENCE_ID,)),
        Rule("PLAN-SLOT", "landing on slot grid", (SCENARIO_PLANNING_EVIDENCE_ID,)),
        Rule("PLAN-ROUTE", "configured taxi route", (ev("taxi_routes"),)),
        Rule("PLAN-TIMING", "derived timing consistent", (ev("taxi_routes"), ev("service"))),
        Rule("RWY-OPEN", "runway open for occupancy", (ev("runways"), ev("subject"))),
        Rule("RWY-SEP-LEAD", "separation behind preceding movement", (ev("separation"), ev("runway_movements"), ev("subject"))),
        Rule("RWY-SEP-FOLLOW", "separation ahead of following movement", (ev("separation"), ev("runway_movements"), ev("subject"))),
        Rule("GATE-COMPAT", "gate category compatibility", (ev("gates"), ev("subject"))),
        Rule("GATE-OPEN", "gate open during use", (ev("gates"), ev("service"))),
        Rule("GATE-OCCUPANCY", "buffered gate occupancy", (ev("gates"), ev("gate_occupancy"), ev("service"))),
        Rule("TAXI-BLOCK", "taxi segment blockage", (ev("taxi_routes"), ev("taxi_blockages"))),
        Rule("CREW-COVERAGE", "crew coverage for service", (ev("crew"), ev("service"))),
        Rule("FUEL-RESERVE", "reserve at gate-in incl. taxi burn", (ev("fuel"), ev("taxi_routes"), SCENARIO_PLANNING_EVIDENCE_ID)),
    )
}
if tuple(RULES) != RULE_IDS:  # the published contract list and the engine must agree
    raise RuntimeError("engine RULES out of sync with contracts.RULE_IDS")


def _violation(rule_id: str, reason: str, observed: str, required: str, units: str) -> Violation:
    return Violation(
        rule_id=rule_id,
        reason=reason,
        observed=observed,
        required=required,
        units=units,
        evidence_ids=list(RULES[rule_id].evidence_ids),
    )


@dataclass(frozen=True)
class FuelState:
    holding_min: int
    airborne_burn_kg: float
    taxi_burn_kg: float
    fuel_at_gate_in_kg: float
    reserve_margin_kg: float


def fuel_state(scenario: Scenario, landing_min: int, taxi_min: int) -> FuelState:
    fuel = scenario.operations.fuel
    holding = landing_min - scenario.earliest_landing_min
    airborne = fuel.approach_burn_kg + fuel.airborne_burn_kg_per_min * holding
    taxi = fuel.taxi_burn_kg_per_min * taxi_min
    at_gate = fuel.snapshot_fuel_kg - airborne - taxi
    return FuelState(holding, airborne, taxi, at_gate, at_gate - fuel.min_reserve_kg)


@dataclass(frozen=True)
class CrewChoice:
    crew_id: str
    overtime_min: int


def crew_options(scenario: Scenario, start: int, end: int) -> tuple[list[CrewChoice], list[str]]:
    """Crews that can cover [start, end) and reasons for those that cannot."""
    options: list[CrewChoice] = []
    reasons: list[str] = []
    for crew in scenario.operations.crew:
        if not crew.available.contains(start, end):
            reasons.append(
                f"{crew.crew_id} available [{crew.available.start_min},{crew.available.end_min})"
            )
            continue
        clash = next((b for b in crew.busy if overlaps(start, end, b.start_min, b.end_min)), None)
        if clash is not None:
            reasons.append(f"{crew.crew_id} busy [{clash.start_min},{clash.end_min})")
            continue
        overtime = max(0, end - max(crew.regular_end_min, start))
        options.append(CrewChoice(crew.crew_id, overtime))
    options.sort(key=lambda c: (c.overtime_min, c.crew_id))
    return options, reasons


def validate_plan(scenario: Scenario, plan: Plan) -> ValidationReport:
    ensure_sealed(scenario)
    ops = scenario.operations
    subject = ops.subject
    violations: list[Violation] = []
    metrics: dict[str, float] = {}

    # ---- structural checks; later checks need a sound plan ---------------
    if plan.scenario_id != scenario.scenario_id or plan.state_hash != scenario.state_hash:
        violations.append(
            _violation(
                "PLAN-STATE",
                "plan was generated for a different scenario/state (stale)",
                f"{plan.scenario_id}@{plan.state_hash[:12]}",
                f"{scenario.scenario_id}@{scenario.state_hash[:12]}",
                "hash",
            )
        )
    if plan.landing_min not in scenario.slots:
        violations.append(
            _violation(
                "PLAN-SLOT",
                "landing_min is not on the configured slot grid",
                f"landing_min={plan.landing_min}",
                f"{scenario.earliest_landing_min} + k*{scenario.slot_step_min} < "
                f"{scenario.earliest_landing_min + scenario.planning_horizon_min}",
                "min",
            )
        )
    route = ops.route(plan.taxi_route_id)
    if route is None or (route.runway_id, route.gate_id, route.taxi_min) != (plan.runway_id, plan.gate_id, plan.taxi_min):
        violations.append(
            _violation(
                "PLAN-ROUTE",
                "taxi route missing or inconsistent with runway/gate/taxi_min",
                f"route={plan.taxi_route_id} runway={plan.runway_id} gate={plan.gate_id} taxi={plan.taxi_min}",
                "configured route" if route is None else f"runway={route.runway_id} gate={route.gate_id} taxi={route.taxi_min}",
                "min",
            )
        )
    expected_end = plan.gate_in_min + ops.service.service_duration_min
    if plan.gate_in_min != plan.landing_min + plan.taxi_min or plan.service_end_min != expected_end:
        violations.append(
            _violation(
                "PLAN-TIMING",
                "derived plan timing inconsistent with configuration",
                f"gate_in={plan.gate_in_min} service_end={plan.service_end_min}",
                f"gate_in={plan.landing_min + plan.taxi_min} service_end={expected_end}",
                "min",
            )
        )
    if violations:
        return ValidationReport(plan_id=plan.plan_id, feasible=False, violations=violations, metrics={})

    landing, gate_in, service_end = plan.landing_min, plan.gate_in_min, plan.service_end_min

    # ---- runway ----------------------------------------------------------
    runway = ops.runway(plan.runway_id)
    occ_end = landing + subject.runway_occupancy_min
    if not any(w.contains(landing, occ_end) for w in runway.open_windows):
        windows = ", ".join(f"[{w.start_min},{w.end_min})" for w in runway.open_windows) or "none"
        violations.append(
            _violation(
                "RWY-OPEN",
                f"runway {runway.runway_id} not open for the whole landing occupancy",
                f"occupancy [{landing},{occ_end})",
                f"inside one open window: {windows}",
                "min",
            )
        )
    lead_margin: float | None = None
    follow_margin: float | None = None
    for move in ops.runway_movements:
        if move.runway_id != plan.runway_id:
            continue
        if move.time_min <= landing:  # movement precedes (or ties) the candidate
            need = ops.separation.required_min(move.wake_category, subject.wake_category)
            gap = landing - move.time_min
            lead_margin = gap - need if lead_margin is None else min(lead_margin, gap - need)
            if gap < need:
                violations.append(
                    _violation(
                        "RWY-SEP-LEAD",
                        f"insufficient separation behind {move.kind} {move.movement_id} ({move.wake_category})",
                        f"gap={gap}",
                        f">= {need} ({move.wake_category} leader -> {subject.wake_category} follower)",
                        "min",
                    )
                )
        if move.time_min >= landing:  # movement follows (or ties) the candidate
            need = ops.separation.required_min(subject.wake_category, move.wake_category)
            gap = move.time_min - landing
            follow_margin = gap - need if follow_margin is None else min(follow_margin, gap - need)
            if gap < need:
                violations.append(
                    _violation(
                        "RWY-SEP-FOLLOW",
                        f"insufficient separation ahead of {move.kind} {move.movement_id} ({move.wake_category})",
                        f"gap={gap}",
                        f">= {need} ({subject.wake_category} leader -> {move.wake_category} follower)",
                        "min",
                    )
                )

    # ---- gate ------------------------------------------------------------
    gate = ops.gate(plan.gate_id)
    if subject.wake_category not in gate.compatible_categories:
        violations.append(
            _violation(
                "GATE-COMPAT",
                f"gate {gate.gate_id} cannot accept category {subject.wake_category}",
                subject.wake_category,
                "one of " + ",".join(gate.compatible_categories),
                "category",
            )
        )
    if not any(w.contains(gate_in, service_end) for w in gate.open_windows):
        windows = ", ".join(f"[{w.start_min},{w.end_min})" for w in gate.open_windows) or "none"
        violations.append(
            _violation(
                "GATE-OPEN",
                f"gate {gate.gate_id} not open for the whole service",
                f"use [{gate_in},{service_end})",
                f"inside one open window: {windows}",
                "min",
            )
        )
    buffer = gate.turnover_buffer_min
    for occ in ops.gate_occupancy:
        if occ.gate_id != gate.gate_id:
            continue
        if overlaps(gate_in, service_end + buffer, occ.start_min, occ.end_min + buffer):
            violations.append(
                _violation(
                    "GATE-OCCUPANCY",
                    f"gate {gate.gate_id} conflicts with existing use {occ.occupancy_id}",
                    f"proposed [{gate_in},{service_end + buffer}) vs existing [{occ.start_min},{occ.end_min + buffer})",
                    f"no overlap of uses each extended by turnover buffer {buffer}",
                    "min",
                )
            )

    # ---- taxi ------------------------------------------------------------
    route_segments = set(route.segment_ids)  # type: ignore[union-attr]
    for block in ops.taxi_blockages:
        if block.segment_id in route_segments and overlaps(landing, gate_in, block.start_min, block.end_min):
            violations.append(
                _violation(
                    "TAXI-BLOCK",
                    f"segment {block.segment_id} blocked ({block.blockage_id}) during taxi",
                    f"taxi [{landing},{gate_in})",
                    f"no overlap with blockage [{block.start_min},{block.end_min})",
                    "min",
                )
            )

    # ---- crew ------------------------------------------------------------
    options, crew_reasons = crew_options(scenario, gate_in, service_end)
    if not options:
        violations.append(
            _violation(
                "CREW-COVERAGE",
                "no ground crew available for the required service",
                "; ".join(crew_reasons) or "no crews configured",
                f"one crew available and not busy for [{gate_in},{service_end})",
                "min",
            )
        )

    # ---- fuel ------------------------------------------------------------
    fs = fuel_state(scenario, landing, plan.taxi_min)
    if fs.fuel_at_gate_in_kg < ops.fuel.min_reserve_kg:
        violations.append(
            _violation(
                "FUEL-RESERVE",
                "projected fuel at gate-in below simulated minimum reserve",
                f"{fs.fuel_at_gate_in_kg:.3f} (approach+holding {fs.airborne_burn_kg:.3f}, taxi {fs.taxi_burn_kg:.3f})",
                f">= {ops.fuel.min_reserve_kg:.3f}",
                "kg",
            )
        )

    # Keys carry the owning role's prefix (contracts.METRIC_ROLE_PREFIXES).
    metrics["pilot_fuel_at_gate_in_kg"] = fs.fuel_at_gate_in_kg
    metrics["pilot_reserve_margin_kg"] = fs.reserve_margin_kg
    metrics["pilot_holding_min"] = float(fs.holding_min)
    if lead_margin is not None:
        metrics["atc_min_lead_separation_margin_min"] = float(lead_margin)
    if follow_margin is not None:
        metrics["atc_min_follow_separation_margin_min"] = float(follow_margin)
    metrics["ground_covering_crew_count"] = float(len(options))
    return ValidationReport(
        plan_id=plan.plan_id, feasible=not violations, violations=violations, metrics=metrics
    )
