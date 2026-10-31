"""Ingestion and scenario-builder tests.

Tests marked ``real_data`` use the genuine flights.csv when present and are
skipped otherwise; every other test uses small SYNTHETIC CSVs written to tmp_path.
"""

from __future__ import annotations

import csv
import json
import random
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from airspace.contracts import canonical_hash
from airspace.data import challenge as ch
from airspace.data import fixtures as fx
from airspace.data.ingest import (
    FLIGHT_COLUMNS,
    PROVENANCE_KEY,
    InvalidHHMMError,
    MissingColumnsError,
    NonQualifyingRowError,
    NoQualifyingRowError,
    default_data_dir,
    parse_hhmm,
    select_flight,
)
from airspace.data.scenario import MissingSafetyInputError, ScenarioBuildError, build_scenario, synthetic_row
from airspace.engine.pipeline import solve_scenario

FLIGHTS = default_data_dir() / "flights.csv"
real_data = pytest.mark.skipif(not FLIGHTS.exists(), reason="genuine flights.csv not present")


# ---- HHMM -----------------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [("5", (0, 5, 0)), ("0005", (0, 5, 0)), ("0", (0, 0, 0)), ("0000", (0, 0, 0)),
     ("2359", (23, 59, 0)), ("2400", (0, 0, 1)), ("1230", (12, 30, 0)), (930, (9, 30, 0))],
)
def test_hhmm_parsing(text, expected):
    t = parse_hhmm(text)
    assert (t.hour, t.minute, t.day_offset) == expected


@pytest.mark.parametrize("bad", ["2401", "2460", "2500", "1260", "abc", "-5", "12345", "12.5", True])
def test_hhmm_invalid_rejected(bad):
    with pytest.raises(InvalidHHMMError):
        parse_hhmm(bad)


def test_hhmm_null_preserved():
    assert parse_hhmm(None) is None and parse_hhmm("") is None


# ---- synthetic CSV helpers ------------------------------------------------
BASE_ROW = {
    "YEAR": "2015", "MONTH": "10", "DAY": "1", "DAY_OF_WEEK": "4", "AIRLINE": "AA", "FLIGHT_NUMBER": "1",
    "TAIL_NUMBER": "N1", "ORIGIN_AIRPORT": "AAA", "DESTINATION_AIRPORT": "BBB", "SCHEDULED_DEPARTURE": "0800",
    "DEPARTURE_TIME": "0810", "DEPARTURE_DELAY": "10", "TAXI_OUT": "10", "WHEELS_OFF": "0820",
    "SCHEDULED_TIME": "120", "ELAPSED_TIME": "130", "AIR_TIME": "100", "DISTANCE": "500", "WHEELS_ON": "1000",
    "TAXI_IN": "5", "SCHEDULED_ARRIVAL": "2400", "ARRIVAL_TIME": "0030", "ARRIVAL_DELAY": "30", "DIVERTED": "0",
    "CANCELLED": "0", "CANCELLATION_REASON": "", "AIR_SYSTEM_DELAY": "", "SECURITY_DELAY": "",
    "AIRLINE_DELAY": "", "LATE_AIRCRAFT_DELAY": "", "WEATHER_DELAY": "",
}


def write_csv(dir_: Path, rows: list[dict], *, airports: list[dict] | None = None) -> Path:
    path = dir_ / "flights.csv"
    with path.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(FLIGHT_COLUMNS))
        w.writeheader()
        for r in rows:
            w.writerow({**BASE_ROW, **r})
    (dir_ / "airlines.csv").write_text("IATA_CODE,AIRLINE\nAA,American Airlines Inc.\n")
    airports = airports if airports is not None else [
        {"IATA_CODE": "AAA", "AIRPORT": "Alpha", "CITY": "A", "STATE": "AA", "COUNTRY": "USA", "LATITUDE": "1.5", "LONGITUDE": "-2.5"},
        {"IATA_CODE": "BBB", "AIRPORT": "Bravo", "CITY": "B", "STATE": "BB", "COUNTRY": "USA", "LATITUDE": "", "LONGITUDE": ""},
    ]
    with (dir_ / "airports.csv").open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["IATA_CODE", "AIRPORT", "CITY", "STATE", "COUNTRY", "LATITUDE", "LONGITUDE"])
        w.writeheader()
        w.writerows(airports)
    return path


THRESHOLD_ROWS = [
    {"WEATHER_DELAY": "15", "LATE_AIRCRAFT_DELAY": "20"},  # 0: both exactly at threshold -> no
    {"WEATHER_DELAY": "", "LATE_AIRCRAFT_DELAY": ""},  # 1: nulls -> no
    {"WEATHER_DELAY": "16", "CANCELLED": "1", "ARRIVAL_TIME": "", "ARRIVAL_DELAY": ""},  # 2: cancelled
    {"LATE_AIRCRAFT_DELAY": "25", "DIVERTED": "1"},  # 3: diverted
    {"LATE_AIRCRAFT_DELAY": "21"},  # 4: first qualifying (> 20)
    {"WEATHER_DELAY": "16"},  # 5: also qualifies (> 15)
]


def test_strict_threshold_and_first_qualifying_original_index(tmp_path):
    path = write_csv(tmp_path, THRESHOLD_ROWS)
    for chunk_rows in (1, 2, 4, 1000):  # selection must not depend on chunking
        row = select_flight(path, chunk_rows=chunk_rows)
        prov = row[PROVENANCE_KEY]
        assert prov["row_index"] == 4, chunk_rows
        assert prov["selection_method"] == "first_qualifying_in_file_order"
    assert row["LATE_AIRCRAFT_DELAY"] == 21
    assert row["WEATHER_DELAY"] is None  # null preserved, not 0
    assert row["AIR_SYSTEM_DELAY"] is None
    assert row["SCHEDULED_ARRIVAL"] == "2400"
    assert prov["derived_local_clock_times"]["SCHEDULED_ARRIVAL"]["day_offset"] == 1
    assert prov["row_hash"] == canonical_hash({c: row[c] for c in FLIGHT_COLUMNS})


def test_explicit_row_index_qualifying(tmp_path):
    path = write_csv(tmp_path, THRESHOLD_ROWS)
    row = select_flight(path, row_index=5, chunk_rows=2)
    assert row[PROVENANCE_KEY]["row_index"] == 5
    assert row[PROVENANCE_KEY]["selection_method"] == "explicit_row_index"
    assert row["WEATHER_DELAY"] == 16


@pytest.mark.parametrize("index,fragment", [(0, "predicate false"), (1, "null never qualifies"),
                                            (2, "CANCELLED"), (3, "DIVERTED"), (99, "out of range")])
def test_explicit_nonqualifying_row_is_never_replaced(tmp_path, index, fragment):
    path = write_csv(tmp_path, THRESHOLD_ROWS)
    with pytest.raises(NonQualifyingRowError, match=fragment) as err:
        select_flight(path, row_index=index, chunk_rows=2)
    assert err.value.row_index == index


def test_no_qualifying_row(tmp_path):
    path = write_csv(tmp_path, THRESHOLD_ROWS[:4])
    with pytest.raises(NoQualifyingRowError):
        select_flight(path)


def test_malformed_values_rejected_for_explicit_row_and_skipped_counted_otherwise(tmp_path):
    rows = [{"LATE_AIRCRAFT_DELAY": "21", "ARRIVAL_TIME": "2460"}, {"WEATHER_DELAY": "40"}]
    path = write_csv(tmp_path, rows)
    with pytest.raises(NonQualifyingRowError, match="invalid value"):
        select_flight(path, row_index=0)
    row = select_flight(path)
    assert row[PROVENANCE_KEY]["row_index"] == 1
    assert row[PROVENANCE_KEY]["skipped_invalid_candidate_rows"] == 1


def test_missing_required_column(tmp_path):
    path = tmp_path / "flights.csv"
    path.write_text("YEAR,MONTH\n2015,1\n")
    with pytest.raises(MissingColumnsError):
        select_flight(path)


def test_numeric_airport_ids_unresolved_unless_explicitly_mapped(tmp_path):
    path = write_csv(tmp_path, [{"ORIGIN_AIRPORT": "10397", "DESTINATION_AIRPORT": "BBB", "WEATHER_DELAY": "30"}])
    joins = select_flight(path)[PROVENANCE_KEY]["joins"]
    origin = joins["origin_airport"]
    assert origin["status"] == "unresolved" and origin["source_id"] == "10397"
    assert "latitude" not in origin  # nothing guessed
    dest = joins["destination_airport"]
    assert dest["status"] == "resolved" and dest["latitude"] is None and dest["longitude"] is None

    mapping = tmp_path / "airport_id_map.csv"
    mapping.write_text("SOURCE_ID,IATA_CODE\n10397,AAA\n")
    row = select_flight(path, airport_id_map_path=mapping)
    origin = row[PROVENANCE_KEY]["joins"]["origin_airport"]
    assert origin["status"] == "mapped_explicit" and origin["iata_code"] == "AAA"
    assert origin["source_id"] == "10397" and row["ORIGIN_AIRPORT"] == "10397"  # original id kept


def test_airport_ids_read_as_text(tmp_path):
    path = write_csv(tmp_path, [{"ORIGIN_AIRPORT": "01234", "WEATHER_DELAY": "30"}])
    assert select_flight(path)["ORIGIN_AIRPORT"] == "01234"


# ---- scenario builder ------------------------------------------------------
def _write_config(tmp_path: Path, cfg: dict, name: str = "cfg.json") -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(cfg))
    return p


def test_historical_anchor_scenario_and_unresolved_join_stays_visible(tmp_path):
    path = write_csv(tmp_path, [{"ORIGIN_AIRPORT": "10397", "WEATHER_DELAY": "30"}])
    row = select_flight(path)
    scenario = build_scenario(row, _write_config(tmp_path, fx.config_dict("success")))
    assert scenario.source_mode == "historical_anchor"
    ref = scenario.historical_ref
    assert ref["row_index"] == 0 and ref["selection_predicate"].startswith("WEATHER_DELAY > 15")
    assert ref["origin_join_status"] == "unresolved"
    origin_ev = next(e for e in scenario.provenance if e.evidence_id == "hist:join.origin_airport")
    assert origin_ev.note.startswith("UNVERIFIED")
    kinds = {e.kind for e in scenario.provenance}
    assert kinds == {"historical", "derived", "simulated"}


def test_synthetic_scenario_records_missing_data(tmp_path):
    scenario = build_scenario(synthetic_row("flights.csv absent"), _write_config(tmp_path, fx.config_dict("success")))
    assert scenario.source_mode == "synthetic" and scenario.historical_row == {}
    assert scenario.historical_ref["mode"] == "synthetic"
    assert any(e.evidence_id == "sim:scenario.synthetic_anchor" for e in scenario.provenance)


def test_unlabelled_or_edited_rows_refused(tmp_path):
    cfg = _write_config(tmp_path, fx.config_dict("success"))
    with pytest.raises(ScenarioBuildError, match="lacks"):
        build_scenario({c: None for c in FLIGHT_COLUMNS}, cfg)
    row = fx.load_anchor_row()
    row = {**row, "ARRIVAL_DELAY": 0}
    with pytest.raises(ScenarioBuildError, match="row_hash"):
        build_scenario(row, cfg)
    with pytest.raises(ScenarioBuildError, match="must not carry"):
        build_scenario({**synthetic_row(), "WEATHER_DELAY": 30}, cfg)


@pytest.mark.parametrize("section", ["fuel", "separation", "crew", "taxi_routes"])
def test_missing_safety_inputs_fail_specifically(tmp_path, section):
    cfg = fx.config_dict("success")
    del cfg["operations"][section]
    with pytest.raises(MissingSafetyInputError, match=f"operations.{section}"):
        build_scenario(synthetic_row(), _write_config(tmp_path, cfg))
    cfg = fx.config_dict("success")
    del cfg["operations"]["fuel"]["min_reserve_kg"]
    with pytest.raises(MissingSafetyInputError, match="operations.fuel.min_reserve_kg"):
        build_scenario(synthetic_row(), _write_config(tmp_path, cfg))


def _shuffle_keys(obj, rng):
    if isinstance(obj, dict):
        items = list(obj.items())
        rng.shuffle(items)
        return {k: _shuffle_keys(v, rng) for k, v in items}
    if isinstance(obj, list):
        return [_shuffle_keys(v, rng) for v in obj]
    return obj


def test_state_hash_stable_across_key_order_file_name_and_time(tmp_path):
    row = fx.load_anchor_row()
    cfg = fx.config_dict("success")
    first = build_scenario(row, _write_config(tmp_path, cfg, "a.json"))
    rng = random.Random(7)
    for i in range(5):
        shuffled = _shuffle_keys(cfg, rng)
        other = build_scenario(row, _write_config(tmp_path, shuffled, f"b{i}.json"))
        assert other.state_hash == first.state_hash
    assert first.state_hash == fx.load_fixture_scenario("success").state_hash
    cfg["operations"]["fuel"]["min_reserve_kg"] = 3399.0
    assert build_scenario(row, _write_config(tmp_path, cfg, "c.json")).state_hash != first.state_hash


def test_historical_outcome_fields_do_not_leak_into_decisions(tmp_path):
    cfg = _write_config(tmp_path, fx.config_dict("success"))
    row = fx.load_anchor_row()
    edited = {**row, "ARRIVAL_DELAY": 0, "ARRIVAL_TIME": "0001", "WHEELS_ON": "0000", "WEATHER_DELAY": 999}
    edited[PROVENANCE_KEY] = {**row[PROVENANCE_KEY], "row_hash": canonical_hash({c: edited[c] for c in FLIGHT_COLUMNS})}
    a, b = solve_scenario(build_scenario(row, cfg)), solve_scenario(build_scenario(edited, cfg))
    assert a.state_hash != b.state_hash  # context differs ...
    assert [x.plan.plan_id for x in a.assessments] == [x.plan.plan_id for x in b.assessments]
    assert [x.utilities for x in a.assessments] == [x.utilities for x in b.assessments]  # ... decisions do not
    assert (a.fallback.plan_id, a.compromise.plan_id) == (b.fallback.plan_id, b.compromise.plan_id)


def test_committed_anchor_row_is_a_labelled_genuine_extract():
    row = fx.load_anchor_row()
    prov = row[PROVENANCE_KEY]
    assert prov["mode"] == "historical" and prov["source_file"] == "flights.csv"
    assert len(prov["source_sha256"]) == 64
    assert row["WEATHER_DELAY"] > 15 or row["LATE_AIRCRAFT_DELAY"] > 20


# ---- genuine flights.csv ------------------------------------------------------
@real_data
def test_real_first_qualifying_row_matches_committed_anchor():
    row = select_flight(FLIGHTS)
    assert row == fx.load_anchor_row()
    assert row[PROVENANCE_KEY]["row_index"] == 70


@real_data
def test_real_explicit_nonqualifying_row_raises():
    with pytest.raises(NonQualifyingRowError, match="null never qualifies"):
        select_flight(FLIGHTS, row_index=0)


@real_data
def test_real_october_numeric_airport_ids_flagged_unresolved():
    # Genuine Oct-2015 row with numeric airport ids (14057 -> 14747), WEATHER_DELAY = 26.
    row = select_flight(FLIGHTS, row_index=4386398)
    joins = row[PROVENANCE_KEY]["joins"]
    assert row["ORIGIN_AIRPORT"] == "14057" and row["WEATHER_DELAY"] == 26
    assert joins["origin_airport"]["status"] == "unresolved"
    assert joins["destination_airport"]["status"] == "unresolved"


# ---- reader resource management (regression: handle leaked on exception paths) ----
@pytest.fixture
def tracked_readers(monkeypatch):
    """Record every chunked reader select_flight opens; closure is then checked directly,
    independent of garbage collection."""
    import airspace.data.ingest as ingest

    real = ingest.pd.read_csv
    readers = []

    def spy(*args, **kwargs):
        result = real(*args, **kwargs)
        if kwargs.get("chunksize"):
            readers.append(result)
        return result

    monkeypatch.setattr(ingest.pd, "read_csv", spy)
    return readers


def _assert_all_closed(readers, expected_count):
    assert len(readers) == expected_count
    assert all(r.handles.handle.closed for r in readers), "chunked flights.csv reader left open"


def test_reader_closed_after_early_return(tmp_path, tracked_readers):
    row = select_flight(write_csv(tmp_path, THRESHOLD_ROWS), chunk_rows=2)  # match in chunk 3 of 3
    assert row[PROVENANCE_KEY]["row_index"] == 4
    _assert_all_closed(tracked_readers, 1)


def test_reader_closed_when_chunk_processing_raises(tmp_path, tracked_readers, monkeypatch):
    import airspace.data.ingest as ingest

    def boom(chunk):
        raise RuntimeError("simulated failure while processing a chunk")

    monkeypatch.setattr(ingest, "_vector_candidates", boom)
    with pytest.raises(RuntimeError, match="simulated"):
        select_flight(write_csv(tmp_path, THRESHOLD_ROWS), chunk_rows=2)
    _assert_all_closed(tracked_readers, 1)


@pytest.mark.parametrize("index", [0, 1, 2, 3])  # predicate false / nulls / cancelled / diverted
def test_reader_closed_when_explicit_row_fails_validation(tmp_path, tracked_readers, index):
    with pytest.raises(NonQualifyingRowError):
        select_flight(write_csv(tmp_path, THRESHOLD_ROWS), row_index=index, chunk_rows=2)
    _assert_all_closed(tracked_readers, 1)


def test_reader_closed_when_explicit_row_has_malformed_value(tmp_path, tracked_readers):
    path = write_csv(tmp_path, [{"LATE_AIRCRAFT_DELAY": "21", "ARRIVAL_TIME": "2460"}])
    with pytest.raises(NonQualifyingRowError, match="invalid value"):
        select_flight(path, row_index=0)
    _assert_all_closed(tracked_readers, 1)


def test_reader_closed_when_requested_row_missing(tmp_path, tracked_readers):
    with pytest.raises(NonQualifyingRowError, match="out of range"):
        select_flight(write_csv(tmp_path, THRESHOLD_ROWS), row_index=99, chunk_rows=2)
    _assert_all_closed(tracked_readers, 1)


def test_reader_closed_after_normal_exhaustion(tmp_path, tracked_readers):
    with pytest.raises(NoQualifyingRowError):
        select_flight(write_csv(tmp_path, THRESHOLD_ROWS[:4]), chunk_rows=2)
    _assert_all_closed(tracked_readers, 1)


def test_repeated_sequential_ingestion_is_closed_and_identical(tmp_path, tracked_readers):
    path = write_csv(tmp_path, THRESHOLD_ROWS)
    results = [select_flight(path, chunk_rows=2) for _ in range(5)]
    assert all(r == results[0] for r in results)
    _assert_all_closed(tracked_readers, 5)


# ---- challenge workbook (challenge2_operational_constraints.xlsx) --------------------

WORKBOOK = default_data_dir() / ch.WORKBOOK_NAME
EXTRACT = fx.FIXTURES_DIR / ch.EXTRACT_NAME


def _make_workbook(path: Path, mutate=None) -> Path:
    """Write a structurally faithful workbook from the committed extract (SYNTHETIC test copy)."""
    openpyxl = pytest.importorskip("openpyxl")
    data = json.loads(EXTRACT.read_text())
    sheets = {
        "airport_capacity": [[data["sheet_notes"]["airport_capacity"]], [], list(ch.CAPACITY_COLUMNS)]
        + [[a["iata_code"], a["airport"], "c", "ST", a["predicate_rows_as_origin"], a["size_class"], a["active_runways"],
            a["gates"], a["max_runway_ops_per_hour"], a["min_turnaround_min"], a["spare_gates_at_any_time"]]
           for a in data["airports"].values()],
        "wake_separation": [[data["sheet_notes"]["wake_separation"]], [], list(ch.WAKE_COLUMNS)]
        + [[{v: k for k, v in ch._WAKE_LABELS.items()}.get(r["leader"], ch.WAKE_WILDCARD),
            {v: k for k, v in ch._WAKE_LABELS.items()}.get(r["follower"]), r["min_separation_nm"], r["min_departure_time_s"]]
           for r in data["wake_rules"]],
        "fuel_and_crew": [["rule", "value", "applies_to"]] + [
            ["Final fuel reserve", "30 minutes of holding fuel", "x"], ["Contingency fuel", "5% of trip fuel", "x"],
            ["Holding fuel burn (narrowbody)", "2,400 kg per hour", "x"], ["Holding fuel burn (widebody)", "6,500 kg per hour", "x"],
            ["Assumed spare fuel on arrival", "45 minutes of holding (unless the team states otherwise)", "x"],
            ["Flight crew max duty", "13 hours from report time", "x"],
            ["Ground crew shift", "8 hours; overtime costs 1.5x after that", "x"],
            ["Gate change notice", "At least 20 minutes before arrival", "x"]],
        "rules_of_play": [["topic", "detail"]] + [[k, v] for k, v in data["rules_of_play"].items()],
    }
    if mutate:
        mutate(sheets)
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    wb.save(path)
    return path


def test_workbook_loader_round_trips_extract(tmp_path):
    c = ch.load_workbook(_make_workbook(tmp_path / "wb.xlsx"))
    ref = ch.load_extract(EXTRACT)
    strip = lambda d: {k: replace(v, source_row=0) for k, v in d.items()}  # test copy has different row numbers
    assert strip(c.airports) == strip(ref.airports) and c.fuel_crew == ref.fuel_crew
    assert [replace(r, source_row=0) for r in c.wake_rules] == [replace(r, source_row=0) for r in ref.wake_rules]
    assert c.fuel_crew.holding_burn_narrowbody_kg_per_h == 2400 and c.fuel_crew.ground_overtime_multiplier == 1.5


@pytest.mark.parametrize("mutation,match", [
    (lambda s: s["airport_capacity"][2].__setitem__(6, "runways"), "columns changed"),
    (lambda s: s["airport_capacity"].append(list(s["airport_capacity"][3])), "duplicate IATA_CODE"),
    (lambda s: s["airport_capacity"][3].__setitem__(0, "10397"), "invalid IATA_CODE"),  # numeric ids never enter
    (lambda s: s["airport_capacity"][3].__setitem__(7, None), "expected integer"),  # missing value, not 0
    (lambda s: s["airport_capacity"][3].__setitem__(10, 999), "spare gates exceed"),
    (lambda s: s["wake_separation"][-1].__setitem__(1, "Heavy"), "wildcard row must have an empty follower"),
    (lambda s: s["wake_separation"][3].__setitem__(0, "Jumbo"), "unknown wake category"),
    (lambda s: s["fuel_and_crew"][3].__setitem__(1, "about 2400 kg/h"), "unrecognised value"),
    (lambda s: s["fuel_and_crew"].pop(), "rules changed"),
])
def test_workbook_loader_fails_closed(tmp_path, mutation, match):
    path = _make_workbook(tmp_path / "wb.xlsx", mutation)
    with pytest.raises(ch.ChallengeDataError, match=match):
        ch.load_workbook(path)


def test_numeric_october_airport_ids_are_never_mapped_to_profiles():
    with pytest.raises(ch.ChallengeDataError, match="never mapped"):
        ch.load_extract(EXTRACT).airport("14057")


def test_wake_wildcard_and_conversion():
    from airspace.data.scenario_suite import separation_minutes

    c = ch.load_extract(EXTRACT)
    assert c.wake("large", "large").leader == "*"  # unlisted pair -> 'Any other pair'
    assert c.wake("heavy", "large").min_separation_nm == 5.0
    sep = separation_minutes(c, 140.0)
    assert sep[("heavy", "large")] == 3  # ceil(max(5/140*60=2.14, 120 s=2.0))
    assert sep[("super", "small")] == 4  # ceil(max(8/140*60=3.43, 3.0))
    assert sep[("large", "large")] == 2  # ceil(max(1.29, 1.0))
    assert separation_minutes(c, 280.0)[("super", "small")] == 3  # departure time dominates at high speed


@pytest.mark.skipif(not WORKBOOK.exists(), reason="original challenge workbook not present")
def test_real_workbook_matches_committed_extract():
    pytest.importorskip("openpyxl")
    c = ch.load_workbook(WORKBOOK)
    data = json.loads(EXTRACT.read_text())
    assert data["source_sha256"] == c.source_sha256
    assert data == ch.to_extract(c, list(data["airports"]))
    assert len(c.airports) == 322


@real_data
@pytest.mark.skipif(not WORKBOOK.exists(), reason="original challenge workbook not present")
def test_real_workbook_count_column_is_derived_from_flights_csv():
    """'delayed_departures_in_flights_csv' == per-ORIGIN count of the selection predicate."""
    pytest.importorskip("openpyxl")
    c = ch.load_workbook(WORKBOOK)
    counts: dict[str, int] = {}
    with pd.read_csv(FLIGHTS, usecols=["ORIGIN_AIRPORT", "WEATHER_DELAY", "LATE_AIRCRAFT_DELAY"],
                     dtype={"ORIGIN_AIRPORT": str}, chunksize=1_000_000) as reader:
        for chunk in reader:
            hit = chunk[(chunk.WEATHER_DELAY > 15) | (chunk.LATE_AIRCRAFT_DELAY > 20)]
            for k, v in hit.ORIGIN_AIRPORT.value_counts().items():
                counts[k] = counts.get(k, 0) + int(v)
    assert all(counts.get(code, 0) == p.predicate_rows_as_origin for code, p in c.airports.items())


def test_committed_anchor_rows_are_genuine_qualifying_extracts():
    from airspace.data.scenario_suite import ANCHOR_ROW_INDEX, load_anchor_rows

    rows = load_anchor_rows()
    assert set(rows) == set(ANCHOR_ROW_INDEX)
    for code, row in rows.items():
        prov = row[PROVENANCE_KEY]
        assert prov["row_index"] == ANCHOR_ROW_INDEX[code] and row["DESTINATION_AIRPORT"] == code
        assert prov["row_hash"] == canonical_hash({c: row[c] for c in FLIGHT_COLUMNS})
        assert row["WEATHER_DELAY"] > 15 or row["LATE_AIRCRAFT_DELAY"] > 20


@real_data
def test_real_anchor_rows_reproduce_from_flights_csv():
    from airspace.data.scenario_suite import ANCHOR_ROW_INDEX, load_anchor_rows

    rows = load_anchor_rows()
    for code, idx in ANCHOR_ROW_INDEX.items():
        assert select_flight(FLIGHTS, row_index=idx) == rows[code]
