"""Convenience composition of the public engine APIs (no separate solver logic).

The coordinator (Member 2) may call the individual APIs directly; this helper
exists for docs, fixtures and evaluation so every caller uses the same engine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from airspace.contracts import ROLES, Assessment, Plan, Role, Scenario, ValidationReport
from airspace.engine.bargaining import choose_compromise, gains, role_best
from airspace.engine.candidates import generate_candidates
from airspace.engine.fallback import choose_fallback
from airspace.engine.scoring import evaluate_candidates

EngineReason = Literal["compromise_found", "no_positive_bargain", "no_feasible_plan"]


@dataclass(frozen=True)
class EngineOutcome:
    scenario_id: str
    state_hash: str
    candidate_count: int
    assessments: list[Assessment]
    rejected: list[ValidationReport]
    fallback: Plan | None
    compromise: Plan | None
    reason: EngineReason
    role_best_ids: dict[Role, str] = field(default_factory=dict)

    def assessment(self, plan_id: str) -> Assessment:
        return next(a for a in self.assessments if a.plan.plan_id == plan_id)

    def summary(self) -> dict[str, object]:
        out: dict[str, object] = {
            "scenario_id": self.scenario_id,
            "state_hash": self.state_hash,
            "candidate_count": self.candidate_count,
            "feasible_count": len(self.assessments),
            "rejected_count": len(self.rejected),
            "fallback_plan_id": self.fallback.plan_id if self.fallback else None,
            "compromise_plan_id": self.compromise.plan_id if self.compromise else None,
            "reason": self.reason,
            "role_best_plan_ids": dict(self.role_best_ids),
        }
        if self.fallback and self.compromise:
            d = self.assessment(self.fallback.plan_id).utilities
            g = gains(self.assessment(self.compromise.plan_id), d)
            out["compromise_gains"] = {r: float(g[r]) for r in ROLES}
            out["compromise_nash_product"] = float(g["atc"] * g["pilot"] * g["ground"])
        return out


def solve_scenario(scenario: Scenario, *, epsilon: float = 1e-9) -> EngineOutcome:
    plans = generate_candidates(scenario)
    evaluation = evaluate_candidates(scenario, plans)
    fallback = choose_fallback(evaluation.assessments)
    compromise = None
    if fallback is None:
        reason: EngineReason = "no_feasible_plan"
    else:
        compromise = choose_compromise(evaluation.assessments, fallback.plan_id, epsilon=epsilon)
        reason = "compromise_found" if compromise else "no_positive_bargain"
    best = {}
    for role in ROLES:
        top = role_best(evaluation.assessments, role)
        if top is not None:
            best[role] = top.plan.plan_id
    return EngineOutcome(
        scenario_id=scenario.scenario_id,
        state_hash=scenario.state_hash,
        candidate_count=len(plans),
        assessments=evaluation.assessments,
        rejected=evaluation.rejected,
        fallback=fallback,
        compromise=compromise,
        reason=reason,
        role_best_ids=best,
    )
