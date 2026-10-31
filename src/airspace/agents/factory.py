"""build_agents(provider_mode) -> {role: Agent} — three independent agents."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from airspace.agents.base import Agent
from airspace.agents.llm import LLMAgent
from airspace.agents.mock import MockAgent
from airspace.agents.provider import AnthropicProvider, Provider
from airspace.agents.replay import ReplayAgent, load_trace
from airspace.contracts import ROLES, Role


def build_agents(provider_mode: str, *, mock_behaviors: dict[Role, Any] | None = None,
                 replay_path: Path | str | None = None,
                 provider: Provider | None = None) -> dict[Role, Agent]:
    """Build one agent per role.

    live   -> one AnthropicProvider shared by three LLMAgents (separate prompts,
              histories and requests). Raises LiveProviderUnavailable if no key.
    mock   -> deterministic MockAgents (labelled simulation).
    replay -> ReplayAgents over a sanitized trace (path or AIRSPACE_REPLAY_TRACE).
    """
    if provider_mode == "live":
        prov = provider or AnthropicProvider()  # raises explicitly; never falls back to mock
        return {r: LLMAgent(r, prov, max_tokens=getattr(prov, "max_tokens", 700)) for r in ROLES}
    if provider_mode == "mock":
        mb = mock_behaviors or {}
        return {r: MockAgent(r, mb.get(r, "success")) for r in ROLES}
    if provider_mode == "replay":
        path = replay_path or os.environ.get("AIRSPACE_REPLAY_TRACE")
        if not path:
            raise ValueError("replay mode needs replay_path or AIRSPACE_REPLAY_TRACE")
        trace = load_trace(path)
        return {r: ReplayAgent(r, trace) for r in ROLES}
    raise ValueError(f"unknown provider_mode {provider_mode!r}; expected live, mock or replay")
