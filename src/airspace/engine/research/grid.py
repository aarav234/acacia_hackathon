"""Candidate-space coverage research: integer-minute refinement and an event grid.

The contract decision space is integer landing minutes on a configured slot
grid. ``refine(s, 1)`` re-seals a scenario on the full integer-minute grid (a
scenario-parameter change, not a contract change), so every plan below is a
normal production ``Plan`` checked by the production validator.

Event grid (exact reduction, claim C-EVT). For a fixed route, partition the
integer minutes into maximal intervals on which (i) the set of violated hard
rules, (ii) the set of background movements inside the spillover window,
(iii) the set of missed connection groups and (iv) the set of crews able to
cover the service are all constant. Inside such an interval every raw cost is
non-decreasing in landing_min (delay, fuel, turbulence, spillover terms,
turnaround, overtime grow; gate change is constant), so the interval's first
minute weakly dominates the rest for every role. Hence the fallback, the
Pareto set (up to weakly-dominated duplicates) and the production Nash winner
(whose tie-break prefers earlier landings) all lie on interval starts.
``event_times`` over-approximates the interval starts analytically from the
configuration; ``signature_starts`` computes them by brute force for testing.
"""

from __future__ import annotations

import math
from fractions import Fraction

from airspace.contracts import Plan, Scenario
from airspace.engine.candidates import generate_candidates


def refine(s: Scenario, step: int = 1) -> Scenario:
    data = s.model_dump(mode="json")
    data["slot_step_min"] = step
    data.pop("state_hash")
    return Scenario.seal(**data)


def event_times(s: Scenario, route) -> set[int]:
    """Analytic candidate landing minutes for one route (superset of interval starts)."""
    ops = s.operations
    e0, e1 = s.earliest_landing_min, s.earliest_landing_min + s.planning_horizon_min
    tau, svc, occ = route.taxi_min, ops.service.service_duration_min, ops.subject.runway_occupancy_min
    W = ops.utility.spillover_window_min
    sep = {(r.leader_category, r.follower_category): r.min_separation_min for r in ops.separation.rules}
    subj = ops.subject.wake_category
    pts = {e0}
    for w in next(r for r in ops.runways if r.runway_id == route.runway_id).open_windows:
        pts |= {w.start_min, w.end_min - occ + 1}
    for m in ops.runway_movements:
        if m.runway_id != route.runway_id:
            continue
        lead, follow = sep[(m.wake_category, subj)], sep[(subj, m.wake_category)]
        pts |= {m.time_min - follow + 1, m.time_min, m.time_min + 1, m.time_min + lead,  # separation regimes
                m.time_min - W + 1}  # spillover window entry (exit is time_min + 1)
    gate = next(g for g in ops.gates if g.gate_id == route.gate_id)
    B = gate.turnover_buffer_min
    for w in gate.open_windows:
        pts |= {w.start_min - tau, w.end_min - svc - tau + 1}
    for o in ops.gate_occupancy:
        if o.gate_id == gate.gate_id:
            pts |= {o.start_min - svc - B - tau + 1, o.end_min + B - tau}
    for b in ops.taxi_blockages:
        if b.segment_id in route.segment_ids:
            pts |= {b.start_min - tau + 1, b.end_min}
    for c in ops.crew:
        pts |= {c.available.start_min - tau, c.available.end_min - svc - tau + 1}
        for b in c.busy:
            pts |= {b.start_min - svc - tau + 1, b.end_min - tau}
    for g in ops.connections:
        pts.add(g.deadline_gate_in_min - tau + 1)
    f = ops.fuel  # first minute at which FUEL-RESERVE fails on this route (exact rational threshold)
    if f.airborne_burn_kg_per_min > 0:
        slack = (Fraction(f.snapshot_fuel_kg) - Fraction(f.approach_burn_kg) - Fraction(f.taxi_burn_kg_per_min) * tau
                 - Fraction(f.min_reserve_kg)) / Fraction(f.airborne_burn_kg_per_min)
        pts.add(e0 + math.floor(slack) + 1)
    return {p for p in pts if e0 <= p < e1}


def event_candidates(s1: Scenario) -> list[Plan]:
    """Subset of the integer-minute catalogue at event times (s1 must have slot_step_min=1)."""
    assert s1.slot_step_min == 1, "event grid is defined on the integer-minute scenario"
    times = {r.route_id: event_times(s1, r) for r in s1.operations.taxi_routes}
    return [p for p in generate_candidates(s1) if p.landing_min in times[p.taxi_route_id]]


def signature_starts(s1: Scenario, oracle_result) -> set[tuple[str, int]]:
    """Brute-force interval starts: (route, minute) where the regime signature changes."""
    ops = s1.operations
    W = ops.utility.spillover_window_min
    starts = set()
    for route in ops.taxi_routes:
        prev = None
        for L in s1.slots:
            pid = f"P{L:04d}_{route.runway_id}_{route.gate_id}_{route.route_id}"
            G, E = L + route.taxi_min, L + route.taxi_min + ops.service.service_duration_min
            sig = (
                frozenset(oracle_result.plans[pid]["failed"]),
                frozenset(m.movement_id for m in ops.runway_movements if m.runway_id == route.runway_id and L <= m.time_min < L + W),
                frozenset(g.group_id for g in ops.connections if G > g.deadline_gate_in_min),
                frozenset(c.crew_id for c in ops.crew if c.available.start_min <= G and E <= c.available.end_min
                          and not any(b.start_min < E and G < b.end_min for b in c.busy)),
            )
            if sig != prev:
                starts.add((route.route_id, L))
            prev = sig
    return starts
