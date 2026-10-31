"""Shared helpers for Member 2 tests.

Scenarios come from Member 1's canonical fixtures (``airspace.data.fixtures``):
``success`` (historical anchor + simulated ops, three different role preferences
and a distinct Nash compromise), ``no_feasible`` and ``no_positive_gain``.
"""
from __future__ import annotations

import asyncio
from typing import Any

from airspace.agents import build_agents
from airspace.contracts import Plan, ValidationReport, Violation
from airspace.negotiation import EngineAPI, negotiate


def conflict_scenario(name: str = "success"):
    from airspace.data.fixtures import load_fixture_scenario
    return load_fixture_scenario(name)


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


def infeasible(plan: Plan, rule: str = "FUEL-RESERVE") -> ValidationReport:
    return ValidationReport(plan_id=plan.plan_id, feasible=False, metrics={},
                            violations=[Violation(rule_id=rule, reason="forced by test", observed="x",
                                                  required="y", units="kg",
                                                  evidence_ids=["sim:operations.fuel"])])
