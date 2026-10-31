"""Deterministic message validation and objection (claim) checking.

Validation is semantic, on top of the strict schema parse in the agent layer:
identity, round, state hash, action semantics, referenced plan/evidence/rule
IDs, ranking limits and rationale length. Any problem makes the message an
invalid audit event — it can never become a vote.

Claim checks verify objections against the engine. Free-text preference is
logged but never treated as a safety rule; the LLM never adjudicates numbers.
"""
from __future__ import annotations

import re
from typing import Any, Callable

from airspace.agents.context import known_ids
from airspace.contracts import AgentMessage, Assessment, Plan, Role, ValidationReport

MAX_RANKED = 3
MAX_RATIONALE_WORDS = 120
ROUND_ACTIONS = {1: {"propose", "reject"}, 2: {"accept", "reject"}}
BLOCKING_CODES = {"hard_constraint", "missing_evidence"}


def validate_message(msg: AgentMessage, ctx: dict[str, Any], *, expected_role: Role,
                     expected_round: int) -> list[str]:
    problems: list[str] = []
    if msg.scenario_id != ctx["scenario_id"]:
        problems.append("scenario_id_mismatch")
    if msg.state_hash != ctx["state_hash"]:
        problems.append("state_hash_mismatch")
    if msg.role != expected_role:
        problems.append("role_mismatch")
    if msg.round != expected_round:
        problems.append("round_mismatch")
    if msg.action not in ROUND_ACTIONS.get(expected_round, set()):
        problems.append(f"action_not_allowed_in_round_{expected_round}")
    plans, evs, rules = known_ids(ctx)
    shortlist = {p["plan_id"] for p in ctx.get("shortlist", [])}

    ranked = list(msg.ranked_plan_ids)
    if len(ranked) > MAX_RANKED:
        problems.append("too_many_ranked_plan_ids")
    if len(set(ranked)) != len(ranked):
        problems.append("duplicate_ranked_plan_ids")
    if any(p not in plans for p in ranked):
        problems.append("unknown_ranked_plan_id")

    if expected_round == 1:
        if msg.action == "propose":
            if msg.plan_id is None or msg.plan_id not in shortlist:
                problems.append("proposed_plan_not_in_shortlist")
            if ranked and ranked[0] != msg.plan_id:
                problems.append("plan_id_must_be_first_ranked")
            if any(p not in shortlist for p in ranked):
                problems.append("ranked_plan_not_in_shortlist")
        elif msg.plan_id is not None and msg.plan_id not in plans:
            problems.append("unknown_plan_id")
    else:
        required = ctx["required_response"]["plan_id"]
        if msg.plan_id != required:
            problems.append("vote_not_for_exact_compromise")
        if msg.action == "accept" and any(
                o.code in BLOCKING_CODES and o.plan_id == required for o in msg.objections):
            problems.append("accept_contradicts_own_blocking_objection")

    if any(e not in evs for e in msg.evidence_ids):
        problems.append("unknown_evidence_id")
    for o in msg.objections:
        if o.plan_id is not None and o.plan_id not in plans:
            problems.append("objection_unknown_plan_id")
        if o.rule_id is not None and o.rule_id not in rules:
            problems.append("objection_unknown_rule_id")
        if any(e not in evs for e in o.evidence_ids):
            problems.append("objection_unknown_evidence_id")
    if len(msg.rationale.split()) > MAX_RATIONALE_WORDS:
        problems.append("rationale_too_long")
    return sorted(set(problems))


_NUM = re.compile(r"(?<![A-Za-z0-9_\-])-?\d+(?:\.\d+)?")


def unverified_numbers(msg: AgentMessage, ctx: dict[str, Any]) -> list[str]:
    """Numbers in free text that do not appear anywhere in the supplied context.

    Advisory audit flag only (an LLM might write '2 rounds'); never used as a vote
    or a safety input. Plan/rule/evidence IDs are stripped before matching.
    """
    plans, evs, rules = known_ids(ctx)
    text = " ".join([msg.rationale, *(o.reason for o in msg.objections)])
    for ident in sorted(plans | evs | rules, key=len, reverse=True):
        text = text.replace(ident, " ")
    supplied: set[float] = set()

    def walk(v: Any) -> None:
        if isinstance(v, bool):
            return
        if isinstance(v, (int, float)):
            supplied.add(round(float(v), 3))
        elif isinstance(v, str):
            for m in _NUM.findall(v):
                supplied.add(round(float(m), 3))
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)

    walk(ctx)
    out = []
    for m in _NUM.findall(text):
        f = float(m)
        if not any(abs(f - s) <= max(0.051, abs(s) * 0.005) for s in supplied):
            out.append(m)
    return out


def check_objections(role: Role, msg: AgentMessage, *, catalogue: dict[str, Plan],
                     assessments: dict[str, Assessment], fallback_utils: dict[str, float],
                     validate: Callable[[Plan], ValidationReport],
                     epsilon: float = 1e-9) -> list[dict[str, Any]]:
    """Verdicts: confirmed | refuted | unresolved | preference_logged | logged | not_applicable."""
    results = []
    for i, o in enumerate(msg.objections):
        verdict, detail = "logged", ""
        if o.code == "preference":
            verdict, detail = "preference_logged", "soft preference; not a safety rule"
        elif o.code == "hard_constraint":
            if o.plan_id is None or o.rule_id is None:
                verdict, detail = "unresolved", "safety claim without plan_id/rule_id"
            elif o.plan_id not in catalogue:
                verdict, detail = "unresolved", "plan not in catalogue"
            else:
                rep = validate(catalogue[o.plan_id])
                hit = [v for v in rep.violations if v.rule_id == o.rule_id]
                if hit:
                    verdict, detail = "confirmed", f"engine: {hit[0].observed} vs {hit[0].required} {hit[0].units}"
                else:
                    verdict, detail = "refuted", f"engine finds no {o.rule_id} violation for {o.plan_id}"
        elif o.code == "missing_evidence":
            verdict, detail = "unresolved", "role reports evidence missing; engine cannot supply it"
        elif o.code == "utility_below_fallback":
            a = assessments.get(o.plan_id or "")
            if a is None:
                verdict, detail = "not_applicable", "plan not in feasible catalogue"
            else:
                gain = a.utilities[role] - fallback_utils[role]
                verdict = "confirmed" if gain <= epsilon else "refuted"
                detail = f"engine gain for {role}: {gain:.4f}"
        results.append({"role": role, "round": msg.round, "objection_index": i, "code": o.code,
                        "plan_id": o.plan_id, "rule_id": o.rule_id, "verdict": verdict,
                        "detail": detail})
    return results


def blocked_plans(checks: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Plans with confirmed or unresolved safety/evidence claims (cannot reach consensus).

    A blocking claim with no plan_id is keyed '*' and blocks every plan, which
    escalates visibly to human review rather than being ignored.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for c in checks:
        if c["code"] in BLOCKING_CODES and c["verdict"] in {"confirmed", "unresolved"}:
            out.setdefault(c["plan_id"] or "*", []).append(c)
    return out


def is_blocked(plan_id: str, blocked: dict[str, list[dict[str, Any]]]) -> bool:
    return plan_id in blocked or "*" in blocked
