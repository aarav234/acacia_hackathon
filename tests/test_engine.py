"""Engine tests: candidates, hard constraints, scoring, fallback, bargaining, oracle.

Fixture scenarios are SYNTHETIC operating states (the success scenario is anchored
to a genuine row for context only).
"""

from __future__ import annotations

import collections
import json
import math
import random
from fractions import Fraction
from pathlib import Path

import pytest

from airspace.contracts import (
    ROLES,
    Assessment,
    Plan,
    Scenario,
    StaleStateError,
    ValidationReport,
    make_plan_id,
)
from airspace.data import fixtures as fx
from airspace.data.scenario import build_scenario, synthetic_row
from airspace.engine.bargaining import (
    choose_compromise,
    dominates,
    eligible,
    gains,
    pareto_front,
)
from airspace.engine.candidates import generate_candidates
from airspace.engine.constraints import RULES, validate_plan
from airspace.engine.errors import CandidateCapExceededError, InconsistentAssessmentsError, UnknownPlanError
from airspace.engine.fallback import choose_fallback
from airspace.engine.oracle import oracle
from airspace.engine.pipeline import solve_scenario
from airspace.engine.scoring import assess_candidates, evaluate_candidates


def scenario_from(cfg: dict, tmp_path: Path, name: str = "cfg.json") -> Scenario:
    path = tmp_path / name
    path.write_text(json.dumps(cfg))
    return build_scenario(synthetic_row("engine unit test"), path)


def plan_by_id(scenario: Scenario, plan_id: str) -> Plan:
    return next(p for p in generate_candidates(scenario) if p.plan_id == plan_id)


def rule_ids(scenario: Scenario, plan_id: str) -> set[str]:
    return {v.rule_id for v in validate_plan(scenario, plan_by_id(scenario, plan_id)).violations}


@pytest.fixture(scope="module")
def success() -> Scenario:
    return fx.load_fixture_scenario("success")


# ---- candidates ----------------------------------------------------------------
def test_candidates_deterministic_route_pairs_and_consistent_timing(success):
    plans = generate_candidates(success)
    assert plans == generate_candidates(success)
    assert len(plans) == 8 * 6
    assert len({p.plan_id for p in plans}) == len(plans)
    keys = [(p.landing_min, p.runway_id, p.gate_id, p.taxi_route_id) for p in plans]
    assert keys == sorted(keys)
    pairs = {(p.runway_id, p.gate_id) for p in plans}
    assert ("R1", "G3") not in pairs and ("R2", "G4") not in pairs  # no Cartesian pairing
    assert {p.landing_min for p in plans} == set(range(10, 50, 5))
    for p in plans:
        assert p.gate_in_min == p.landing_min + p.taxi_min
        assert p.service_end_min == p.gate_in_min + 30
        assert p.plan_id == make_plan_id(p.landing_min, p.runway_id, p.gate_id, p.taxi_route_id)


def test_candidate_cap_is_an_explicit_error(tmp_path):
    cfg = fx.config_dict("success")
    cfg["operations"]["planning"]["candidate_cap"] = 47
    with pytest.raises(CandidateCapExceededError, match="48"):
        generate_candidates(scenario_from(cfg, tmp_path))
    cfg["operations"]["planning"]["candidate_cap"] = 48
    assert len(generate_candidates(scenario_from(cfg, tmp_path))) == 48


# ---- runway ------------------------------------------------------------------
def test_leading_separation_boundary(success, tmp_path):
    # R1 @15 behind heavy departure @12: gap 3, heavy->medium requires 3 -> exactly OK.
    assert "RWY-SEP-LEAD" not in rule_ids(success, "P0015_R1_G2_T2")
    # R2 @10 behind heavy arrival @8: gap 2 < 3.
    assert "RWY-SEP-LEAD" in rule_ids(success, "P0010_R2_G3_T4")
    cfg = fx.config_dict("success")
    for r in cfg["operations"]["separation"]["rules"]:
        if (r["leader_category"], r["follower_category"]) == ("heavy", "medium"):
            r["min_separation_min"] = 4
    assert "RWY-SEP-LEAD" in rule_ids(scenario_from(cfg, tmp_path), "P0015_R1_G2_T2")


def test_following_separation_boundary(success, tmp_path):
    # R1 @10 ahead of heavy departure @12: medium->heavy requires 2, gap 2 -> OK.
    assert "RWY-SEP-FOLLOW" not in rule_ids(success, "P0010_R1_G2_T2")
    cfg = fx.config_dict("success")
    for r in cfg["operations"]["separation"]["rules"]:
        if (r["leader_category"], r["follower_category"]) == ("medium", "heavy"):
            r["min_separation_min"] = 3
    ids = rule_ids(scenario_from(cfg, tmp_path), "P0010_R1_G2_T2")
    assert "RWY-SEP-FOLLOW" in ids and "RWY-SEP-LEAD" not in ids


def test_runway_open_window_covers_whole_occupancy(tmp_path):
    cfg = fx.config_dict("success")
    cfg["operations"]["runways"][0]["open_windows"] = [{"start_min": 0, "end_min": 12}]  # R1
    s = scenario_from(cfg, tmp_path)
    assert "RWY-OPEN" not in rule_ids(s, "P0010_R1_G2_T2")  # occupancy [10,12) fits exactly
    cfg["operations"]["runways"][0]["open_windows"] = [{"start_min": 0, "end_min": 11}]
    assert "RWY-OPEN" in rule_ids(scenario_from(cfg, tmp_path), "P0010_R1_G2_T2")


# ---- gates -------------------------------------------------------------------
def test_gate_buffer_exact_boundary_both_directions(success, tmp_path):
    # G1 existing [0,15) + buffer 5 -> proposed gate-in 20 touches, does not overlap.
    assert "GATE-OCCUPANCY" not in rule_ids(success, "P0015_R2_G1_T3")  # gate_in 20
    assert "GATE-OCCUPANCY" in rule_ids(success, "P0010_R1_G1_T1")  # gate_in 16 < 20
    # Proposed use BEFORE an existing one: G3 existing starts at 70; proposed [18,48)+5=53 -> OK.
    cfg = fx.config_dict("success")
    cfg["operations"]["gate_occupancy"][1]["start_min"] = 53  # proposed buffered end touches start
    assert "GATE-OCCUPANCY" not in rule_ids(scenario_from(cfg, tmp_path), "P0015_R2_G3_T4")
    cfg["operations"]["gate_occupancy"][1]["start_min"] = 52
    assert "GATE-OCCUPANCY" in rule_ids(scenario_from(cfg, tmp_path, "b.json"), "P0015_R2_G3_T4")


def test_gate_compatibility(success):
    for p in generate_candidates(success):
        if p.gate_id == "G4":  # heavy-only gate, subject is medium
            assert "GATE-COMPAT" in rule_ids(success, p.plan_id)


# ---- taxi ----------------------------------------------------------------------
def test_taxi_blockage_half_open_boundary(success, tmp_path):
    # R1 @35 via T2 (segment TWC) taxis [35,39); blockage TWC [38,46) overlaps.
    assert "TAXI-BLOCK" in rule_ids(success, "P0035_R1_G2_T2")
    cfg = fx.config_dict("success")
    cfg["operations"]["taxi_blockages"][0]["start_min"] = 39  # touching only
    assert "TAXI-BLOCK" not in rule_ids(scenario_from(cfg, tmp_path), "P0035_R1_G2_T2")


# ---- fuel ------------------------------------------------------------------------
def test_fuel_reserve_boundary_includes_taxi_burn(tmp_path):
    cfg = fx.config_dict("success")
    # P0015_R2_G3_T4: 6000 - 1500 - 30*5 - 15*3 = 4305 kg at gate-in.
    cfg["operations"]["fuel"]["min_reserve_kg"] = 4305.0
    s = scenario_from(cfg, tmp_path)
    report = validate_plan(s, plan_by_id(s, "P0015_R2_G3_T4"))
    assert report.feasible and report.metrics["pilot_reserve_margin_kg"] == 0.0
    # Same landing minute, longer taxi (T3, 5 min) burns 30 kg more -> below reserve.
    assert "FUEL-RESERVE" in rule_ids(s, "P0015_R2_G1_T3")
    cfg["operations"]["fuel"]["min_reserve_kg"] = 4306.0
    s2 = scenario_from(cfg, tmp_path, "b.json")
    v = validate_plan(s2, plan_by_id(s2, "P0015_R2_G3_T4"))
    fuel = next(x for x in v.violations if x.rule_id == "FUEL-RESERVE")
    assert fuel.units == "kg" and "4305.000" in fuel.observed


# ---- crew ------------------------------------------------------------------------
def test_crew_hard_unavailability_vs_soft_overtime(success, tmp_path):
    # Soft: the compromise needs C1 past its regular end (40) -> feasible with overtime.
    a = next(x for x in assess_candidates(success, generate_candidates(success)) if x.plan.plan_id == "P0015_R2_G1_T3")
    assert a.components["ground"]["raw.crew_overtime_min"] == 10.0
    # Hard: without C1, a service starting before C2's availability (25) is infeasible.
    cfg = fx.config_dict("success")
    cfg["operations"]["crew"] = [c for c in cfg["operations"]["crew"] if c["crew_id"] != "C1"]
    s = scenario_from(cfg, tmp_path)
    assert "CREW-COVERAGE" in rule_ids(s, "P0015_R2_G1_T3")
    assert "CREW-COVERAGE" not in rule_ids(s, "P0020_R2_G1_T3")  # [25,55) inside C2


def test_crew_busy_interval_blocks(tmp_path):
    cfg = fx.config_dict("success")
    cfg["operations"]["crew"] = [{"crew_id": "C1", "available": {"start_min": 0, "end_min": 200},
                                  "regular_end_min": 200, "busy": [{"start_min": 49, "end_min": 60}]}]
    s = scenario_from(cfg, tmp_path)
    assert "CREW-COVERAGE" in rule_ids(s, "P0015_R2_G1_T3")  # service [20,50) overlaps [49,60)
    cfg["operations"]["crew"][0]["busy"] = [{"start_min": 50, "end_min": 60}]
    assert "CREW-COVERAGE" not in rule_ids(scenario_from(cfg, tmp_path, "b.json"), "P0015_R2_G1_T3")


# ---- reports / gating --------------------------------------------------------------
def test_violations_cite_known_evidence_and_units(success):
    known = success.evidence_ids()
    rejected = evaluate_candidates(success, generate_candidates(success)).rejected
    assert rejected
    for report in rejected:
        for v in report.violations:
            assert v.rule_id in RULES and v.units and set(v.evidence_ids) <= known


def test_infeasible_plans_never_reach_selectable_assessments(success):
    ev = evaluate_candidates(success, generate_candidates(success))
    selectable = {a.plan.plan_id for a in ev.assessments}
    assert selectable.isdisjoint({r.plan_id for r in ev.rejected})
    assert all(a.validation.feasible and not a.validation.violations for a in ev.assessments)
    for report in ev.rejected:
        with pytest.raises(ValueError):
            Assessment(plan=plan_by_id(success, report.plan_id), validation=report,
                       utilities={r: 1.0 for r in ROLES}, components={r: {"cost": 0.0} for r in ROLES})


def test_stale_plans_rejected(success, tmp_path):
    other = scenario_from(fx.config_dict("success"), tmp_path)  # synthetic -> different hash
    stale = generate_candidates(other)[0]
    with pytest.raises(StaleStateError):
        assess_candidates(success, [stale])
    assert "PLAN-STATE" in {v.rule_id for v in validate_plan(success, stale).violations}


def test_tampered_plan_fields_rejected(success):
    p = plan_by_id(success, "P0015_R2_G1_T3")
    forged = Plan.model_validate({**p.model_dump(), "taxi_min": 1, "gate_in_min": 16})
    assert "PLAN-ROUTE" in {v.rule_id for v in validate_plan(success, forged).violations}
    off_grid = Plan.model_validate({**p.model_dump(), "landing_min": 16, "gate_in_min": 21, "service_end_min": 51})
    assert "PLAN-SLOT" in {v.rule_id for v in validate_plan(success, off_grid).violations}


# ---- scoring -----------------------------------------------------------------------
def test_raw_scaled_and_utilities_published_and_consistent(success):
    for a in assess_candidates(success, generate_candidates(success)):
        for role in ROLES:
            comp = a.components[role]
            total = 0.0
            for spec in success.operations.utility.role(role).metrics:
                raw = comp[f"raw.{spec.metric}"]
                assert comp[f"normalized.{spec.metric}"] == raw / spec.scale
                assert comp[f"weighted.{spec.metric}"] == spec.weight * (raw / spec.scale)
                total += comp[f"weighted.{spec.metric}"]
            assert comp["cost"] == total and a.utilities[role] == 1.0 - total


def test_costs_are_not_clipped(tmp_path):
    cfg = fx.config_dict("success")
    for spec in cfg["operations"]["utility"]["atc"]["metrics"]:
        spec["scale"] = 1.0
    s = scenario_from(cfg, tmp_path)
    worst = min(assess_candidates(s, generate_candidates(s)), key=lambda a: a.utilities["atc"])
    assert worst.components["atc"]["cost"] > 1.0 and worst.utilities["atc"] < 0.0


def test_known_metric_values_for_compromise(success):
    a = next(x for x in assess_candidates(success, generate_candidates(success)) if x.plan.plan_id == "P0015_R2_G1_T3")
    c = a.components
    assert c["atc"]["raw.landing_delay_min"] == 5 and c["atc"]["raw.spillover_proxy_min"] == 3  # dep @22: 10-7
    assert c["pilot"]["raw.incremental_fuel_kg"] == 30 * 5 + 15 * 5
    assert c["pilot"]["raw.connection_penalty_pax"] == 0  # gate-in 20 <= deadline 22
    assert c["ground"]["raw.gate_change_indicator"] == 0 and c["ground"]["raw.turnaround_delay_min"] == 0


# ---- fallback / bargaining ---------------------------------------------------------
def test_success_fixture_has_distinct_role_preferences_and_compromise(success):
    out = solve_scenario(success)
    best = out.role_best_ids
    assert len(set(best.values())) == 3
    assert out.compromise.plan_id not in set(best.values())
    assert out.fallback.plan_id not in set(best.values()) | {out.compromise.plan_id}
    assert out.reason == "compromise_found"


def test_no_feasible_fixture_returns_no_plans():
    out = solve_scenario(fx.load_fixture_scenario("no_feasible"))
    assert out.assessments == [] and out.fallback is None and out.compromise is None
    assert out.reason == "no_feasible_plan"
    assert all("FUEL-RESERVE" in {v.rule_id for v in r.violations} for r in out.rejected)
    assert choose_fallback([]) is None


def test_no_positive_gain_fixture_returns_none():
    s = fx.load_fixture_scenario("no_positive_gain")
    out = solve_scenario(s)
    assert out.fallback is not None and out.compromise is None and out.reason == "no_positive_bargain"
    # ground alone could gain, but a bargain needs ALL three gains > epsilon
    d = out.assessment(out.fallback.plan_id).utilities
    assert any(a.utilities["ground"] > d["ground"] for a in out.assessments)


def test_fallback_order(success):
    assessments = assess_candidates(success, generate_candidates(success))
    fb = choose_fallback(list(reversed(assessments)))
    assert fb.plan_id == "P0010_R1_G2_T2"
    assert fb == min((a.plan for a in assessments), key=lambda p: (p.landing_min, p.gate_in_min, p.plan_id))


def test_fallback_membership_and_epsilon_validation(success):
    assessments = assess_candidates(success, generate_candidates(success))
    with pytest.raises(UnknownPlanError):
        choose_compromise(assessments, "P0010_R2_G3_T4")  # infeasible id
    for eps in (float("nan"), float("inf"), -1e-9, True):
        with pytest.raises(ValueError):
            choose_compromise(assessments, "P0010_R1_G2_T2", epsilon=eps)
    with pytest.raises(InconsistentAssessmentsError):
        choose_compromise(assessments + assessments[:1], "P0010_R1_G2_T2")


def test_input_order_does_not_change_selection(success):
    assessments = assess_candidates(success, generate_candidates(success))
    rng = random.Random(3)
    for _ in range(5):
        shuffled = assessments[:]
        rng.shuffle(shuffled)
        assert choose_fallback(shuffled).plan_id == "P0010_R1_G2_T2"
        assert choose_compromise(shuffled, "P0010_R1_G2_T2").plan_id == "P0015_R2_G1_T3"


# Hand-built assessments with exactly representable utilities for tie/epsilon tests.
_TEMPLATE = fx.load_fixture_scenario("success")


def _assessment(landing: int, taxi: int, tag: str, u: tuple[float, float, float]) -> Assessment:
    plan = Plan(plan_id=f"P{landing:04d}_{tag}", scenario_id=_TEMPLATE.scenario_id, state_hash=_TEMPLATE.state_hash,
                landing_min=landing, runway_id="R1", gate_id="G1", taxi_route_id="T1", taxi_min=taxi,
                gate_in_min=landing + taxi, service_end_min=landing + taxi + 30)
    return Assessment(plan=plan, validation=ValidationReport(plan_id=plan.plan_id, feasible=True, violations=[], metrics={}),
                      utilities=dict(zip(ROLES, u)), components={r: {"cost": 1.0 - x} for r, x in zip(ROLES, u)})


FB = _assessment(10, 4, "FB", (0.5, 0.5, 0.5))


def test_zero_and_negative_gains_are_not_bargains():
    zero = _assessment(15, 4, "ZERO", (0.75, 0.5, 0.75))  # pilot gain exactly 0
    neg = _assessment(15, 5, "NEG", (0.875, 0.25, 0.875))
    assert choose_compromise([FB, zero, neg], FB.plan.plan_id) is None


def test_epsilon_is_a_strict_threshold():
    tiny = _assessment(15, 4, "TINY", (0.5 + 2**-20, 0.75, 0.75))
    assert choose_compromise([FB, tiny], FB.plan.plan_id, epsilon=2**-20) is None
    assert choose_compromise([FB, tiny], FB.plan.plan_id, epsilon=2**-21).plan_id == tiny.plan.plan_id


def test_deterministic_tie_breaks():
    # equal product 1/8 * 1/8 * 1/2 = 1/128 vs 1/4 * 1/4 * 1/8 = 1/128; larger minimum gain wins
    a = _assessment(30, 4, "A", (0.625, 0.625, 1.0))  # gains 1/8,1/8,1/2 -> min 1/8
    b = _assessment(15, 4, "B", (0.75, 0.75, 0.625))  # gains 1/4,1/4,1/8 -> min 1/8 ... equal min
    c = _assessment(20, 4, "C", (0.75, 0.625, 0.75))  # gains 1/4,1/8,1/4 -> product 1/128, min 1/8
    assert gains(a, FB.utilities)["ground"] == Fraction(1, 2)
    pool = [FB, a, b, c]
    # a: cost sum 0.75; b,c: 0.875 -> a wins on smaller summed cost despite later landing
    assert choose_compromise(pool, FB.plan.plan_id).plan_id == a.plan.plan_id
    # b vs c: equal product, min gain and cost sum -> earlier landing (b @15)
    assert choose_compromise([FB, b, c], FB.plan.plan_id).plan_id == b.plan.plan_id
    # same landing: earlier gate-in, then plan_id
    d = _assessment(15, 3, "D", (0.75, 0.625, 0.75))
    assert choose_compromise([FB, b, d], FB.plan.plan_id).plan_id == d.plan.plan_id
    e1, e2 = _assessment(15, 4, "E1", (0.75, 0.625, 0.75)), _assessment(15, 4, "E2", (0.75, 0.625, 0.75))
    assert choose_compromise([FB, e2, e1], FB.plan.plan_id).plan_id == e1.plan.plan_id
    # larger minimum gain beats smaller with equal product: gains (1/4,1/4,1/4) vs (1/2,1/2,1/16)
    m1 = _assessment(40, 4, "M1", (0.75, 0.75, 0.75))
    m2 = _assessment(15, 4, "M2", (1.0, 1.0, 0.5625))
    assert choose_compromise([FB, m2, m1], FB.plan.plan_id).plan_id == m1.plan.plan_id


def test_compromise_is_pareto_optimal(success):
    assessments = assess_candidates(success, generate_candidates(success))
    chosen = next(a for a in assessments if a.plan.plan_id == choose_compromise(assessments, "P0010_R1_G2_T2").plan_id)
    assert not any(dominates(b, chosen) for b in assessments)
    assert chosen in pareto_front(assessments)
    assert all(not dominates(a, a) for a in assessments)


# ---- oracle agreement --------------------------------------------------------------
@pytest.mark.parametrize("name", list(fx.SCENARIO_FILES))
def test_oracle_agrees_on_fixtures(name):
    s = fx.load_fixture_scenario(name)
    out, ref = solve_scenario(s), oracle(s)
    assert ref.fallback_id == (out.fallback.plan_id if out.fallback else None)
    assert ref.compromise_id == (out.compromise.plan_id if out.compromise else None)


def _perturb(rng: random.Random, mild: bool) -> dict:
    """Small random variants of the success config.

    mild: stays near the demo regime (scales x0.7-1.4, contract weights, +-2 min shifts);
    wild: random weights/scales/deadlines (mostly no bargain).
    """
    cfg = fx.config_dict("success")
    ops = cfg["operations"]
    shift = 2 if mild else 4
    for m in ops["runway_movements"]:
        m["time_min"] += rng.randint(-shift, shift)
    ops["turbulence"]["runway_exposure"][0]["exposure_index"] = float(rng.choice([2, 3, 4] if mild else [0, 1, 3]))
    for g in ops["connections"]:
        g["deadline_gate_in_min"] += rng.randint(-3, 3) if mild else rng.randint(-10, 10)
    ops["crew"][0]["regular_end_min"] = rng.randint(35, 45) if mild else rng.randint(30, 60)
    for role in ROLES:
        specs = ops["utility"][role]["metrics"]
        if mild:
            for spec in specs:
                spec["scale"] = spec["scale"] * rng.choice([0.75, 1.0, 1.25])
        else:
            raw = [rng.randint(1, 8) for _ in specs]
            for spec, r in zip(specs, raw):
                spec["weight"] = r / sum(raw)
                spec["scale"] = float(rng.choice([1, 5, 10, 30, 100, 500]))
            specs[-1]["weight"] = 1.0 - math.fsum(x["weight"] for x in specs[:-1])
    return cfg


def test_oracle_agrees_on_randomized_small_scenarios(tmp_path):
    rng = random.Random(2024)
    outcomes = collections.Counter()
    for i in range(60):
        s = scenario_from(_perturb(rng, mild=i % 2 == 0), tmp_path, f"r{i}.json")
        out, ref = solve_scenario(s), oracle(s)
        assert ref.feasible_ids == tuple(a.plan.plan_id for a in out.assessments), i
        assert ref.fallback_id == (out.fallback.plan_id if out.fallback else None), i
        assert ref.compromise_id == (out.compromise.plan_id if out.compromise else None), i
        if out.fallback:
            assert bool(eligible(out.assessments, out.fallback.plan_id)) == (out.compromise is not None), i
        outcomes[out.reason] += 1
    assert outcomes["compromise_found"] >= 5 and outcomes["no_positive_bargain"] >= 5, outcomes


def test_example_assessments_fixture_matches_engine(success):
    assert fx.load_example_assessments() == assess_candidates(success, generate_candidates(success))


def test_documented_worked_example_is_current_engine_output(success):
    from airspace.engine.report import worked_example

    doc = (fx.REPO_ROOT / "docs" / "data_and_model.md").read_text()
    begin, end = "<!-- BEGIN GENERATED WORKED EXAMPLE -->\n", "<!-- END GENERATED WORKED EXAMPLE -->"
    assert doc[doc.index(begin) + len(begin): doc.index(end)] == worked_example(success)


def test_validation_metrics_are_role_prefixed_and_rule_ids_published(success):
    from airspace.contracts import METRIC_ROLE_PREFIXES, RULE_IDS

    assert tuple(RULES) == RULE_IDS
    prefixes = tuple(METRIC_ROLE_PREFIXES.values())
    for plan in generate_candidates(success):
        report = validate_plan(success, plan)
        assert all(key.startswith(prefixes) for key in report.metrics), report.metrics


# ---- provenance-only evidence: hist:* and derived:clock.* never act as operational facts ----
PROVENANCE_ONLY = ("hist:", "derived:clock.")


def test_no_rule_cites_historical_or_clock_provenance():
    for rule in RULES.values():
        assert not any(e.startswith(PROVENANCE_ONLY) for e in rule.evidence_ids), rule.rule_id


@pytest.mark.parametrize("name", list(fx.SCENARIO_FILES))
def test_violations_never_cite_provenance_only_evidence(name):
    s = fx.load_fixture_scenario(name)
    rejected = evaluate_candidates(s, generate_candidates(s)).rejected
    cited = {e for r in rejected for v in r.violations for e in v.evidence_ids}
    assert cited and not any(e.startswith(PROVENANCE_ONLY) for e in cited)


def test_engine_source_never_reads_historical_fields():
    import ast
    import airspace.engine as engine_pkg

    forbidden = {"historical_row", "historical_ref", "provenance"}
    for path in Path(engine_pkg.__path__[0]).glob("*.py"):
        tree = ast.parse(path.read_text())
        names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names |= {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert not (names & forbidden), (path.name, names & forbidden)


def test_historical_and_clock_evidence_is_labelled_provenance(success):
    hist = [e for e in success.provenance if e.evidence_id.startswith(PROVENANCE_ONLY)]
    assert {e.evidence_id for e in hist} >= {"hist:row", "derived:clock.SCHEDULED_ARRIVAL"}
    for e in hist:
        assert e.kind == ("historical" if e.evidence_id.startswith("hist:") else "derived")
        assert e.field_path.startswith(("historical_row", "historical_ref"))
    assert "not a decision input" in next(e.note for e in hist if e.evidence_id == "hist:row")
    assert all("display only" in e.note for e in hist if e.evidence_id.startswith("derived:clock."))


# ---- challenge-calibrated suite + fully independent oracle -------------------------
from airspace.data.scenario_suite import ScenarioSpec, build_suite, build_suite_scenario, suite_specs  # noqa: E402
from airspace.engine.oracle import independent_oracle  # noqa: E402


@pytest.fixture(scope="module")
def suite():
    return build_suite()


def test_suite_is_deterministic_distinct_and_labelled(suite):
    assert len(suite) == 40
    again = build_suite()
    assert [s.state_hash for _, s in suite] == [s.state_hash for _, s in again]
    assert len({s.state_hash for _, s in suite}) == 40 and len({s.scenario_id for _, s in suite}) == 40
    for spec, s in suite:
        assert s.source_mode == "historical_anchor" and s.historical_ref["row_index"] in (70, 171, 682, 2845)
        notes = " ".join(e.note for e in s.provenance)
        assert "challenge workbook" in notes and "SIMULATED" in notes


def test_suite_uses_workbook_values(suite):
    from airspace.data.scenario_suite import ChallengeAssumptions

    spec, s = next((sp, x) for sp, x in suite if sp.scenario_id == "suite-mia-baseline")
    fuel = s.operations.fuel
    assert fuel.airborne_burn_kg_per_min == 40.0 and fuel.min_reserve_kg == 1200.0  # 2400 kg/h, 30 min
    assert fuel.snapshot_fuel_kg == 1800.0  # 45 min incl. reserve (labelled assumption)
    assert s.operations.service.service_duration_min == 35 and len(s.operations.runways) == 3  # MIA medium hub
    assert len(s.operations.gates) == 4  # assigned + 3 spare gates
    alt = build_suite_scenario(ScenarioSpec(**{**spec.__dict__, "assumptions": ChallengeAssumptions(spare_fuel_includes_final_reserve=False)}))
    assert alt.operations.fuel.snapshot_fuel_kg == 3000.0


def test_suite_covers_required_outcome_categories(suite):
    reasons = {solve_scenario(s).reason for _, s in suite}
    assert reasons == {"compromise_found", "no_positive_bargain", "no_feasible_plan"}
    cats = {sp.category for sp in suite_specs()}
    assert {"runway_closure", "dense_traffic", "gate_restricted", "crew_restricted", "tight_fuel", "large_delay"} <= cats


def test_independent_oracle_agrees_rule_by_rule(suite):
    scenarios = [fx.load_fixture_scenario(n) for n in fx.SCENARIO_FILES] + [s for _, s in suite]
    checked = 0
    for s in scenarios:
        out, ref = solve_scenario(s), independent_oracle(s)
        for p in generate_candidates(s):
            assert {v.rule_id for v in validate_plan(s, p).violations} == ref.plans[p.plan_id]["failed"], p.plan_id
            checked += 1
        assert {a.plan.plan_id for a in out.assessments} == set(ref.feasible_ids)
        assert ref.fallback_id == (out.fallback.plan_id if out.fallback else None)
        assert ref.compromise_id == (out.compromise.plan_id if out.compromise else None)
        for a in out.assessments:
            for r in ROLES:
                assert abs(Fraction(a.utilities[r]) - ref.utilities[a.plan.plan_id][r]) < Fraction(1, 10**12)
        if out.compromise:
            assert out.compromise.plan_id in ref.pareto_ids
    assert checked > 2500


def test_single_feasible_plan_is_the_fallback_and_no_bargain(tmp_path):
    cfg = fx.config_dict("success")
    cfg["planning_horizon_min"] = 5  # one slot (minute 10): R2 fails lead separation, G1 occupied, G4 heavy-only
    s = scenario_from(cfg, tmp_path)
    out, ref = solve_scenario(s), independent_oracle(s)
    assert [a.plan.plan_id for a in out.assessments] == ["P0010_R1_G2_T2"] == list(ref.feasible_ids)
    assert out.fallback.plan_id == "P0010_R1_G2_T2" and out.compromise is None and out.reason == "no_positive_bargain"
    assert ref.pareto_ids == ("P0010_R1_G2_T2",)
