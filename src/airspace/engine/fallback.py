"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE."""
from __future__ import annotations

from airspace.contracts import Assessment, Plan


def choose_fallback(assessments: list[Assessment]) -> Plan | None:
    feas = [a for a in assessments if a.validation.feasible]
    if not feas:
        return None
    return min(feas, key=lambda a: (a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id)).plan
