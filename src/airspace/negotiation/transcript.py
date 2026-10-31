"""Narrative transcript for the UI — derived from, and kept separate from, the RunResult.

The deterministic outcome lives in RunResult.status/selected_plan_id; this view
only renders what each agent said, labelled with the run mode.
"""
from __future__ import annotations

from typing import Any

from airspace.contracts import RunResult

MODE_LABEL = {"live": "LIVE", "mock": "MOCK SIMULATION (not an LLM)", "replay": "REPLAY (not live)"}


def build_transcript(result: RunResult) -> list[dict[str, Any]]:
    rows = []
    for e in result.events:
        if e.event_type not in ("agent_response_valid", "agent_response_invalid", "agent_timeout"):
            continue
        m = e.payload.get("message") or {}
        rows.append({
            "sequence": e.sequence, "round": e.round, "role": e.payload.get("role"),
            "mode": MODE_LABEL[result.provider_mode],
            "valid": e.event_type == "agent_response_valid",
            "event": e.event_type, "action": m.get("action"), "plan_id": m.get("plan_id"),
            "ranked_plan_ids": m.get("ranked_plan_ids", []),
            "objections": m.get("objections", []),
            "rationale": m.get("rationale"),
            "problems": e.payload.get("problems") or ([e.payload["error_kind"]] if e.payload.get("error_kind") else []),
        })
    return rows
