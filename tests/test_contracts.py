"""Contract-level tests: strict validation, invariants, hashing, drift of committed artefacts."""

from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

from airspace.contracts import (
    PUBLIC_MODELS,
    AgentMessage,
    Assessment,
    OperationalConfig,
    Plan,
    RunResult,
    Scenario,
    SimulationConfig,
    StaleStateError,
    ValidationReport,
    Violation,
    canonical_hash,
    ensure_sealed,
)
from airspace.data import fixtures as fx


def _ops() -> dict:
    return fx.config_dict("success")["operations"]


def _validate_ops(ops: dict) -> OperationalConfig:
    return OperationalConfig.model_validate_json(json.dumps(ops))


# ---- strict numerics / forbidden extras ---------------------------------
@pytest.mark.parametrize("bad", ["3400", True, None])
def test_safety_numbers_reject_loose_coercion(bad):
    ops = _ops()
    ops["fuel"]["min_reserve_kg"] = bad
    with pytest.raises(ValidationError):
        OperationalConfig.model_validate(ops)


def test_nan_and_infinite_fuel_rejected_from_json():
    text = json.dumps(_ops()).replace('"min_reserve_kg": 3400.0', '"min_reserve_kg": NaN')
    with pytest.raises(ValidationError):
        OperationalConfig.model_validate_json(text)


def test_extra_fields_forbidden():
    ops = _ops()
    ops["fuel"]["llm_suggested_reserve"] = 1.0
    with pytest.raises(ValidationError, match="extra"):
        _validate_ops(ops)


def test_missing_safety_section_is_an_error_not_a_default():
    ops = _ops()
    del ops["separation"]
    with pytest.raises(ValidationError, match="separation"):
        _validate_ops(ops)


def test_incomplete_separation_matrix_rejected():
    ops = _ops()
    ops["separation"]["rules"] = [
        r for r in ops["separation"]["rules"] if (r["leader_category"], r["follower_category"]) != ("heavy", "light")
    ]
    with pytest.raises(ValidationError, match="incomplete"):
        _validate_ops(ops)


def test_utility_weights_must_sum_to_one_and_scales_positive():
    ops = _ops()
    ops["utility"]["atc"]["metrics"][0]["weight"] = 0.7
    with pytest.raises(ValidationError, match="sum to 1"):
        _validate_ops(ops)
    ops = _ops()
    ops["utility"]["pilot"]["metrics"][0]["scale"] = 0.0
    with pytest.raises(ValidationError):
        _validate_ops(ops)
    ops = _ops()
    ops["utility"]["ground"]["metrics"][2]["metric"] = "passenger_happiness"
    with pytest.raises(ValidationError, match="exactly"):
        _validate_ops(ops)


def test_dangling_references_rejected():
    ops = _ops()
    ops["taxi_routes"][0]["gate_id"] = "G99"
    with pytest.raises(ValidationError, match="unknown gate"):
        _validate_ops(ops)
    ops = _ops()
    ops["taxi_blockages"][0]["segment_id"] = "NOPE"
    with pytest.raises(ValidationError, match="unknown taxi segment"):
        _validate_ops(ops)


def test_collection_order_does_not_change_canonical_form():
    ops = _ops()
    a = _validate_ops(ops)
    ops["runway_movements"].reverse()
    ops["gates"].reverse()
    ops["separation"]["rules"].reverse()
    b = _validate_ops(ops)
    assert canonical_hash(a.model_dump(mode="json")) == canonical_hash(b.model_dump(mode="json"))


# ---- Scenario hashing / sealing -----------------------------------------
def test_scenario_round_trip_and_tamper_detection():
    scenario = fx.load_fixture_scenario("success")
    again = Scenario.model_validate_json(scenario.model_dump_json())
    assert again == scenario and again.state_hash == scenario.state_hash
    data = json.loads(scenario.model_dump_json())
    data["operations"]["fuel"]["min_reserve_kg"] = 1.0
    with pytest.raises(ValidationError, match="state_hash mismatch"):
        Scenario.model_validate(data)


def test_scenario_is_frozen_and_mutation_of_nested_dict_is_detected():
    scenario = fx.load_fixture_scenario("success")
    with pytest.raises(ValidationError):
        scenario.earliest_landing_min = 0  # type: ignore[misc]
    scenario.historical_row["ARRIVAL_DELAY"] = 0  # dicts are mutable; hash check catches it
    with pytest.raises(StaleStateError):
        ensure_sealed(scenario)


def test_synthetic_scenario_cannot_claim_historical_reference():
    data = json.loads(fx.load_fixture_scenario("no_feasible").model_dump_json())
    data["historical_ref"]["source_sha256"] = "0" * 64
    data.pop("state_hash")
    with pytest.raises(ValidationError, match="must not carry"):
        Scenario.seal(**data)


def test_historical_scenario_row_hash_must_match():
    data = json.loads(fx.load_fixture_scenario("success").model_dump_json())
    data["historical_row"]["WEATHER_DELAY"] = 999
    data.pop("state_hash")
    with pytest.raises(ValidationError, match="row_hash"):
        Scenario.seal(**data)


# ---- Plan / report / assessment invariants ------------------------------
def _plan(**kw) -> Plan:
    base = dict(plan_id="P0010_R1_G2_T2", scenario_id="s", state_hash="a" * 64, landing_min=10,
                runway_id="R1", gate_id="G2", taxi_route_id="T2", taxi_min=4, gate_in_min=14, service_end_min=44)
    base.update(kw)
    return Plan(**base)


def test_plan_timing_invariant():
    assert _plan().gate_in_min == 14
    with pytest.raises(ValidationError, match="gate_in_min"):
        _plan(gate_in_min=15)


def test_report_feasible_flag_must_match_violations():
    v = Violation(rule_id="FUEL-RESERVE", reason="r", observed="1", required=">= 2", units="kg", evidence_ids=["sim:operations.fuel"])
    with pytest.raises(ValidationError):
        ValidationReport(plan_id="P", feasible=True, violations=[v], metrics={})
    with pytest.raises(ValidationError):
        ValidationReport(plan_id="P", feasible=False, violations=[], metrics={})


def _components(cost: float) -> dict[str, float]:
    return {"cost": cost}


def test_assessment_requires_feasible_finite_and_all_roles():
    plan = _plan()
    ok = ValidationReport(plan_id=plan.plan_id, feasible=True, violations=[], metrics={})
    good = dict(plan=plan, validation=ok, utilities={"atc": 0.5, "pilot": 0.5, "ground": 0.5},
                components={r: _components(0.5) for r in ("atc", "pilot", "ground")})
    Assessment(**good)
    bad_report = ValidationReport(
        plan_id=plan.plan_id, feasible=False, metrics={},
        violations=[Violation(rule_id="X", reason="r", observed="o", required="q", units="u", evidence_ids=[])],
    )
    with pytest.raises(ValidationError, match="feasible"):
        Assessment(**{**good, "validation": bad_report})
    with pytest.raises(ValidationError, match="exactly the keys"):
        Assessment(**{**good, "utilities": {"atc": 0.5, "pilot": 0.5}})
    with pytest.raises(ValidationError):
        Assessment(**{**good, "utilities": {"atc": math.inf, "pilot": 0.5, "ground": 0.5}})
    with pytest.raises(ValidationError, match="1 - components"):
        Assessment(**{**good, "utilities": {"atc": 0.4, "pilot": 0.5, "ground": 0.5}})


# ---- agent protocol / run result ----------------------------------------
def _msg(**kw) -> dict:
    base = dict(scenario_id="s", state_hash="a" * 64, role="atc", round=1, action="propose", plan_id="P1",
                ranked_plan_ids=["P1"], objections=[], rationale="short", evidence_ids=[])
    base.update(kw)
    return base


def test_agent_message_protocol_rules():
    AgentMessage(**_msg())
    for bad in (
        _msg(round=1, action="accept"),
        _msg(round=2, action="propose"),
        _msg(round=True),
        _msg(ranked_plan_ids=["a", "b", "c", "d"]),
        _msg(rationale="word " * 121),
        _msg(action="propose", plan_id=None),
        _msg(utility=0.9),  # no LLM-generated numbers
    ):
        with pytest.raises(ValidationError):
            AgentMessage(**bad)


def test_agent_message_known_ids_and_stale_hash():
    msg = AgentMessage(**_msg(ranked_plan_ids=["P1", "P9"]))
    with pytest.raises(ValueError, match="unknown plan ids"):
        msg.check_known("s", "a" * 64, {"P1"})
    with pytest.raises(StaleStateError):
        msg.check_known("s", "b" * 64, {"P1", "P9"})


def test_run_result_invariants():
    result = fx.load_example_result()
    assert result.human_approval_required is True and result.provider_mode == "mock"
    data = result.model_dump(mode="json")
    for patch in (
        {"human_approval_required": False},
        {"agent_message_count": 5},
        {"status": "human_review"},  # selected plan present -> invalid
        {"status": "fallback"},  # selected != fallback
    ):
        with pytest.raises(ValidationError):
            RunResult.model_validate({**data, **patch})
    data["events"][0]["timestamp_utc"] = "2026-10-31T08:00:00+08:00"
    with pytest.raises(ValidationError, match="UTC"):
        RunResult.model_validate(data)


# ---- committed artefacts stay in sync with the code ---------------------
def test_committed_configs_match_builders():
    for name in fx.BUILDERS:
        committed = SimulationConfig.model_validate_json(fx.config_path(name).read_text(encoding="utf-8"))
        assert committed == fx.build_config(name), name


def test_committed_json_schemas_match_models():
    for name, model in PUBLIC_MODELS.items():
        path = fx.FIXTURES_DIR / "schemas" / f"{name}.schema.json"
        assert json.loads(path.read_text(encoding="utf-8")) == model.model_json_schema(), name


def test_fixture_loaders_return_validated_models():
    for name in fx.SCENARIO_FILES:
        assert isinstance(fx.load_fixture_scenario(name), Scenario)
    assert fx.load_example_assessments()


def test_compute_state_hash_accepts_scenario_or_dump():
    from airspace.contracts import ContractError, compute_state_hash

    s = fx.load_fixture_scenario("success")
    assert compute_state_hash(s) == compute_state_hash(s.model_dump()) == s.state_hash
    assert compute_state_hash(s.model_dump(mode="json")) == s.state_hash
    tampered = s.model_dump(mode="json")
    tampered["operations"]["fuel"]["min_reserve_kg"] = 1.0
    assert compute_state_hash(tampered) != s.state_hash
    with pytest.raises(ContractError):
        compute_state_hash({"scenario_id": "x"})


def test_run_result_schema_version_defaults_to_contract_version():
    data = fx.load_example_result().model_dump(mode="json")
    data.pop("schema_version")
    assert RunResult.model_validate(data).schema_version == "1.0"
