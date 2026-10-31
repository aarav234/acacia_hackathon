"""Replay mode: re-run validation on raw replies saved in a sanitized trace.

A replay never calls an LLM and is never described as live. It identifies the
original run mode, model, prompt version and configuration hash.

Trace format ``airspace-negotiation-trace-v1``::

    {"trace_format": "airspace-negotiation-trace-v1", "sanitized": true,
     "original_run_mode": "live", "provider": "anthropic", "model": "...",
     "prompt_version": "...", "config_hash": "...", "scenario_id": "...",
     "state_hash": "...", "captured_utc": "...",
     "responses": [{"role": "atc", "round": 1, "raw_text": "{...}"}, ...]}
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from airspace.agents.base import AgentReplyError, CallRecord, parse_agent_message
from airspace.contracts import AgentMessage, ProviderMode, Role, RunResult

TRACE_FORMAT = "airspace-negotiation-trace-v1"


def load_trace(path: Path | str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("trace_format") != TRACE_FORMAT:
        raise ValueError(f"unsupported trace format {data.get('trace_format')!r}")
    if data.get("sanitized") is not True:
        raise ValueError("refusing to replay a trace not marked sanitized")
    for k in ("original_run_mode", "model", "prompt_version", "config_hash", "responses"):
        if k not in data:
            raise ValueError(f"trace missing {k}")
    return data


def export_trace(result: RunResult) -> dict[str, Any]:
    """Build a sanitized trace from a RunResult's audit events (raw replies are pre-redacted)."""
    start = next(e for e in result.events if e.event_type == "run_started")
    responses = [
        {"role": e.payload["role"], "round": e.round, "raw_text": e.payload.get("raw_text")}
        for e in result.events
        if e.event_type in ("agent_response_valid", "agent_response_invalid")
        and e.payload.get("raw_text") is not None
    ]
    models = start.payload.get("agents", {})
    first = next(iter(models.values()), {}) if models else {}
    return {
        "trace_format": TRACE_FORMAT,
        "sanitized": True,
        "original_run_mode": result.provider_mode,
        "provider": first.get("provider"),
        "model": first.get("model"),
        "prompt_version": start.payload.get("prompt_version"),
        "config_hash": start.payload.get("config_hash"),
        "scenario_id": result.scenario_id,
        "state_hash": result.state_hash,
        "captured_utc": start.timestamp_utc,
        "responses": responses,
    }


class ReplayAgent:
    provider_mode: ProviderMode = "replay"

    def __init__(self, role: Role, trace: dict[str, Any]) -> None:
        self.role = role
        self._trace = trace
        self._by_round = {int(r["round"]): r["raw_text"] for r in trace["responses"] if r["role"] == role}
        self._last: CallRecord | None = None

    @property
    def last_call(self) -> CallRecord | None:
        return self._last

    def describe(self) -> dict[str, object]:
        t = self._trace
        return {"role": self.role, "mode": "replay", "provider": "replay",
                "model": f"replay-of:{t.get('model')}", "original_run_mode": t["original_run_mode"],
                "original_model": t.get("model"), "prompt_version": t.get("prompt_version"),
                "original_config_hash": t.get("config_hash"),
                "label": f"[REPLAY of a saved {t['original_run_mode']} trace - not live]"}

    async def respond(self, context: dict[str, object]) -> AgentMessage:
        rnd = int(context["round"])  # type: ignore[arg-type]
        self._last = CallRecord(role=self.role, round=rnd, raw_text=None, provider="replay",
                                model=str(self._trace.get("model")))
        if (context.get("scenario_id"), context.get("state_hash")) != (
                self._trace.get("scenario_id"), self._trace.get("state_hash")):
            raise AgentReplyError("replay_state_mismatch",
                                  "trace was captured for a different scenario/state", record=self._last)
        raw = self._by_round.get(rnd)
        if raw is None:
            raise AgentReplyError("replay_missing", f"no saved reply for {self.role} round {rnd}",
                                  record=self._last)
        self._last = CallRecord(role=self.role, round=rnd, raw_text=raw, provider="replay",
                                model=str(self._trace.get("model")), latency_ms=0.0)
        return parse_agent_message(raw, record=self._last)
