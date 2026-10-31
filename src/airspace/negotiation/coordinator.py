"""Bounded two-round negotiation coordinator.

State machine (see docs/negotiation.md)::

  INIT -> VALIDATE_SCENARIO -> CATALOGUE -> FALLBACK
       -> [no feasible plan]            -> HUMAN_REVIEW (0 agent calls)
       -> ROUND_1 (3 concurrent calls)  -> any timeout/invalid -> FALLBACK_PATH
       -> BROADCAST + CLAIM_CHECK + NASH
       -> [no positive bargain]         -> FALLBACK_PATH (round 2 skipped)
       -> [unresolved safety claim]     -> FALLBACK_PATH
       -> ROUND_2 (3 concurrent calls)  -> any timeout/invalid/reject -> FALLBACK_PATH
       -> FINAL_VALIDATION              -> CONSENSUS | FALLBACK_PATH
  FALLBACK_PATH: stale? -> HUMAN_REVIEW; revalidate fallback -> FALLBACK | HUMAN_REVIEW
  Any stale state hash at a checkpoint -> HUMAN_REVIEW (stale_state); UI must start a new run.

Limits are enforced here in code: at most 2 rounds, one call per role per round,
at most 6 agent requests (failed ones included). No third round, no hidden retry,
no JSON repair, no summariser call. The deterministic engine computes all
margins, utilities, the fallback and the Nash compromise; agents only make and
review offers. Every result requires human approval; nothing is executed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from airspace.agents.base import AgentReplyError
from airspace.agents.context import (
    PROTOCOL_DISCLOSURE,
    build_round1_context,
    build_round2_context,
    offer_envelope,
    role_rule_ids,
)
from airspace.agents.prompts import PROMPT_VERSION, prompt_bundle_hash
from airspace.contracts import (
    ROLES,
    AgentMessage,
    Assessment,
    Plan,
    Role,
    RunResult,
    Scenario,
    ValidationReport,
)
from airspace.negotiation.audit import AuditLog, clip_raw, utc_now
from airspace.negotiation.shortlist import MAX_SHORTLIST, build_shortlist
from airspace.negotiation.validation import (
    blocked_plans,
    check_objections,
    is_blocked,
    unverified_numbers,
    validate_message,
)

MAX_ROUNDS = 2
MAX_AGENT_REQUESTS = 6
COORDINATOR_VERSION = "airspace-coordinator-v1.0"
NASH_DISCLOSURE = ("Discrete Nash-product selection inspired by Nash bargaining, computed by the "
                   "deterministic engine over the full feasible catalogue. Not discovered by an LLM; "
                   "no claim of strategic truthfulness or aviation safety certification.")


class ProtocolLimitError(RuntimeError):
    """A code path tried to exceed the round/call limits. Should never happen."""


@dataclass
class EngineAPI:
    """Member 1's engine functions, injectable for tests. Defaults to the real modules."""

    generate_candidates: Callable[[Scenario], list[Plan]]
    validate_plan: Callable[[Scenario, Plan], ValidationReport]
    assess_candidates: Callable[[Scenario, list[Plan]], list[Assessment]]
    choose_fallback: Callable[[list[Assessment]], Plan | None]
    choose_compromise: Callable[..., Plan | None]
    state_hash_fn: Callable[[Any], str] | None = None
    sealed_check: Callable[[Scenario], None] | None = None
    name: str = "airspace.engine"

    @classmethod
    def default(cls) -> EngineAPI:
        from airspace import contracts
        from airspace.engine import (
            bargaining,
            candidates,
            constraints,
            fallback,
            scoring,
        )

        return cls(generate_candidates=candidates.generate_candidates,
                   validate_plan=constraints.validate_plan,
                   assess_candidates=scoring.assess_candidates,
                   choose_fallback=fallback.choose_fallback,
                   choose_compromise=bargaining.choose_compromise,
                   state_hash_fn=contracts.compute_state_hash,
                   sealed_check=contracts.ensure_sealed)


class _Budget:
    def __init__(self) -> None:
        self.count = 0
        self._seen: set[tuple[str, int]] = set()

    def take(self, role: str, rnd: int) -> None:
        if rnd not in (1, 2) or rnd > MAX_ROUNDS:
            raise ProtocolLimitError(f"round {rnd} not permitted")
        if (role, rnd) in self._seen:
            raise ProtocolLimitError(f"{role} already called in round {rnd}")
        if self.count >= MAX_AGENT_REQUESTS:
            raise ProtocolLimitError("agent request budget (6) exhausted")
        self._seen.add((role, rnd))
        self.count += 1


@dataclass
class _Outcome:
    role: Role
    status: str  # valid | invalid | timeout
    message: AgentMessage | None = None
    problems: list[str] = field(default_factory=list)
    error_kind: str | None = None
    latency_ms: float | None = None


def _sha(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, default=str, separators=(",", ":")).encode()).hexdigest()


def _swallow(task: asyncio.Task) -> None:
    if not task.cancelled():
        task.exception()


class _Run:
    def __init__(self, scenario: Scenario, agents: dict[Role, Any], *, max_rounds: int,
                 per_call_timeout_s: float, engine: EngineAPI, state_probe: Callable[[], str] | None,
                 epsilon: float, clock: Callable[[], str]) -> None:
        self.s = scenario
        self.agents = agents
        self.max_rounds = max_rounds
        self.timeout = per_call_timeout_s
        self.engine = engine
        self.probe = state_probe
        self.eps = epsilon
        self.log = AuditLog(scenario.scenario_id, scenario.state_hash, clock)
        self.budget = _Budget()
        self.t0 = time.perf_counter()
        self.rounds_completed = 0
        self.mode = next(iter(agents.values())).provider_mode
        self.fallback: Assessment | None = None
        self.blocked: dict[str, list[dict[str, Any]]] = {}
        self.finished = False
        self._providers = self._find_providers()
        self._provider_start = {id(p): p.request_count for p in self._providers}

    # ------------------------------------------------------------- helpers
    def _find_providers(self) -> list[Any]:
        seen: dict[int, Any] = {}
        for a in self.agents.values():
            p = getattr(a, "_provider", None)
            if p is not None and hasattr(p, "request_count"):
                seen[id(p)] = p
        return list(seen.values())

    def _stale(self) -> bool:
        if self.probe is None:
            return False
        current = self.probe()
        if current != self.s.state_hash:
            self.log.add(self.rounds_completed, "coordinator", "stale_state_detected",
                         {"expected": self.s.state_hash, "current": current,
                          "action": "abort run; UI must request a new run"})
            return True
        return False

    def _finish(self, status: str, reason: str, selected: str | None, **extra: Any) -> RunResult:
        if self.finished:
            raise RuntimeError("run already finished")
        prov = {p.name if hasattr(p, "name") else str(id(p)):
                p.request_count - self._provider_start[id(p)] for p in self._providers}
        self.log.add(self.rounds_completed, "coordinator", "run_terminated", {
            "status": status, "reason_code": reason, "selected_plan_id": selected,
            "fallback_plan_id": self.fallback.plan.plan_id if self.fallback else None,
            "rounds_completed": self.rounds_completed, "agent_requests": self.budget.count,
            "provider_sdk_requests": prov, "limits": PROTOCOL_DISCLOSURE, **extra})
        self.log.add(0, "coordinator", "human_approval_required", {
            "required": True, "note": "Recommendation only. No clearance, dispatch or other "
            "real-world action is implemented. A human duty manager must approve."})
        self.finished = True
        events = self.log.close()
        return RunResult(
            scenario_id=self.s.scenario_id, state_hash=self.s.state_hash, status=status,
            reason_code=reason, selected_plan_id=selected,
            fallback_plan_id=self.fallback.plan.plan_id if self.fallback else None,
            rounds_completed=self.rounds_completed, agent_message_count=self.budget.count,
            provider_mode=self.mode, events=events,
            elapsed_ms=(time.perf_counter() - self.t0) * 1000, human_approval_required=True)

    def _fallback_path(self, trigger: str, **extra: Any) -> RunResult:
        if self._stale():
            return self._finish("human_review", "stale_state", None, trigger=trigger, **extra)
        fb = self.fallback
        assert fb is not None
        try:
            rep = self.engine.validate_plan(self.s, fb.plan)
        except Exception as exc:  # engine failure -> review, never a guess
            rep = None
            extra["engine_error"] = type(exc).__name__
        ok = rep is not None and rep.feasible and not is_blocked(fb.plan.plan_id, self.blocked)
        self.log.add(self.rounds_completed, "coordinator", "fallback_revalidated", {
            "plan_id": fb.plan.plan_id, "feasible": bool(rep and rep.feasible),
            "violations": [v.model_dump() for v in rep.violations] if rep else None,
            "blocked_by_claims": self.blocked.get(fb.plan.plan_id, []) + self.blocked.get("*", []),
            "trigger": trigger})
        if ok:
            return self._finish("fallback", trigger, fb.plan.plan_id, **extra)
        return self._finish("human_review", "fallback_invalid", None, trigger=trigger, **extra)

    # ------------------------------------------------------------- rounds
    async def _timed(self, agent: Any, ctx: dict[str, Any]) -> tuple[Any, float]:
        t0 = time.perf_counter()
        msg = await agent.respond(ctx)
        return msg, (time.perf_counter() - t0) * 1000

    async def _round(self, rnd: int, contexts: dict[Role, dict[str, Any]]) -> dict[Role, _Outcome]:
        tasks: dict[Role, asyncio.Task] = {}
        started = time.perf_counter()
        for role in ROLES:
            self.budget.take(role, rnd)
            self.log.add(rnd, role, "agent_request", {
                "role": role, "context_sha256": _sha(contexts[role]), "context": contexts[role],
                "deadline_s": self.timeout, "request_number": self.budget.count})
        for role in ROLES:
            t = asyncio.create_task(self._timed(self.agents[role], contexts[role]))
            t.add_done_callback(_swallow)
            tasks[role] = t
        done, pending = await asyncio.wait(tasks.values(), timeout=self.timeout)
        for t in pending:
            t.cancel()  # late results are never read: this round is closed
        round_ms = (time.perf_counter() - started) * 1000
        out: dict[Role, _Outcome] = {}
        for role in ROLES:
            t, agent = tasks[role], self.agents[role]
            if t not in done:
                out[role] = _Outcome(role, "timeout", error_kind="timeout", latency_ms=round_ms)
                self.log.add(rnd, role, "agent_timeout", {"role": role, "deadline_s": self.timeout,
                                                          "waited_ms": round_ms})
                continue
            rec = getattr(agent, "last_call", None)
            meta = {"model": getattr(rec, "model", None), "provider": getattr(rec, "provider", None),
                    "request_id": getattr(rec, "request_id", None),
                    "stop_reason": getattr(rec, "stop_reason", None),
                    "input_tokens": getattr(rec, "input_tokens", None),
                    "output_tokens": getattr(rec, "output_tokens", None)}
            exc = t.exception()
            if exc is not None:
                kind = exc.kind if isinstance(exc, AgentReplyError) else f"agent_exception:{type(exc).__name__}"
                raw = exc.raw_text if isinstance(exc, AgentReplyError) else None
                out[role] = _Outcome(role, "invalid", error_kind=kind)
                self.log.add(rnd, role, "agent_response_invalid", {
                    "role": role, "error_kind": kind, "detail": str(exc)[:300],
                    "raw_text": clip_raw(raw), "message": None, **meta})
                continue
            msg, latency = t.result()
            if not isinstance(msg, AgentMessage):
                out[role] = _Outcome(role, "invalid", error_kind="not_agent_message", latency_ms=latency)
                self.log.add(rnd, role, "agent_response_invalid", {
                    "role": role, "error_kind": "not_agent_message", "raw_text": None, "message": None, **meta})
                continue
            problems = validate_message(msg, contexts[role], expected_role=role, expected_round=rnd)
            raw = clip_raw(getattr(rec, "raw_text", None) or msg.model_dump_json())
            if problems:
                out[role] = _Outcome(role, "invalid", message=msg, problems=problems,
                                     error_kind="semantic_invalid", latency_ms=latency)
                self.log.add(rnd, role, "agent_response_invalid", {
                    "role": role, "error_kind": "semantic_invalid", "problems": problems,
                    "raw_text": raw, "message": msg.model_dump(), "latency_ms": latency, **meta})
            else:
                out[role] = _Outcome(role, "valid", message=msg, latency_ms=latency)
                self.log.add(rnd, role, "agent_response_valid", {
                    "role": role, "message": msg.model_dump(), "raw_text": raw, "latency_ms": latency,
                    "unverified_numbers": unverified_numbers(msg, contexts[role]), **meta})
        return out

    @staticmethod
    def _round_failure(rnd: int, outcomes: dict[Role, _Outcome]) -> str | None:
        st = [o for o in outcomes.values() if o.status != "valid"]
        if not st:
            return None
        if any(o.status == "timeout" for o in st):
            return f"round{rnd}_timeout"
        if any((o.error_kind or "").startswith("provider") for o in st):
            return f"round{rnd}_provider_error"
        return f"round{rnd}_invalid_response"

    # ------------------------------------------------------------- main
    async def run(self) -> RunResult:
        s, eng = self.s, self.engine
        cfg = {"coordinator": COORDINATOR_VERSION, "prompt_version": PROMPT_VERSION,
               "prompt_bundle_hash": prompt_bundle_hash(), "max_rounds": self.max_rounds,
               "max_agent_requests": MAX_AGENT_REQUESTS, "per_call_timeout_s": self.timeout,
               "epsilon": self.eps, "shortlist_max": MAX_SHORTLIST,
               "agents": {r: self.agents[r].describe() for r in ROLES}}
        self.log.add(0, "coordinator", "run_started", {
            **cfg, "config_hash": _sha(cfg), "provider_mode": self.mode, "limits": PROTOCOL_DISCLOSURE,
            "engine": eng.name, "source_mode": s.source_mode, "historical_ref": s.historical_ref,
            "mode_label": {"live": "LIVE provider calls", "mock": "MOCK deterministic simulation - not an LLM",
                           "replay": "REPLAY of a saved trace - not live"}[self.mode]})

        # 1. validate / hash scenario
        try:
            Scenario.model_validate_json(s.model_dump_json())  # strict re-validation + hash re-check
            if eng.sealed_check is not None:
                eng.sealed_check(s)  # Member 1's ensure_sealed: detects in-place mutation
            if eng.state_hash_fn is not None and eng.state_hash_fn(s.model_dump()) != s.state_hash:
                return self._finish("human_review", "scenario_hash_mismatch", None)
        except Exception as exc:
            reason = "stale_state" if type(exc).__name__ == "StaleStateError" else "invalid_scenario"
            return self._finish("human_review", reason, None, error=type(exc).__name__)
        if self._stale():
            return self._finish("human_review", "stale_state", None)

        try:
            plans = eng.generate_candidates(s)
            reports = {p.plan_id: eng.validate_plan(s, p) for p in plans}
            assessments = eng.assess_candidates(s, plans)
        except Exception as exc:
            return self._finish("human_review", "engine_error", None, error=f"{type(exc).__name__}: {exc}"[:300])
        catalogue = {p.plan_id: p for p in plans}
        rejected = {pid: [v.rule_id for v in r.violations] for pid, r in reports.items() if not r.feasible}
        self.log.add(0, "engine", "catalogue_generated", {
            "candidate_count": len(plans), "feasible_count": len(plans) - len(rejected),
            "catalogue_hash": _sha([(p.model_dump(), reports[p.plan_id].feasible) for p in plans]),
            "truncated": False})
        self.log.add(0, "engine", "candidate_rejections", {"rejections": rejected})
        good: list[Assessment] = []
        for a in assessments:
            pid = a.plan.plan_id
            if (a.plan.state_hash == s.state_hash and a.plan.scenario_id == s.scenario_id
                    and a.validation.feasible and pid in reports and reports[pid].feasible):
                good.append(a)
            else:
                self.log.add(0, "coordinator", "assessment_discarded", {"plan_id": pid})
        assess_by_id = {a.plan.plan_id: a for a in good}
        self.log.add(0, "engine", "feasible_catalogue", {"assessments": [
            {"plan_id": a.plan.plan_id, "utilities": a.utilities, "metrics": a.validation.metrics}
            for a in good]})
        fb_plan = eng.choose_fallback(good) if good else None
        if fb_plan is None or fb_plan.plan_id not in assess_by_id:
            return self._finish("human_review", "no_feasible_plan", None)
        self.fallback = fb = assess_by_id[fb_plan.plan_id]
        d = dict(fb.utilities)
        self.log.add(0, "engine", "fallback_selected", {
            "plan_id": fb.plan.plan_id, "disagreement_utilities": d,
            "rule": "first feasible by (landing_min, gate_in_min, plan_id)"})

        short, tags = build_shortlist(good, fb.plan.plan_id)
        self.log.add(0, "coordinator", "shortlist_published", {
            "plan_ids": [a.plan.plan_id for a in short], "tags": tags,
            "full_feasible_count": len(good), "note": "prompt shortlist only; optimizer searches full catalogue"})

        # 2. round 1
        ctx1 = {r: build_round1_context(role=r, scenario=s, shortlist=short, tags=tags, fallback=fb,
                                        rule_ids=role_rule_ids(r))
                for r in ROLES}
        out1 = await self._round(1, ctx1)
        self.rounds_completed = 1
        fail = self._round_failure(1, out1)
        if fail:
            return self._fallback_path(fail)

        # 3. broadcast + deterministic claim checks
        def validate(p: Plan) -> ValidationReport:
            return eng.validate_plan(s, p)

        checks: list[dict[str, Any]] = []
        for r in ROLES:
            checks += check_objections(r, out1[r].message, catalogue=catalogue, assessments=assess_by_id,
                                       fallback_utils=d, validate=validate, epsilon=self.eps)
        for c in checks:
            self.log.add(1, "coordinator", "claim_check", c)
        offers = {r: offer_envelope(out1[r].message, checks) for r in ROLES}
        self.log.add(1, "coordinator", "offers_broadcast", {"offers": offers})
        self.blocked = blocked_plans(checks)
        if any(c["verdict"] == "confirmed" and c["code"] == "hard_constraint"
               and c["plan_id"] in assess_by_id for c in checks):
            return self._finish("human_review", "engine_inconsistency", None,
                                detail="confirmed violation on a plan the engine marked feasible")

        # 4. Nash compromise (engine)
        comp_plan = eng.choose_compromise(good, fb.plan.plan_id, epsilon=self.eps)
        if comp_plan is None:
            self.log.add(1, "engine", "compromise_computed", {"plan_id": None, "method": NASH_DISCLOSURE})
            self.log.add(1, "coordinator", "round_skipped", {"round": 2, "reason": "no_positive_bargain"})
            return self._fallback_path("no_positive_bargain")
        comp = assess_by_id.get(comp_plan.plan_id)
        gains = {r: comp.utilities[r] - d[r] for r in ROLES} if comp else {}
        self.log.add(1, "engine", "compromise_computed", {
            "plan_id": comp_plan.plan_id, "gains": gains, "method": NASH_DISCLOSURE})
        if comp is None or min(gains.values()) <= self.eps:
            return self._fallback_path("engine_inconsistency")
        if is_blocked(comp.plan.plan_id, self.blocked):
            self.log.add(1, "coordinator", "round_skipped", {"round": 2, "reason": "unresolved_safety_objection"})
            return self._fallback_path("unresolved_safety_objection")
        if self.max_rounds < 2:
            return self._fallback_path("max_rounds_reached")
        if self._stale():
            return self._finish("human_review", "stale_state", None)

        # 5. round 2
        pre = eng.validate_plan(s, comp.plan)
        ctx2 = {r: build_round2_context(role=r, round1_ctx=ctx1[r], offers=offers, compromise=comp,
                                        fallback=fb, claim_checks=checks,
                                        compromise_validation_feasible=pre.feasible)
                for r in ROLES}
        out2 = await self._round(2, ctx2)
        self.rounds_completed = 2
        fail = self._round_failure(2, out2)
        if fail:
            return self._fallback_path(fail)
        checks2: list[dict[str, Any]] = []
        for r in ROLES:
            checks2 += check_objections(r, out2[r].message, catalogue=catalogue, assessments=assess_by_id,
                                        fallback_utils=d, validate=validate, epsilon=self.eps)
        for c in checks2:
            self.log.add(2, "coordinator", "claim_check", c)
        for k, v in blocked_plans(checks2).items():
            self.blocked.setdefault(k, []).extend(v)
        votes = {r: out2[r].message.action for r in ROLES}
        if any(v != "accept" for v in votes.values()):
            return self._fallback_path("round2_rejected", votes=votes)
        if is_blocked(comp.plan.plan_id, self.blocked):
            return self._fallback_path("unresolved_safety_objection", votes=votes)

        # 6. final deterministic validation
        if self._stale():
            return self._finish("human_review", "stale_state", None, votes=votes)
        final = eng.validate_plan(s, comp.plan)
        exact = all(out2[r].message.plan_id == comp.plan.plan_id and out2[r].message.state_hash == s.state_hash
                    for r in ROLES)
        self.log.add(2, "engine", "final_validation", {
            "plan_id": comp.plan.plan_id, "feasible": final.feasible,
            "violations": [v.model_dump() for v in final.violations], "exact_votes": exact, "gains": gains})
        if not final.feasible or not exact:
            return self._fallback_path("final_validation_failed", votes=votes)
        return self._finish("consensus", "consensus", comp.plan.plan_id, votes=votes)


async def negotiate(scenario: Scenario, agents: dict[Role, Any], *, max_rounds: int = 2,
                    per_call_timeout_s: float = 15.0, engine: EngineAPI | None = None,
                    state_probe: Callable[[], str] | None = None, epsilon: float = 1e-9,
                    clock: Callable[[], str] = utc_now) -> RunResult:
    """Run the bounded protocol. Returns a RunResult; human approval always required.

    ``engine``/``state_probe``/``epsilon``/``clock`` are optional keyword-only
    additions (docs/contract_changes.md CC-1). ``state_probe`` returns the
    current authoritative state hash; any change aborts the run as stale.
    """
    if set(agents) != set(ROLES):
        raise ValueError("agents must contain exactly atc, pilot and ground")
    if len({id(a) for a in agents.values()}) != 3:
        raise ValueError("three distinct agent objects required; one agent cannot play several roles")
    for r, a in agents.items():
        if getattr(a, "role", None) != r:
            raise ValueError(f"agent under key {r!r} reports role {getattr(a, 'role', None)!r}")
    modes = {a.provider_mode for a in agents.values()}
    if len(modes) != 1:
        raise ValueError(f"mixed provider modes {sorted(modes)}; a run has one disclosed mode")
    if max_rounds not in (1, 2):
        raise ValueError("max_rounds must be 1 or 2 (hard protocol cap is 2)")
    if not per_call_timeout_s > 0:
        raise ValueError("per_call_timeout_s must be positive")
    run = _Run(scenario, agents, max_rounds=max_rounds, per_call_timeout_s=per_call_timeout_s,
               engine=engine or EngineAPI.default(), state_probe=state_probe, epsilon=epsilon, clock=clock)
    return await run.run()
