"""Provider-independent Agent protocol and agent error types.

An *agent* is one role (atc, pilot or ground) with its own system prompt and
its own history. A *provider* is the model service behind it; one provider may
power three agents, but every agent call is a separate request carrying only
that role's context. One call never plays more than one role.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from airspace.contracts import AgentMessage, ProviderMode, Role


@dataclass(frozen=True)
class CallRecord:
    """Audit metadata for the most recent call an agent made (no secrets, no CoT)."""

    role: Role
    round: int
    raw_text: str | None
    stop_reason: str | None = None
    model: str | None = None
    provider: str | None = None
    request_id: str | None = None
    latency_ms: float | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class AgentReplyError(Exception):
    """The agent produced no usable AgentMessage. Never repaired, never retried.

    kind is one of: malformed_json, schema_invalid, refusal, truncated,
    provider_error, provider_unavailable, replay_missing, replay_state_mismatch.
    """

    def __init__(self, kind: str, detail: str, *, raw_text: str | None = None,
                 record: CallRecord | None = None) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail
        self.raw_text = raw_text
        self.record = record


class LiveProviderUnavailable(RuntimeError):
    """Live mode was requested but no key / SDK / network is available.

    Raised explicitly; the system never silently substitutes a mock.
    """


@runtime_checkable
class Agent(Protocol):
    role: Role
    provider_mode: ProviderMode

    async def respond(self, context: dict[str, object]) -> AgentMessage:
        """Return one AgentMessage for this role's context (one request at most)."""
        ...

    def describe(self) -> dict[str, object]:
        """Model/provider/prompt metadata for the audit log (no secrets)."""
        ...

    @property
    def last_call(self) -> CallRecord | None:
        ...


def parse_agent_message(raw_text: str, *, record: CallRecord | None = None) -> AgentMessage:
    """Strict, validated JSON parsing at the boundary. No auto-fix of JSON."""
    import json

    from pydantic import ValidationError

    try:
        json.loads(raw_text)
    except (TypeError, ValueError) as exc:
        raise AgentReplyError("malformed_json", str(exc)[:200], raw_text=raw_text, record=record) from None
    try:
        return AgentMessage.model_validate_json(raw_text)
    except ValidationError as exc:
        raise AgentReplyError("schema_invalid", _short_errors(exc), raw_text=raw_text, record=record) from None


def _short_errors(exc: Exception) -> str:
    try:
        errs = exc.errors()  # type: ignore[attr-defined]
        return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in errs[:5])
    except Exception:  # pragma: no cover
        return str(exc)[:300]
