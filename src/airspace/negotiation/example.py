"""Minimal integration example for Member 3 (UI / evaluation).

    python -m airspace.negotiation.example --mode mock
    python -m airspace.negotiation.example --mode mock --save-trace runs/trace.json
    python -m airspace.negotiation.example --mode replay --trace runs/trace.json
    ANTHROPIC_API_KEY=... python -m airspace.negotiation.example --mode live

The scenario comes from ``_load_fixture`` — swap in Member 1's canonical fixture.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from airspace.agents import LiveProviderUnavailable, build_agents
from airspace.agents.replay import export_trace
from airspace.negotiation import agent_votes, build_transcript, negotiate


def _load_fixture():
    from airspace.engine._stub_fixture import build_conflict_scenario  # TODO(M1): canonical fixture
    return build_conflict_scenario()


async def main(mode: str, trace: str | None, save_trace: str | None, timeout: float) -> int:
    scenario = _load_fixture()
    try:
        agents = build_agents(mode, replay_path=trace)
    except LiveProviderUnavailable as exc:
        print(f"LIVE UNAVAILABLE: {exc}")  # explicit; we do not switch to mock
        return 2
    result = await negotiate(scenario, agents, per_call_timeout_s=timeout)

    print(f"mode={result.provider_mode}  limits=2 negotiation rounds / up to 6 agent responses")
    print(f"status={result.status} reason={result.reason_code} selected={result.selected_plan_id} "
          f"fallback={result.fallback_plan_id} rounds={result.rounds_completed} "
          f"requests={result.agent_message_count} elapsed_ms={result.elapsed_ms:.0f}")
    print("human approval required:", result.human_approval_required)
    for row in build_transcript(result):
        print(f"  R{row['round']} {row['role']:<6} {'ok ' if row['valid'] else 'BAD'} "
              f"{row['action']} {row['plan_id']} {row['problems'] or ''}")
    print("votes:", json.dumps(agent_votes(result)))
    if save_trace:
        Path(save_trace).parent.mkdir(parents=True, exist_ok=True)
        Path(save_trace).write_text(json.dumps(export_trace(result), indent=2))
        print("saved sanitized trace ->", save_trace)
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["live", "mock", "replay"], default="mock")
    ap.add_argument("--trace")
    ap.add_argument("--save-trace")
    ap.add_argument("--timeout", type=float, default=15.0)
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main(a.mode, a.trace, a.save_trace, a.timeout)))
