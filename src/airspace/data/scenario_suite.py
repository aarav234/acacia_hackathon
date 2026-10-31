"""Challenge-calibrated scenario generator and the validation suite (40 scenarios).

Values taken from the challenge workbook (challenge-supplied planning rules,
not measurements): holding burn, final reserve, spare fuel on arrival, wake
separation, min turnaround, active runways, spare gates, runway ops/hour,
ground-crew shift length. Everything else is a SIMULATED assumption listed in
``ChallengeAssumptions`` or generated from a fixed seed. The anchor rows are
genuine flights.csv rows used as context only (never as decision inputs).

    PYTHONPATH=src python -m airspace.data.scenario_suite   # rebuild committed suite fixtures
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from airspace.contracts import SCHEMA_VERSION, Scenario, SimulationConfig
from airspace.data.challenge import EXTRACT_NAME, FAA_CATEGORIES, ChallengeConstraints, load_extract
from airspace.data.fixtures import DEMO_SCALES, FIXTURES_DIR, SIMULATION_EPOCH, utility_block
from airspace.data.scenario import scenario_from_config, synthetic_row

ANCHOR_ROWS_FILE = "challenge_anchor_rows.json"
# First qualifying completed row (file order) per destination size class.
ANCHOR_ROW_INDEX = {"MIA": 70, "DEN": 171, "SJC": 682, "ITO": 2845}
SUITE_SEED = 20261031


@dataclass(frozen=True)
class ChallengeAssumptions:
    """Labelled assumptions needed to turn workbook rules into the contract model."""

    approach_groundspeed_kt: float = 140.0  # converts arrival nm separation to minutes
    subject_category: str = "large"  # A320/737 class (aircraft type is not in flights.csv)
    spare_fuel_includes_final_reserve: bool = True  # conservative reading of "45 min spare on arrival"
    taxi_burn_kg_per_min: float = 15.0  # not in workbook
    max_overtime_min: int = 60  # hard end of crew availability after the 8 h shift; not in workbook
    turnover_buffer_min: int = 5  # not in workbook
    earliest_landing_min: int = 20  # decision at minute 0 => 20-min gate-change notice holds by construction
    planning_horizon_min: int = 45
    slot_step_min: int = 5


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    airport: str
    category: str
    seed: int
    delay_behind_schedule_min: int = 15  # simulated lateness vs schedule at the earliest landing
    traffic_utilisation: float = 0.6  # share of workbook max_runway_ops_per_hour used by background
    heavy_share: float = 0.2
    closed_runways: tuple[tuple[int, int, int], ...] = ()  # (runway index, start, end) closures
    assigned_gate_busy_until: int | None = None
    spare_gates_busy: int = 0
    crew_regular_end_offsets: tuple[int, ...] = (30, 90)  # regular_end minus scheduled gate-in
    crew_available_from: tuple[int, ...] = (0, 0)
    assumptions: ChallengeAssumptions = field(default_factory=ChallengeAssumptions)


def separation_minutes(c: ChallengeConstraints, groundspeed_kt: float) -> dict[tuple[str, str], int]:
    """ceil(max(arrival nm / groundspeed, departure seconds)) per leader/follower (conservative)."""
    out = {}
    for lead in FAA_CATEGORIES:
        for follow in FAA_CATEGORIES:
            rule = c.wake(lead, follow)
            minutes = max(rule.min_separation_nm / groundspeed_kt * 60.0, rule.min_departure_time_s / 60.0)
            out[(lead, follow)] = math.ceil(minutes - 1e-9)
    return out


def challenge_config_dict(c: ChallengeConstraints, spec: ScenarioSpec) -> dict:
    a = spec.assumptions
    prof = c.airport(spec.airport)
    rng = random.Random(spec.seed)
    fc = c.fuel_crew
    sep = separation_minutes(c, a.approach_groundspeed_kt)
    burn = fc.holding_burn_narrowbody_kg_per_h / 60.0
    on_arrival_min = fc.spare_fuel_on_arrival_holding_min + (0 if a.spare_fuel_includes_final_reserve else fc.final_reserve_holding_min)
    earliest = a.earliest_landing_min
    end = earliest + a.planning_horizon_min + 60

    runways = [f"R{i + 1}" for i in range(prof.active_runways)]
    closures = {}
    for idx, start, stop in spec.closed_runways:
        closures.setdefault(runways[idx], []).append((start, stop))
    runway_cfg = []
    for rid in runways:
        windows, cursor = [], 0
        for start, stop in sorted(closures.get(rid, [])):
            if start > cursor:
                windows.append({"start_min": cursor, "end_min": start})
            cursor = max(cursor, stop)
        if cursor < end + 60:
            windows.append({"start_min": cursor, "end_min": end + 60})
        runway_cfg.append({"runway_id": rid, "open_windows": windows})

    # Background traffic: per-runway rate from workbook capacity x utilisation, separated among itself.
    per_runway_per_min = spec.traffic_utilisation * prof.max_runway_ops_per_hour / prof.active_runways / 60.0
    moves = []
    for rid in runways:
        t, prev = rng.uniform(0, 3), None
        while t < end:
            cat = "heavy" if rng.random() < spec.heavy_share else ("small" if rng.random() < 0.1 else "large")
            if prev is not None:
                t = max(t, prev[0] + sep[(prev[1], cat)])
            moves.append({"movement_id": f"BG-{rid}-{len(moves):03d}", "runway_id": rid, "time_min": int(round(t)),
                          "wake_category": cat, "kind": rng.choice(["arrival", "departure"])})
            prev = (int(round(t)), cat)
            t += rng.expovariate(per_runway_per_min) if per_runway_per_min > 0 else end

    gates = [f"G{i + 1}" for i in range(1 + prof.spare_gates_at_any_time)]  # assigned + workbook spare gates
    gate_cfg = [{"gate_id": g, "compatible_categories": ["heavy", "large", "small"] if i == 0 or rng.random() < 0.7 else ["large", "small"],
                 "open_windows": [{"start_min": 0, "end_min": end + 240}], "turnover_buffer_min": a.turnover_buffer_min}
                for i, g in enumerate(gates)]
    occupancy = []
    if spec.assigned_gate_busy_until is not None:
        occupancy.append({"occupancy_id": "OCC-G1", "gate_id": "G1", "start_min": 0, "end_min": spec.assigned_gate_busy_until})
    for g in gates[1:1 + spec.spare_gates_busy]:
        start = rng.randint(earliest, earliest + 30)
        occupancy.append({"occupancy_id": f"OCC-{g}", "gate_id": g, "start_min": start, "end_min": start + prof.min_turnaround_min})

    routes = []
    for gi, g in enumerate(gates):
        reachable = [r for r in runways if rng.random() < 0.75] or [runways[gi % len(runways)]]
        for r in reachable:
            routes.append({"route_id": f"T{r[1:]}{g[1:]}", "runway_id": r, "gate_id": g, "taxi_min": rng.randint(3, 12),
                           "segment_ids": [f"EXIT-{r}", f"APRON-{g}"] + (["XING-1"] if rng.random() < 0.3 else [])})
    blockages = []
    if any("XING-1" in rt["segment_ids"] for rt in routes):
        s = rng.randint(earliest, earliest + a.planning_horizon_min)
        blockages.append({"blockage_id": "BLK-XING-1", "segment_id": "XING-1", "start_min": s, "end_min": s + 6})

    typical_taxi = 6
    scheduled_gate_in = earliest + typical_taxi - spec.delay_behind_schedule_min
    shift = fc.ground_crew_shift_h * 60
    crews = []
    for i, (off, avail_from) in enumerate(zip(spec.crew_regular_end_offsets, spec.crew_available_from)):
        regular_end = scheduled_gate_in + off
        start = max(regular_end - shift, avail_from)
        crews.append({"crew_id": f"C{i + 1}", "available": {"start_min": start, "end_min": regular_end + a.max_overtime_min},
                      "regular_end_min": max(start, regular_end), "busy": []})

    return {
        "schema_version": SCHEMA_VERSION,
        "config_version": "sim-challenge2-v1",
        "scenario_id": spec.scenario_id,
        "simulation_epoch_utc": SIMULATION_EPOCH,
        "earliest_landing_min": earliest,
        "planning_horizon_min": a.planning_horizon_min,
        "slot_step_min": a.slot_step_min,
        "operations": {
            "schema_version": SCHEMA_VERSION,
            "subject": {"flight_label": f"Disrupted inbound to {spec.airport} (challenge-calibrated simulation)",
                        "wake_category": a.subject_category, "runway_occupancy_min": 1,
                        "assigned_gate_id": "G1", "scheduled_gate_in_min": scheduled_gate_in},
            "separation": {"categories": list(FAA_CATEGORIES),
                           "rules": [{"leader_category": l, "follower_category": f, "min_separation_min": m} for (l, f), m in sep.items()]},
            "runways": runway_cfg,
            "runway_movements": moves,
            "gates": gate_cfg,
            "gate_occupancy": occupancy,
            "taxi_routes": routes,
            "taxi_blockages": blockages,
            "service": {"service_duration_min": prof.min_turnaround_min},
            "fuel": {"snapshot_fuel_kg": on_arrival_min * burn, "approach_burn_kg": 0.0,
                     "airborne_burn_kg_per_min": burn, "taxi_burn_kg_per_min": a.taxi_burn_kg_per_min,
                     "min_reserve_kg": fc.final_reserve_holding_min * burn},
            "crew": crews,
            "connections": [
                {"group_id": "CX1", "passenger_count": rng.randint(10, 60), "deadline_gate_in_min": scheduled_gate_in + rng.randint(15, 30)},
                {"group_id": "CX2", "passenger_count": rng.randint(10, 60), "deadline_gate_in_min": scheduled_gate_in + rng.randint(30, 50)},
            ],
            "turbulence": {"runway_exposure": [{"runway_id": r, "exposure_index": round(rng.uniform(0, 3), 2)} for r in runways],
                           "holding_exposure_per_min": 0.05},
            "utility": utility_block(DEMO_SCALES),
            "planning": {"candidate_cap": 2000},
        },
        "assumptions": [
            {"field_path": "operations.separation",
             "note": f"From challenge workbook wake_separation (sha256 {c.source_sha256[:12]}, simplified FAA JO 7110.65): "
                     f"ceil(max(nm / {a.approach_groundspeed_kt:g} kt, departure seconds)) minutes. Groundspeed is an assumption."},
            {"field_path": "operations.fuel",
             "note": f"Workbook: holding burn {fc.holding_burn_narrowbody_kg_per_h} kg/h (narrowbody), final reserve "
                     f"{fc.final_reserve_holding_min} min, spare on arrival {fc.spare_fuel_on_arrival_holding_min} min "
                     f"({'including' if a.spare_fuel_includes_final_reserve else 'in addition to'} the final reserve - assumption). "
                     "Snapshot = fuel on board at the earliest landing. Taxi burn and contingency fuel are not modelled from the workbook."},
            {"field_path": "operations.service", "note": f"Workbook min_turnaround_min for {spec.airport} ({prof.size_class})."},
            {"field_path": "operations.runways", "note": f"Workbook active_runways={prof.active_runways} for {spec.airport}; closures are simulated: {list(spec.closed_runways)}."},
            {"field_path": "operations.runway_movements", "note": f"SIMULATED (seed {spec.seed}): utilisation {spec.traffic_utilisation} of workbook max_runway_ops_per_hour={prof.max_runway_ops_per_hour}."},
            {"field_path": "operations.gates", "note": f"Assigned gate + workbook spare_gates_at_any_time={prof.spare_gates_at_any_time}; the other {prof.gates - len(gates)} gates are not modelled."},
            {"field_path": "operations.crew", "note": f"Workbook ground crew shift {fc.ground_crew_shift_h} h (overtime after it). Shift timing and the {a.max_overtime_min} min hard overtime cap are simulated. The 1.5x overtime cost multiplier is not representable as a cross-metric weight."},
            {"field_path": "planning", "note": f"Earliest landing {earliest} min after the decision, so the workbook {fc.gate_change_notice_min}-min gate-change notice holds by construction."},
        ],
    }


def _spec(airport: str, category: str, i: int, **kw) -> ScenarioSpec:
    return ScenarioSpec(scenario_id=f"suite-{airport.lower()}-{category}", airport=airport, category=category,
                        seed=SUITE_SEED + 101 * i, **kw)


SUITE_CATEGORIES: dict[str, dict] = {
    "baseline": {},
    "on_time": {"delay_behind_schedule_min": 0},
    "large_delay": {"delay_behind_schedule_min": 60},
    "runway_closure": {"closed_runways": ((0, 0, 40),)},
    "dense_traffic": {"traffic_utilisation": 0.95, "heavy_share": 0.4},
    "gate_restricted": {"assigned_gate_busy_until": 40, "spare_gates_busy": 2},
    "crew_restricted": {"crew_available_from": (55, 0), "crew_regular_end_offsets": (60, 30)},
    "tight_fuel": {"assumptions": replace(ChallengeAssumptions(), taxi_burn_kg_per_min=30.0)},
    "generous_fuel": {"assumptions": replace(ChallengeAssumptions(), spare_fuel_includes_final_reserve=False),
                      "traffic_utilisation": 0.3},
    "all_runways_closed": {"closed_runways": ((0, 0, 200), (1, 0, 200), (2, 0, 200), (3, 0, 200))},
}


def suite_specs() -> list[ScenarioSpec]:
    specs = []
    for i, (airport, cat) in enumerate((a, c) for a in ANCHOR_ROW_INDEX for c in SUITE_CATEGORIES):
        kw = dict(SUITE_CATEGORIES[cat])
        if "closed_runways" in kw:  # drop closures for runways the airport does not have
            n = {"MIA": 3, "DEN": 4, "SJC": 2, "ITO": 1}[airport]
            kw["closed_runways"] = tuple(cr for cr in kw["closed_runways"] if cr[0] < n)
        specs.append(_spec(airport, cat, i, **kw))
    return specs


def load_anchor_rows() -> dict[str, dict]:
    return json.loads((FIXTURES_DIR / ANCHOR_ROWS_FILE).read_text(encoding="utf-8"))


def load_challenge_extract() -> ChallengeConstraints:
    return load_extract(FIXTURES_DIR / EXTRACT_NAME)


def build_suite_scenario(spec: ScenarioSpec, c: ChallengeConstraints | None = None,
                         rows: dict[str, dict] | None = None) -> Scenario:
    c = c or load_challenge_extract()
    rows = rows if rows is not None else load_anchor_rows()
    config = SimulationConfig.model_validate_json(json.dumps(challenge_config_dict(c, spec)))
    row = rows.get(spec.airport) or synthetic_row(f"anchor row for {spec.airport} unavailable")
    return scenario_from_config(row, config)


def build_suite() -> list[tuple[ScenarioSpec, Scenario]]:
    c, rows = load_challenge_extract(), load_anchor_rows()
    return [(s, build_suite_scenario(s, c, rows)) for s in suite_specs()]


def write_suite_fixtures(workbook: Path, flights_csv: Path) -> list[Path]:
    """Regenerate the committed extract + anchor rows from the original files (Member 1 only)."""
    from airspace.data.challenge import write_extract
    from airspace.data.ingest import select_flight

    out = [write_extract(workbook, FIXTURES_DIR / EXTRACT_NAME, sorted(ANCHOR_ROW_INDEX))]
    rows = {code: select_flight(flights_csv, row_index=idx) for code, idx in ANCHOR_ROW_INDEX.items()}
    for code, row in rows.items():
        assert row["DESTINATION_AIRPORT"] == code, (code, row["DESTINATION_AIRPORT"])
    path = FIXTURES_DIR / ANCHOR_ROWS_FILE
    path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    return out + [path]


if __name__ == "__main__":
    from airspace.data.ingest import default_data_dir

    d = default_data_dir()
    for p in write_suite_fixtures(d / "challenge2_operational_constraints.xlsx", d / "flights.csv"):
        print(p)
