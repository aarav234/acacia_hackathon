"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE.

DELIBERATELY LABELLED SIMULATION FIXTURE (source_mode='synthetic'). Every
operating value below is simulated. It is designed so the three roles' modeled
interests conflict (ATC: quiet runway, pilot: early/low-fuel landing, ground:
planned gate and no overtime). It is NOT a historical flight record.
"""
from __future__ import annotations

from airspace.contracts import Evidence, OperationalConfig, Scenario, compute_state_hash


def _ops(**overrides) -> dict:
    ops = {
        "aircraft_category": "M",
        "separation_min": {"M>M": 3, "H>M": 5, "M>H": 4},
        "runways": [
            {"runway_id": "27L", "open_windows": [{"start_min": 0, "end_min": 120}], "turbulence_exposure": 0.2},
            {"runway_id": "27R", "open_windows": [{"start_min": 0, "end_min": 120}], "turbulence_exposure": 0.6},
        ],
        "existing_movements": [
            {"runway_id": "27L", "time_min": 9, "category": "H"},
            {"runway_id": "27L", "time_min": 22, "category": "M"},
            {"runway_id": "27R", "time_min": 6, "category": "M"},
            {"runway_id": "27R", "time_min": 13, "category": "M"},
            {"runway_id": "27R", "time_min": 16, "category": "M"},
        ],
        "gates": [
            {"gate_id": "A1", "compatible_categories": ["M"], "occupied": [{"start_min": 0, "end_min": 30}], "buffer_min": 5},
            {"gate_id": "B4", "compatible_categories": ["M"], "occupied": [], "buffer_min": 5},
            {"gate_id": "C2", "compatible_categories": ["L"], "occupied": [], "buffer_min": 5},
        ],
        "planned_gate_id": "A1",
        "scheduled_gate_in_min": 25,
        "service_duration_min": 30,
        "taxi_routes": [
            {"taxi_route_id": "T1", "runway_id": "27L", "gate_id": "A1", "taxi_min": 6, "blocked": []},
            {"taxi_route_id": "T2", "runway_id": "27L", "gate_id": "B4", "taxi_min": 6, "blocked": []},
            {"taxi_route_id": "T3", "runway_id": "27R", "gate_id": "A1", "taxi_min": 8, "blocked": []},
            {"taxi_route_id": "T4", "runway_id": "27R", "gate_id": "B4", "taxi_min": 20, "blocked": [{"start_min": 40, "end_min": 50}]},
            {"taxi_route_id": "T5", "runway_id": "27L", "gate_id": "C2", "taxi_min": 4, "blocked": []},
        ],
        "fuel": {"snapshot_kg": 5200.0, "approach_burn_kg": 600.0, "airborne_burn_kg_per_min": 45.0,
                 "taxi_burn_kg_per_min": 12.0, "min_reserve_kg": 3200.0},
        "crew": {"available": {"start_min": 0, "end_min": 100}, "overtime_after_min": 70},
        "connections": [{"deadline_min": 28, "passengers": 40}, {"deadline_min": 60, "passengers": 25}],
        "utility": {
            "atc": {"weights": {"landing_delay": 0.6, "spillover": 0.4}, "scales": {"landing_delay": 30.0, "spillover": 2.0}},
            "pilot": {"weights": {"incremental_fuel": 0.6, "connection": 0.3, "turbulence": 0.1},
                      "scales": {"incremental_fuel": 1400.0, "connection": 65.0, "turbulence": 10.0}},
            "ground": {"weights": {"turnaround_delay": 0.5, "crew_overtime": 0.3, "gate_change": 0.2},
                       "scales": {"turnaround_delay": 40.0, "crew_overtime": 30.0, "gate_change": 1.0}},
        },
        "catalogue_cap": 2000,
    }
    ops.update(overrides)
    return ops


def build_conflict_scenario(scenario_id: str = "SIM-CONFLICT-001", **ops_overrides) -> Scenario:
    data = {
        "scenario_id": scenario_id,
        "schema_version": "1.0",
        "source_mode": "synthetic",
        "historical_row": {},
        "historical_ref": {"source": "synthetic", "note": "simulation fixture; no historical row"},
        "simulation_epoch_utc": "1970-01-01T00:00:00Z",
        "earliest_landing_min": 10,
        "planning_horizon_min": 30,
        "slot_step_min": 5,
        "operations": OperationalConfig.model_validate(_ops(**ops_overrides)).model_dump(),
        "provenance": [
            Evidence(evidence_id=f"EV-{k}", kind="simulated", field_path=f"operations.{k}",
                     source_ref="fixture:_stub_fixture", note=f"simulated {k}").model_dump()
            for k in ("runways", "separation", "gates", "taxi_routes", "fuel", "crew", "connections", "utility")
        ],
    }
    data["state_hash"] = compute_state_hash(data)
    return Scenario.model_validate(data)
