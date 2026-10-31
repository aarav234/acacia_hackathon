"""Shared helpers for Member 2 tests.

``conflict_scenario`` is the ONE place that obtains the scenario fixture. It
currently uses the bootstrap stub's labelled simulation fixture; on rebase,
point it at Member 1's canonical fixture loader.
"""
from __future__ import annotations

import asyncio
from typing import Any

from airspace.agents import build_agents
from airspace.contracts import Plan, ValidationReport, Violation
from airspace.negotiation import EngineAPI, negotiate


def conflict_scenario():
    from airspace.engine._stub_fixture import build_conflict_scenario  # TODO(M1): canonical fixture
    return build_conflict_scenario()


def run_mock(behaviors: dict[str, Any] | None = None, **kw):
    agents = build_agents("mock", mock_behaviors=behaviors or {})
    return asyncio.run(negotiate(conflict_scenario(), agents, **kw))


def engine_with(**overrides) -> EngineAPI:
    """Real engine with selected functions replaced (forcing edge cases)."""
    base = EngineAPI.default()
    for k, v in overrides.items():
        setattr(base, k, v)
    base.name = "airspace.engine+test-overrides"
    return base


def infeasible(plan: Plan, rule: str = "R-TEST") -> ValidationReport:
    return ValidationReport(plan_id=plan.plan_id, feasible=False, metrics={},
                            violations=[Violation(rule_id=rule, reason="forced by test", observed="x",
                                                  required="y", units="n/a", evidence_ids=[])])
