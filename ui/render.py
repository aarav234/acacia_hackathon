"""Pure view helpers: RunResult + Scenario -> escaped HTML fragments (Wayfind material).

Nothing here computes a decision. Every figure comes from the RunResult's audit
events or from Member 1's engine; agent rationale is untrusted model text and is
always escaped.
"""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any

from airspace.contracts import ROLES, RunResult, Scenario
from airspace.engine.candidates import generate_candidates

ROLE_NAME = {"atc": "ATC", "pilot": "Pilot", "ground": "Ground ops"}
STATUS_HEAD = {"consensus": "Consensus", "fallback": "Fallback", "human_review": "Human review"}
MODE_NAME = {"live": "Live model calls", "mock": "Mock simulation, not a model",
             "replay": "Replay of a saved trace, not live"}


def e(x: Any) -> str:
    return escape("" if x is None else str(x), quote=True)


# --------------------------------------------------------------------------- extraction
@dataclass
class RoleTurn:
    state: str  # valid | invalid | timeout | skipped
    action: str | None = None
    plan_id: str | None = None
    ranked: tuple[str, ...] = ()
    rationale: str | None = None
    problem: str | None = None
    objections: tuple[dict, ...] = ()


@dataclass
class RunView:
    result: RunResult
    plans: dict[str, dict[str, Any]]
    utilities: dict[str, dict[str, float]]
    disagreement: dict[str, float]
    compromise_id: str | None
    turns: dict[int, dict[str, RoleTurn]]
    agents: dict[str, dict[str, Any]]
    candidate_count: int | None
    feasible_count: int | None
    human: dict[str, Any] | None

    def gain(self, role: str, plan_id: str | None) -> float | None:
        if not plan_id or plan_id not in self.utilities or role not in self.disagreement:
            return None
        return self.utilities[plan_id][role] - self.disagreement[role]


def build_view(result: RunResult, scenario: Scenario) -> RunView:
    plans = {p.plan_id: p.model_dump() for p in generate_candidates(scenario)}
    utilities: dict[str, dict[str, float]] = {}
    disagreement: dict[str, float] = {}
    compromise_id = None
    turns: dict[int, dict[str, RoleTurn]] = {1: {}, 2: {}}
    agents: dict[str, dict[str, Any]] = {}
    cand = feas = None
    human = None
    for ev in result.events:
        p = ev.payload
        t = ev.event_type
        if t == "run_started":
            agents = p.get("agents", {})
        elif t == "catalogue_generated":
            cand, feas = p.get("candidate_count"), p.get("feasible_count")
        elif t == "feasible_catalogue":
            utilities = {a["plan_id"]: a["utilities"] for a in p.get("assessments", [])}
        elif t == "fallback_selected":
            disagreement = dict(p.get("disagreement_utilities", {}))
        elif t == "compromise_computed":
            compromise_id = p.get("plan_id")
        elif t in ("agent_response_valid", "agent_response_invalid") and ev.round in (1, 2):
            m = p.get("message") or {}
            turns[ev.round][p["role"]] = RoleTurn(
                state="valid" if t == "agent_response_valid" else "invalid",
                action=m.get("action"), plan_id=m.get("plan_id"),
                ranked=tuple(m.get("ranked_plan_ids", [])), rationale=m.get("rationale"),
                problem=", ".join(p.get("problems") or []) or p.get("error_kind"),
                objections=tuple(m.get("objections", [])))
        elif t == "agent_timeout" and ev.round in (1, 2):
            turns[ev.round][p["role"]] = RoleTurn(state="timeout", problem="no reply before the deadline")
        elif t == "human_decision":
            human = {"actor": ev.actor, "at": ev.timestamp_utc, **p}
    for rnd in (1, 2):
        for r in ROLES:
            turns[rnd].setdefault(r, RoleTurn(state="skipped"))
    return RunView(result, plans, utilities, disagreement, compromise_id, turns, agents, cand, feas, human)


def _first_role(view: RunView, rnd: int, states: tuple[str, ...], actions: tuple[str, ...] = ()) -> str:
    for r in ROLES:
        t = view.turns[rnd][r]
        if t.state in states or (t.state == "valid" and t.action in actions):
            return ROLE_NAME[r]
    return "A role"


def reason_sentence(view: RunView) -> str:
    code = view.result.reason_code
    if code == "consensus":
        return "All three roles accepted the engine's compromise, and it passed final validation."
    if code == "no_positive_bargain":
        return "No plan improves on the fallback for all three roles, so the fallback stands."
    if code == "round2_rejected":
        return f"{_first_role(view, 2, (), ('reject',))} rejected the compromise, so the fallback stands."
    for rnd in (1, 2):
        if code == f"round{rnd}_timeout":
            return f"{_first_role(view, rnd, ('timeout',))} did not reply in time in round {rnd}. The fallback stands."
        if code == f"round{rnd}_invalid_response":
            return f"{_first_role(view, rnd, ('invalid',))}'s round {rnd} reply failed validation and was not counted. The fallback stands."
        if code == f"round{rnd}_provider_error":
            return f"The model provider failed in round {rnd}. Nothing was substituted; the fallback stands."
    return {
        "unresolved_safety_objection": "A safety objection could not be settled from the evidence, so the fallback stands.",
        "final_validation_failed": "The engine rejected the compromise at final validation, so the fallback stands.",
        "no_feasible_plan": "No candidate plan passes the hard constraints. There is nothing safe to recommend.",
        "fallback_invalid": "The fallback failed revalidation. There is no actionable plan.",
        "stale_state": "The scenario changed during the run. Start a new run.",
        "engine_inconsistency": "The engine and a confirmed objection disagree. Review by hand.",
    }.get(code, f"Stopped: {code}.")


# --------------------------------------------------------------------------- fragments
def plan_line(plans: dict[str, dict[str, Any]], plan_id: str | None) -> str:
    p = plans.get(plan_id or "")
    if not p:
        return e(plan_id or "No plan")
    return (f"Runway {e(p['runway_id'])}, gate {e(p['gate_id'])}, "
            f"wheels-on minute {e(p['landing_min'])}")


def masthead(scenario: Scenario) -> str:
    row = scenario.historical_row or {}
    if scenario.source_mode == "historical_anchor" and row:
        flight = f"{e(row.get('AIRLINE'))}{e(row.get('FLIGHT_NUMBER'))}"
        route = f"{e(row.get('ORIGIN_AIRPORT'))} to {e(row.get('DESTINATION_AIRPORT'))}"
        ref = scenario.historical_ref
        src = (f"Anchored to flights.csv row {e(ref.get('row_index'))}; weather delay "
               f"{e(row.get('WEATHER_DELAY'))} min on record. Every operating value here is simulated.")
        title = f"{flight}, {route}"
    else:
        title = "Synthetic scenario"
        src = "No historical flight behind this one. Every operating value is simulated."
    return f"""
<header class="mast">
  <a class="wordmark" href="./" aria-label="Airspace, start over">Airspace</a>
  <p class="mast-note">Decision support for one disrupted arrival. Recommendations only; a duty manager approves.</p>
</header>
<section class="flight" aria-label="Scenario">
  <h1>{title}</h1>
  <p class="flight-src">{src}</p>
</section>"""


def empty_state() -> str:
    return """
<section class="state state--empty" aria-live="polite">
  <h2>No run yet</h2>
  <p>Pick a scenario and a mode, then run the negotiation. Three role agents each propose a plan,
  the engine computes the compromise, and each role accepts or rejects it.</p>
</section>"""


def loading_state() -> str:
    rows = "".join(
        f'<tr><th scope="row">{ROLE_NAME[r]}</th><td><span class="skel"></span></td>'
        f'<td><span class="skel skel--s"></span></td><td class="num"><span class="skel skel--s"></span></td></tr>'
        for r in ROLES)
    return f"""
<section class="band band--ink board" aria-busy="true" aria-label="Negotiation board, waiting for replies">
  <h2>Waiting for the three roles</h2>
  <table class="board-table"><thead><tr><th scope="col">Role</th><th scope="col">Round 1 proposal</th>
  <th scope="col">Round 2 vote</th><th scope="col" class="num">Gain</th></tr></thead><tbody>{rows}</tbody></table>
</section>"""


def error_state(title: str, detail: str, fix: str) -> str:
    return f"""
<section class="state state--error" role="alert">
  <h2>{e(title)}</h2>
  <p>{e(detail)}</p>
  <p class="state-fix">{e(fix)}</p>
</section>"""


def outcome(view: RunView) -> str:
    r = view.result
    sel = r.selected_plan_id
    p = view.plans.get(sel or "")
    if p:
        facts = f"""
  <dl class="facts">
    <div><dt>Runway</dt><dd>{e(p['runway_id'])}</dd></div>
    <div><dt>Gate</dt><dd>{e(p['gate_id'])}</dd></div>
    <div><dt>Wheels-on</dt><dd class="num">min {e(p['landing_min'])}</dd></div>
    <div><dt>Gate-in</dt><dd class="num">min {e(p['gate_in_min'])}</dd></div>
    <div><dt>Taxi route</dt><dd>{e(p['taxi_route_id'])}</dd></div>
  </dl>"""
        plan = f'<p class="plan-id">{e(sel)}</p>'
    else:
        facts = ""
        plan = '<p class="plan-id plan-id--none">No actionable plan</p>'
    mode = MODE_NAME.get(r.provider_mode, r.provider_mode)
    return f"""
<section class="outcome outcome--{e(r.status)}" aria-label="Outcome">
  <p class="outcome-status">{STATUS_HEAD[r.status]}</p>
  {plan}
  <p class="outcome-why">{e(reason_sentence(view))}</p>
  {facts}
  <p class="outcome-meta">{e(r.rounds_completed)} of 2 rounds, {e(r.agent_message_count)} of 6 agent responses,
  {r.elapsed_ms:,.0f} ms. {e(mode)}.</p>
</section>"""


def _turn_cell(view: RunView, t: RoleTurn, rnd: int) -> str:
    if t.state == "skipped":
        return '<span class="muted">Not reached</span>'
    if t.state == "timeout":
        return '<span class="flag">No reply</span>'
    if t.state == "invalid":
        return f'<span class="flag">Invalid</span> <span class="muted small">{e(t.problem)}</span>'
    if rnd == 1:
        if t.action == "reject":
            return '<span class="flag">Declined to propose</span>'
        alt = [x for x in t.ranked if x != t.plan_id]
        alts = f'<span class="muted small">then {", ".join(e(a) for a in alt)}</span>' if alt else ""
        mark = ' <span class="tag">same as compromise</span>' if t.plan_id == view.compromise_id else ""
        return f'<span class="pid">{e(t.plan_id)}</span>{mark}<br>{alts}'
    return ('<span class="vote vote--yes">Accept</span>' if t.action == "accept"
            else '<span class="flag">Reject</span>')


def _gain(g: float | None) -> str:
    return "n/a" if g is None else f"{g:+.3f}"


def board(view: RunView) -> str:
    sel = view.result.selected_plan_id
    if view.result.rounds_completed == 0:
        return """
<section class="band band--ink board" aria-label="Negotiation board">
  <h2>No agent was called</h2>
  <p class="board-note">The engine stopped the run before round 1, so no model saw this scenario.
  Nothing here needs a vote; it needs a person.</p>
</section>"""
    rows = []
    for r in ROLES:
        g = view.gain(r, sel)
        rows.append(
            f'<tr><th scope="row">{ROLE_NAME[r]}</th>'
            f'<td>{_turn_cell(view, view.turns[1][r], 1)}</td>'
            f'<td>{_turn_cell(view, view.turns[2][r], 2)}</td>'
            f'<td class="num figure">{_gain(g)}</td></tr>')
    comp = (f'Engine compromise <span class="pid">{e(view.compromise_id)}</span>: {plan_line(view.plans, view.compromise_id)}.'
            if view.compromise_id else "The engine found no compromise that beats the fallback for every role.")
    return f"""
<section class="band band--ink board" aria-label="Negotiation board">
  <h2>Who wanted what</h2>
  <p class="board-note">{comp}</p>
  <table class="board-table">
    <thead><tr><th scope="col">Role</th><th scope="col">Round 1 proposal</th>
    <th scope="col">Round 2 vote</th><th scope="col" class="num">Gain vs fallback</th></tr></thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
  <p class="board-foot">Gain is the role's engine-computed utility at the recommended plan minus its utility at the fallback.
  2 negotiation rounds, up to 6 agent responses.</p>
</section>"""


def comparison(view: RunView) -> str:
    fb = view.result.fallback_plan_id
    comp = view.compromise_id
    if not fb:
        return ""
    cols = [("Fallback", fb)] + ([("Compromise", comp)] if comp else [])
    head = "".join(f'<th scope="col" class="num">{n}<br><span class="pid">{e(pid)}</span></th>' for n, pid in cols)
    body = []
    for r in ROLES:
        cells = "".join(f'<td class="num">{view.utilities.get(pid, {}).get(r, float("nan")):.3f}</td>' for _, pid in cols)
        body.append(f'<tr><th scope="row">{ROLE_NAME[r]} utility</th>{cells}</tr>')
    for label, key in (("Wheels-on minute", "landing_min"), ("Gate-in minute", "gate_in_min"),
                       ("Runway", "runway_id"), ("Gate", "gate_id")):
        cells = "".join(f'<td class="num">{e(view.plans.get(pid, {}).get(key))}</td>' for _, pid in cols)
        body.append(f'<tr><th scope="row">{label}</th>{cells}</tr>')
    cat = (f"{e(view.feasible_count)} of {e(view.candidate_count)} candidate plans pass every hard constraint."
           if view.candidate_count is not None else "")
    return f"""
<section class="compare" aria-label="Compromise against fallback">
  <h2>Compromise against the fallback</h2>
  <p class="muted">{cat} The fallback is the earliest feasible plan; utilities come from the engine, never from a model.</p>
  <table class="data-table"><thead><tr><th scope="col">Measure</th>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>
</section>"""


def statements(view: RunView) -> str:
    rows = []
    for rnd in (1, 2):
        for r in ROLES:
            t = view.turns[rnd][r]
            if t.state == "skipped":
                continue
            objs = "".join(
                f'<li>{e(o.get("code", "").replace("_", " "))} on {e(o.get("plan_id") or "any plan")}: {e(o.get("reason"))}</li>'
                for o in t.objections)
            said = e(t.rationale) if t.rationale else f'<span class="muted">{e(t.problem)}</span>'
            rows.append(
                f'<tr><td class="num">{rnd}</td><th scope="row">{ROLE_NAME[r]}</th><td>{said}'
                f'{"<ul class=objs>" + objs + "</ul>" if objs else ""}</td></tr>')
    if not rows:
        return ""
    return f"""
<section class="said" aria-label="What each role said">
  <h2>What each role said</h2>
  <p class="muted">Model-written text, shown as data. It never changes a figure or a safety rule.</p>
  <table class="data-table"><thead><tr><th scope="col" class="num">Round</th><th scope="col">Role</th>
  <th scope="col">Rationale and objections</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
</section>"""


def human_line(view: RunView) -> str:
    h = view.human
    if not h:
        return ""
    verb = {"approved": "Approved", "rejected": "Rejected", "escalated": "Escalated"}.get(h.get("decision"), "Recorded")
    who = e(str(h.get("actor", "")).removeprefix("human:"))
    return (f'<p class="decision decision--{e(h.get("decision"))}" role="status">{verb} by {who} at '
            f'{e(h.get("at"))}. Logged separately from the agents\' votes. Nothing was executed.</p>')
