"""Live provider adapter: Anthropic Messages API with JSON structured outputs.

Follows the current official structured-outputs docs: ``output_config={"format":
{"type": "json_schema", "schema": ...}}`` on ``messages.create`` (no beta header).
Schema limits respected: ``additionalProperties: false`` everywhere, no
maxItems/maxLength/minimum (those are enforced by our own validator instead).

Environment (coordinate names with Member 3's .env.example):
  ANTHROPIC_API_KEY        required for live mode (never logged)
  AIRSPACE_LLM_MODEL       default "claude-sonnet-5-5"
  AIRSPACE_LLM_MAX_TOKENS  default 700 (reply-token limit per call)
  AIRSPACE_LLM_TIMEOUT_S   default 15 (SDK timeout; coordinator deadline also applies)
  ANTHROPIC_BASE_URL       optional, honoured by the SDK

SDK automatic retries are disabled (``max_retries=0``). ``request_count`` counts
every SDK request attempted, including failed ones.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Protocol

from airspace.agents.base import LiveProviderUnavailable

DEFAULT_MODEL = "claude-sonnet-5-5"
DEFAULT_MAX_TOKENS = 700
DEFAULT_TIMEOUT_S = 15.0

_OBJECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "code": {"type": "string", "enum": ["hard_constraint", "utility_below_fallback",
                                            "missing_evidence", "preference", "protocol_error"]},
        "plan_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "rule_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
        "reason": {"type": "string"},
    },
    "required": ["code", "plan_id", "rule_id", "evidence_ids", "reason"],
    "additionalProperties": False,
}

AGENT_MESSAGE_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scenario_id": {"type": "string"},
        "state_hash": {"type": "string"},
        "role": {"type": "string", "enum": ["atc", "pilot", "ground"]},
        "round": {"type": "integer", "enum": [1, 2]},
        "action": {"type": "string", "enum": ["propose", "accept", "reject"]},
        "plan_id": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "ranked_plan_ids": {"type": "array", "items": {"type": "string"},
                            "description": "At most 3 plan IDs from the context."},
        "objections": {"type": "array", "items": _OBJECTION_SCHEMA},
        "rationale": {"type": "string", "description": "At most 120 words."},
        "evidence_ids": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["scenario_id", "state_hash", "role", "round", "action", "plan_id",
                 "ranked_plan_ids", "objections", "rationale", "evidence_ids"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class ProviderReply:
    text: str
    stop_reason: str | None
    model: str
    request_id: str | None
    latency_ms: float
    input_tokens: int | None
    output_tokens: int | None


class ProviderCallError(Exception):
    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(f"{kind}: {detail}")
        self.kind = kind
        self.detail = detail


class Provider(Protocol):
    name: str
    model: str
    request_count: int

    async def complete(self, *, system: str, messages: list[dict[str, str]],
                       schema: dict[str, Any], max_tokens: int) -> ProviderReply: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 max_tokens: int | None = None, timeout_s: float | None = None,
                 client: Any | None = None) -> None:
        self.model = model or os.environ.get("AIRSPACE_LLM_MODEL", DEFAULT_MODEL)
        self.max_tokens = int(max_tokens or os.environ.get("AIRSPACE_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS))
        self.timeout_s = float(timeout_s or os.environ.get("AIRSPACE_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        self.request_count = 0
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise LiveProviderUnavailable(
                "live mode unavailable: ANTHROPIC_API_KEY is not set. "
                "Use provider_mode='mock' or 'replay' explicitly; no automatic fallback.")
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on env
            raise LiveProviderUnavailable(
                "live mode unavailable: the 'anthropic' package is not installed") from exc
        self._client = anthropic.AsyncAnthropic(api_key=key, max_retries=0, timeout=self.timeout_s)

    @property
    def sdk_max_retries(self) -> int | None:
        return getattr(self._client, "max_retries", None)

    async def complete(self, *, system: str, messages: list[dict[str, str]],
                       schema: dict[str, Any], max_tokens: int) -> ProviderReply:
        self.request_count += 1  # count before sending: failed requests count too
        t0 = time.perf_counter()
        try:
            resp = await self._client.messages.create(
                model=self.model,
                max_tokens=min(max_tokens, self.max_tokens),
                system=system,
                messages=messages,
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except Exception as exc:  # SDK errors -> explicit provider error, never mock
            status = getattr(exc, "status_code", None)
            raise ProviderCallError(type(exc).__name__, f"status={status}") from None
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        usage = getattr(resp, "usage", None)
        return ProviderReply(
            text=text,
            stop_reason=getattr(resp, "stop_reason", None),
            model=getattr(resp, "model", self.model),
            request_id=getattr(resp, "id", None),
            latency_ms=(time.perf_counter() - t0) * 1000,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
        )
