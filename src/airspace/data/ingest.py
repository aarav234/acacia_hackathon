"""Chunked, provenance-preserving selection of one historical flights.csv row.

Public API (Shared Contract v1):
    select_flight(csv_path, *, row_index=None) -> dict[str, object]

The returned dict holds the 31 normalized flights.csv columns plus ONE
namespaced key, PROVENANCE_KEY, which build_scenario requires. See
docs/data_and_model.md ("Ingestion provenance entry") for its fields.

Rules implemented here:
* selection predicate is strict: WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20;
  a null cause never satisfies the predicate and is never converted to 0;
* only completed, non-diverted, non-cancelled flights with arrival data qualify;
* row_index is the original zero-based data-row index (header excluded);
* an explicitly requested non-qualifying row raises - it is never replaced;
* airport ids are read as text; numeric ids not in the IATA lookup stay
  unresolved unless an explicit mapping file is supplied.
"""

from __future__ import annotations

import csv
import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from airspace.contracts import canonical_hash

PROVENANCE_KEY = "__airspace_provenance__"
PROVENANCE_NAMESPACE = "airspace.ingest/v1"
SELECTION_PREDICATE = "WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20"
SUITABILITY_RULE = (
    "CANCELLED == 0 AND DIVERTED == 0 AND SCHEDULED_ARRIVAL, WHEELS_ON, ARRIVAL_TIME, "
    "ARRIVAL_DELAY present"
)
WEATHER_THRESHOLD_MIN = 15
LATE_AIRCRAFT_THRESHOLD_MIN = 20
DEFAULT_CHUNK_ROWS = 200_000

FLIGHT_COLUMNS: tuple[str, ...] = (
    "YEAR", "MONTH", "DAY", "DAY_OF_WEEK", "AIRLINE", "FLIGHT_NUMBER", "TAIL_NUMBER",
    "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "SCHEDULED_DEPARTURE", "DEPARTURE_TIME",
    "DEPARTURE_DELAY", "TAXI_OUT", "WHEELS_OFF", "SCHEDULED_TIME", "ELAPSED_TIME",
    "AIR_TIME", "DISTANCE", "WHEELS_ON", "TAXI_IN", "SCHEDULED_ARRIVAL", "ARRIVAL_TIME",
    "ARRIVAL_DELAY", "DIVERTED", "CANCELLED", "CANCELLATION_REASON", "AIR_SYSTEM_DELAY",
    "SECURITY_DELAY", "AIRLINE_DELAY", "LATE_AIRCRAFT_DELAY", "WEATHER_DELAY",
)
HHMM_COLUMNS = frozenset(
    {"SCHEDULED_DEPARTURE", "DEPARTURE_TIME", "WHEELS_OFF", "WHEELS_ON", "SCHEDULED_ARRIVAL", "ARRIVAL_TIME"}
)
TEXT_COLUMNS = frozenset(
    {"AIRLINE", "TAIL_NUMBER", "ORIGIN_AIRPORT", "DESTINATION_AIRPORT", "CANCELLATION_REASON"}
)
INT_COLUMNS = frozenset(FLIGHT_COLUMNS) - HHMM_COLUMNS - TEXT_COLUMNS
AIRLINE_COLUMNS = ("IATA_CODE", "AIRLINE")
AIRPORT_COLUMNS = ("IATA_CODE", "AIRPORT", "CITY", "STATE", "COUNTRY", "LATITUDE", "LONGITUDE")

_INT_RE = re.compile(r"^-?\d+$")
_HHMM_RE = re.compile(r"^\d{1,4}$")


class IngestError(ValueError):
    """Base class for ingestion failures."""


class MissingColumnsError(IngestError):
    pass


class InvalidHHMMError(IngestError):
    pass


class InvalidValueError(IngestError):
    pass


class NonQualifyingRowError(IngestError):
    """An explicitly requested row does not satisfy predicate/suitability."""

    def __init__(self, row_index: int, reasons: list[str]):
        self.row_index = row_index
        self.reasons = reasons
        super().__init__(f"row {row_index} does not qualify: " + "; ".join(reasons))


class NoQualifyingRowError(IngestError):
    pass


# --------------------------------------------------------------------------
# HHMM local clock text
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ClockTime:
    """Historical LOCAL clock reading; contextual only, never a UTC instant."""

    hour: int
    minute: int
    day_offset: int  # 1 when the source said 2400 (midnight at end of day)

    @property
    def minutes_after_midnight(self) -> int:
        return self.hour * 60 + self.minute

    @property
    def display(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}" + (" (+1 day)" if self.day_offset else "")


def parse_hhmm(value: object) -> ClockTime | None:
    """Parse HHMM text: '5' -> 00:05, '0000' -> 00:00, '2400' -> 00:00 next day.

    None/'' -> None (null preserved). Anything else invalid raises InvalidHHMMError.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidHHMMError(f"HHMM must be text or int, got {type(value).__name__}")
    text = str(value).strip()
    if text == "":
        return None
    if not _HHMM_RE.match(text):
        raise InvalidHHMMError(f"invalid HHMM {value!r}")
    number = int(text)
    hour, minute = divmod(number, 100)
    if minute >= 60:
        raise InvalidHHMMError(f"invalid HHMM minutes in {value!r}")
    if hour == 24 and minute == 0:
        return ClockTime(0, 0, 1)
    if hour >= 24:
        raise InvalidHHMMError(f"invalid HHMM hour in {value!r}")
    return ClockTime(hour, minute, 0)


# --------------------------------------------------------------------------
# Row normalization
# --------------------------------------------------------------------------
def _parse_int(column: str, text: str) -> int | None:
    if text == "":
        return None
    if not _INT_RE.match(text):
        raise InvalidValueError(f"{column}: expected integer, got {text!r}")
    return int(text)


def normalize_row(raw: dict[str, str]) -> dict[str, str | int | None]:
    """Normalize raw text values. Missing values stay None (never zero)."""
    out: dict[str, str | int | None] = {}
    for col in FLIGHT_COLUMNS:
        text = (raw.get(col) or "").strip()
        if col in INT_COLUMNS:
            out[col] = _parse_int(col, text)
        elif col in HHMM_COLUMNS:
            parse_hhmm(text)  # validate; keep original clock text
            out[col] = text or None
        else:
            out[col] = text or None
    return out


def predicate_reasons(row: dict[str, str | int | None]) -> list[str]:
    """Reasons a normalized row fails predicate or suitability (empty = qualifies)."""
    reasons: list[str] = []
    weather, late = row["WEATHER_DELAY"], row["LATE_AIRCRAFT_DELAY"]
    weather_ok = isinstance(weather, int) and weather > WEATHER_THRESHOLD_MIN
    late_ok = isinstance(late, int) and late > LATE_AIRCRAFT_THRESHOLD_MIN
    if not (weather_ok or late_ok):
        reasons.append(
            f"predicate false: WEATHER_DELAY={weather!r} (needs > {WEATHER_THRESHOLD_MIN}), "
            f"LATE_AIRCRAFT_DELAY={late!r} (needs > {LATE_AIRCRAFT_THRESHOLD_MIN}); null never qualifies"
        )
    if row["CANCELLED"] != 0:
        reasons.append(f"not completed: CANCELLED={row['CANCELLED']!r}")
    if row["DIVERTED"] != 0:
        reasons.append(f"diverted or unknown: DIVERTED={row['DIVERTED']!r}")
    for col in ("SCHEDULED_ARRIVAL", "WHEELS_ON", "ARRIVAL_TIME", "ARRIVAL_DELAY"):
        if row[col] is None:
            reasons.append(f"missing {col}")
    return reasons


def _vector_candidates(chunk: pd.DataFrame) -> pd.Series:
    """Fast prefilter (exact same semantics as predicate_reasons for valid rows)."""
    weather = pd.to_numeric(chunk["WEATHER_DELAY"].where(chunk["WEATHER_DELAY"] != ""), errors="coerce")
    late = pd.to_numeric(chunk["LATE_AIRCRAFT_DELAY"].where(chunk["LATE_AIRCRAFT_DELAY"] != ""), errors="coerce")
    pred = (weather > WEATHER_THRESHOLD_MIN) | (late > LATE_AIRCRAFT_THRESHOLD_MIN)  # NaN -> False
    completed = (chunk["CANCELLED"].str.strip() == "0") & (chunk["DIVERTED"].str.strip() == "0")
    present = (
        (chunk["SCHEDULED_ARRIVAL"] != "")
        & (chunk["WHEELS_ON"] != "")
        & (chunk["ARRIVAL_TIME"] != "")
        & (chunk["ARRIVAL_DELAY"] != "")
    )
    return pred & completed & present


# --------------------------------------------------------------------------
# Lookups and joins
# --------------------------------------------------------------------------
_SHA_CACHE: dict[tuple[str, int, int], str] = {}


def file_sha256(path: Path) -> str:
    stat = path.stat()
    key = (str(path.resolve()), stat.st_size, stat.st_mtime_ns)
    if key not in _SHA_CACHE:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                digest.update(block)
        _SHA_CACHE[key] = digest.hexdigest()
    return _SHA_CACHE[key]


def _read_lookup(path: Path, required: tuple[str, ...]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise MissingColumnsError(f"{path.name} missing columns {missing}")
        return [{k: (v or "").strip() for k, v in r.items()} for r in reader]


def _optional_float(text: str) -> float | None:
    if text == "":
        return None  # missing coordinates are NOT zero
    return float(text)


def _join_airport(
    source_id: str | None,
    airports: dict[str, dict[str, str]] | None,
    id_map: dict[str, str] | None,
) -> dict[str, object]:
    entry: dict[str, object] = {"source_id": source_id}
    if source_id is None:
        entry.update(status="missing", reason="airport id absent in source row")
        return entry
    if airports is None:
        entry.update(status="lookup_unavailable", reason="airports.csv not supplied")
        return entry
    code = source_id
    if source_id not in airports and id_map is not None and source_id in id_map:
        code = id_map[source_id]
        entry["mapped_via"] = "explicit_airport_id_map"
    if code not in airports:
        reason = (
            "numeric airport id not present in the IATA lookup and no explicit mapping supplied"
            if source_id.isdigit()
            else "code not present in airports.csv"
        )
        entry.update(status="unresolved", reason=reason)
        return entry
    rec = airports[code]
    entry.update(
        status="mapped_explicit" if code != source_id else "resolved",
        iata_code=code,
        airport=rec["AIRPORT"] or None,
        city=rec["CITY"] or None,
        state=rec["STATE"] or None,
        country=rec["COUNTRY"] or None,
        latitude=_optional_float(rec["LATITUDE"]),
        longitude=_optional_float(rec["LONGITUDE"]),
    )
    return entry


def _default_lookup(csv_path: Path, name: str, explicit: Path | None) -> Path | None:
    if explicit is not None:
        return explicit
    candidate = csv_path.parent / name
    return candidate if candidate.exists() else None


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def select_flight(
    csv_path: Path,
    *,
    row_index: int | None = None,
    airlines_path: Path | None = None,
    airports_path: Path | None = None,
    airport_id_map_path: Path | None = None,
    chunk_rows: int = DEFAULT_CHUNK_ROWS,
) -> dict[str, object]:
    """Select one qualifying historical row (see module docstring).

    Contract signature is ``select_flight(csv_path, *, row_index=None)``; the extra
    keyword-only arguments are additive (documented in docs/contract_changes.md).
    Lookups default to airlines.csv / airports.csv next to csv_path. The airport
    id map is a CSV with columns SOURCE_ID,IATA_CODE and is never guessed.
    """
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"flights data not found: {csv_path}")
    if row_index is not None and (isinstance(row_index, bool) or not isinstance(row_index, int) or row_index < 0):
        raise IngestError(f"row_index must be a non-negative int, got {row_index!r}")

    header = pd.read_csv(csv_path, nrows=0).columns.tolist()
    missing = [c for c in FLIGHT_COLUMNS if c not in header]
    if missing:
        raise MissingColumnsError(f"{csv_path.name} missing required columns {missing}")

    reader = pd.read_csv(
        csv_path,
        dtype=str,
        keep_default_na=False,
        usecols=list(FLIGHT_COLUMNS),
        chunksize=chunk_rows,
    )
    selected: dict[str, str | int | None] | None = None
    selected_index: int | None = None
    skipped_invalid = 0
    offset = 0
    for chunk in reader:
        n = len(chunk)
        chunk = chunk.reset_index(drop=True)
        if row_index is not None:
            if row_index < offset + n:
                raw = chunk.iloc[row_index - offset].to_dict()
                try:
                    norm = normalize_row(raw)
                except IngestError as exc:
                    raise NonQualifyingRowError(row_index, [f"invalid value: {exc}"]) from exc
                reasons = predicate_reasons(norm)
                if reasons:
                    raise NonQualifyingRowError(row_index, reasons)
                selected, selected_index = norm, row_index
                break
        else:
            mask = _vector_candidates(chunk)
            for local in mask[mask].index:
                try:
                    norm = normalize_row(chunk.iloc[local].to_dict())
                except IngestError:
                    skipped_invalid += 1
                    continue
                if predicate_reasons(norm):  # defensive: must agree with prefilter
                    continue
                selected, selected_index = norm, offset + int(local)
                break
            if selected is not None:
                break
        offset += n
    reader.close()

    if selected is None:
        if row_index is not None:
            raise NonQualifyingRowError(row_index, [f"row_index out of range (file has {offset} data rows)"])
        raise NoQualifyingRowError(f"no row in {csv_path.name} satisfies {SELECTION_PREDICATE} and suitability")

    airlines_file = _default_lookup(csv_path, "airlines.csv", airlines_path)
    airports_file = _default_lookup(csv_path, "airports.csv", airports_path)
    lookup_files: dict[str, str] = {}
    airlines = airports = id_map = None
    if airlines_file is not None:
        airlines = {r["IATA_CODE"]: r for r in _read_lookup(airlines_file, AIRLINE_COLUMNS)}
        lookup_files[airlines_file.name] = file_sha256(airlines_file)
    if airports_file is not None:
        airports = {r["IATA_CODE"]: r for r in _read_lookup(airports_file, AIRPORT_COLUMNS)}
        lookup_files[airports_file.name] = file_sha256(airports_file)
    if airport_id_map_path is not None:
        id_map = {
            r["SOURCE_ID"]: r["IATA_CODE"]
            for r in _read_lookup(Path(airport_id_map_path), ("SOURCE_ID", "IATA_CODE"))
        }
        lookup_files[Path(airport_id_map_path).name] = file_sha256(Path(airport_id_map_path))

    airline_code = selected["AIRLINE"]
    if airlines is None:
        airline_join: dict[str, object] = {"source_id": airline_code, "status": "lookup_unavailable"}
    elif airline_code in airlines:
        airline_join = {"source_id": airline_code, "status": "resolved", "name": airlines[airline_code]["AIRLINE"]}
    else:
        airline_join = {"source_id": airline_code, "status": "unresolved", "reason": "code not in airlines.csv"}

    derived_times: dict[str, object] = {}
    for col in sorted(HHMM_COLUMNS):
        clock = parse_hhmm(selected[col])
        derived_times[col] = None if clock is None else {
            "local_clock": clock.display,
            "minutes_after_local_midnight": clock.minutes_after_midnight,
            "day_offset": clock.day_offset,
        }

    provenance: dict[str, object] = {
        "namespace": PROVENANCE_NAMESPACE,
        "mode": "historical",
        "source_file": csv_path.name,
        "source_sha256": file_sha256(csv_path),
        "row_index": selected_index,
        "row_index_basis": "zero-based data row index in the original file, header excluded",
        "selection_method": "explicit_row_index" if row_index is not None else "first_qualifying_in_file_order",
        "selection_predicate": SELECTION_PREDICATE,
        "suitability_rule": SUITABILITY_RULE,
        "predicate_values": {
            "WEATHER_DELAY": selected["WEATHER_DELAY"],
            "LATE_AIRCRAFT_DELAY": selected["LATE_AIRCRAFT_DELAY"],
        },
        "row_hash": canonical_hash(selected),
        "skipped_invalid_candidate_rows": skipped_invalid,
        "joins": {
            "airline": airline_join,
            "origin_airport": _join_airport(selected["ORIGIN_AIRPORT"], airports, id_map),
            "destination_airport": _join_airport(selected["DESTINATION_AIRPORT"], airports, id_map),
        },
        "derived_local_clock_times": derived_times,
        "lookup_files_sha256": lookup_files,
        "time_semantics": (
            "HHMM values are historical local clock text (2400 = midnight next day). They are "
            "contextual display only; SCHEDULED_ARRIVAL is gate-in, not a landing timestamp."
        ),
    }
    return {**selected, PROVENANCE_KEY: provenance}


def default_data_dir() -> Path:
    """Directory holding the (uncommitted) dataset CSVs: $AIRSPACE_DATA_DIR or repo root."""
    env = os.environ.get("AIRSPACE_DATA_DIR")
    return Path(env) if env else Path(__file__).resolve().parents[3]
