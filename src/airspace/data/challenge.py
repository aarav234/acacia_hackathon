"""Loader for the challenge workbook ``challenge2_operational_constraints.xlsx``.

What the workbook is (audited, see docs/reports/02_dataset_assessment.md):
* ``airport_capacity`` - SYNTHETIC per-airport planning limits. Every limit is a
  function of ``size_class`` only; ``delayed_departures_in_flights_csv`` equals the
  per-ORIGIN count of flights.csv rows satisfying the selection predicate
  (WEATHER_DELAY > 15 OR LATE_AIRCRAFT_DELAY > 20), i.e. it is DERIVED from the
  same 2015 data and is not independent evidence.
* ``wake_separation`` - "Simplified from FAA JO 7110.65": distance (nm) for
  arrivals and time (s) for same-runway departures; ``Any other pair`` is a
  wildcard row, not a missing value.
* ``fuel_and_crew`` - challenge rules stated as text ("30 minutes of holding fuel").
* ``rules_of_play`` - protocol text for the teams.

Nothing here is a measurement, an observed decision or a preference label.
Values are challenge-supplied planning assumptions and are labelled as such.

The xlsx is read with openpyxl (via pandas) only when the workbook itself is
loaded; tests and CI use the small committed extract
``fixtures/challenge2_extract.json`` produced by :func:`write_extract`.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass
from pathlib import Path

WORKBOOK_NAME = "challenge2_operational_constraints.xlsx"
EXTRACT_NAME = "challenge2_extract.json"
EXTRACT_SCHEMA = "airspace.challenge2-extract/v1"
SHEETS = ("airport_capacity", "wake_separation", "fuel_and_crew", "rules_of_play")
CAPACITY_COLUMNS = (
    "IATA_CODE", "AIRPORT", "CITY", "STATE", "delayed_departures_in_flights_csv", "size_class",
    "active_runways", "gates", "max_runway_ops_per_hour", "min_turnaround_min", "spare_gates_at_any_time",
)
WAKE_COLUMNS = ("leader", "follower", "min_separation_nm", "min_time_s (same runway departures)")
WAKE_WILDCARD = "Any other pair"
FAA_CATEGORIES = ("super", "heavy", "large", "small")
_WAKE_LABELS = {"Super (A380)": "super", "Heavy": "heavy", "Large": "large", "Small": "small"}

# Exact text forms the parser accepts; anything else fails closed so a changed
# workbook cannot be silently misread.
_FUEL_CREW_PATTERNS: dict[str, tuple[str, str]] = {
    "Final fuel reserve": (r"^(\d+) minutes of holding fuel$", "final_reserve_holding_min"),
    "Contingency fuel": (r"^(\d+)% of trip fuel$", "contingency_pct_of_trip_fuel"),
    "Holding fuel burn (narrowbody)": (r"^([\d,]+) kg per hour$", "holding_burn_narrowbody_kg_per_h"),
    "Holding fuel burn (widebody)": (r"^([\d,]+) kg per hour$", "holding_burn_widebody_kg_per_h"),
    "Assumed spare fuel on arrival": (r"^(\d+) minutes of holding \(unless the team states otherwise\)$", "spare_fuel_on_arrival_holding_min"),
    "Flight crew max duty": (r"^(\d+) hours from report time$", "flight_crew_max_duty_h"),
    "Ground crew shift": (r"^(\d+) hours; overtime costs ([\d.]+)x after that$", "ground_crew_shift_h"),
    "Gate change notice": (r"^At least (\d+) minutes before arrival$", "gate_change_notice_min"),
}


class ChallengeDataError(ValueError):
    pass


@dataclass(frozen=True)
class AirportProfile:
    iata_code: str
    airport: str
    size_class: str
    active_runways: int
    gates: int
    max_runway_ops_per_hour: int
    min_turnaround_min: int
    spare_gates_at_any_time: int
    predicate_rows_as_origin: int  # = workbook 'delayed_departures_in_flights_csv' (misnomer)
    source_row: int  # 1-based spreadsheet row


@dataclass(frozen=True)
class WakeRule:
    leader: str  # FAA category or "*" (wildcard)
    follower: str
    min_separation_nm: float
    min_departure_time_s: int
    source_row: int


@dataclass(frozen=True)
class FuelCrewRules:
    final_reserve_holding_min: int
    contingency_pct_of_trip_fuel: int
    holding_burn_narrowbody_kg_per_h: int
    holding_burn_widebody_kg_per_h: int
    spare_fuel_on_arrival_holding_min: int
    flight_crew_max_duty_h: int
    ground_crew_shift_h: int
    ground_overtime_multiplier: float
    gate_change_notice_min: int


@dataclass(frozen=True)
class ChallengeConstraints:
    source_file: str
    source_sha256: str
    airports: dict[str, AirportProfile]
    wake_rules: tuple[WakeRule, ...]
    fuel_crew: FuelCrewRules
    rules_of_play: dict[str, str]
    sheet_notes: dict[str, str]

    def airport(self, iata_code: str) -> AirportProfile:
        if iata_code not in self.airports:
            raise ChallengeDataError(f"airport {iata_code!r} not in workbook airport_capacity (numeric ids are never mapped)")
        return self.airports[iata_code]

    def wake(self, leader: str, follower: str) -> WakeRule:
        """Explicit pair if listed, else the 'Any other pair' wildcard."""
        for rule in self.wake_rules:
            if (rule.leader, rule.follower) == (leader, follower):
                return rule
        return next(r for r in self.wake_rules if r.leader == "*")

    def provenance(self) -> dict[str, str]:
        return {"source_file": self.source_file, "source_sha256": self.source_sha256, "schema": EXTRACT_SCHEMA}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or value is None or (isinstance(value, float) and (math.isnan(value) or value != int(value))):
        raise ChallengeDataError(f"{what}: expected integer, got {value!r}")
    return int(value)


def _parse_capacity(df, note: str) -> dict[str, AirportProfile]:
    if tuple(df.columns) != CAPACITY_COLUMNS:
        raise ChallengeDataError(f"airport_capacity columns changed: {tuple(df.columns)}")
    if df["IATA_CODE"].duplicated().any():
        raise ChallengeDataError("duplicate IATA_CODE in airport_capacity")
    out: dict[str, AirportProfile] = {}
    for i, rec in enumerate(df.to_dict("records")):
        row = i + 4  # title row, blank row, header row
        code = rec["IATA_CODE"]
        if not isinstance(code, str) or not re.fullmatch(r"[A-Z0-9]{3}", code):
            raise ChallengeDataError(f"row {row}: invalid IATA_CODE {code!r}")
        prof = AirportProfile(
            iata_code=code,
            airport=str(rec["AIRPORT"]),
            size_class=str(rec["size_class"]),
            active_runways=_int(rec["active_runways"], f"row {row} active_runways"),
            gates=_int(rec["gates"], f"row {row} gates"),
            max_runway_ops_per_hour=_int(rec["max_runway_ops_per_hour"], f"row {row} max_runway_ops_per_hour"),
            min_turnaround_min=_int(rec["min_turnaround_min"], f"row {row} min_turnaround_min"),
            spare_gates_at_any_time=_int(rec["spare_gates_at_any_time"], f"row {row} spare_gates"),
            predicate_rows_as_origin=_int(rec["delayed_departures_in_flights_csv"], f"row {row} delayed_departures"),
            source_row=row,
        )
        if min(prof.active_runways, prof.gates, prof.max_runway_ops_per_hour, prof.min_turnaround_min) <= 0:
            raise ChallengeDataError(f"row {row}: non-positive capacity limit")
        if prof.spare_gates_at_any_time > prof.gates:
            raise ChallengeDataError(f"row {row}: spare gates exceed gates")
        out[code] = prof
    return out


def _parse_wake(df) -> tuple[WakeRule, ...]:
    if tuple(df.columns) != WAKE_COLUMNS:
        raise ChallengeDataError(f"wake_separation columns changed: {tuple(df.columns)}")
    rules = []
    for i, rec in enumerate(df.to_dict("records")):
        row = i + 4
        leader = rec["leader"]
        follower = rec["follower"]
        if leader == WAKE_WILDCARD:
            if not (follower is None or (isinstance(follower, float) and math.isnan(follower))):
                raise ChallengeDataError(f"row {row}: wildcard row must have an empty follower")
            lead, follow = "*", "*"
        else:
            if leader not in _WAKE_LABELS or follower not in _WAKE_LABELS:
                raise ChallengeDataError(f"row {row}: unknown wake category {leader!r}/{follower!r}")
            lead, follow = _WAKE_LABELS[leader], _WAKE_LABELS[follower]
        nm = rec["min_separation_nm"]
        if not isinstance(nm, (int, float)) or isinstance(nm, bool) or nm <= 0:
            raise ChallengeDataError(f"row {row}: invalid min_separation_nm {nm!r}")
        rules.append(WakeRule(lead, follow, float(nm), _int(rec[WAKE_COLUMNS[3]], f"row {row} min_time_s"), row))
    if sum(r.leader == "*" for r in rules) != 1:
        raise ChallengeDataError("wake_separation must contain exactly one 'Any other pair' row")
    pairs = [(r.leader, r.follower) for r in rules]
    if len(set(pairs)) != len(pairs):
        raise ChallengeDataError("duplicate wake pair")
    return tuple(rules)


def _parse_fuel_crew(df) -> FuelCrewRules:
    if tuple(df.columns) != ("rule", "value", "applies_to"):
        raise ChallengeDataError(f"fuel_and_crew columns changed: {tuple(df.columns)}")
    seen = dict(zip(df["rule"], df["value"]))
    if set(seen) != set(_FUEL_CREW_PATTERNS):
        raise ChallengeDataError(f"fuel_and_crew rules changed: {sorted(set(seen) ^ set(_FUEL_CREW_PATTERNS))}")
    values: dict[str, float] = {}
    for rule, (pattern, field) in _FUEL_CREW_PATTERNS.items():
        m = re.fullmatch(pattern, str(seen[rule]).strip())
        if not m:
            raise ChallengeDataError(f"fuel_and_crew {rule!r}: unrecognised value {seen[rule]!r}")
        values[field] = int(m.group(1).replace(",", ""))
        if rule == "Ground crew shift":
            values["ground_overtime_multiplier"] = float(m.group(2))
    return FuelCrewRules(**values)  # type: ignore[arg-type]


def load_workbook(path: Path) -> ChallengeConstraints:
    """Read and strictly validate the original workbook (never modified)."""
    import pandas as pd  # openpyxl engine is needed only here

    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"challenge workbook not found: {path}")
    xl = pd.ExcelFile(path, engine="openpyxl")
    if tuple(xl.sheet_names) != SHEETS:
        raise ChallengeDataError(f"unexpected sheets {xl.sheet_names}")
    notes = {}
    for sheet in ("airport_capacity", "wake_separation"):
        notes[sheet] = str(pd.read_excel(xl, sheet, header=None, nrows=1).iloc[0, 0])
    capacity = pd.read_excel(xl, "airport_capacity", header=2, dtype={"IATA_CODE": str})
    wake = pd.read_excel(xl, "wake_separation", header=2)
    fuel = pd.read_excel(xl, "fuel_and_crew", header=0)
    rules = pd.read_excel(xl, "rules_of_play", header=0)
    xl.close()
    if tuple(rules.columns) != ("topic", "detail"):
        raise ChallengeDataError("rules_of_play columns changed")
    return ChallengeConstraints(
        source_file=path.name,
        source_sha256=_sha256(path),
        airports=_parse_capacity(capacity, notes["airport_capacity"]),
        wake_rules=_parse_wake(wake),
        fuel_crew=_parse_fuel_crew(fuel),
        rules_of_play=dict(zip(rules["topic"], rules["detail"])),
        sheet_notes=notes,
    )


def to_extract(c: ChallengeConstraints, airports: list[str]) -> dict:
    """Small JSON extract (selected airports + all rule sheets) for tests/CI."""
    return {
        "schema": EXTRACT_SCHEMA,
        "source_file": c.source_file,
        "source_sha256": c.source_sha256,
        "note": "Derived extract of the challenge workbook (values unchanged). Airport rows are a subset.",
        "airports": {code: asdict(c.airport(code)) for code in sorted(airports)},
        "wake_rules": [asdict(r) for r in c.wake_rules],
        "fuel_crew": asdict(c.fuel_crew),
        "rules_of_play": c.rules_of_play,
        "sheet_notes": c.sheet_notes,
    }


def from_extract(data: dict) -> ChallengeConstraints:
    if data.get("schema") != EXTRACT_SCHEMA:
        raise ChallengeDataError(f"unsupported extract schema {data.get('schema')!r}")
    return ChallengeConstraints(
        source_file=data["source_file"],
        source_sha256=data["source_sha256"],
        airports={k: AirportProfile(**v) for k, v in data["airports"].items()},
        wake_rules=tuple(WakeRule(**r) for r in data["wake_rules"]),
        fuel_crew=FuelCrewRules(**data["fuel_crew"]),
        rules_of_play=dict(data["rules_of_play"]),
        sheet_notes=dict(data["sheet_notes"]),
    )


def load_extract(path: Path) -> ChallengeConstraints:
    return from_extract(json.loads(Path(path).read_text(encoding="utf-8")))


def write_extract(workbook: Path, out: Path, airports: list[str]) -> Path:
    out = Path(out)
    out.write_text(json.dumps(to_extract(load_workbook(workbook), airports), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out
