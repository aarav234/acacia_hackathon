"""Role cost/utility scoring for feasible plans.

Raw metrics (units in contracts.METRIC_UNITS), with d = landing_min - earliest_landing_min:

ATC
  landing_delay_min      = d
  spillover_proxy_min    = sum over fixed background movements on the same runway with
                           landing <= t < landing + W of (W - (t - landing)),
                           W = utility.spillover_window_min.
                           PROXY of how tightly the inserted landing packs against
                           following traffic. The background is fixed, so this cannot
                           measure real knock-on delay or network throughput.
PILOT
  incremental_fuel_kg    = airborne_burn_kg_per_min * d + taxi_burn_kg_per_min * taxi_min
                           (preference; the reserve itself is a hard rule in constraints)
  connection_penalty_pax = sum of ASSUMED passenger_count of groups with gate_in > deadline
  turbulence_exposure_index = runway exposure_index + holding_exposure_per_min * d
GROUND
  turnaround_delay_min   = max(0, service_end - (scheduled_gate_in + service_duration))
  crew_overtime_min      = least overtime over crews that can cover the service
                           (availability is hard in constraints; overtime is soft here)
  gate_change_indicator  = 1 if gate_id != assigned_gate_id else 0

normalized = raw / scale; C_role = sum(weight * normalized); U_role = 1 - C_role.
C and U are NOT clipped. Delay components are not double-counted within a role:
ATC measures delay from the earliest feasible landing, ground measures turnaround
lateness against the simulated schedule; neither adds the historical delay causes.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from airspace.contracts import (
    ROLE_METRICS,
    ROLES,
    Assessment,
    Plan,
    Role,
    Scenario,
    StaleStateError,
    ValidationReport,
    ensure_sealed,
)
from airspace.engine.constraints import crew_options, validate_plan
from airspace.engine.errors import PlanningError

logger = logging.getLogger(__name__)


def raw_metrics(scenario: Scenario, plan: Plan) -> dict[Role, dict[str, float]]:
    ops = scenario.operations
    delay = plan.landing_min - scenario.earliest_landing_min
    window = ops.utility.spillover_window_min
    spill = 0
    for move in ops.runway_movements:
        if move.runway_id == plan.runway_id and plan.landing_min <= move.time_min < plan.landing_min + window:
            spill += window - (move.time_min - plan.landing_min)
    missed = sum(g.passenger_count for g in ops.connections if plan.gate_in_min > g.deadline_gate_in_min)
    target_end = ops.subject.scheduled_gate_in_min + ops.service.service_duration_min
    options, _ = crew_options(scenario, plan.gate_in_min, plan.service_end_min)
    if not options:
        raise PlanningError(f"{plan.plan_id}: no crew can cover service; plan must be infeasible")
    return {
        "atc": {"landing_delay_min": float(delay), "spillover_proxy_min": float(spill)},
        "pilot": {
            "incremental_fuel_kg": ops.fuel.airborne_burn_kg_per_min * delay
            + ops.fuel.taxi_burn_kg_per_min * plan.taxi_min,
            "connection_penalty_pax": float(missed),
            "turbulence_exposure_index": ops.runway_exposure(plan.runway_id)
            + ops.turbulence.holding_exposure_per_min * delay,
        },
        "ground": {
            "turnaround_delay_min": float(max(0, plan.service_end_min - target_end)),
            "crew_overtime_min": float(options[0].overtime_min),
            "gate_change_indicator": 0.0 if plan.gate_id == ops.subject.assigned_gate_id else 1.0,
        },
    }


def score(scenario: Scenario, raw: dict[Role, dict[str, float]]) -> tuple[dict[Role, float], dict[Role, dict[str, float]]]:
    utilities: dict[Role, float] = {}
    components: dict[Role, dict[str, float]] = {}
    for role in ROLES:
        comp: dict[str, float] = {}
        cost = 0.0
        for metric in ROLE_METRICS[role]:
            spec = scenario.operations.utility.spec(role, metric)
            value = raw[role][metric]
            normalized = value / spec.scale
            weighted = spec.weight * normalized
            comp[f"raw.{metric}"] = value
            comp[f"normalized.{metric}"] = normalized
            comp[f"weighted.{metric}"] = weighted
            cost += weighted
        comp["cost"] = cost
        utility = 1.0 - cost
        if not (math.isfinite(cost) and math.isfinite(utility)):
            raise PlanningError(f"non-finite {role} cost")
        components[role] = comp
        utilities[role] = utility
    return utilities, components


@dataclass(frozen=True)
class CandidateEvaluation:
    assessments: list[Assessment]  # feasible only, input order preserved
    rejected: list[ValidationReport]  # infeasible reports for the audit log


def evaluate_candidates(scenario: Scenario, plans: list[Plan]) -> CandidateEvaluation:
    ensure_sealed(scenario)
    seen: set[str] = set()
    assessments: list[Assessment] = []
    rejected: list[ValidationReport] = []
    for plan in plans:
        if plan.state_hash != scenario.state_hash or plan.scenario_id != scenario.scenario_id:
            raise StaleStateError(f"plan {plan.plan_id} has a stale scenario/state hash")
        if plan.plan_id in seen:
            raise PlanningError(f"duplicate plan_id {plan.plan_id}")
        seen.add(plan.plan_id)
        report = validate_plan(scenario, plan)
        if not report.feasible:
            rejected.append(report)
            continue
        utilities, components = score(scenario, raw_metrics(scenario, plan))
        assessments.append(
            Assessment(plan=plan, validation=report, utilities=utilities, components=components)
        )
    return CandidateEvaluation(assessments, rejected)


def assess_candidates(scenario: Scenario, plans: list[Plan]) -> list[Assessment]:
    """Feasible assessments only; infeasible reports are logged separately."""
    result = evaluate_candidates(scenario, plans)
    for report in result.rejected:
        logger.debug(
            "rejected %s: %s", report.plan_id, ",".join(v.rule_id for v in report.violations)
        )
    logger.info(
        "scenario %s: %d feasible, %d rejected",
        scenario.scenario_id,
        len(result.assessments),
        len(result.rejected),
    )
    return result.assessments
