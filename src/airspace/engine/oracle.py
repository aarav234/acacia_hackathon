"""Brute-force correctness oracle for small fixtures.

Deliberately independent of fallback.choose_fallback / bargaining.choose_compromise:
it re-derives every role cost from the published raw metrics and the config
weights/scales, picks the fallback and the compromise by exhaustive pairwise
comparison (no sort keys, no shared helpers), and uses exact rationals. It
reuses the constraint validator only to learn which plans are feasible.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from airspace.contracts import Assessment, Scenario
from airspace.engine.candidates import generate_candidates
from airspace.engine.scoring import assess_candidates

_ROLES = ("atc", "pilot", "ground")


@dataclass(frozen=True)
class OracleResult:
    feasible_ids: tuple[str, ...]
    fallback_id: str | None
    compromise_id: str | None


def _cost(scenario: Scenario, a: Assessment, role: str) -> Fraction:
    total = Fraction(0)
    for spec in getattr(scenario.operations.utility, role).metrics:
        raw = Fraction(a.components[role][f"raw.{spec.metric}"])
        total += Fraction(spec.weight) * raw / Fraction(spec.scale)
    return total


def _earlier(a: Assessment, b: Assessment) -> bool:
    if a.plan.landing_min != b.plan.landing_min:
        return a.plan.landing_min < b.plan.landing_min
    if a.plan.gate_in_min != b.plan.gate_in_min:
        return a.plan.gate_in_min < b.plan.gate_in_min
    return a.plan.plan_id < b.plan.plan_id


def oracle(scenario: Scenario, epsilon: float = 1e-9) -> OracleResult:
    feasible = assess_candidates(scenario, generate_candidates(scenario))
    ids = tuple(a.plan.plan_id for a in feasible)
    if not feasible:
        return OracleResult(ids, None, None)

    fb = None
    for a in feasible:
        if all(a is b or _earlier(a, b) for b in feasible):
            fb = a
    assert fb is not None

    utility = {
        a.plan.plan_id: {r: 1 - _cost(scenario, a, r) for r in _ROLES} for a in feasible
    }
    d = utility[fb.plan.plan_id]
    eps = Fraction(epsilon)
    pool = []
    for a in feasible:
        g = {r: utility[a.plan.plan_id][r] - d[r] for r in _ROLES}
        if g["atc"] > eps and g["pilot"] > eps and g["ground"] > eps:
            pool.append((a, g))

    def beats(x, y) -> bool:
        (ax, gx), (ay, gy) = x, y
        px = gx["atc"] * gx["pilot"] * gx["ground"]
        py = gy["atc"] * gy["pilot"] * gy["ground"]
        if px != py:
            return px > py
        if min(gx.values()) != min(gy.values()):
            return min(gx.values()) > min(gy.values())
        cx = sum(1 - utility[ax.plan.plan_id][r] for r in _ROLES)
        cy = sum(1 - utility[ay.plan.plan_id][r] for r in _ROLES)
        if cx != cy:
            return cx < cy
        return _earlier(ax, ay)

    winner = None
    for x in pool:
        if all(x is y or beats(x, y) for y in pool):
            winner = x[0]
    return OracleResult(ids, fb.plan.plan_id, None if winner is None else winner.plan.plan_id)
