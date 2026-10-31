"""OpenAI adapter tests (Member 2). No network: a fake Responses client is injected."""
from __future__ import annotations

import asyncio
import json
import re
from types import SimpleNamespace as NS

import pytest

from airspace.agents import AgentReplyError, LiveProviderUnavailable, build_agents
from airspace.agents.llm import LLMAgent
from airspace.agents.provider import (AnthropicProvider, OpenAIProvider, build_live_provider)
from airspace.contracts import ROLES
from airspace.negotiation import negotiate

from fixtures_negotiation import conflict_scenario


def _resp(text=None, *, refusal=None, status="completed", reason=None):
    parts = []
    if text is not None:
        parts.append(NS(type="output_text", text=text))
    if refusal is not None:
        parts.append(NS(type="refusal", refusal=refusal))
    return NS(id="resp_1", model="gpt-5.4-mini", status=status,
              incomplete_details=NS(reason=reason) if reason else None,
              output=[NS(type="reasoning"), NS(type="message", content=parts)],
              usage=NS(input_tokens=100, output_tokens=50))


class FakeResponses:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    async def create(self, **kw):
        self.calls.append(kw)
        return self.handler(kw)


def _client(handler):
    r = FakeResponses(handler)
    return NS(responses=r), r


def _ctx_from(kw):
    user = kw["input"][-1]["content"]
    return json.loads(re.search(r"<DATA[^>]*>\n(.*)\n</DATA>", user, re.S).group(1))


def _honest_reply(kw):
    """Behaves like a compliant model: reads its own DATA block and answers in schema."""
    c = _ctx_from(kw)
    base = {"scenario_id": c["scenario_id"], "state_hash": c["state_hash"], "role": c["role"],
            "round": c["round"], "objections": [], "evidence_ids": [], "rationale": "fake model"}
    if c["round"] == 1:
        best = max(c["shortlist"], key=lambda p: (p["your_utility"], p["plan_id"]))
        base.update(action="propose", plan_id=best["plan_id"], ranked_plan_ids=[best["plan_id"]])
    else:
        base.update(action="accept", plan_id=c["required_response"]["plan_id"], ranked_plan_ids=[])
    return _resp(json.dumps(base))


def test_request_shape_strict_schema_store_false_default_model(monkeypatch):
    for k in ("AIRSPACE_LLM_MODEL", "AIRSPACE_LLM_MAX_TOKENS", "AIRSPACE_LLM_REASONING_EFFORT"):
        monkeypatch.delenv(k, raising=False)
    client, fake = _client(lambda kw: _resp('{"x": 1}'))
    prov = OpenAIProvider(client=client)
    with pytest.raises(AgentReplyError):  # {"x":1} is not an AgentMessage
        asyncio.run(LLMAgent("pilot", prov, max_tokens=prov.max_tokens).respond({"role": "pilot", "round": 1}))
    kw = fake.calls[0]
    assert kw["model"] == "gpt-5.4-mini"
    assert kw["store"] is False
    fmt = kw["text"]["format"]
    assert fmt["type"] == "json_schema" and fmt["strict"] is True and fmt["name"] == "agent_message"
    assert fmt["schema"]["additionalProperties"] is False
    assert kw["reasoning"] == {"effort": "low"}
    assert kw["max_output_tokens"] == 1500
    assert "PILOT" in kw["instructions"] and kw["input"][0]["role"] == "user"
    assert prov.request_count == 1


def test_reasoning_effort_can_be_omitted():
    client, fake = _client(lambda kw: _resp("{}"))
    prov = OpenAIProvider(client=client, reasoning_effort="")
    with pytest.raises(AgentReplyError):
        asyncio.run(LLMAgent("atc", prov).respond({"role": "atc", "round": 1}))
    assert "reasoning" not in fake.calls[0]


@pytest.mark.parametrize("resp,kind", [
    (_resp(refusal="I can't help with that"), "refusal"),
    (_resp('{"scenario_id": "S", ', status="incomplete", reason="max_output_tokens"), "truncated"),
    (_resp("", status="incomplete", reason="content_filter"), "refusal"),
    (_resp("not json"), "malformed_json"),
])
def test_refusal_truncation_malformed_never_votes(resp, kind):
    client, _ = _client(lambda kw: resp)
    with pytest.raises(AgentReplyError) as ei:
        asyncio.run(LLMAgent("ground", OpenAIProvider(client=client)).respond({"role": "ground", "round": 1}))
    assert ei.value.kind == kind


def test_sdk_exception_is_provider_error_and_counted():
    def boom(kw):
        raise RuntimeError("connection reset")
    client, _ = _client(boom)
    prov = OpenAIProvider(client=client)
    with pytest.raises(AgentReplyError) as ei:
        asyncio.run(LLMAgent("atc", prov).respond({"role": "atc", "round": 1}))
    assert ei.value.kind == "provider_error" and prov.request_count == 1


def test_real_sdk_client_has_retries_disabled():
    pytest.importorskip("openai")
    prov = OpenAIProvider(api_key="sk-test-not-a-real-key-0000000000")
    assert prov.sdk_max_retries == 0


def test_provider_selection_is_explicit(monkeypatch):
    for k in ("AIRSPACE_LLM_PROVIDER", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-a-real-key-0000000000")
    assert isinstance(build_live_provider(), OpenAIProvider)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real-000000")
    with pytest.raises(LiveProviderUnavailable):  # both keys, no explicit choice
        build_live_provider()
    monkeypatch.setenv("AIRSPACE_LLM_PROVIDER", "anthropic")
    assert isinstance(build_live_provider(), AnthropicProvider)
    monkeypatch.setenv("AIRSPACE_LLM_PROVIDER", "gemini")
    with pytest.raises(LiveProviderUnavailable):
        build_live_provider()


def test_full_live_path_with_fake_openai_client():
    """Six separate Responses calls, three distinct system prompts, consensus via the engine."""
    client, fake = _client(_honest_reply)
    prov = OpenAIProvider(client=client)
    agents = build_agents("live", provider=prov)
    r = asyncio.run(negotiate(conflict_scenario(), agents))
    assert r.provider_mode == "live" and r.status == "consensus"
    assert prov.request_count == 6 == len(fake.calls) == r.agent_message_count
    assert len({c["instructions"] for c in fake.calls}) == 3
    # round-2 requests carry only that role's own round-1 history
    r2 = [c for c in fake.calls if len(c["input"]) == 3]
    assert len(r2) == 3 and all(_ctx_from(c)["round"] == 2 for c in r2)
    term = next(e for e in r.events if e.event_type == "run_terminated")
    assert term.payload["provider_sdk_requests"] == {"openai": 6}
    started = r.events[0].payload["agents"]
    assert all(started[x]["provider"] == "openai" and started[x]["model"] == "gpt-5.4-mini" for x in ROLES)
