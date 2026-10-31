"""Engine tests: candidates, hard constraints, scoring, fallback, bargaining, oracle.

Fixture scenarios are SYNTHETIC operating states (the success scenario is anchored
to a genuine row for context only).
"""

from __future__ import annotations

import collections
from collections import Counter
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
    path.write_text(json.dumps(cfg), encoding="utf-8")
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

    doc = (fx.REPO_ROOT / "docs" / "data_and_model.md").read_text(encoding="utf-8")
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
        tree = ast.parse(path.read_text(encoding="utf-8"))
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
from airspace.engine import sensitivity as sens  # noqa: E402
from airspace.contracts import CONTRACT_DEFAULT_WEIGHTS, ROLE_METRICS  # noqa: E402


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


def test_independent_oracle_under_reweighting(suite):
    fams = sens.weight_families(seed=7, n_dirichlet=3)
    configs = fams["simplex_vertices"][::5] + fams["dirichlet_uniform_k1"]
    for _, base in suite[:10]:
        for w in configs:
            s = sens.reweighted(base, w)
            out, ref = solve_scenario(s), independent_oracle(s)
            assert ref.compromise_id == (out.compromise.plan_id if out.compromise else None)
            assert ref.fallback_id == (out.fallback.plan_id if out.fallback else None)


def test_weight_families_lie_on_the_simplex():
    for fam, configs in sens.weight_families(seed=1, n_dirichlet=10).items():
        for w in configs:
            for role in ROLES:
                vals = list(w[role].values())
                assert all(v >= 0 for v in vals) and abs(sum(vals) - 1) <= 1e-12, (fam, role)


def test_reweighting_reseals_and_recomputes_disagreement(success):
    vertex = {r: {m: float(i == 0) for i, m in enumerate(ROLE_METRICS[r])} for r in ROLES}
    s2 = sens.reweighted(success, vertex)
    assert s2.state_hash != success.state_hash
    a1 = solve_scenario(success)
    a2 = solve_scenario(s2)
    assert a1.fallback.plan_id == a2.fallback.plan_id  # fallback is weight-independent ...
    d1, d2 = a1.assessment(a1.fallback.plan_id).utilities, a2.assessment(a2.fallback.plan_id).utilities
    assert d1 != d2  # ... but the disagreement utilities are recomputed per weight vector


def test_common_positive_affine_transform_preserves_selection_without_epsilon(suite):
    for _, s in [(None, fx.load_fixture_scenario("success"))] + suite:
        ref = independent_oracle(s)
        if ref.fallback_id is None:
            continue
        order = {p: (ref.plans[p]["landing"], ref.plans[p]["gate_in"], p) for p in ref.feasible_ids}
        u = {p: ref.utilities[p] for p in ref.feasible_ids}
        base = sens.select_exact(u, u[ref.fallback_id], order, Fraction(0))
        for a, b in ((Fraction(3), Fraction(-2)), (Fraction(1, 7), Fraction(5))):
            u2 = {p: {r: a * v[r] + b for r in ROLES} for p, v in u.items()}
            c2 = {p: {r: 1 - v[r] for r in ROLES} for p, v in u2.items()}
            assert sens.select_exact(u2, u2[ref.fallback_id], order, Fraction(0), c2) == base


def test_select_exact_matches_engine_at_contract_weights(suite):
    for _, s in suite:
        ref = independent_oracle(s)
        if ref.fallback_id is None:
            continue
        order = {p: (ref.plans[p]["landing"], ref.plans[p]["gate_in"], p) for p in ref.feasible_ids}
        u = {p: ref.utilities[p] for p in ref.feasible_ids}
        assert sens.select_exact(u, u[ref.fallback_id], order, Fraction(1e-9)) == ref.compromise_id


def test_single_feasible_plan_is_the_fallback_and_no_bargain(tmp_path):
    cfg = fx.config_dict("success")
    cfg["planning_horizon_min"] = 5  # one slot (minute 10): R2 fails lead separation, G1 occupied, G4 heavy-only
    s = scenario_from(cfg, tmp_path)
    out, ref = solve_scenario(s), independent_oracle(s)
    assert [a.plan.plan_id for a in out.assessments] == ["P0010_R1_G2_T2"] == list(ref.feasible_ids)
    assert out.fallback.plan_id == "P0010_R1_G2_T2" and out.compromise is None and out.reason == "no_positive_bargain"
    assert ref.pareto_ids == ("P0010_R1_G2_T2",)


# ---- research variants: metamorphic / property tests ----------------------------------
from airspace.data.scenario_suite import PARTITION_BY_K, operations_fingerprint, stratified_specs  # noqa: E402
from airspace.engine.research import selectors as rsel  # noqa: E402
from airspace.engine.research.diagnosis import diagnose  # noqa: E402
from airspace.engine.research.grid import event_candidates, refine, event_times, signature_starts  # noqa: E402


def _suite_by_id(suite, sid):
    return next(s for sp, s in suite if sp.scenario_id == sid)


def test_candidate_order_does_not_change_production_selection(suite):
    rng = random.Random(11)
    for _, s in [(None, fx.load_fixture_scenario("success"))] + suite[:12]:
        plans = generate_candidates(s)
        base = evaluate_candidates(s, plans).assessments
        shuffled = plans[:]
        rng.shuffle(shuffled)
        other = assess_candidates(s, shuffled)
        fb1, fb2 = choose_fallback(base), choose_fallback(other)
        assert fb1 == fb2
        if fb1:
            assert choose_compromise(base, fb1.plan_id) == choose_compromise(other, fb2.plan_id)


def test_adding_a_strictly_dominated_candidate_never_changes_the_choice():
    s = fx.load_fixture_scenario("success")
    assessments = assess_candidates(s, generate_candidates(s))
    fb = choose_fallback(assessments)
    best = choose_compromise(assessments, fb.plan_id)
    worst = {r: min(a.utilities[r] for a in assessments) - 0.25 for r in ROLES}
    plan = Plan(plan_id="P0049_R9_G9_T9", scenario_id=s.scenario_id, state_hash=s.state_hash, landing_min=49,
                runway_id="R9", gate_id="G9", taxi_route_id="T9", taxi_min=1, gate_in_min=50, service_end_min=80)
    dominated = Assessment(plan=plan, validation=ValidationReport(plan_id=plan.plan_id, feasible=True, violations=[], metrics={}),
                           utilities=worst, components={r: {"cost": 1.0 - worst[r]} for r in ROLES})
    aug = assessments + [dominated]
    assert choose_fallback(aug) == fb and choose_compromise(aug, fb.plan_id) == best


def test_weights_never_change_physical_feasibility(suite):
    fams = sens.weight_families(seed=3, n_dirichlet=4)
    for _, s in suite[:8]:
        ref = independent_oracle(s)
        for w in fams["dirichlet_uniform_k1"] + fams["simplex_vertices"][:3]:
            alt = independent_oracle(sens.reweighted(s, w))
            assert {p: v["failed"] for p, v in alt.plans.items()} == {p: v["failed"] for p, v in ref.plans.items()}


def test_event_grid_is_an_exact_reduction_of_the_integer_minute_grid(suite):
    for _, s in [(None, fx.load_fixture_scenario("success"))] + suite[::6]:
        s1 = refine(s, 1)
        ref1 = independent_oracle(s1)
        ev = {(r.route_id, L) for r in s1.operations.taxi_routes for L in event_times(s1, r)}
        assert signature_starts(s1, ref1) <= ev
        full = assess_candidates(s1, generate_candidates(s1))
        sub = assess_candidates(s1, event_candidates(s1))
        fb_full, fb_sub = choose_fallback(full), choose_fallback(sub)
        assert fb_full == fb_sub
        if fb_full:
            assert choose_compromise(full, fb_full.plan_id) == choose_compromise(sub, fb_sub.plan_id)


def test_research_strict_nash_equals_production(suite):
    for _, s in [(None, fx.load_fixture_scenario(n)) for n in fx.SCENARIO_FILES] + suite:
        out = solve_scenario(s)
        assert rsel.strict_nash(rsel.Table.from_oracle(independent_oracle(s))) == (out.compromise.plan_id if out.compromise else None)


def test_scaled_nash_is_invariant_to_role_specific_affine_maps_and_production_is_not():
    order = {"X": (20, 25, "X"), "Y": (25, 30, "Y"), "F": (10, 15, "F")}
    u = {"F": {"atc": Fraction(0), "pilot": Fraction(0), "ground": Fraction(0)},
         "X": {"atc": Fraction(1, 2), "pilot": Fraction(1, 2), "ground": Fraction(1, 16)},
         "Y": {"atc": Fraction(1, 4), "pilot": Fraction(1, 4), "ground": Fraction(1, 4)}}
    t = rsel.Table(("F", "X", "Y"), u, order, "F", {})
    a = {"atc": Fraction(1), "pilot": Fraction(1), "ground": Fraction(8)}
    t2 = rsel.Table(t.ids, {p: {r: a[r] * v[r] + 3 for r in ROLES} for p, v in u.items()}, order, "F", {})
    assert rsel.strict_nash(t) == "Y" and rsel.strict_nash(t2) == "X"  # production rule flips (min-gain tie-break)
    assert rsel.strict_nash_scaled(t) == rsel.strict_nash_scaled(t2)  # normalised tie-break does not
    tiny = rsel.Table(("F", "X"), {"F": u["F"], "X": {"atc": Fraction(5, 10**10), "pilot": Fraction(1), "ground": Fraction(1)}}, order, "F", {})
    big = rsel.Table(tiny.ids, {p: {r: (10 if r == "atc" else 1) * v[r] for r in ROLES} for p, v in tiny.u.items()}, order, "F", {})
    assert rsel.strict_nash(tiny) is None and rsel.strict_nash(big) == "X"  # absolute eps is not scale-invariant
    assert rsel.strict_nash_scaled(tiny) == rsel.strict_nash_scaled(big) == "X"


def test_weak_ir_handles_the_weakly_dominated_fallback(suite):
    s = _suite_by_id(suite, "suite-den-tight_fuel")
    t = rsel.Table.from_oracle(independent_oracle(s))
    assert rsel.strict_nash(t) is None and not rsel.pareto_efficient(t, t.fallback)
    for name in ("B_weak_ir_lex", "D1_kalai_smorodinsky", "D2_leximin_normalised", "E2_utilitarian_ir"):
        pick = rsel.recommended(t, name)
        assert pick != t.fallback and rsel.pareto_efficient(t, pick) and rsel.weakly_improves(t, pick, t.fallback), name


def test_every_research_selector_returns_a_feasible_plan(suite):
    for _, s in suite:
        t = rsel.Table.from_oracle(independent_oracle(s))
        for name in rsel.SELECTORS:
            pick = rsel.recommended(t, name)
            assert (pick is None) == (t.fallback is None) and (pick is None or pick in t.ids), name


def test_decimal_tie_appears_as_binary_noise_and_eps_absorbs_it(suite):
    s = _suite_by_id(suite, "suite-mia-large_delay")
    tf = rsel.Table.from_oracle(independent_oracle(s))
    wd = {r: {m: Fraction(str(w)) for m, w in CONTRACT_DEFAULT_WEIGHTS[r].items()} for r in ROLES}
    td = rsel.Table.from_oracle(independent_oracle(s, weights=wd))
    gf, gd = tf.gains("P0030_R1_G4_T14")["atc"], td.gains("P0030_R1_G4_T14")["atc"]
    assert gd == 0 and 0 < gf < Fraction(1, 10**16)  # +10 min delay exactly offset by -5 spillover-min
    assert gf <= rsel.EPS  # production eps treats it as no gain


def test_diagnosis_labels_known_cases(suite):
    assert diagnose(fx.load_fixture_scenario("success"))["labels"] == ["POSITIVE_BARGAIN"]
    assert diagnose(fx.load_fixture_scenario("no_feasible"))["labels"] == ["NO_FEASIBLE"]
    assert diagnose(_suite_by_id(suite, "suite-den-tight_fuel"))["labels"][:2] == ["WEAK_ONLY", "CONFLICT"]
    npg = diagnose(fx.load_fixture_scenario("no_positive_gain"))["labels"]
    assert npg[0] == "FALLBACK_EFFICIENT" and "ROLE_CANNOT_GAIN:atc" in npg


def test_stratified_suite_partition_is_fixed_distinct_and_disjoint(suite):
    specs = stratified_specs()
    assert len(specs) == 160
    assert Counter(sp.scenario_id.split("-")[1] for sp in specs) == {"dev": 64, "tuning": 32, "holdout": 64}
    assert {k: PARTITION_BY_K[k] for k in range(5)} == {0: "dev", 1: "dev", 2: "tuning", 3: "holdout", 4: "holdout"}
    fps = [operations_fingerprint(build_suite_scenario(sp)) for sp in specs[::4]]
    assert len(set(fps)) == len(fps)
    assert not set(fps) & {operations_fingerprint(s) for _, s in suite}
    assert [sp.seed for sp in stratified_specs()] == [sp.seed for sp in specs]  # reproducible


# ---- one-minute candidate generation (v2 profile) and 5-minute backward compatibility ----
import hashlib  # noqa: E402

from airspace.contracts import SimulationConfig  # noqa: E402

from airspace.data.scenario_suite import (  # noqa: E402
    BASELINE_SLOT_STEP_MIN, PRODUCTION_SLOT_STEP_MIN, at_resolution, final_evaluation_specs, suite_specs as _suite_specs,
)

DIGEST_SUITE40_5MIN = "5243aa5aa6f60edfe88c874fd23e0682f1b9011c0fae0ac152f68350d7ef0e63"
DIGEST_STRATIFIED160_5MIN = "75d6c297e6e56778353a8db5ad8624eca1335e527946586ab56555159a899ae9"


@pytest.fixture(scope="module")
def success_v2() -> Scenario:
    return fx.load_fixture_scenario("success", "v2")


def v2_from(cfg: dict, tmp_path: Path, name: str = "v2.json") -> Scenario:
    return scenario_from(cfg, tmp_path, name)


def test_five_minute_baselines_are_bit_for_bit_unchanged(suite):
    assert fx.DEFAULT_FIXTURE_VERSION == "v1" and fx.load_fixture_scenario("success").slot_step_min == 5
    assert hashlib.sha256("".join(s.state_hash for _, s in suite).encode()).hexdigest() == DIGEST_SUITE40_5MIN
    strat = "".join(build_suite_scenario(sp).state_hash for sp in stratified_specs())
    assert hashlib.sha256(strat.encode()).hexdigest() == DIGEST_STRATIFIED160_5MIN
    assert all(sp.assumptions.slot_step_min == BASELINE_SLOT_STEP_MIN for sp in _suite_specs() + stratified_specs())


def test_production_scenario_generation_defaults_to_one_minute():
    from airspace.data.scenario_suite import ChallengeAssumptions

    assert ChallengeAssumptions().slot_step_min == PRODUCTION_SLOT_STEP_MIN == 1
    assert fx.PREFERRED_FIXTURE_VERSION == "v2"


def test_v2_profile_differs_from_v1_only_in_resolution_and_labels():
    for name in fx.BUILDERS:
        v1, v2 = fx.config_dict(name, "v1"), fx.config_dict(name, "v2")
        assert v2["operations"] == v1["operations"] and v2["earliest_landing_min"] == v1["earliest_landing_min"]
        assert v2["planning_horizon_min"] == v1["planning_horizon_min"]
        assert (v1["slot_step_min"], v2["slot_step_min"]) == (5, 1)
        assert v2["scenario_id"].endswith("-v2") and v2["config_version"].endswith("-v2")
        assert fx.build_config(name, "v2") == SimulationConfig.model_validate_json(fx.config_path(name, "v2").read_text(encoding="utf-8"))


def test_one_minute_slots_cover_every_integer_minute_and_route(success_v2):
    plans = generate_candidates(success_v2)
    assert list(success_v2.slots) == list(range(10, 50))
    routes = success_v2.operations.taxi_routes
    assert len(plans) == 40 * len(routes) == 240
    assert {p.landing_min for p in plans} == set(range(10, 50))


def test_generator_equals_exhaustive_independent_enumeration(success_v2):
    ops = success_v2.operations
    expected = set()
    for L in range(success_v2.earliest_landing_min, success_v2.earliest_landing_min + success_v2.planning_horizon_min):
        for rw in ops.runways:
            for g in ops.gates:
                for r in ops.taxi_routes:
                    if (r.runway_id, r.gate_id) == (rw.runway_id, g.gate_id):
                        expected.add((L, rw.runway_id, g.gate_id, r.route_id, L + r.taxi_min, L + r.taxi_min + ops.service.service_duration_min))
    got = {(p.landing_min, p.runway_id, p.gate_id, p.taxi_route_id, p.gate_in_min, p.service_end_min) for p in generate_candidates(success_v2)}
    assert got == expected  # nothing missing, nothing extra
    unconnected = {(rw.runway_id, g.gate_id) for rw in ops.runways for g in ops.gates} - {(r.runway_id, r.gate_id) for r in ops.taxi_routes}
    assert unconnected and not {(p[1], p[2]) for p in got} & unconnected


def test_one_minute_ids_unique_ordered_and_repeatable(success_v2):
    a, b = generate_candidates(success_v2), generate_candidates(success_v2)
    assert a == b and len({p.plan_id for p in a}) == len(a)
    keys = [(p.landing_min, p.runway_id, p.gate_id, p.taxi_route_id) for p in a]
    assert keys == sorted(keys)
    assert solve_scenario(success_v2).summary() == solve_scenario(success_v2).summary()


def test_v1_and_v2_share_identical_plans_on_five_minute_minutes(success, success_v2):
    v1 = {a.plan.plan_id: a for a in assess_candidates(success, generate_candidates(success))}
    v2 = {a.plan.plan_id: a for a in assess_candidates(success_v2, generate_candidates(success_v2))}
    assert set(v1) <= set(v2)  # every v1 feasible plan id exists, unchanged, in v2
    for pid, a in v1.items():
        assert a.components == v2[pid].components  # identical raw metrics and utilities


def test_horizon_boundaries_one_minute(success_v2):
    plans = generate_candidates(success_v2)
    assert min(p.landing_min for p in plans) == 10 and max(p.landing_min for p in plans) == 49
    edge = next(p for p in plans if p.plan_id == "P0049_R1_G2_T2")
    beyond = Plan.model_validate({**edge.model_dump(), "plan_id": "P0050_R1_G2_T2", "landing_min": 50,
                                  "gate_in_min": 54, "service_end_min": 84})
    assert "PLAN-SLOT" in {v.rule_id for v in validate_plan(success_v2, beyond).violations}


def test_narrow_runway_window_one_minute(tmp_path):
    cfg = fx.config_dict("success", "v2")
    cfg["operations"]["runways"][0]["open_windows"] = [{"start_min": 12, "end_min": 15}]  # R1, occupancy 2 min
    s = v2_from(cfg, tmp_path)
    ok = {p.landing_min for p in generate_candidates(s) if p.runway_id == "R1" and "RWY-OPEN" not in
          {v.rule_id for v in validate_plan(s, p).violations}}
    assert ok == {12, 13}  # [L, L+2) inside [12, 15)


def test_fuel_limited_window_matches_exact_cutoff_per_route(success_v2):
    f = success_v2.operations.fuel
    for route in success_v2.operations.taxi_routes:
        slack = (Fraction(f.snapshot_fuel_kg) - Fraction(f.approach_burn_kg) - Fraction(f.taxi_burn_kg_per_min) * route.taxi_min
                 - Fraction(f.min_reserve_kg)) / Fraction(f.airborne_burn_kg_per_min)
        cutoff = success_v2.earliest_landing_min + math.floor(slack)  # last minute with reserve >= minimum
        for p in generate_candidates(success_v2):
            if p.taxi_route_id == route.route_id:
                fails = "FUEL-RESERVE" in {v.rule_id for v in validate_plan(success_v2, p).violations}
                assert fails == (p.landing_min > cutoff), (route.route_id, p.landing_min)


def test_gate_conflict_and_separation_boundaries_one_minute(success_v2):
    def rules(pid):
        return rule_ids(success_v2, pid)
    # G1 existing [0,15) + 5 buffer: T3 (taxi 5) gate-in 20 at L=15 touches; L=14 overlaps
    assert "GATE-OCCUPANCY" not in rules("P0015_R2_G1_T3") and "GATE-OCCUPANCY" in rules("P0014_R2_G1_T3")
    # R2 heavy arrival @8, heavy->medium 3: L=11 exactly separated, L=10 not
    assert "RWY-SEP-LEAD" not in rules("P0011_R2_G3_T4") and "RWY-SEP-LEAD" in rules("P0010_R2_G3_T4")
    # R1 heavy departure @12: follow needs 2 (L<=10 ok, L=11 fails); lead needs 3 (L=15 ok, L=14 fails)
    assert "RWY-SEP-FOLLOW" not in rules("P0010_R1_G2_T2") and "RWY-SEP-FOLLOW" in rules("P0011_R1_G2_T2")
    assert "RWY-SEP-LEAD" in rules("P0014_R1_G2_T2") and "RWY-SEP-LEAD" not in rules("P0015_R1_G2_T2")


def test_unique_and_no_feasible_plan_one_minute(tmp_path):
    cfg = fx.config_dict("success", "v2")
    cfg["planning_horizon_min"] = 1
    out = solve_scenario(v2_from(cfg, tmp_path))
    assert [a.plan.plan_id for a in out.assessments] == ["P0010_R1_G2_T2"] and out.reason == "no_positive_bargain"
    none = solve_scenario(fx.load_fixture_scenario("no_feasible", "v2"))
    assert none.candidate_count == 240 and none.assessments == [] and none.reason == "no_feasible_plan"


def test_candidate_cap_at_one_minute_is_explicit(tmp_path):
    cfg = fx.config_dict("success", "v2")
    cfg["planning_horizon_min"] = 333  # 333 x 6 routes = 1998 <= 2000
    assert len(generate_candidates(v2_from(cfg, tmp_path, "a.json"))) == 1998
    cfg["planning_horizon_min"] = 334  # 2004 > 2000: error, never truncation
    with pytest.raises(CandidateCapExceededError, match="2004"):
        generate_candidates(v2_from(cfg, tmp_path, "b.json"))


def test_v2_provenance_and_hash_stable(tmp_path, success_v2):
    rng = random.Random(5)
    cfg = fx.config_dict("success", "v2")
    def shuffled(obj):
        if isinstance(obj, dict):
            items = list(obj.items())
            rng.shuffle(items)
            return {k: shuffled(v) for k, v in items}
        return [shuffled(v) for v in obj] if isinstance(obj, list) else obj

    for i in range(3):
        p = tmp_path / f"s{i}.json"
        p.write_text(json.dumps(shuffled(cfg)), encoding="utf-8")
        assert build_scenario(fx.load_anchor_row(), p).state_hash == success_v2.state_hash
    assert success_v2.source_mode == "historical_anchor"
    assert any("1-minute landing resolution" in e.note for e in success_v2.provenance)


def test_weights_never_change_feasibility_at_one_minute(success_v2):
    ref = independent_oracle(success_v2)
    for w in sens.weight_families(seed=9, n_dirichlet=3)["dirichlet_uniform_k1"]:
        alt = independent_oracle(sens.reweighted(success_v2, w))
        assert {p: v["failed"] for p, v in alt.plans.items()} == {p: v["failed"] for p, v in ref.plans.items()}


def test_oracle_agrees_and_selection_feasible_at_one_minute(suite):
    scen = [fx.load_fixture_scenario(n, "v2") for n in fx.SCENARIO_FILES]
    scen += [build_suite_scenario(at_resolution(sp, 1)) for sp in _suite_specs()[::4]]
    for s in scen:
        assert s.slot_step_min == 1
        out, ref = solve_scenario(s), independent_oracle(s)
        assert {a.plan.plan_id for a in out.assessments} == set(ref.feasible_ids)
        assert ref.fallback_id == (out.fallback.plan_id if out.fallback else None)
        assert ref.compromise_id == (out.compromise.plan_id if out.compromise else None)
        chosen = out.compromise or out.fallback
        if chosen:
            assert validate_plan(s, chosen).feasible


def test_v2_example_assessments_match_engine(success_v2):
    assert fx.load_example_assessments("v2") == assess_candidates(success_v2, generate_candidates(success_v2))


def test_final_evaluation_set_is_new_and_disjoint():
    fin = final_evaluation_specs()
    fps = {operations_fingerprint(build_suite_scenario(sp)) for sp in fin}
    prior = {operations_fingerprint(build_suite_scenario(sp)) for sp in stratified_specs()[::3] + _suite_specs()}
    assert len(fin) == len(fps) == 64 and not fps & prior
