"""Tests for airspace.negotiation (Member 2). Run: pytest tests/test_negotiation.py -q"""
from __future__ import annotations

import asyncio
import time

import pytest

from airspace.agents import build_agents
from airspace.agents.llm import LLMAgent
from airspace.agents.mock import MockAgent
from airspace.contracts import ROLES, AgentMessage
from airspace.negotiation import (MAX_AGENT_REQUESTS, ProtocolLimitError, agent_votes,
                                  build_transcript, negotiate, record_human_decision)
from airspace.negotiation.audit import redact
from airspace.negotiation.coordinator import _Budget

from fixtures_negotiation import conflict_scenario, engine_with, infeasible, run_mock
from test_agents import FakeProvider


def _types(r):
    return [e.event_type for e in r.events]


def _calls(agents):
    return sum(len(a.contexts) for a in agents.values())


# ------------------------------------------------------------------ happy path
def test_success_consensus_within_two_rounds_six_calls():
    agents = build_agents("mock")
    r = asyncio.run(negotiate(conflict_scenario(), agents))
    assert r.status == "consensus" and r.reason_code == "consensus"
    assert r.rounds_completed == 2 and r.agent_message_count == 6 <= MAX_AGENT_REQUESTS
    assert _calls(agents) == 6
    assert r.human_approval_required is True
    assert r.selected_plan_id != r.fallback_plan_id
    assert _types(r)[-1] == "human_approval_required"
    v = agent_votes(r)
    assert all(v[x]["action"] == "accept" and v[x]["plan_id"] == r.selected_plan_id for x in ROLES)


def test_round1_has_exactly_three_different_role_contexts():
    agents = build_agents("mock")
    asyncio.run(negotiate(conflict_scenario(), agents))
    r1 = [a.contexts[0] for a in agents.values()]
    assert [c["round"] for c in r1] == [1, 1, 1]
    assert {c["role"] for c in r1} == set(ROLES)
    assert len({str(c) for c in r1}) == 3  # genuinely different contexts


def test_round1_independent_choices_differ_on_conflict_fixture():
    r = run_mock()
    props = {t["role"]: t["plan_id"] for t in build_transcript(r) if t["round"] == 1}
    assert len(set(props.values())) >= 2  # modeled interests conflict
    assert all("MOCK" in t["mode"] for t in build_transcript(r))


def test_shared_offers_appear_in_round2_contexts():
    agents = build_agents("mock")
    r = asyncio.run(negotiate(conflict_scenario(), agents))
    for role, a in agents.items():
        c2 = a.contexts[1]
        others = {o["from_role"] for o in c2["other_offers"]}
        assert others == set(ROLES) - {role}
        assert c2["your_round1_offer"]["from_role"] == role
        assert c2["required_response"]["plan_id"] == r.selected_plan_id
        assert set(c2["compromise"]["verified_gains_all_roles"]) == set(ROLES)
        assert all(o["valid"] for o in c2["other_offers"])


# ------------------------------------------------------------------ invalid replies
@pytest.mark.parametrize("behavior,kind", [
    ("malformed", "malformed_json"), ("schema_invalid", "schema_invalid")])
def test_malformed_reply_cannot_become_acceptance(behavior, kind):
    r = run_mock({"pilot": {1: "success", 2: behavior}})
    assert r.status == "fallback" and r.reason_code == "round2_invalid_response"
    bad = [e for e in r.events if e.event_type == "agent_response_invalid"]
    assert len(bad) == 1 and bad[0].payload["error_kind"] == kind
    assert bad[0].payload["raw_text"]  # preserved in audit, not as a vote
    assert agent_votes(r)["pilot"]["valid"] is False


@pytest.mark.parametrize("behavior,problem", [
    ("accept_other_plan", "vote_not_for_exact_compromise"),
    ("wrong_hash", "state_hash_mismatch"),
    ("wrong_role", "role_mismatch"),
    ("wrong_round", "round_mismatch"),
    ("unknown_plan", "vote_not_for_exact_compromise"),
])
def test_round2_wrong_plan_hash_role_round_fail(behavior, problem):
    r = run_mock({"ground": {1: "success", 2: behavior}})
    assert r.status != "consensus"
    ev = next(e for e in r.events if e.event_type == "agent_response_invalid")
    assert problem in ev.payload["problems"]


def test_round1_unknown_plan_is_invalid_and_stops_before_round2():
    agents = build_agents("mock", mock_behaviors={"atc": "unknown_plan"})
    r = asyncio.run(negotiate(conflict_scenario(), agents))
    assert r.reason_code == "round1_invalid_response" and r.rounds_completed == 1
    assert _calls(agents) == 3 and r.status == "fallback"


# ------------------------------------------------------------------ timeouts / late replies
def test_timeout_produces_fallback_within_deadline():
    agents = build_agents("mock", mock_behaviors={"atc": {1: "success", 2: "timeout"}})
    t0 = time.perf_counter()
    r = asyncio.run(negotiate(conflict_scenario(), agents, per_call_timeout_s=0.2))
    assert time.perf_counter() - t0 < 2.0
    assert r.status == "fallback" and r.reason_code == "round2_timeout"
    assert r.selected_plan_id == r.fallback_plan_id
    assert agents["atc"].cancelled == 1


class StubbornLateAgent(MockAgent):
    """Ignores cancellation and later produces a (valid-looking) accept."""

    async def respond(self, context):
        if int(context["round"]) == 1:
            return await super().respond(context)
        self.contexts.append(context)
        try:
            await asyncio.sleep(0.3)
        except asyncio.CancelledError:
            await asyncio.sleep(0.3)  # keeps running after cancel
        self.finished_late = True
        return AgentMessage.model_validate({
            "scenario_id": context["scenario_id"], "state_hash": context["state_hash"], "role": self.role,
            "round": 2, "action": "accept", "plan_id": context["required_response"]["plan_id"],
            "ranked_plan_ids": [], "objections": [], "rationale": "late", "evidence_ids": []})


def test_old_responses_cannot_mutate_finished_result():
    async def scenario():
        agents = build_agents("mock")
        agents["pilot"] = StubbornLateAgent("pilot")
        r = await negotiate(conflict_scenario(), agents, per_call_timeout_s=0.1)
        snapshot = r.model_dump_json()
        await asyncio.sleep(0.8)  # let the late reply finish
        return r, snapshot, agents["pilot"]

    r, snap, late = asyncio.run(scenario())
    assert getattr(late, "finished_late", False) is True
    assert r.model_dump_json() == snap
    assert r.reason_code == "round2_timeout" and r.status == "fallback"


# ------------------------------------------------------------------ engine-driven branches
def test_no_positive_gain_skips_second_round():
    agents = build_agents("mock")
    eng = engine_with(choose_compromise=lambda a, fid, epsilon=1e-9: None)
    r = asyncio.run(negotiate(conflict_scenario(), agents, engine=eng))
    assert r.reason_code == "no_positive_bargain" and r.status == "fallback"
    assert r.rounds_completed == 1 and r.agent_message_count == 3 and _calls(agents) == 3
    assert "round_skipped" in _types(r)


def test_no_feasible_plan_calls_no_model():
    agents = build_agents("mock")
    eng = engine_with(assess_candidates=lambda s, plans: [])
    r = asyncio.run(negotiate(conflict_scenario(), agents, engine=eng))
    assert r.status == "human_review" and r.reason_code == "no_feasible_plan"
    assert r.selected_plan_id is None and r.agent_message_count == 0 and _calls(agents) == 0


def test_no_feasible_plan_with_real_engine_config():
    from airspace.engine._stub_fixture import build_conflict_scenario
    s = build_conflict_scenario(fuel={"snapshot_kg": 3000.0, "approach_burn_kg": 600.0,
                                      "airborne_burn_kg_per_min": 45.0, "taxi_burn_kg_per_min": 12.0,
                                      "min_reserve_kg": 3200.0})
    agents = build_agents("mock")
    r = asyncio.run(negotiate(s, agents))
    assert r.reason_code == "no_feasible_plan" and _calls(agents) == 0


def test_three_accepts_cannot_override_engine_violation():
    base = engine_with()
    real_validate = base.validate_plan
    state = {"comp": None}

    def choose(a, fid, epsilon=1e-9):
        from airspace.engine.bargaining import choose_compromise
        state["comp"] = choose_compromise(a, fid, epsilon=epsilon)
        return state["comp"]

    def validate(s, p):
        # becomes infeasible only at final validation (after round 2 began)
        if state.get("round2") and state["comp"] and p.plan_id == state["comp"].plan_id:
            return infeasible(p)
        return real_validate(s, p)

    eng = engine_with(choose_compromise=choose, validate_plan=validate)
    agents = build_agents("mock")
    orig = agents["ground"].respond

    async def mark(ctx):
        if ctx["round"] == 2:
            state["round2"] = True
        return await orig(ctx)

    agents["ground"].respond = mark
    r = asyncio.run(negotiate(conflict_scenario(), agents, engine=eng))
    assert all(v["action"] == "accept" for v in agent_votes(r).values())
    assert r.status == "fallback" and r.reason_code == "final_validation_failed"
    assert r.selected_plan_id == r.fallback_plan_id


def test_fallback_after_one_rejection():
    r = run_mock({"ground": {1: "success", 2: "reject"}})
    assert r.status == "fallback" and r.reason_code == "round2_rejected"
    assert r.selected_plan_id == r.fallback_plan_id is not None


def test_no_actionable_plan_when_fallback_invalid():
    base = engine_with()
    real = base.validate_plan
    seen = {"r2": False}

    def validate(s, p):
        if seen["r2"] and p.plan_id.startswith("P010"):
            return infeasible(p, "R-FUEL-RESERVE")
        return real(s, p)

    agents = build_agents("mock", mock_behaviors={"ground": {1: "success", 2: "reject"}})
    orig = agents["atc"].respond

    async def mark(ctx):
        if ctx["round"] == 2:
            seen["r2"] = True
        return await orig(ctx)

    agents["atc"].respond = mark
    r = asyncio.run(negotiate(conflict_scenario(), agents, engine=engine_with(validate_plan=validate)))
    assert r.status == "human_review" and r.reason_code == "fallback_invalid"
    assert r.selected_plan_id is None


def test_changed_state_hash_aborts_stale_run():
    s = conflict_scenario()
    agents = build_agents("mock")
    calls = {"n": 0}

    def probe():
        calls["n"] += 1
        return s.state_hash if calls["n"] == 1 else "changed"

    r = asyncio.run(negotiate(s, agents, state_probe=probe))
    assert r.status == "human_review" and r.reason_code == "stale_state"
    assert r.selected_plan_id is None and r.rounds_completed == 1 and _calls(agents) == 3
    assert "stale_state_detected" in _types(r)


# ------------------------------------------------------------------ claims
def test_false_safety_claim_is_refuted_but_reject_still_vetoes():
    r = run_mock({"pilot": {1: "success", 2: "false_safety_claim"}})
    checks = [e.payload for e in r.events if e.event_type == "claim_check" and e.round == 2]
    assert any(c["code"] == "hard_constraint" and c["verdict"] == "refuted" for c in checks)
    assert r.status == "fallback" and r.reason_code == "round2_rejected"


def test_unresolved_missing_evidence_blocks_consensus():
    r = run_mock({"atc": {1: "success", 2: "missing_evidence"}})
    assert r.status == "fallback"
    checks = [e.payload for e in r.events if e.event_type == "claim_check" and e.round == 2]
    assert any(c["verdict"] == "unresolved" for c in checks)


def test_preferences_are_logged_not_safety_rules():
    r = run_mock()
    checks = [e.payload for e in r.events if e.event_type == "claim_check"]
    assert checks and all(c["verdict"] == "preference_logged" for c in checks)
    assert r.status == "consensus"


# ------------------------------------------------------------------ provider failure / limits
def test_provider_failure_never_becomes_mock_success():
    prov = FakeProvider(fail=True)
    agents = {r: LLMAgent(r, prov) for r in ROLES}
    res = asyncio.run(negotiate(conflict_scenario(), agents))
    assert res.provider_mode == "live"
    assert res.status == "fallback" and res.reason_code == "round1_provider_error"
    assert prov.request_count == 3 == res.agent_message_count
    assert "mock" not in res.model_dump_json().lower()
    term = next(e for e in res.events if e.event_type == "run_terminated")
    assert term.payload["provider_sdk_requests"] == {"fake": 3}


def test_call_budget_and_round_cap_enforced_in_code():
    b = _Budget()
    for rnd in (1, 2):
        for role in ROLES:
            b.take(role, rnd)
    with pytest.raises(ProtocolLimitError):
        b.take("atc", 3)
    with pytest.raises(ProtocolLimitError):
        _Budget().take("atc", 3)
    b2 = _Budget()
    b2.take("atc", 1)
    with pytest.raises(ProtocolLimitError):
        b2.take("atc", 1)  # no hidden retry
    with pytest.raises(ValueError):
        asyncio.run(negotiate(conflict_scenario(), build_agents("mock"), max_rounds=3))


def test_agent_set_must_be_three_distinct_same_mode():
    one = MockAgent("atc")
    with pytest.raises(ValueError):
        asyncio.run(negotiate(conflict_scenario(), {"atc": one, "pilot": one, "ground": one}))
    mixed = build_agents("mock")
    mixed["pilot"] = LLMAgent("pilot", FakeProvider())
    with pytest.raises(ValueError):
        asyncio.run(negotiate(conflict_scenario(), mixed))


# ------------------------------------------------------------------ audit
def test_audit_sequence_utc_and_versions():
    r = run_mock()
    assert [e.sequence for e in r.events] == list(range(1, len(r.events) + 1))
    assert all(e.timestamp_utc.endswith("Z") for e in r.events)
    start = r.events[0].payload
    assert start["prompt_version"] and start["config_hash"] and start["limits"].startswith("2 negotiation rounds")
    assert "MOCK" in start["mode_label"]
    assert any(e.event_type == "feasible_catalogue" for e in r.events)
    assert any(e.event_type == "candidate_rejections" for e in r.events)


def test_secrets_redacted(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecretvalue123456")
    out = redact({"x": "key sk-ant-supersecretvalue123456 here", "api_key": "abc",
                  "nested": ["Authorization: Bearer abcdefghijklmnop"]})
    assert "supersecret" not in str(out) and "abcdefghijklmnop" not in str(out)
    assert out["api_key"] == "[REDACTED]"


def test_human_decision_separate_from_agent_agreement():
    r = run_mock()
    r2 = record_human_decision(r, approver_id="duty-manager-1", decision="approved")
    assert len(r2.events) == len(r.events) + 1 and len(r.events) == len(r.events)
    h = r2.events[-1]
    assert h.actor == "human:duty-manager-1" and h.event_type == "human_decision"
    assert agent_votes(r2) == agent_votes(r)
    assert "no clearance" in h.payload["execution"]
