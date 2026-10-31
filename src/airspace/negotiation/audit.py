"""Ordered, redacted audit log; human decisions kept separate from agent agreement."""
from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from typing import Any, Callable

from airspace.contracts import LogEvent, RunResult

RAW_TEXT_LIMIT = 4000
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(api[_\-]?key|x-api-key|authorization)(\"?\s*[:=]\s*\"?)[^\s\",}]+"),
]
_SECRET_ENV = ("ANTHROPIC_API_KEY", "OPENAI_API_KEY")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact(value: Any) -> Any:
    if isinstance(value, str):
        out = value
        for env in _SECRET_ENV:
            secret = os.environ.get(env)
            if secret and len(secret) >= 8:
                out = out.replace(secret, "[REDACTED]")
        for pat in _SECRET_PATTERNS:
            out = pat.sub(lambda m: (m.group(1) + m.group(2) + "[REDACTED]") if m.lastindex and m.lastindex >= 2 else "[REDACTED]", out)
        return out
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if isinstance(k, str) and k.lower() in {"api_key", "x-api-key", "authorization"}
                    else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def clip_raw(raw: str | None) -> str | None:
    if raw is None:
        return None
    raw = redact(raw)
    return raw if len(raw) <= RAW_TEXT_LIMIT else raw[:RAW_TEXT_LIMIT] + "...[truncated]"


class AuditLog:
    def __init__(self, scenario_id: str, state_hash: str, clock: Callable[[], str] = utc_now) -> None:
        self.scenario_id = scenario_id
        self.state_hash = state_hash
        self._clock = clock
        self._events: list[LogEvent] = []
        self._closed = False

    def add(self, round_: int, actor: str, event_type: str, payload: dict[str, Any]) -> LogEvent:
        if self._closed:
            raise RuntimeError("audit log is closed; late events are rejected")
        ev = LogEvent(sequence=len(self._events) + 1, timestamp_utc=self._clock(),
                      scenario_id=self.scenario_id, state_hash=self.state_hash, round=round_,
                      actor=actor, event_type=event_type, payload=redact(payload))
        self._events.append(ev)
        return ev

    def close(self) -> list[LogEvent]:
        self._closed = True
        return list(self._events)

    @property
    def events(self) -> list[LogEvent]:
        return list(self._events)


# ---------------------------------------------------------------- consumers
def agent_votes(result: RunResult) -> dict[str, dict[str, Any]]:
    """Agent agreement view (round 2), separate from any human decision."""
    votes: dict[str, dict[str, Any]] = {}
    for e in result.events:
        if e.round == 2 and e.event_type in ("agent_response_valid", "agent_response_invalid", "agent_timeout"):
            p = e.payload
            votes[p["role"]] = {"valid": e.event_type == "agent_response_valid",
                                "action": (p.get("message") or {}).get("action"),
                                "plan_id": (p.get("message") or {}).get("plan_id"),
                                "event": e.event_type}
    return votes


def record_human_decision(result: RunResult, *, approver_id: str, decision: str,
                          note: str = "", clock: Callable[[], str] = utc_now) -> RunResult:
    """Return a NEW RunResult with a human decision event appended (actor 'human:<id>').

    Human approval is distinct from agent agreement. No real-world clearance or
    dispatch action is implemented; this only records the decision.
    """
    if decision not in {"approved", "rejected", "escalated"}:
        raise ValueError("decision must be approved, rejected or escalated")
    ev = LogEvent(sequence=len(result.events) + 1, timestamp_utc=clock(),
                  scenario_id=result.scenario_id, state_hash=result.state_hash, round=0,
                  actor=f"human:{redact(approver_id)}", event_type="human_decision",
                  payload={"decision": decision, "note": redact(note)[:500],
                           "recommendation_status": result.status,
                           "recommended_plan_id": result.selected_plan_id,
                           "execution": "none - recommendation only; no clearance/dispatch implemented"})
    return result.model_copy(update={"events": [*result.events, ev]})
