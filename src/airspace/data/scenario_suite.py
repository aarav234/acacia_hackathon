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
# Landing-time resolution. Production scenario generation uses the contract's full integer-minute
# decision space. The frozen research suites (suite_specs, stratified_specs) pin the published
# 5-minute baseline so every reported result stays reproducible.
PRODUCTION_SLOT_STEP_MIN = 1
BASELINE_SLOT_STEP_MIN = 5
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
    slot_step_min: int = PRODUCTION_SLOT_STEP_MIN


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
    crews, off_shift = [], []
    for i, (off, avail_from) in enumerate(zip(spec.crew_regular_end_offsets, spec.crew_available_from)):
        regular_end = scheduled_gate_in + off
        start = max(regular_end - shift, avail_from)
        end = regular_end + a.max_overtime_min
        if end <= start:  # shift (incl. overtime cap) is over before it starts: crew unavailable, not an empty window
            off_shift.append(f"C{i + 1}")
            continue
        crews.append({"crew_id": f"C{i + 1}", "available": {"start_min": start, "end_min": end},
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
            {"field_path": "operations.crew", "note": f"Workbook ground crew shift {fc.ground_crew_shift_h} h (overtime after it). Shift timing and the {a.max_overtime_min} min hard overtime cap are simulated. The 1.5x overtime cost multiplier is not representable as a cross-metric weight."
                     + (f" Off shift for the whole scenario (omitted): {off_shift}." if off_shift else "")},
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


def _baseline(spec: ScenarioSpec) -> ScenarioSpec:
    return replace(spec, assumptions=replace(spec.assumptions, slot_step_min=BASELINE_SLOT_STEP_MIN))


def suite_specs() -> list[ScenarioSpec]:
    """The published 40-scenario suite, pinned to the 5-minute baseline grid."""
    specs = []
    for i, (airport, cat) in enumerate((a, c) for a in ANCHOR_ROW_INDEX for c in SUITE_CATEGORIES):
        kw = dict(SUITE_CATEGORIES[cat])
        if "closed_runways" in kw:  # drop closures for runways the airport does not have
            n = {"MIA": 3, "DEN": 4, "SJC": 2, "ITO": 1}[airport]
            kw["closed_runways"] = tuple(cr for cr in kw["closed_runways"] if cr[0] < n)
        specs.append(_baseline(_spec(airport, cat, i, **kw)))
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


# --------------------------------------------------------------------------------------
# Stratified expanded suite (research evaluation; the 40-scenario suite above is unchanged)
# --------------------------------------------------------------------------------------
# 4 destination tiers x 8 families x 5 seeds = 160 distinct configurations. The partition
# is fixed by seed index BEFORE any result is inspected: k in {0,1} -> "dev",
# k == 2 -> "tuning", k in {3,4} -> "holdout". Parameter ranges are documented in
# STRATIFIED_FAMILIES; draws use random.Random(seed) so every scenario is reproducible.
STRATIFIED_BASE_SEED = 7_000_000
PARTITION_BY_K = {0: "dev", 1: "dev", 2: "tuning", 3: "holdout", 4: "holdout"}
STRATIFIED_FAMILIES: dict[str, str] = {
    "normal": "delay U[5,25] min, utilisation U[0.4,0.7]",
    "short_delay": "delay U[0,10] min",
    "long_delay": "delay U[45,90] min",
    "runway_reduced": "runway R1 closed from minute 0 for U[20,60] min",
    "wake_dense": "utilisation U[0.85,1.0], heavy share U[0.3,0.5]",
    "gate_conflict": "assigned gate busy until U[25,50], U{1..3} spare gates busy",
    "tight_ground": "crew C1 available from U[40,70]; regular-end offsets U[30,60] / U[10,40]",
    "tight_fuel": "taxi burn U[20,35] kg/min",
}
COMMON_FACTORS = "all families: groundspeed U{120,140,160} kt, max overtime U{30,60,90} min, seeded background traffic"


def _stratified_spec(airport: str, family: str, k: int, cell: int) -> ScenarioSpec:
    spec = _stratified_spec_seeded(airport, family, STRATIFIED_BASE_SEED + 1000 * cell + k)
    return replace(spec, scenario_id=f"strat-{PARTITION_BY_K[k]}-{airport.lower()}-{family}-k{k}")


def _stratified_spec_seeded(airport: str, family: str, seed: int) -> ScenarioSpec:
    rng = random.Random(seed)
    a = replace(ChallengeAssumptions(), approach_groundspeed_kt=float(rng.choice([120, 140, 160])),
                max_overtime_min=rng.choice([30, 60, 90]))
    kw: dict = {"delay_behind_schedule_min": rng.randint(5, 25), "traffic_utilisation": round(rng.uniform(0.4, 0.7), 3)}
    if family == "short_delay":
        kw["delay_behind_schedule_min"] = rng.randint(0, 10)
    elif family == "long_delay":
        kw["delay_behind_schedule_min"] = rng.randint(45, 90)
    elif family == "runway_reduced":
        kw["closed_runways"] = ((0, 0, rng.randint(20, 60)),)
    elif family == "wake_dense":
        kw["traffic_utilisation"] = round(rng.uniform(0.85, 1.0), 3)
        kw["heavy_share"] = round(rng.uniform(0.3, 0.5), 3)
    elif family == "gate_conflict":
        kw["assigned_gate_busy_until"] = rng.randint(25, 50)
        kw["spare_gates_busy"] = rng.randint(1, 3)
    elif family == "tight_ground":
        kw["crew_available_from"] = (rng.randint(40, 70), 0)
        kw["crew_regular_end_offsets"] = (rng.randint(30, 60), rng.randint(10, 40))
    elif family == "tight_fuel":
        a = replace(a, taxi_burn_kg_per_min=float(rng.randint(20, 35)))
    return ScenarioSpec(scenario_id=f"seeded-{airport.lower()}-{family}-{seed}", airport=airport,
                        category=family, seed=seed, assumptions=a, **kw)


def stratified_specs(partition: str | None = None) -> list[ScenarioSpec]:
    specs = []
    for ai, airport in enumerate(ANCHOR_ROW_INDEX):
        for fi, family in enumerate(STRATIFIED_FAMILIES):
            for k in range(5):
                if partition is None or PARTITION_BY_K[k] == partition:
                    specs.append(_baseline(_stratified_spec(airport, family, k, cell=ai * len(STRATIFIED_FAMILIES) + fi)))
    return specs


def at_resolution(spec: ScenarioSpec, slot_step_min: int) -> ScenarioSpec:
    """Same scenario at another landing-time resolution (labelled in the scenario id)."""
    return replace(spec, scenario_id=f"{spec.scenario_id}-step{slot_step_min}",
                   assumptions=replace(spec.assumptions, slot_step_min=slot_step_min))


# Independent final evaluation set for the candidate-generator change (never used before):
# 4 tiers x 8 families x 2 seeds, new seed base, fixed before any result is inspected.
FINAL_BASE_SEED = 9_100_000


def final_evaluation_specs() -> list[ScenarioSpec]:
    specs = []
    for ai, airport in enumerate(ANCHOR_ROW_INDEX):
        for fi, family in enumerate(STRATIFIED_FAMILIES):
            for j in range(2):
                seed = FINAL_BASE_SEED + 1000 * (ai * len(STRATIFIED_FAMILIES) + fi) + j
                sp = _stratified_spec_seeded(airport, family, seed)
                specs.append(_baseline(replace(sp, scenario_id=f"final-{airport.lower()}-{family}-j{j}")))
    return specs


def with_fuel_reading(spec: ScenarioSpec, includes_reserve: bool) -> ScenarioSpec:
    """Paired fuel-interpretation variant (labelled sensitivity case, same seed)."""
    tag = "fuelA" if includes_reserve else "fuelB"
    return replace(spec, scenario_id=f"{spec.scenario_id}-{tag}",
                   assumptions=replace(spec.assumptions, spare_fuel_includes_final_reserve=includes_reserve))


def operations_fingerprint(s: Scenario) -> str:
    """Hash of the decision inputs only (ignores ids/labels) for near-duplicate detection."""
    from airspace.contracts import canonical_hash

    ops = s.model_dump(mode="json")["operations"]
    ops["subject"].pop("flight_label")
    return canonical_hash({"ops": ops, "grid": [s.earliest_landing_min, s.planning_horizon_min, s.slot_step_min]})
