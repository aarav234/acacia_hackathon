"""Deterministic MockAgent — a visibly labelled SIMULATION, never an LLM.

Successful mock choices are derived only from the supplied role-scoped context
(own utility), so any round-1 disagreement comes from the modeled interests in
the labelled simulation fixture, not from a scripted transcript.

Behaviours (per round, via ``behaviors={1: ..., 2: ...}`` or one string for both):
  success, reject, malformed, schema_invalid, unknown_plan, wrong_role, wrong_hash,
  wrong_round, accept_other_plan, timeout, provider_error, missing_evidence,
  false_safety_claim
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

from airspace.agents.base import AgentReplyError, CallRecord, parse_agent_message
from airspace.agents.prompts import PROMPT_VERSION
from airspace.contracts import ProviderMode, Role

MOCK_LABEL = "[MOCK deterministic simulation - not an LLM]"
BEHAVIORS = {"success", "reject", "malformed", "schema_invalid", "unknown_plan", "wrong_role",
             "wrong_hash", "wrong_round", "accept_other_plan", "timeout", "provider_error",
             "missing_evidence", "false_safety_claim"}


def _rank(ctx: dict[str, Any]) -> list[dict[str, Any]]:
    return sorted(ctx["shortlist"], key=lambda p: (-p["your_utility"], p["landing_min"],
                                                    p["gate_in_min"], p["plan_id"]))


class MockAgent:
    provider_mode: ProviderMode = "mock"

    def __init__(self, role: Role, behaviors: str | dict[int, str] = "success", *,
                 timeout_sleep_s: float = 3600.0) -> None:
        if isinstance(behaviors, str):
            behaviors = {1: behaviors, 2: behaviors}
        for b in behaviors.values():
            if b not in BEHAVIORS:
                raise ValueError(f"unknown mock behaviour {b!r}")
        self.role = role
        self.behaviors = dict(behaviors)
        self.timeout_sleep_s = timeout_sleep_s
        self.contexts: list[dict[str, Any]] = []  # what this agent was shown (tests)
        self.cancelled = 0
        self._last: CallRecord | None = None

    @property
    def last_call(self) -> CallRecord | None:
        return self._last

    def describe(self) -> dict[str, object]:
        return {"role": self.role, "mode": "mock", "provider": "mock", "model": "mock-deterministic",
                "prompt_version": PROMPT_VERSION, "label": MOCK_LABEL, "behaviors": self.behaviors}

    async def respond(self, context: dict[str, object]) -> Any:
        ctx: dict[str, Any] = json.loads(json.dumps(context))  # private copy
        self.contexts.append(ctx)
        rnd = int(ctx["round"])
        b = self.behaviors.get(rnd, "success")
        self._last = CallRecord(role=self.role, round=rnd, raw_text=None, provider="mock",
                                model="mock-deterministic", extra={"behavior": b})
        if b == "timeout":
            try:
                await asyncio.sleep(self.timeout_sleep_s)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
        if b == "provider_error":
            raise AgentReplyError("provider_error", "mock provider failure", record=self._last)
        if b == "malformed":
            raw = '{"role": "' + self.role + '", "action": "accept", '  # truncated JSON
            return self._emit(raw, rnd, b)
        msg = self._message(ctx, rnd, b)
        if b == "schema_invalid":
            msg["round"] = "two"
        return self._emit(json.dumps(msg), rnd, b)

    def _emit(self, raw: str, rnd: int, b: str):
        self._last = CallRecord(role=self.role, round=rnd, raw_text=raw, provider="mock",
                                model="mock-deterministic", latency_ms=0.0, extra={"behavior": b})
        return parse_agent_message(raw, record=self._last)

    def _message(self, ctx: dict[str, Any], rnd: int, b: str) -> dict[str, Any]:
        ev = [e["evidence_id"] for e in ctx.get("evidence", [])][:2]
        base = {"scenario_id": ctx["scenario_id"], "state_hash": ctx["state_hash"],
                "role": self.role, "round": rnd, "ranked_plan_ids": [], "objections": [],
                "evidence_ids": ev}
        if rnd == 1:
            ranked = [p["plan_id"] for p in _rank(ctx)][:3]
            fb = ctx["fallback_plan_id"]
            base.update(action="propose", plan_id=ranked[0], ranked_plan_ids=ranked,
                        rationale=f"{MOCK_LABEL} Preferred plan has the highest supplied {self.role} utility.")
            if ranked[0] != fb:
                base["objections"] = [{"code": "preference", "plan_id": fb, "rule_id": None,
                                       "evidence_ids": ev, "reason": f"{MOCK_LABEL} fallback has lower supplied {self.role} utility"}]
            if b == "reject":
                base.update(action="reject", plan_id=None, ranked_plan_ids=[],
                            rationale=f"{MOCK_LABEL} declines to propose.")
        else:
            target = ctx["required_response"]["plan_id"]
            gain = ctx["compromise"]["your_gain_vs_fallback"]
            base.update(action="accept" if gain > 0 else "reject", plan_id=target,
                        rationale=f"{MOCK_LABEL} Compromise gain for {self.role} is positive per supplied metrics."
                        if gain > 0 else f"{MOCK_LABEL} Compromise does not beat fallback.")
            if b == "reject":
                base.update(action="reject", rationale=f"{MOCK_LABEL} rejects compromise (preference).",
                            objections=[{"code": "preference", "plan_id": target, "rule_id": None,
                                         "evidence_ids": ev, "reason": f"{MOCK_LABEL} prefers own round-1 plan"}])
        if b == "unknown_plan":
            base["plan_id"] = "P999-NOPE"
            base["ranked_plan_ids"] = ["P999-NOPE"]
        elif b == "wrong_role":
            base["role"] = next(r for r in ("atc", "pilot", "ground") if r != self.role)
        elif b == "wrong_hash":
            base["state_hash"] = "0" * 64
        elif b == "wrong_round":
            base["round"] = 1 if rnd == 2 else 2
        elif b == "accept_other_plan" and rnd == 2:
            other = ctx["fallback"]["plan_id"]
            base.update(action="accept", plan_id=other)
        elif b == "missing_evidence":
            tgt = base.get("plan_id") or ctx.get("fallback_plan_id")
            base.update(action="reject", objections=[{
                "code": "missing_evidence", "plan_id": tgt, "rule_id": None, "evidence_ids": [],
                "reason": f"{MOCK_LABEL} required evidence not supplied"}])
            if rnd == 1:
                base.update(plan_id=None, ranked_plan_ids=[])
        elif b == "false_safety_claim":
            tgt = ctx["required_response"]["plan_id"] if rnd == 2 else ctx["fallback_plan_id"]
            rule = (ctx.get("rule_ids") or [None])[0]
            base.update(action="reject", plan_id=tgt if rnd == 2 else None,
                        ranked_plan_ids=[] if rnd == 1 else base["ranked_plan_ids"],
                        objections=[{"code": "hard_constraint", "plan_id": tgt, "rule_id": rule,
                                     "evidence_ids": ev, "reason": f"{MOCK_LABEL} claims a hard violation"}])
        return base
