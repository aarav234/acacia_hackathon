# Proposed contract changes (not applied unilaterally)

Proposals only. Each needs the owner's agreement before it is treated as contract.

## CC-1 (Member 2) — optional keyword-only arguments on `negotiate`
`negotiate(scenario, agents, *, max_rounds=2, per_call_timeout_s=15.0,
engine: EngineAPI | None = None, state_probe: Callable[[], str] | None = None,
epsilon: float = 1e-9, clock=utc_now) -> RunResult`

Additive. The defaults keep the published signature's behaviour. `engine` lets tests force
edge cases (no feasible plan, failing final validation) without a shadow solver; it defaults
to the real `airspace.engine` modules. `state_probe` gives the UI a way to report the current
authoritative state hash, so a changed state aborts the run (`stale_state`).
**Ask:** Member 3 to confirm. Member 1 to confirm `EngineAPI.default()` imports.

## CC-2 (Member 1) — metric naming convention for role scoping
Role contexts include `ValidationReport.metrics` keys by prefix: `atc_*`, `pilot_*`,
`ground_*`. Evidence goes to a role by field-path keywords (runway/separation/taxi,
fuel/connection/turbulence, gate/crew/service). **Ask:** Member 1 to adopt the prefixes,
or publish an explicit `METRIC_DOMAIN: dict[str, Role]` that Member 2 will import instead.

## CC-3 (Member 1) — optional `compute_state_hash` export
If `airspace.contracts` exports `compute_state_hash(scenario_dict) -> str`, the coordinator
recomputes it in step 1 and returns `human_review / scenario_hash_mismatch` on a mismatch.
If it is absent, the coordinator skips the recompute. **Ask:** export it, or name the
equivalent function.

## CC-4 (Member 1) — rule IDs and their evidence
Agents may only cite rule IDs that appear in engine `Violation`s for the scenario (taken from
rejection reports). A published `RULE_IDS` constant would let roles cite rules that no
candidate happened to violate.

## CC-5 (Member 3) — dependency and env names
Optional extra `live = ["anthropic>=0.75"]`. Env names: `ANTHROPIC_API_KEY`,
`AIRSPACE_LLM_MODEL`, `AIRSPACE_LLM_MAX_TOKENS`, `AIRSPACE_LLM_TIMEOUT_S`,
`AIRSPACE_REPLAY_TRACE`. No pytest-asyncio is needed; the tests use `asyncio.run`.
