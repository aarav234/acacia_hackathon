"""Live provider adapters: OpenAI (Responses API) and Anthropic (Messages API),
both using strict JSON-schema structured outputs. Select with AIRSPACE_LLM_PROVIDER.

Anthropic adapter:

Follows the current official structured-outputs docs: ``output_config={"format":
{"type": "json_schema", "schema": ...}}`` on ``messages.create`` (no beta header).
Schema limits respected: ``additionalProperties: false`` everywhere, no
maxItems/maxLength/minimum (those are enforced by our own validator instead).

Environment (coordinate names with Member 3's .env.example):
  ANTHROPIC_API_KEY        required for live mode (never logged)
  AIRSPACE_LLM_MODEL       default "claude-sonnet-5-5" (OpenAI: "gpt-5.4-mini")
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


# --------------------------------------------------------------------------
# OpenAI (Responses API, Structured Outputs with a strict JSON schema)
# --------------------------------------------------------------------------
OPENAI_DEFAULT_MODEL = "gpt-5.4-mini"
OPENAI_DEFAULT_MAX_TOKENS = 1500  # reasoning tokens count toward max_output_tokens
OPENAI_DEFAULT_REASONING_EFFORT = "low"


class OpenAIProvider:
    """OpenAI Responses API adapter.

    Uses ``text.format = {"type": "json_schema", "strict": true, ...}``. Every
    property in the schema is required and ``additionalProperties`` is false, as
    strict mode demands. ``store=False`` keeps responses off OpenAI's stored
    history. SDK retries are disabled (``max_retries=0``) and ``request_count``
    counts every attempted request.

    Environment:
      OPENAI_API_KEY                 required for live mode (never logged)
      AIRSPACE_LLM_MODEL             default "gpt-5.4-mini"
      AIRSPACE_LLM_MAX_TOKENS        default 1500 (max_output_tokens; includes reasoning)
      AIRSPACE_LLM_TIMEOUT_S         default 15
      AIRSPACE_LLM_REASONING_EFFORT  default "low"; set empty to omit the parameter
      OPENAI_BASE_URL                optional, honoured by the SDK
    """

    name = "openai"

    def __init__(self, *, api_key: str | None = None, model: str | None = None,
                 max_tokens: int | None = None, timeout_s: float | None = None,
                 reasoning_effort: str | None = None, client: Any | None = None) -> None:
        self.model = model or os.environ.get("AIRSPACE_LLM_MODEL", OPENAI_DEFAULT_MODEL)
        self.max_tokens = int(max_tokens or os.environ.get("AIRSPACE_LLM_MAX_TOKENS", OPENAI_DEFAULT_MAX_TOKENS))
        self.timeout_s = float(timeout_s or os.environ.get("AIRSPACE_LLM_TIMEOUT_S", DEFAULT_TIMEOUT_S))
        effort = reasoning_effort if reasoning_effort is not None else os.environ.get(
            "AIRSPACE_LLM_REASONING_EFFORT", OPENAI_DEFAULT_REASONING_EFFORT)
        self.reasoning_effort = effort.strip() or None
        self.request_count = 0
        if client is not None:
            self._client = client
            return
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise LiveProviderUnavailable(
                "live mode unavailable: OPENAI_API_KEY is not set. "
                "Use provider_mode='mock' or 'replay' explicitly; no automatic fallback.")
        try:
            import openai
        except ImportError as exc:  # pragma: no cover - depends on env
            raise LiveProviderUnavailable(
                "live mode unavailable: the 'openai' package is not installed") from exc
        self._client = openai.AsyncOpenAI(api_key=key, max_retries=0, timeout=self.timeout_s)

    @property
    def sdk_max_retries(self) -> int | None:
        return getattr(self._client, "max_retries", None)

    async def complete(self, *, system: str, messages: list[dict[str, str]],
                       schema: dict[str, Any], max_tokens: int) -> ProviderReply:
        self.request_count += 1  # count before sending: failed requests count too
        kwargs: dict[str, Any] = {
            "model": self.model,
            "instructions": system,
            "input": [{"role": m["role"], "content": m["content"]} for m in messages],
            "max_output_tokens": min(max_tokens, self.max_tokens),
            "store": False,
            "text": {"format": {"type": "json_schema", "name": "agent_message",
                                "schema": schema, "strict": True}},
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        t0 = time.perf_counter()
        try:
            resp = await self._client.responses.create(**kwargs)
        except Exception as exc:  # SDK errors -> explicit provider error, never mock
            status = getattr(exc, "status_code", None)
            raise ProviderCallError(type(exc).__name__, f"status={status}") from None
        text, refused = "", False
        for item in getattr(resp, "output", None) or []:
            if getattr(item, "type", "") != "message":
                continue  # reasoning items are neither requested nor stored
            for part in getattr(item, "content", None) or []:
                ptype = getattr(part, "type", "")
                if ptype == "output_text":
                    text += getattr(part, "text", "")
                elif ptype == "refusal":
                    refused = True
                    text += getattr(part, "refusal", "")
        status = getattr(resp, "status", None)
        reason = getattr(getattr(resp, "incomplete_details", None), "reason", None)
        if refused or reason == "content_filter":
            stop = "refusal"
        elif status == "incomplete" and reason == "max_output_tokens":
            stop = "max_tokens"
        elif status == "completed":
            stop = "end_turn"
        else:
            stop = f"{status}:{reason}" if reason else str(status)
        usage = getattr(resp, "usage", None)
        return ProviderReply(
            text=text, stop_reason=stop, model=getattr(resp, "model", self.model),
            request_id=getattr(resp, "id", None), latency_ms=(time.perf_counter() - t0) * 1000,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None))


def build_live_provider() -> Any:
    """Choose the live provider explicitly; never guess between two keys, never mock.

    AIRSPACE_LLM_PROVIDER = "openai" | "anthropic". If unset, the provider whose
    key is present is used; with both keys or neither, the call raises.
    """
    choice = os.environ.get("AIRSPACE_LLM_PROVIDER", "").strip().lower()
    if not choice:
        has_oa, has_an = bool(os.environ.get("OPENAI_API_KEY")), bool(os.environ.get("ANTHROPIC_API_KEY"))
        if has_oa and has_an:
            raise LiveProviderUnavailable(
                "both OPENAI_API_KEY and ANTHROPIC_API_KEY are set; set AIRSPACE_LLM_PROVIDER")
        if not (has_oa or has_an):
            raise LiveProviderUnavailable(
                "live mode unavailable: set OPENAI_API_KEY (or ANTHROPIC_API_KEY) and "
                "AIRSPACE_LLM_PROVIDER. No automatic fallback to mock.")
        choice = "openai" if has_oa else "anthropic"
    if choice == "openai":
        return OpenAIProvider()
    if choice == "anthropic":
        return AnthropicProvider()
    raise LiveProviderUnavailable(f"unknown AIRSPACE_LLM_PROVIDER {choice!r}")
