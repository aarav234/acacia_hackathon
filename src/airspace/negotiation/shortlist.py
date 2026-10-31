"""Deterministic <=12-plan prompt shortlist (the full catalogue stays in the audit).

Contents, in priority order: fallback; each role's utility-maximising plan;
then Pareto-optimal plans spread evenly across the (landing_min, gate_in_min,
plan_id) ordering; then (only if still short) highest total utility. Stable
tie-breaking throughout.
"""
from __future__ import annotations

from airspace.contracts import ROLES, Assessment

MAX_SHORTLIST = 12


def _order(a: Assessment) -> tuple:
    return (a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id)


def role_best(assessments: list[Assessment], role: str) -> Assessment:
    return min(assessments, key=lambda a: (-a.utilities[role], *_order(a)))


def pareto_front(assessments: list[Assessment]) -> list[Assessment]:
    front = []
    for a in assessments:
        dominated = any(
            all(b.utilities[r] >= a.utilities[r] for r in ROLES)
            and any(b.utilities[r] > a.utilities[r] for r in ROLES)
            for b in assessments if b is not a)
        if not dominated:
            front.append(a)
    return sorted(front, key=_order)


def build_shortlist(assessments: list[Assessment], fallback_id: str,
                    k: int = MAX_SHORTLIST) -> tuple[list[Assessment], dict[str, list[str]]]:
    if k < 4:
        raise ValueError("shortlist must hold fallback + three role optima")
    by_id = {a.plan.plan_id: a for a in assessments}
    picked: list[str] = []
    tags: dict[str, list[str]] = {}

    def add(pid: str, tag: str) -> None:
        tags.setdefault(pid, [])
        if tag not in tags[pid]:
            tags[pid].append(tag)
        if pid not in picked:
            picked.append(pid)

    add(fallback_id, "fallback")
    for r in ROLES:
        add(role_best(assessments, r).plan.plan_id, f"best_for_{r}")
    front = [a.plan.plan_id for a in pareto_front(assessments)]
    for pid in front:
        if pid in tags:
            tags[pid].append("pareto")
    rest = [p for p in front if p not in picked]
    room = k - len(picked)
    if rest and room > 0:
        if len(rest) <= room:
            chosen = rest
        else:  # evenly spaced, deterministic
            chosen = [rest[round(i * (len(rest) - 1) / (room - 1))] for i in range(room)] if room > 1 else [rest[0]]
        for pid in dict.fromkeys(chosen):
            add(pid, "pareto")
    if len(picked) < k:
        extra = sorted((a for a in assessments if a.plan.plan_id not in picked),
                       key=lambda a: (-sum(a.utilities.values()), *_order(a)))
        for a in extra[: k - len(picked)]:
            add(a.plan.plan_id, "high_total_utility")
    short = [by_id[p] for p in picked[:k]]
    return short, {p: tags[p] for p in picked[:k]}
