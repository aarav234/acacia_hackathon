"""UI view helpers: escaping of untrusted model text, honest empty/blocked states."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # repo root, for the ui package

from airspace.agents import build_agents  # noqa: E402
from airspace.agents.mock import MockAgent  # noqa: E402
from airspace.negotiation import negotiate, record_human_decision  # noqa: E402
from fixtures_negotiation import conflict_scenario  # noqa: E402
from ui import render as R  # noqa: E402


def _run(name="success", **behaviors):
    s = conflict_scenario(name)
    return s, asyncio.run(negotiate(s, build_agents("mock", mock_behaviors=behaviors)))


class InjectingAgent(MockAgent):
    """Valid message whose rationale tries to inject markup."""

    async def respond(self, context):
        msg = await super().respond(context)
        return msg.model_copy(update={"rationale": '<script>alert(1)</script><img src=x onerror=1>'})


def test_untrusted_rationale_is_escaped():
    s = conflict_scenario()
    agents = build_agents("mock")
    agents["pilot"] = InjectingAgent("pilot")
    r = asyncio.run(negotiate(s, agents))
    html = R.statements(R.build_view(r, s)) + R.board(R.build_view(r, s))
    assert "<script>" not in html and "<img" not in html
    assert "&lt;script&gt;" in html


def test_consensus_view_figures_come_from_engine_events():
    s, r = _run()
    v = R.build_view(r, s)
    assert r.status == "consensus" and v.compromise_id == r.selected_plan_id
    for role in ("atc", "pilot", "ground"):
        assert v.gain(role, r.selected_plan_id) > 0
    out = R.outcome(v)
    assert "Consensus" in out and r.selected_plan_id in out
    assert "Mock simulation, not a model" in out


def test_no_feasible_shows_no_fake_board_and_no_comparison():
    s, r = _run("no_feasible")
    v = R.build_view(r, s)
    assert r.status == "human_review"
    assert "No agent was called" in R.board(v)
    assert "n/a" not in R.board(v)
    assert R.comparison(v) == ""
    assert "No actionable plan" in R.outcome(v)


def test_rejection_names_the_role():
    s, r = _run(ground={1: "success", 2: "reject"})
    assert "Ground ops rejected the compromise" in R.reason_sentence(R.build_view(r, s))


def test_human_decision_rendered_separately():
    s, r = _run()
    r2 = record_human_decision(r, approver_id="dm<b>1</b>", decision="approved")
    line = R.human_line(R.build_view(r2, s))
    assert "Approved by dm&lt;b&gt;1&lt;/b&gt;" in line and "Nothing was executed" in line


def test_masthead_is_honest_about_source():
    assert "Every operating value here is simulated" in R.masthead(conflict_scenario("success"))
    assert "Synthetic scenario" in R.masthead(conflict_scenario("no_feasible"))
