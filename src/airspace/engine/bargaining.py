"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE. Discrete Nash-product selection."""
from __future__ import annotations

from airspace.contracts import ROLES, Assessment, Plan


def choose_compromise(assessments: list[Assessment], fallback_id: str, *,
                      epsilon: float = 1e-9) -> Plan | None:
    by_id = {a.plan.plan_id: a for a in assessments}
    if fallback_id not in by_id:
        raise ValueError("fallback_id must name a feasible assessment")
    d = by_id[fallback_id].utilities
    best, best_key = None, None
    for a in assessments:
        gains = [a.utilities[r] - d[r] for r in ROLES]
        if min(gains) <= epsilon:
            continue
        prod = gains[0] * gains[1] * gains[2]
        cost = sum(1.0 - a.utilities[r] for r in ROLES)  # sum of C_i
        key = (-prod, -min(gains), cost, a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id)
        if best_key is None or key < best_key:
            best, best_key = a.plan, key
    return best
