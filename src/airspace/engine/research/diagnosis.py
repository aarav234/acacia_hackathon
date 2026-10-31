"""Per-scenario diagnosis of why a strictly positive Nash bargain does / does not exist.

Labels (multi-label; each is demonstrated by a computation, never assumed):
  NO_FEASIBLE         F(s) is empty -> human review.
  POSITIVE_BARGAIN    some x in F has g_i > eps for every role.
  FALLBACK_EFFICIENT  no x in F weakly Pareto-improves the fallback.
  WEAK_ONLY           some x weakly improves the fallback but none strictly improves all roles.
  ROLE_CANNOT_GAIN:r  max_{x in F} g_r(x) <= eps (role r is already best off at the fallback).
  CONFLICT            every role can gain somewhere, but never all at once.
  CONSTRAINT_BOUND:R  a strict improvement exists among hard-INFEASIBLE candidates, which fail rules R
                      (counterfactual; the rules are not relaxed).
  GRID_LIMITED        the integer-minute refinement contains a strict bargain the slot grid lacks.
  WEIGHT_SENSITIVE    a strict bargain exists under some sampled weight vector (fraction reported).
"""

from __future__ import annotations

from collections import Counter
from fractions import Fraction

from airspace.engine.oracle import independent_oracle
from airspace.engine.research.grid import refine
from airspace.engine.research.selectors import EPS, ROLES, Table, strict_nash, weakly_improves


def diagnose(s, weight_bargain_rate: float | None = None) -> dict:
    ref = independent_oracle(s)
    t = Table.from_oracle(ref)
    if t.fallback is None:
        return {"labels": ["NO_FEASIBLE"], "feasible": 0, "candidates": len(ref.plans)}
    fb = t.fallback
    d = t.u[fb]
    max_gain = {r: max(t.gains(p)[r] for p in t.ids) for r in ROLES}
    others = [p for p in t.ids if p != fb]
    margin = max((min(t.gains(p).values()) for p in others), default=None)  # best worst-role gain, x != fallback
    weak = [p for p in t.ids if weakly_improves(t, p, fb)]
    strict = [p for p in t.ids if all(v > EPS for v in t.gains(p).values())]
    labels = []
    if strict:
        labels.append("POSITIVE_BARGAIN")
    elif weak:
        labels.append("WEAK_ONLY")
    else:
        labels.append("FALLBACK_EFFICIENT")
    blocked = [r for r in ROLES if max_gain[r] <= EPS]
    labels += [f"ROLE_CANNOT_GAIN:{r}" for r in blocked]
    if not strict and not blocked:
        labels.append("CONFLICT")
    # counterfactual: strict improvements among infeasible plans with defined metrics
    w = {r: {m.metric: m.weight for m in getattr(s.operations.utility, r).metrics} for r in ROLES}
    sc = {m.metric: m.scale for r in ROLES for m in getattr(s.operations.utility, r).metrics}
    bound = Counter()
    for pid, v in ref.plans.items():
        if not v["failed"] or v["raw"]["ground"]["crew_overtime_min"] is None:
            continue
        u = {r: 1 - sum(Fraction(w[r][m]) * v["raw"][r][m] / Fraction(sc[m]) for m in w[r]) for r in ROLES}
        if all(u[r] - d[r] > EPS for r in ROLES):
            bound[frozenset(v["failed"])] += 1
    if bound and not strict:
        rules = sorted({r for k in bound for r in k})
        labels.append("CONSTRAINT_BOUND:" + "+".join(rules))
    if not strict:
        t1 = Table.from_oracle(independent_oracle(refine(s, 1)))
        if strict_nash(t1) is not None:
            labels.append("GRID_LIMITED")
    if weight_bargain_rate:
        labels.append("WEIGHT_SENSITIVE")
    return {
        "labels": labels, "feasible": len(t.ids), "candidates": len(ref.plans), "fallback": fb,
        "max_gain": {r: float(v) for r, v in max_gain.items()}, "best_simultaneous_min_gain": None if margin is None else float(margin),
        "weak_improvements": len(weak), "strict_improvements": len(strict),
        "infeasible_strict_improvements_by_rules": {"+".join(sorted(k)): n for k, n in bound.items()},
        "weight_bargain_rate": weight_bargain_rate,
    }
