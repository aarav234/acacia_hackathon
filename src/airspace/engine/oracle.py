"""Brute-force correctness oracle for small fixtures.

Deliberately independent of fallback.choose_fallback / bargaining.choose_compromise:
it re-derives every role cost from the published raw metrics and the config
weights/scales, picks the fallback and the compromise by exhaustive pairwise
comparison (no sort keys, no shared helpers), and uses exact rationals. It
reuses the constraint validator only to learn which plans are feasible.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

from airspace.contracts import Assessment, Scenario
from airspace.engine.candidates import generate_candidates
from airspace.engine.scoring import assess_candidates

_ROLES = ("atc", "pilot", "ground")


@dataclass(frozen=True)
class OracleResult:
    feasible_ids: tuple[str, ...]
    fallback_id: str | None
    compromise_id: str | None


def _cost(scenario: Scenario, a: Assessment, role: str) -> Fraction:
    total = Fraction(0)
    for spec in getattr(scenario.operations.utility, role).metrics:
        raw = Fraction(a.components[role][f"raw.{spec.metric}"])
        total += Fraction(spec.weight) * raw / Fraction(spec.scale)
    return total


def _earlier(a: Assessment, b: Assessment) -> bool:
    if a.plan.landing_min != b.plan.landing_min:
        return a.plan.landing_min < b.plan.landing_min
    if a.plan.gate_in_min != b.plan.gate_in_min:
        return a.plan.gate_in_min < b.plan.gate_in_min
    return a.plan.plan_id < b.plan.plan_id


def oracle(scenario: Scenario, epsilon: float = 1e-9) -> OracleResult:
    feasible = assess_candidates(scenario, generate_candidates(scenario))
    ids = tuple(a.plan.plan_id for a in feasible)
    if not feasible:
        return OracleResult(ids, None, None)

    fb = None
    for a in feasible:
        if all(a is b or _earlier(a, b) for b in feasible):
            fb = a
    assert fb is not None

    utility = {
        a.plan.plan_id: {r: 1 - _cost(scenario, a, r) for r in _ROLES} for a in feasible
    }
    d = utility[fb.plan.plan_id]
    eps = Fraction(epsilon)
    pool = []
    for a in feasible:
        g = {r: utility[a.plan.plan_id][r] - d[r] for r in _ROLES}
        if g["atc"] > eps and g["pilot"] > eps and g["ground"] > eps:
            pool.append((a, g))

    def beats(x, y) -> bool:
        (ax, gx), (ay, gy) = x, y
        px = gx["atc"] * gx["pilot"] * gx["ground"]
        py = gy["atc"] * gy["pilot"] * gy["ground"]
        if px != py:
            return px > py
        if min(gx.values()) != min(gy.values()):
            return min(gx.values()) > min(gy.values())
        cx = sum(1 - utility[ax.plan.plan_id][r] for r in _ROLES)
        cy = sum(1 - utility[ay.plan.plan_id][r] for r in _ROLES)
        if cx != cy:
            return cx < cy
        return _earlier(ax, ay)

    winner = None
    for x in pool:
        if all(x is y or beats(x, y) for y in pool):
            winner = x[0]
    return OracleResult(ids, fb.plan.plan_id, None if winner is None else winner.plan.plan_id)


# --------------------------------------------------------------------------
# Fully independent oracle (enumeration, hard rules, raw metrics, selection)
# --------------------------------------------------------------------------
# Written from the rule semantics in docs/data_and_model.md, not from
# constraints.py / scoring.py / bargaining.py, and computed in exact rationals.
# Utility weights/scales can be overridden so weight-sensitivity runs can be
# checked without rebuilding the scenario.


@dataclass(frozen=True)
class IndependentResult:
    plans: dict[str, dict]  # plan_id -> {"failed": set[str], "raw": {role: {metric: Fraction}}}
    feasible_ids: tuple[str, ...]
    utilities: dict[str, dict[str, Fraction]]
    fallback_id: str | None
    compromise_id: str | None
    eligible_ids: tuple[str, ...]
    pareto_ids: tuple[str, ...]


def _q(x) -> Fraction:
    return Fraction(x)


def _inside(windows, start, end) -> bool:
    return any(w.start_min <= start and end <= w.end_min for w in windows)


def _clash(a0, a1, b0, b1) -> bool:
    return a0 < b1 and b0 < a1


def independent_oracle(scenario: Scenario, *, weights=None, scales=None, epsilon: float = 1e-9) -> IndependentResult:
    ops = scenario.operations
    subj = ops.subject
    sep = {(r.leader_category, r.follower_category): r.min_separation_min for r in ops.separation.rules}
    weights = weights or {r: {m.metric: m.weight for m in getattr(ops.utility, r).metrics} for r in _ROLES}
    scales = scales or {m.metric: m.scale for r in _ROLES for m in getattr(ops.utility, r).metrics}
    slots, t = [], scenario.earliest_landing_min
    while t < scenario.earliest_landing_min + scenario.planning_horizon_min:
        slots.append(t)
        t += scenario.slot_step_min
    fuel = ops.fuel
    plans: dict[str, dict] = {}
    for L in slots:
        for route in ops.taxi_routes:
            G = L + route.taxi_min
            E = G + ops.service.service_duration_min
            pid = f"P{L:04d}_{route.runway_id}_{route.gate_id}_{route.route_id}"
            failed: set[str] = set()
            runway = next(r for r in ops.runways if r.runway_id == route.runway_id)
            if not _inside(runway.open_windows, L, L + subj.runway_occupancy_min):
                failed.add("RWY-OPEN")
            for m in ops.runway_movements:
                if m.runway_id != route.runway_id:
                    continue
                if m.time_min <= L and L - m.time_min < sep[(m.wake_category, subj.wake_category)]:
                    failed.add("RWY-SEP-LEAD")
                if m.time_min >= L and m.time_min - L < sep[(subj.wake_category, m.wake_category)]:
                    failed.add("RWY-SEP-FOLLOW")
            gate = next(g for g in ops.gates if g.gate_id == route.gate_id)
            if subj.wake_category not in gate.compatible_categories:
                failed.add("GATE-COMPAT")
            if not _inside(gate.open_windows, G, E):
                failed.add("GATE-OPEN")
            B = gate.turnover_buffer_min
            if any(o.gate_id == gate.gate_id and _clash(G, E + B, o.start_min, o.end_min + B) for o in ops.gate_occupancy):
                failed.add("GATE-OCCUPANCY")
            if any(b.segment_id in route.segment_ids and _clash(L, G, b.start_min, b.end_min) for b in ops.taxi_blockages):
                failed.add("TAXI-BLOCK")
            overtimes = [
                max(0, E - max(c.regular_end_min, G))
                for c in ops.crew
                if c.available.start_min <= G and E <= c.available.end_min
                and not any(_clash(G, E, b.start_min, b.end_min) for b in c.busy)
            ]
            if not overtimes:
                failed.add("CREW-COVERAGE")
            d = L - scenario.earliest_landing_min
            at_gate = _q(fuel.snapshot_fuel_kg) - _q(fuel.approach_burn_kg) - _q(fuel.airborne_burn_kg_per_min) * d \
                - _q(fuel.taxi_burn_kg_per_min) * route.taxi_min
            if at_gate < _q(fuel.min_reserve_kg):
                failed.add("FUEL-RESERVE")
            W = ops.utility.spillover_window_min
            raw = {
                "atc": {"landing_delay_min": _q(d),
                        "spillover_proxy_min": _q(sum(W - (m.time_min - L) for m in ops.runway_movements
                                                      if m.runway_id == route.runway_id and L <= m.time_min < L + W))},
                "pilot": {"incremental_fuel_kg": _q(fuel.airborne_burn_kg_per_min) * d + _q(fuel.taxi_burn_kg_per_min) * route.taxi_min,
                          "connection_penalty_pax": _q(sum(c.passenger_count for c in ops.connections if G > c.deadline_gate_in_min)),
                          "turbulence_exposure_index": _q(next(e.exposure_index for e in ops.turbulence.runway_exposure if e.runway_id == route.runway_id))
                          + _q(ops.turbulence.holding_exposure_per_min) * d},
                "ground": {"turnaround_delay_min": _q(max(0, E - (subj.scheduled_gate_in_min + ops.service.service_duration_min))),
                           "crew_overtime_min": _q(min(overtimes)) if overtimes else None,
                           "gate_change_indicator": _q(0 if route.gate_id == subj.assigned_gate_id else 1)},
            }
            plans[pid] = {"failed": failed, "raw": raw, "landing": L, "gate_in": G}
    feasible = [p for p, v in plans.items() if not v["failed"]]
    util = {
        p: {r: 1 - sum(_q(weights[r][m]) * plans[p]["raw"][r][m] / _q(scales[m]) for m in weights[r]) for r in _ROLES}
        for p in feasible
    }

    def earlier(a, b):
        ka = (plans[a]["landing"], plans[a]["gate_in"], a)
        kb = (plans[b]["landing"], plans[b]["gate_in"], b)
        return ka < kb

    fb = next((a for a in feasible if all(a == b or earlier(a, b) for b in feasible)), None)
    pareto = tuple(a for a in feasible if not any(
        all(util[b][r] >= util[a][r] for r in _ROLES) and any(util[b][r] > util[a][r] for r in _ROLES) for b in feasible))
    if fb is None:
        return IndependentResult(plans, tuple(feasible), util, None, None, (), pareto)
    eps = _q(epsilon)
    gain = {p: {r: util[p][r] - util[fb][r] for r in _ROLES} for p in feasible}
    pool = [p for p in feasible if all(gain[p][r] > eps for r in _ROLES)]

    def beats(x, y):
        px = gain[x]["atc"] * gain[x]["pilot"] * gain[x]["ground"]
        py = gain[y]["atc"] * gain[y]["pilot"] * gain[y]["ground"]
        if px != py:
            return px > py
        mx, my = min(gain[x].values()), min(gain[y].values())
        if mx != my:
            return mx > my
        cx, cy = sum(1 - util[x][r] for r in _ROLES), sum(1 - util[y][r] for r in _ROLES)
        if cx != cy:
            return cx < cy
        return earlier(x, y)

    win = next((x for x in pool if all(x == y or beats(x, y) for y in pool)), None)
    return IndependentResult(plans, tuple(feasible), util, fb, win, tuple(pool), pareto)
