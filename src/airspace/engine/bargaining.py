"""Discrete Nash-product selection (inspired by Nash bargaining).

d_i = U_i(fallback). Eligible plans have U_i - d_i > epsilon for ALL roles.
Maximize prod_i (U_i - d_i). Ties (exact, see below): larger minimum gain,
then smaller sum of role costs C_i, then earlier landing_min, earlier
gate_in_min, lexicographic plan_id. No eligible plan -> None (no_positive_bargain).

Gains, products and cost sums are computed exactly with fractions.Fraction
over the float utilities, so ties are decided on exact values rather than on
float rounding of the product. Epsilon is never added to the product and the
disagreement point is never moved.

This is a finite, nonconvex catalogue: classical uniqueness axioms are not
inherited, and nothing here proves strategic truthfulness or certifies safety.
"""

from __future__ import annotations

import math
from fractions import Fraction

from airspace.contracts import EPSILON_DEFAULT, ROLES, Assessment, Plan, Role
from airspace.engine.errors import UnknownPlanError
from airspace.engine.fallback import check_assessments


def gains(assessment: Assessment, disagreement: dict[Role, float]) -> dict[Role, Fraction]:
    return {r: Fraction(assessment.utilities[r]) - Fraction(disagreement[r]) for r in ROLES}


def _check_epsilon(epsilon: float) -> Fraction:
    if isinstance(epsilon, bool) or not isinstance(epsilon, (int, float)) or not math.isfinite(epsilon) or epsilon < 0:
        raise ValueError(f"epsilon must be a finite non-negative number, got {epsilon!r}")
    return Fraction(epsilon)


def disagreement_point(assessments: list[Assessment], fallback_id: str) -> dict[Role, float]:
    match = [a for a in assessments if a.plan.plan_id == fallback_id]
    if not match:
        raise UnknownPlanError(f"fallback_id {fallback_id!r} is not a feasible assessment")
    return dict(match[0].utilities)


def selection_key(a: Assessment, g: dict[Role, Fraction]) -> tuple:
    product = g["atc"] * g["pilot"] * g["ground"]
    cost_sum = sum((Fraction(a.components[r]["cost"]) for r in ROLES), Fraction(0))
    return (-product, -min(g.values()), cost_sum, a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id)


def eligible(assessments: list[Assessment], fallback_id: str, *, epsilon: float = EPSILON_DEFAULT) -> list[Assessment]:
    eps = _check_epsilon(epsilon)
    check_assessments(assessments)
    d = disagreement_point(assessments, fallback_id)
    return [a for a in assessments if all(v > eps for v in gains(a, d).values())]


def choose_compromise(
    assessments: list[Assessment], fallback_id: str, *, epsilon: float = EPSILON_DEFAULT
) -> Plan | None:
    candidates = eligible(assessments, fallback_id, epsilon=epsilon)
    if not candidates:
        return None
    d = disagreement_point(assessments, fallback_id)
    return min(candidates, key=lambda a: selection_key(a, gains(a, d))).plan


def nash_product(assessment: Assessment, disagreement: dict[Role, float]) -> Fraction:
    g = gains(assessment, disagreement)
    return g["atc"] * g["pilot"] * g["ground"]


def dominates(a: Assessment, b: Assessment) -> bool:
    """a Pareto-dominates b: >= for every role, > for at least one."""
    ua, ub = a.utilities, b.utilities
    return all(ua[r] >= ub[r] for r in ROLES) and any(ua[r] > ub[r] for r in ROLES)


def pareto_front(assessments: list[Assessment]) -> list[Assessment]:
    """Non-dominated assessments, in input order (helper for shortlists/audit)."""
    return [a for a in assessments if not any(dominates(b, a) for b in assessments)]


def role_best(assessments: list[Assessment], role: Role) -> Assessment | None:
    """Role-utility maximizer with the fallback tie-break order."""
    if not assessments:
        return None
    return min(
        assessments,
        key=lambda a: (-Fraction(a.utilities[role]), a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id),
    )
