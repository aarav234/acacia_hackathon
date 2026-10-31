"""Config builders, fixture writer and fixture loader.

All operational values below are SIMULATED demonstration assumptions. The
success scenario is anchored to one genuine flights.csv row (stored in
fixtures/historical_anchor_row.json with its provenance); the two edge-case
scenarios are explicitly synthetic.

Teammates:
    from airspace.data.fixtures import load_fixture_scenario, load_example_assessments
    scenario = load_fixture_scenario("success")   # or "no_feasible", "no_positive_gain"

Regenerate (Member 1):
    PYTHONPATH=src python -m airspace.data.fixtures            # writes configs/ and fixtures/
    PYTHONPATH=src python -m airspace.data.fixtures --refresh-anchor   # re-select from flights.csv
"""

from __future__ import annotations

import copy
import json
import os
import sys
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from airspace.contracts import (
    CONTRACT_DEFAULT_WEIGHTS,
    ROLE_METRICS,
    SCHEMA_VERSION,
    Assessment,
    RunResult,
    Scenario,
    SimulationConfig,
    export_json_schemas,
)
from airspace.data.ingest import default_data_dir, select_flight
from airspace.data.scenario import build_scenario, dump_scenario, load_scenario, synthetic_row

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURES_DIR = Path(os.environ.get("AIRSPACE_FIXTURES_DIR", REPO_ROOT / "fixtures"))
CONFIGS_DIR = Path(os.environ.get("AIRSPACE_CONFIGS_DIR", REPO_ROOT / "configs"))
ANCHOR_ROW_FILE = "historical_anchor_row.json"
SIMULATION_EPOCH = "2000-01-01T00:00:00Z"  # labelled simulation epoch, NOT a historical instant

FixtureName = Literal["success", "no_feasible", "no_positive_gain"]
CONFIG_FILES: dict[str, str] = {
    "success": "sim_success_v1.json",
    "no_feasible": "sim_no_feasible_v1.json",
    "no_positive_gain": "sim_no_positive_gain_v1.json",
}
SCENARIO_FILES: dict[str, str] = {
    "success": "success_scenario.json",
    "no_feasible": "no_feasible_scenario.json",
    "no_positive_gain": "no_positive_gain_scenario.json",
}


def _w(start: int, end: int) -> dict[str, int]:
    return {"start_min": start, "end_min": end}


def utility_block(
    scales: dict[str, float], weights: dict[str, dict[str, float]] | None = None, spillover_window_min: int = 10
) -> dict:
    weights = weights or CONTRACT_DEFAULT_WEIGHTS
    return {
        role: {"metrics": [{"metric": m, "weight": weights[role][m], "scale": scales[m]} for m in ROLE_METRICS[role]]}
        for role in ("atc", "pilot", "ground")
    } | {"spillover_window_min": spillover_window_min}


DEMO_SCALES: dict[str, float] = {
    "landing_delay_min": 30.0,
    "spillover_proxy_min": 10.0,
    "incremental_fuel_kg": 500.0,
    "connection_penalty_pax": 100.0,
    "turbulence_exposure_index": 0.5,
    "turnaround_delay_min": 30.0,
    "crew_overtime_min": 30.0,
    "gate_change_indicator": 1.0,
}

_TOY_SEPARATION = {
    "categories": ["heavy", "medium", "light"],
    "rules": [
        {"leader_category": lead, "follower_category": follow, "min_separation_min": sep}
        for (lead, follow), sep in {
            ("heavy", "heavy"): 2, ("heavy", "medium"): 3, ("heavy", "light"): 4,
            ("medium", "heavy"): 2, ("medium", "medium"): 2, ("medium", "light"): 3,
            ("light", "heavy"): 2, ("light", "medium"): 2, ("light", "light"): 2,
        }.items()
    ],
}


def success_config_dict() -> dict:
    """Two runways, four gates, six routes, 8 slots -> 48 candidates."""
    return {
        "schema_version": SCHEMA_VERSION,
        "config_version": "sim-success-v1",
        "scenario_id": "demo-success-v1",
        "simulation_epoch_utc": SIMULATION_EPOCH,
        "earliest_landing_min": 10,
        "planning_horizon_min": 40,
        "slot_step_min": 5,
        "operations": {
            "schema_version": SCHEMA_VERSION,
            "subject": {
                "flight_label": "Disrupted inbound (simulated operating state; historical row is context only)",
                "wake_category": "medium",
                "runway_occupancy_min": 2,
                "assigned_gate_id": "G1",
                "scheduled_gate_in_min": 20,
            },
            "separation": _TOY_SEPARATION,
            "runways": [
                {"runway_id": "R1", "open_windows": [_w(0, 120)]},
                {"runway_id": "R2", "open_windows": [_w(0, 120)]},
            ],
            "runway_movements": [
                {"movement_id": "BG-R1-D12", "runway_id": "R1", "time_min": 12, "wake_category": "heavy", "kind": "departure"},
                {"movement_id": "BG-R1-A30", "runway_id": "R1", "time_min": 30, "wake_category": "medium", "kind": "arrival"},
                {"movement_id": "BG-R2-A08", "runway_id": "R2", "time_min": 8, "wake_category": "heavy", "kind": "arrival"},
                {"movement_id": "BG-R2-D22", "runway_id": "R2", "time_min": 22, "wake_category": "medium", "kind": "departure"},
            ],
            "gates": [
                {"gate_id": "G1", "compatible_categories": ["heavy", "medium"], "open_windows": [_w(0, 180)], "turnover_buffer_min": 5},
                {"gate_id": "G2", "compatible_categories": ["medium", "light"], "open_windows": [_w(0, 180)], "turnover_buffer_min": 5},
                {"gate_id": "G3", "compatible_categories": ["medium"], "open_windows": [_w(0, 180)], "turnover_buffer_min": 5},
                {"gate_id": "G4", "compatible_categories": ["heavy"], "open_windows": [_w(0, 180)], "turnover_buffer_min": 5},
            ],
            "gate_occupancy": [
                {"occupancy_id": "OCC-G1-1", "gate_id": "G1", "start_min": 0, "end_min": 15},
                {"occupancy_id": "OCC-G3-1", "gate_id": "G3", "start_min": 70, "end_min": 100},
            ],
            "taxi_routes": [
                {"route_id": "T1", "runway_id": "R1", "gate_id": "G1", "taxi_min": 6, "segment_ids": ["TWA", "TWB"]},
                {"route_id": "T2", "runway_id": "R1", "gate_id": "G2", "taxi_min": 4, "segment_ids": ["TWA", "TWC"]},
                {"route_id": "T3", "runway_id": "R2", "gate_id": "G1", "taxi_min": 5, "segment_ids": ["TWD", "TWB"]},
                {"route_id": "T4", "runway_id": "R2", "gate_id": "G3", "taxi_min": 3, "segment_ids": ["TWD", "TWE"]},
                {"route_id": "T5", "runway_id": "R2", "gate_id": "G2", "taxi_min": 8, "segment_ids": ["TWD", "TWF", "TWC"]},
                {"route_id": "T6", "runway_id": "R1", "gate_id": "G4", "taxi_min": 5, "segment_ids": ["TWA", "TWG"]},
            ],
            "taxi_blockages": [
                {"blockage_id": "BLK-TWC-1", "segment_id": "TWC", "start_min": 38, "end_min": 46},
            ],
            "service": {"service_duration_min": 30},
            "fuel": {
                "snapshot_fuel_kg": 6000.0,
                "approach_burn_kg": 1500.0,
                "airborne_burn_kg_per_min": 30.0,
                "taxi_burn_kg_per_min": 15.0,
                "min_reserve_kg": 3400.0,
            },
            "crew": [
                {"crew_id": "C1", "available": _w(0, 60), "regular_end_min": 40, "busy": []},
                {"crew_id": "C2", "available": _w(25, 150), "regular_end_min": 150, "busy": [_w(80, 95)]},
            ],
            "connections": [
                {"group_id": "CX1", "passenger_count": 40, "deadline_gate_in_min": 22},
                {"group_id": "CX2", "passenger_count": 60, "deadline_gate_in_min": 34},
            ],
            "turbulence": {
                "runway_exposure": [
                    {"runway_id": "R1", "exposure_index": 3.0},
                    {"runway_id": "R2", "exposure_index": 0.0},
                ],
                "holding_exposure_per_min": 0.05,
            },
            "utility": utility_block(DEMO_SCALES),
            "planning": {"candidate_cap": 2000},
        },
        "assumptions": [
            {"field_path": "operations.separation", "note": "Toy wake separation minutes chosen for demonstration; not an FAA/ICAO/Airbus standard."},
            {"field_path": "operations.fuel", "note": "Toy fuel snapshot/burn/reserve; not derived from DISTANCE; not an operational reserve rule."},
            {"field_path": "operations.turbulence", "note": "R1 assumed to have a high configured exposure proxy (e.g. crosswind approach) - simulated."},
            {"field_path": "operations.connections", "note": "Assumed connection groups; flights.csv has no passenger-level data."},
        ],
    }


def no_feasible_config_dict() -> dict:
    """Synthetic: simulated snapshot fuel cannot meet the toy reserve on any plan."""
    cfg = success_config_dict()
    cfg["config_version"] = "sim-no-feasible-v1"
    cfg["scenario_id"] = "synthetic-no-feasible-v1"
    cfg["operations"]["fuel"] = {
        "snapshot_fuel_kg": 4500.0,
        "approach_burn_kg": 1500.0,
        "airborne_burn_kg_per_min": 30.0,
        "taxi_burn_kg_per_min": 15.0,
        "min_reserve_kg": 3400.0,
    }
    cfg["assumptions"] = [
        {"field_path": "operations.fuel", "note": "SYNTHETIC edge case: snapshot fuel too low for the toy reserve; every plan must fail FUEL-RESERVE."}
    ]
    return cfg


def no_positive_gain_config_dict() -> dict:
    """Synthetic: no plan strictly improves ALL three roles over the fallback.

    ATC cannot beat zero delay/zero spillover at the earliest slot, so even though
    ground alone would gain from its assigned gate, no positive bargain exists.
    """
    cfg = success_config_dict()
    ops = cfg["operations"]
    cfg["config_version"] = "sim-no-positive-gain-v1"
    cfg["scenario_id"] = "synthetic-no-positive-gain-v1"
    cfg["planning_horizon_min"] = 20
    ops["runway_movements"] = []
    ops["gate_occupancy"] = []
    ops["taxi_blockages"] = []
    ops["subject"]["scheduled_gate_in_min"] = 16
    ops["crew"] = [{"crew_id": "C1", "available": _w(0, 200), "regular_end_min": 200, "busy": []}]
    ops["turbulence"]["runway_exposure"] = [
        {"runway_id": "R1", "exposure_index": 0.0},
        {"runway_id": "R2", "exposure_index": 0.0},
    ]
    cfg["assumptions"] = [
        {"field_path": "operations", "note": "SYNTHETIC edge case: no background traffic/occupancy; earliest plan to the assigned gate dominates."}
    ]
    return cfg


BUILDERS = {
    "success": success_config_dict,
    "no_feasible": no_feasible_config_dict,
    "no_positive_gain": no_positive_gain_config_dict,
}


def config_dict(name: FixtureName) -> dict:
    return copy.deepcopy(BUILDERS[name]())


def build_config(name: FixtureName) -> SimulationConfig:
    return SimulationConfig.model_validate_json(json.dumps(config_dict(name)))


def config_path(name: FixtureName) -> Path:
    return CONFIGS_DIR / CONFIG_FILES[name]


def load_anchor_row() -> dict[str, object] | None:
    path = FIXTURES_DIR / ANCHOR_ROW_FILE
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def anchor_row_or_synthetic(note: str) -> dict[str, object]:
    row = load_anchor_row()
    return row if row is not None else synthetic_row(note + " (historical anchor fixture missing)")


def fixture_row(name: FixtureName) -> dict[str, object]:
    if name == "success":
        return anchor_row_or_synthetic("success scenario")
    return synthetic_row(f"SYNTHETIC edge-case fixture '{name}'; no historical row is claimed.")


def load_fixture_scenario(name: FixtureName) -> Scenario:
    return load_scenario(FIXTURES_DIR / SCENARIO_FILES[name])


def load_example_assessments() -> list[Assessment]:
    text = (FIXTURES_DIR / "example_assessments.json").read_text(encoding="utf-8")
    return TypeAdapter(list[Assessment]).validate_json(text)


def load_example_result() -> RunResult:
    return RunResult.model_validate_json((FIXTURES_DIR / "example_result.json").read_text(encoding="utf-8"))


def example_run_result(scenario: Scenario, fallback_id: str, compromise_id: str) -> dict:
    """A CONSPICUOUSLY LABELLED mock RunResult illustrating the schema. Not a negotiation run."""
    base = {"scenario_id": scenario.scenario_id, "state_hash": scenario.state_hash}
    ts = "2026-10-31T00:00:00Z"
    events = [
        {**base, "sequence": 0, "timestamp_utc": ts, "round": 0, "actor": "engine", "event_type": "engine_summary",
         "payload": {"note": "EXAMPLE FIXTURE - mock provider, not a live or real negotiation run",
                     "fallback_plan_id": fallback_id, "compromise_plan_id": compromise_id}},
    ]
    seq = 1
    for rnd, action in ((1, "propose"), (2, "accept")):
        for role in ("atc", "pilot", "ground"):
            events.append({**base, "sequence": seq, "timestamp_utc": ts, "round": rnd, "actor": role,
                           "event_type": "agent_message",
                           "payload": {"mock": True, "action": action,
                                       "plan_id": compromise_id if action == "accept" else None}})
            seq += 1
    return {
        "schema_version": SCHEMA_VERSION,
        **base,
        "status": "consensus",
        "reason_code": "example_fixture_mock_not_a_run",
        "selected_plan_id": compromise_id,
        "fallback_plan_id": fallback_id,
        "rounds_completed": 2,
        "agent_message_count": 6,
        "provider_mode": "mock",
        "events": events,
        "elapsed_ms": 0.0,
        "human_approval_required": True,
    }


def write_all(refresh_anchor: bool = False) -> list[Path]:
    from airspace.engine.pipeline import solve_scenario  # local import: no cycle at module import

    written: list[Path] = []
    CONFIGS_DIR.mkdir(parents=True, exist_ok=True)
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    if refresh_anchor:
        flights = default_data_dir() / "flights.csv"
        row = select_flight(flights)
        path = FIXTURES_DIR / ANCHOR_ROW_FILE
        path.write_text(json.dumps(row, indent=2) + "\n", encoding="utf-8")
        written.append(path)
    for name in BUILDERS:
        cfg = build_config(name)  # validates
        cpath = config_path(name)
        cpath.write_text(json.dumps(cfg.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
        written.append(cpath)
        scenario = build_scenario(fixture_row(name), cpath)
        spath = FIXTURES_DIR / SCENARIO_FILES[name]
        dump_scenario(scenario, spath)
        written.append(spath)
        if name == "success":
            outcome = solve_scenario(scenario)
            apath = FIXTURES_DIR / "example_assessments.json"
            apath.write_text(
                json.dumps([a.model_dump(mode="json") for a in outcome.assessments], indent=2) + "\n",
                encoding="utf-8",
            )
            rpath = FIXTURES_DIR / "example_result.json"
            result = RunResult.model_validate(
                example_run_result(scenario, outcome.fallback.plan_id, outcome.compromise.plan_id)
            )
            rpath.write_text(json.dumps(result.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
            written += [apath, rpath]
    written += export_json_schemas(FIXTURES_DIR / "schemas")
    return written


if __name__ == "__main__":
    for p in write_all(refresh_anchor="--refresh-anchor" in sys.argv):
        print(p.relative_to(REPO_ROOT) if p.is_relative_to(REPO_ROOT) else p)
