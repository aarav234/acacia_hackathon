"""LLM-backed role agent: own system prompt, own history, one request per respond()."""
from __future__ import annotations

from airspace.agents.base import AgentReplyError, CallRecord, parse_agent_message
from airspace.agents.prompts import PROMPT_VERSION, render_user_message, system_prompt
from airspace.agents.provider import AGENT_MESSAGE_JSON_SCHEMA, Provider, ProviderCallError
from airspace.contracts import AgentMessage, ProviderMode, Role


class LLMAgent:
    provider_mode: ProviderMode = "live"

    def __init__(self, role: Role, provider: Provider, *, max_tokens: int = 700) -> None:
        self.role = role
        self._provider = provider
        self._system = system_prompt(role)
        self._history: list[dict[str, str]] = []  # independent per agent
        self._max_tokens = max_tokens
        self._last: CallRecord | None = None

    @property
    def last_call(self) -> CallRecord | None:
        return self._last

    @property
    def history(self) -> list[dict[str, str]]:
        return list(self._history)

    def describe(self) -> dict[str, object]:
        return {"role": self.role, "mode": self.provider_mode, "provider": self._provider.name,
                "model": self._provider.model, "prompt_version": PROMPT_VERSION,
                "max_tokens": self._max_tokens}

    async def respond(self, context: dict[str, object]) -> AgentMessage:
        if context.get("role") != self.role:
            raise ValueError(f"context for {context.get('role')!r} sent to {self.role!r} agent")
        rnd = int(context["round"])  # type: ignore[arg-type]
        user = {"role": "user", "content": render_user_message(context)}
        messages = self._history + [user]
        self._last = CallRecord(role=self.role, round=rnd, raw_text=None,
                                provider=self._provider.name, model=self._provider.model)
        try:
            reply = await self._provider.complete(system=self._system, messages=messages,
                                                  schema=AGENT_MESSAGE_JSON_SCHEMA,
                                                  max_tokens=self._max_tokens)
        except ProviderCallError as exc:
            raise AgentReplyError("provider_error", exc.detail, record=self._last) from None
        rec = CallRecord(role=self.role, round=rnd, raw_text=reply.text,
                         stop_reason=reply.stop_reason, model=reply.model,
                         provider=self._provider.name, request_id=reply.request_id,
                         latency_ms=reply.latency_ms, input_tokens=reply.input_tokens,
                         output_tokens=reply.output_tokens)
        self._last = rec
        self._history.extend([user, {"role": "assistant", "content": reply.text}])
        if reply.stop_reason == "refusal":
            raise AgentReplyError("refusal", "provider stop_reason=refusal", raw_text=reply.text, record=rec)
        if reply.stop_reason == "max_tokens":
            raise AgentReplyError("truncated", "reply hit max_tokens", raw_text=reply.text, record=rec)
        return parse_agent_message(reply.text, record=rec)
