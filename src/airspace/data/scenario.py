"""Build a sealed Scenario from a selected row plus a versioned simulation config.

Public API (Shared Contract v1):
    build_scenario(row, config_path) -> Scenario

`row` is either the output of select_flight (historical anchor) or
synthetic_row() (no historical row exists / flights.csv absent). Every
decision input comes from the SIMULATED config; the historical row is
context only and is never read by the engine (no leakage of ARRIVAL_TIME,
ARRIVAL_DELAY, WHEELS_ON, ... into the decision state).
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from airspace.contracts import (
    OPERATIONS_SECTIONS,
    SCENARIO_EPOCH_EVIDENCE_ID,
    SCENARIO_PLANNING_EVIDENCE_ID,
    SCHEMA_VERSION,
    Evidence,
    Scenario,
    SimulationConfig,
    canonical_hash,
    section_evidence_id,
)
from airspace.data.ingest import FLIGHT_COLUMNS, PROVENANCE_KEY, PROVENANCE_NAMESPACE

# Sections whose absence makes the toy safety model undefined.
SAFETY_CRITICAL_PATHS = (
    "earliest_landing_min",
    "planning_horizon_min",
    "slot_step_min",
    "operations.subject",
    "operations.separation",
    "operations.runways",
    "operations.runway_movements",
    "operations.gates",
    "operations.gate_occupancy",
    "operations.taxi_routes",
    "operations.taxi_blockages",
    "operations.service",
    "operations.fuel",
    "operations.crew",
)

_SECTION_NOTES = {
    "subject": "Simulated subject aircraft (wake category, runway occupancy, assigned gate, scheduled gate-in). Not in flights.csv.",
    "separation": "Toy pair-specific wake separation matrix in minutes. Simulated rule, NOT an FAA/ICAO/Airbus standard.",
    "runways": "Simulated runway open windows (half-open). No runway state exists in the dataset; WEATHER_DELAY never implies a closure.",
    "runway_movements": "Fixed simulated background movements; never re-sequenced by the planner.",
    "gates": "Simulated gate compatibility, open windows and turnover buffers.",
    "gate_occupancy": "Simulated existing gate occupancy before buffers.",
    "taxi_routes": "Simulated runway->gate taxi routes; only listed pairs are connected.",
    "taxi_blockages": "Simulated taxi segment blockage windows.",
    "service": "Simulated turnaround service duration.",
    "fuel": "Simulated fuel snapshot, burn rates and minimum reserve. Toy rule, NOT an operational reserve standard; fuel is not derived from DISTANCE.",
    "crew": "Simulated ground crew shifts (hard availability) and regular-hours end (soft overtime).",
    "connections": "ASSUMED connection groups and deadlines; the dataset has no passenger-level information.",
    "turbulence": "Configured turbulence-exposure PROXY; no turbulence data exists in the dataset.",
    "utility": "Demonstration weights/scales (Shared Contract v1 defaults unless a labelled sensitivity run). Not fitted evidence.",
    "planning": "Candidate catalogue cap; exceeding it is an explicit planning error.",
}


class ScenarioBuildError(ValueError):
    pass


class MissingSafetyInputError(ScenarioBuildError):
    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__("missing safety-critical simulation inputs: " + ", ".join(paths))


def synthetic_row(note: str = "No historical flights.csv row used; synthetic scenario.") -> dict[str, object]:
    """A row placeholder for synthetic scenarios. Carries NO fake historical values."""
    return {PROVENANCE_KEY: {"namespace": PROVENANCE_NAMESPACE, "mode": "synthetic", "note": note}}


def load_simulation_config(config_path: Path) -> SimulationConfig:
    text = Path(config_path).read_text(encoding="utf-8")
    try:
        return SimulationConfig.model_validate_json(text)
    except ValidationError as exc:
        missing = [
            ".".join(str(p) for p in err["loc"])
            for err in exc.errors()
            if err["type"] == "missing"
        ]
        if missing:
            raise MissingSafetyInputError(missing) from exc
        raise


def _historical_parts(row: dict[str, object], prov: dict[str, object]) -> tuple[dict, dict, list[Evidence]]:
    missing_cols = [c for c in FLIGHT_COLUMNS if c not in row]
    if missing_cols:
        raise ScenarioBuildError(f"historical row missing columns {missing_cols}")
    hist_row = {c: row[c] for c in FLIGHT_COLUMNS}
    row_hash = canonical_hash(hist_row)
    if row_hash != prov.get("row_hash"):
        raise ScenarioBuildError("row values do not match their provenance row_hash (edited row?)")
    for key in ("source_file", "source_sha256", "row_index", "selection_predicate"):
        if key not in prov:
            raise ScenarioBuildError(f"historical provenance missing {key!r}")
    joins = prov.get("joins", {})
    ref: dict[str, str | int] = {
        "namespace": str(prov["namespace"]),
        "source_file": str(prov["source_file"]),
        "source_sha256": str(prov["source_sha256"]),
        "row_index": int(prov["row_index"]),  # type: ignore[arg-type]
        "row_index_basis": str(prov.get("row_index_basis", "")),
        "selection_predicate": str(prov["selection_predicate"]),
        "selection_method": str(prov.get("selection_method", "")),
        "row_hash": row_hash,
        "airline_join_status": str(joins.get("airline", {}).get("status", "not_attempted")),
        "origin_join_status": str(joins.get("origin_airport", {}).get("status", "not_attempted")),
        "destination_join_status": str(joins.get("destination_airport", {}).get("status", "not_attempted")),
    }
    source = f"{prov['source_file']}#row={prov['row_index']};sha256={prov['source_sha256']}"
    evidence = [
        Evidence(
            evidence_id="hist:row",
            kind="historical",
            field_path="historical_row",
            source_ref=source,
            note=(
                f"Selected by {prov['selection_predicate']} ({prov.get('selection_method')}). "
                "Delay causes are retrospective allocations: selection signal only, not live weather, "
                "holding, fuel or extra delay. Context only; not a decision input."
            ),
        )
    ]
    for name, join in sorted(joins.items()):
        status = join.get("status")
        if status in ("resolved", "mapped_explicit"):
            note = (
                f"{join.get('source_id')} -> {join.get('iata_code', '')} {join.get('airport') or join.get('name') or ''}"
                f" [{status}]"
            ).strip()
            if "latitude" in join:
                note += f"; lat={join.get('latitude')}, lon={join.get('longitude')} (None = missing, not zero)"
        else:
            note = f"UNVERIFIED: {join.get('source_id')!r} {status}: {join.get('reason', '')}".strip()
        evidence.append(
            Evidence(
                evidence_id=f"hist:join.{name}",
                kind="historical",
                field_path=f"historical_ref.{name.removesuffix('_airport')}_join_status",
                source_ref=source,
                note=note,
            )
        )
    for col, parsed in sorted(dict(prov.get("derived_local_clock_times", {})).items()):
        if parsed is None:
            continue
        evidence.append(
            Evidence(
                evidence_id=f"derived:clock.{col}",
                kind="derived",
                field_path=f"historical_row.{col}",
                source_ref=source,
                note=(
                    f"{hist_row[col]} parsed as local clock {parsed['local_clock']}; display only, "
                    "never subtracted across airports nor used as a UTC instant."
                ),
            )
        )
    return hist_row, ref, evidence


def build_scenario(row: dict[str, object], config_path: Path) -> Scenario:
    return scenario_from_config(row, load_simulation_config(Path(config_path)))


def scenario_from_config(row: dict[str, object], config: SimulationConfig) -> Scenario:
    """build_scenario for an already-validated in-memory SimulationConfig."""
    prov = row.get(PROVENANCE_KEY)
    if not isinstance(prov, dict) or prov.get("namespace") != PROVENANCE_NAMESPACE:
        raise ScenarioBuildError(
            f"row lacks a {PROVENANCE_KEY!r} entry from select_flight() or synthetic_row(); "
            "refusing to treat an unlabelled dict as historical"
        )
    config_canon = canonical_hash(config.model_dump(mode="json"))
    config_ref = f"config:{config.config_version};canonical_sha256={config_canon}"
    notes = {a.field_path: a.note for a in config.assumptions}

    provenance: list[Evidence] = []
    if prov.get("mode") == "historical":
        hist_row, hist_ref, hist_evidence = _historical_parts(row, prov)
        source_mode = "historical_anchor"
        provenance.extend(hist_evidence)
    elif prov.get("mode") == "synthetic":
        extra = [k for k in row if k != PROVENANCE_KEY]
        if extra:
            raise ScenarioBuildError(f"synthetic row must not carry flight values, got {extra[:5]}")
        hist_row = {}
        hist_ref = {"mode": "synthetic", "note": str(prov.get("note", "synthetic scenario"))}
        source_mode = "synthetic"
        provenance.append(
            Evidence(
                evidence_id="sim:scenario.synthetic_anchor",
                kind="simulated",
                field_path="historical_ref",
                source_ref=config_ref,
                note="SYNTHETIC: no historical flight row is claimed. " + str(prov.get("note", "")),
            )
        )
    else:
        raise ScenarioBuildError(f"unknown provenance mode {prov.get('mode')!r}")

    provenance.append(
        Evidence(
            evidence_id=SCENARIO_PLANNING_EVIDENCE_ID,
            kind="simulated",
            field_path="earliest_landing_min,planning_horizon_min,slot_step_min",
            source_ref=config_ref,
            note=notes.get(
                "planning",
                "Explicit simulation inputs on the relative clock; not derived from historical WHEELS_ON/ARRIVAL_TIME.",
            ),
        )
    )
    provenance.append(
        Evidence(
            evidence_id=SCENARIO_EPOCH_EVIDENCE_ID,
            kind="simulated",
            field_path="simulation_epoch_utc",
            source_ref=config_ref,
            note="Labelled simulation epoch (minute 0). NOT a reconstructed historical UTC instant.",
        )
    )
    for section in OPERATIONS_SECTIONS:
        provenance.append(
            Evidence(
                evidence_id=section_evidence_id(section),
                kind="simulated",
                field_path=f"operations.{section}",
                source_ref=config_ref,
                note=notes.get(f"operations.{section}", _SECTION_NOTES[section]),
            )
        )
    provenance.append(
        Evidence(
            evidence_id="derived:leakage_guard",
            kind="derived",
            field_path="operations",
            source_ref=config_ref,
            note=(
                "Decision state = operations + planning grid only. Historical ARRIVAL_TIME, ARRIVAL_DELAY, "
                "WHEELS_ON and delay causes are not decision inputs."
            ),
        )
    )

    return Scenario.seal(
        schema_version=SCHEMA_VERSION,
        scenario_id=config.scenario_id,
        source_mode=source_mode,
        historical_row=hist_row,
        historical_ref=hist_ref,
        simulation_epoch_utc=config.simulation_epoch_utc,
        earliest_landing_min=config.earliest_landing_min,
        planning_horizon_min=config.planning_horizon_min,
        slot_step_min=config.slot_step_min,
        operations=config.operations,
        provenance=provenance,
    )


def load_scenario(path: Path) -> Scenario:
    """Strictly validate a serialized Scenario (state hash re-checked)."""
    return Scenario.model_validate_json(Path(path).read_text(encoding="utf-8"))


def dump_scenario(scenario: Scenario, path: Path) -> None:
    Path(path).write_text(json.dumps(scenario.model_dump(mode="json"), indent=2) + "\n", encoding="utf-8")
