"""Deterministic fallback / disagreement point."""

from __future__ import annotations

import math

from airspace.contracts import ROLES, Assessment, Plan
from airspace.engine.errors import InconsistentAssessmentsError


def check_assessments(assessments: list[Assessment]) -> None:
    """Same scenario/state, unique plan ids, feasible, finite utilities."""
    keys = {(a.plan.scenario_id, a.plan.state_hash) for a in assessments}
    if len(keys) > 1:
        raise InconsistentAssessmentsError("assessments mix scenarios or state hashes")
    ids = [a.plan.plan_id for a in assessments]
    if len(set(ids)) != len(ids):
        raise InconsistentAssessmentsError("duplicate plan ids in assessments")
    for a in assessments:
        if not a.validation.feasible:
            raise InconsistentAssessmentsError(f"{a.plan.plan_id} is infeasible")
        for role in ROLES:
            if not math.isfinite(a.utilities[role]):
                raise InconsistentAssessmentsError(f"{a.plan.plan_id}: non-finite {role} utility")


def fallback_key(a: Assessment) -> tuple[int, int, str]:
    return (a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id)


def choose_fallback(assessments: list[Assessment]) -> Plan | None:
    """First feasible plan by (landing_min, gate_in_min, plan_id); None if none feasible.

    Assessments are, by contract, built only from plans that passed the full validator.
    """
    check_assessments(assessments)
    if not assessments:
        return None
    return min(assessments, key=fallback_key).plan
