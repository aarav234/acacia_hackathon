"""Shared Contract v1 — canonical Pydantic models for the airspace package.

Owned by Member 1. Members 2 and 3 import from here; never redefine these
schemas. See docs/shared_contract.md for the field-by-field reference.

Design rules enforced here:
* every model forbids extra fields and is frozen;
* safety-critical numbers are strict (no "5" -> 5, no True -> 1, no NaN/inf);
* ID-keyed operational collections are stored as tuples sorted by ID so the
  canonical JSON (and therefore ``state_hash``) is independent of input order;
* ``Scenario.state_hash`` is recomputed on validation, so a tampered or stale
  scenario cannot be loaded; engine entry points call ``ensure_sealed`` again
  to detect in-place mutation of the (mutable) dict/list fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import (
    AllowInfNan,
    BaseModel,
    ConfigDict,
    Field,
    Strict,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

SCHEMA_VERSION = "1.0"

Role = Literal["atc", "pilot", "ground"]
ROLES: tuple[Role, ...] = ("atc", "pilot", "ground")
ProviderMode = Literal["live", "mock", "replay"]

# --------------------------------------------------------------------------
# Strict scalar types
# --------------------------------------------------------------------------
StrictInt = Annotated[int, Strict()]
Minute = Annotated[int, Strict()]  # integer minutes on the labelled simulation clock
NonNegInt = Annotated[int, Strict(), Field(ge=0)]
PosInt = Annotated[int, Strict(), Field(gt=0)]
FiniteFloat = Annotated[float, Strict(), AllowInfNan(False)]
NonNegFloat = Annotated[float, Strict(), AllowInfNan(False), Field(ge=0)]
PosFloat = Annotated[float, Strict(), AllowInfNan(False), Field(gt=0)]
StrictStr = Annotated[str, Strict()]
Identifier = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=64)
]
PlanId = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$", max_length=200)
]
EvidenceId = Annotated[
    str, Strict(), StringConstraints(pattern=r"^[a-z]+:[A-Za-z0-9_.\[\]-]+$", max_length=200)
]
Sha256Hex = Annotated[str, Strict(), StringConstraints(pattern=r"^[0-9a-f]{64}$")]
SchemaVersion = Literal["1.0"]
HistoricalValue = StrictStr | StrictInt | FiniteFloat | None

EPSILON_DEFAULT = 1e-9
WEIGHT_SUM_TOLERANCE = 1e-9
MAX_RATIONALE_WORDS = 120
MAX_RANKED_PLAN_IDS = 3
DEFAULT_CANDIDATE_CAP = 2000

# Canonical metric names per role (fixed order = summation order).
ROLE_METRICS: dict[Role, tuple[str, ...]] = {
    "atc": ("landing_delay_min", "spillover_proxy_min"),
    "pilot": ("incremental_fuel_kg", "connection_penalty_pax", "turbulence_exposure_index"),
    "ground": ("turnaround_delay_min", "crew_overtime_min", "gate_change_indicator"),
}
METRIC_UNITS: dict[str, str] = {
    "landing_delay_min": "min",
    "spillover_proxy_min": "min (proxy)",
    "incremental_fuel_kg": "kg",
    "connection_penalty_pax": "assumed passengers",
    "turbulence_exposure_index": "index (simulated proxy)",
    "turnaround_delay_min": "min",
    "crew_overtime_min": "min",
    "gate_change_indicator": "0/1",
}
# Transparent demonstration weights from Shared Contract v1 (not fitted evidence).
CONTRACT_DEFAULT_WEIGHTS: dict[Role, dict[str, float]] = {
    "atc": {"landing_delay_min": 0.6, "spillover_proxy_min": 0.4},
    "pilot": {
        "incremental_fuel_kg": 0.6,
        "connection_penalty_pax": 0.3,
        "turbulence_exposure_index": 0.1,
    },
    "ground": {"turnaround_delay_min": 0.5, "crew_overtime_min": 0.3, "gate_change_indicator": 0.2},
}

# Evidence IDs every Scenario must carry (one per simulated config section).
OPERATIONS_SECTIONS: tuple[str, ...] = (
    "subject",
    "separation",
    "runways",
    "runway_movements",
    "gates",
    "gate_occupancy",
    "taxi_routes",
    "taxi_blockages",
    "service",
    "fuel",
    "crew",
    "connections",
    "turbulence",
    "utility",
    "planning",
)
_SEAL_CONTEXT_KEY = "airspace.seal_state_hash"
SCENARIO_PLANNING_EVIDENCE_ID = "sim:scenario.planning"
SCENARIO_EPOCH_EVIDENCE_ID = "sim:scenario.epoch"


def section_evidence_id(section: str) -> str:
    return f"sim:operations.{section}"


REQUIRED_EVIDENCE_IDS: tuple[str, ...] = (
    SCENARIO_PLANNING_EVIDENCE_ID,
    SCENARIO_EPOCH_EVIDENCE_ID,
    *(section_evidence_id(s) for s in OPERATIONS_SECTIONS),
)

# Hard-constraint rule IDs emitted by airspace.engine.constraints (CC-4).
RULE_IDS: tuple[str, ...] = (
    "PLAN-STATE",
    "PLAN-SLOT",
    "PLAN-ROUTE",
    "PLAN-TIMING",
    "RWY-OPEN",
    "RWY-SEP-LEAD",
    "RWY-SEP-FOLLOW",
    "GATE-COMPAT",
    "GATE-OPEN",
    "GATE-OCCUPANCY",
    "TAXI-BLOCK",
    "CREW-COVERAGE",
    "FUEL-RESERVE",
)
# ValidationReport.metrics keys are prefixed by the role whose domain they describe (CC-2).
METRIC_ROLE_PREFIXES: dict[Role, str] = {"atc": "atc_", "pilot": "pilot_", "ground": "ground_"}

HISTORICAL_REF_REQUIRED_KEYS = (
    "source_file",
    "source_sha256",
    "row_index",
    "selection_predicate",
    "row_hash",
)


class ContractError(ValueError):
    """Base class for contract-level failures."""


class StaleStateError(ContractError):
    """A scenario/plan/message refers to a state hash that no longer matches."""


# --------------------------------------------------------------------------
# Canonical hashing
# --------------------------------------------------------------------------
def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, NaN/inf rejected."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_hash(obj: Any) -> str:
    return sha256_text(canonical_json(obj))


def _check_utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"not an ISO 8601 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"timestamp must be explicit UTC (Z or +00:00): {value!r}")
    return value


def _unique(items: tuple[Any, ...], attr: str, what: str) -> None:
    seen: set[str] = set()
    for item in items:
        key = getattr(item, attr)
        if key in seen:
            raise ValueError(f"duplicate {what} {attr}={key!r}")
        seen.add(key)


def _sorted_by(attr: str):
    def sorter(value: tuple[Any, ...]) -> tuple[Any, ...]:
        return tuple(sorted(value, key=lambda item: getattr(item, attr)))

    return sorter


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, validate_default=True)


# --------------------------------------------------------------------------
# Intervals (half-open [start_min, end_min))
# --------------------------------------------------------------------------
class TimeWindow(ContractModel):
    """Half-open interval [start_min, end_min) on the simulation clock."""

    start_min: Minute
    end_min: Minute

    @model_validator(mode="after")
    def _ordered(self) -> TimeWindow:
        if self.end_min <= self.start_min:
            raise ValueError(f"window end_min ({self.end_min}) must exceed start_min ({self.start_min})")
        return self

    def contains(self, start: int, end: int) -> bool:
        return self.start_min <= start and end <= self.end_min


def overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    """Half-open overlap: touching endpoints ([0,5) and [5,9)) do NOT overlap."""
    return a_start < b_end and b_start < a_end


def _sorted_disjoint_windows(value: tuple[TimeWindow, ...], what: str) -> tuple[TimeWindow, ...]:
    ordered = tuple(sorted(value, key=lambda w: (w.start_min, w.end_min)))
    for prev, cur in zip(ordered, ordered[1:]):
        if overlaps(prev.start_min, prev.end_min, cur.start_min, cur.end_min):
            raise ValueError(f"{what} open windows overlap: {prev} / {cur}")
    return ordered


# --------------------------------------------------------------------------
# OperationalConfig nested resource models (all values SIMULATED unless
# independently evidenced)
# --------------------------------------------------------------------------
class SubjectAircraft(ContractModel):
    """The single disrupted inbound flight being planned."""

    flight_label: StrictStr
    wake_category: Identifier
    runway_occupancy_min: PosInt
    assigned_gate_id: Identifier
    scheduled_gate_in_min: Minute  # simulated schedule on the relative clock


class SeparationRule(ContractModel):
    leader_category: Identifier
    follower_category: Identifier
    min_separation_min: NonNegInt


class SeparationConfig(ContractModel):
    """Complete leader x follower matrix of minimum runway time separation."""

    categories: tuple[Identifier, ...] = Field(min_length=1)
    rules: tuple[SeparationRule, ...]

    @field_validator("categories", mode="after")
    @classmethod
    def _cats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("duplicate wake categories")
        return tuple(sorted(value))

    @field_validator("rules", mode="after")
    @classmethod
    def _sort_rules(cls, value: tuple[SeparationRule, ...]) -> tuple[SeparationRule, ...]:
        return tuple(sorted(value, key=lambda r: (r.leader_category, r.follower_category)))

    @model_validator(mode="after")
    def _complete(self) -> SeparationConfig:
        pairs: dict[tuple[str, str], int] = {}
        for rule in self.rules:
            key = (rule.leader_category, rule.follower_category)
            if key in pairs:
                raise ValueError(f"duplicate separation rule for {key}")
            for cat in key:
                if cat not in self.categories:
                    raise ValueError(f"separation rule uses unknown category {cat!r}")
            pairs[key] = rule.min_separation_min
        missing = [
            (lead, follow)
            for lead in self.categories
            for follow in self.categories
            if (lead, follow) not in pairs
        ]
        if missing:
            raise ValueError(f"separation matrix incomplete; missing leader/follower pairs {missing}")
        return self

    def required_min(self, leader: str, follower: str) -> int:
        for rule in self.rules:
            if rule.leader_category == leader and rule.follower_category == follower:
                return rule.min_separation_min
        raise KeyError((leader, follower))


class Runway(ContractModel):
    runway_id: Identifier
    open_windows: tuple[TimeWindow, ...]

    @field_validator("open_windows", mode="after")
    @classmethod
    def _windows(cls, value: tuple[TimeWindow, ...]) -> tuple[TimeWindow, ...]:
        return _sorted_disjoint_windows(value, "runway")


class RunwayMovement(ContractModel):
    """A fixed background movement (not re-sequenced by the planner)."""

    movement_id: Identifier
    runway_id: Identifier
    time_min: Minute
    wake_category: Identifier
    kind: Literal["arrival", "departure"]


class Gate(ContractModel):
    gate_id: Identifier
    compatible_categories: tuple[Identifier, ...] = Field(min_length=1)
    open_windows: tuple[TimeWindow, ...]
    turnover_buffer_min: NonNegInt

    @field_validator("compatible_categories", mode="after")
    @classmethod
    def _cats(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @field_validator("open_windows", mode="after")
    @classmethod
    def _windows(cls, value: tuple[TimeWindow, ...]) -> tuple[TimeWindow, ...]:
        return _sorted_disjoint_windows(value, "gate")


class GateOccupancy(ContractModel):
    """Existing gate use [start_min, end_min) BEFORE the gate's turnover buffer."""

    occupancy_id: Identifier
    gate_id: Identifier
    start_min: Minute
    end_min: Minute

    @model_validator(mode="after")
    def _ordered(self) -> GateOccupancy:
        if self.end_min <= self.start_min:
            raise ValueError(f"occupancy {self.occupancy_id}: end_min must exceed start_min")
        return self


class TaxiRoute(ContractModel):
    """A valid runway->gate taxi route; only listed pairs are connected."""

    route_id: Identifier
    runway_id: Identifier
    gate_id: Identifier
    taxi_min: PosInt
    segment_ids: tuple[Identifier, ...] = Field(min_length=1)


class TaxiBlockage(ContractModel):
    blockage_id: Identifier
    segment_id: Identifier
    start_min: Minute
    end_min: Minute

    @model_validator(mode="after")
    def _ordered(self) -> TaxiBlockage:
        if self.end_min <= self.start_min:
            raise ValueError(f"blockage {self.blockage_id}: end_min must exceed start_min")
        return self


class ServiceConfig(ContractModel):
    service_duration_min: PosInt


class FuelConfig(ContractModel):
    """Toy fuel model. A SIMULATED rule, not an FAA/Airbus reserve standard.

    fuel_at_gate_in = snapshot - approach_burn
                      - airborne_burn_rate * (landing_min - earliest_landing_min)
                      - taxi_burn_rate * taxi_min
    """

    snapshot_fuel_kg: PosFloat
    approach_burn_kg: NonNegFloat  # snapshot (epoch) -> wheels-on at earliest_landing_min
    airborne_burn_kg_per_min: NonNegFloat  # incremental holding burn
    taxi_burn_kg_per_min: NonNegFloat
    min_reserve_kg: PosFloat  # required on board at gate-in


class CrewShift(ContractModel):
    crew_id: Identifier
    available: TimeWindow  # HARD availability (outside = unavailable)
    regular_end_min: Minute  # work after this, still inside `available`, is overtime (soft)
    busy: tuple[TimeWindow, ...] = ()

    @model_validator(mode="after")
    def _regular(self) -> CrewShift:
        if not (self.available.start_min <= self.regular_end_min <= self.available.end_min):
            raise ValueError(f"crew {self.crew_id}: regular_end_min must lie inside available window")
        return self

    _s_busy = field_validator("busy", mode="after")(
        lambda cls, v: tuple(sorted(v, key=lambda w: (w.start_min, w.end_min)))
    )


class ConnectionGroup(ContractModel):
    """ASSUMED onward-connection group. The dataset has no passenger-level data."""

    group_id: Identifier
    passenger_count: NonNegInt
    deadline_gate_in_min: Minute  # connection kept iff gate_in_min <= deadline


class RunwayExposure(ContractModel):
    runway_id: Identifier
    exposure_index: NonNegFloat


class TurbulenceConfig(ContractModel):
    """Configured turbulence-exposure PROXY; no turbulence data exists in the CSVs."""

    runway_exposure: tuple[RunwayExposure, ...]
    holding_exposure_per_min: NonNegFloat

    _sort = field_validator("runway_exposure", mode="after")(lambda cls, v: _sorted_by("runway_id")(v))


class MetricSpec(ContractModel):
    metric: StrictStr
    weight: NonNegFloat
    scale: PosFloat


class RoleUtilityConfig(ContractModel):
    metrics: tuple[MetricSpec, ...]


class UtilityConfig(ContractModel):
    """normalized = raw / scale; C_i = sum(w * normalized); U_i = 1 - C_i (no clipping)."""

    atc: RoleUtilityConfig
    pilot: RoleUtilityConfig
    ground: RoleUtilityConfig
    spillover_window_min: PosInt

    @model_validator(mode="after")
    def _check(self) -> UtilityConfig:
        for role in ROLES:
            specs = getattr(self, role).metrics
            names = tuple(s.metric for s in specs)
            if sorted(names) != sorted(ROLE_METRICS[role]) or len(set(names)) != len(names):
                raise ValueError(f"{role} metrics must be exactly {ROLE_METRICS[role]}, got {names}")
            total = math.fsum(s.weight for s in specs)
            if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
                raise ValueError(f"{role} weights must sum to 1, got {total!r}")
        return self

    def role(self, role: Role) -> RoleUtilityConfig:
        return getattr(self, role)

    def spec(self, role: Role, metric: str) -> MetricSpec:
        for s in self.role(role).metrics:
            if s.metric == metric:
                return s
        raise KeyError((role, metric))


class PlanningConfig(ContractModel):
    candidate_cap: PosInt = DEFAULT_CANDIDATE_CAP


class OperationalConfig(ContractModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    subject: SubjectAircraft
    separation: SeparationConfig
    runways: tuple[Runway, ...] = Field(min_length=1)
    runway_movements: tuple[RunwayMovement, ...]
    gates: tuple[Gate, ...] = Field(min_length=1)
    gate_occupancy: tuple[GateOccupancy, ...]
    taxi_routes: tuple[TaxiRoute, ...] = Field(min_length=1)
    taxi_blockages: tuple[TaxiBlockage, ...]
    service: ServiceConfig
    fuel: FuelConfig
    crew: tuple[CrewShift, ...]
    connections: tuple[ConnectionGroup, ...]
    turbulence: TurbulenceConfig
    utility: UtilityConfig
    planning: PlanningConfig

    _s_runways = field_validator("runways", mode="after")(lambda cls, v: _sorted_by("runway_id")(v))
    _s_moves = field_validator("runway_movements", mode="after")(
        lambda cls, v: tuple(sorted(v, key=lambda m: (m.time_min, m.movement_id)))
    )
    _s_gates = field_validator("gates", mode="after")(lambda cls, v: _sorted_by("gate_id")(v))
    _s_occ = field_validator("gate_occupancy", mode="after")(lambda cls, v: _sorted_by("occupancy_id")(v))
    _s_routes = field_validator("taxi_routes", mode="after")(lambda cls, v: _sorted_by("route_id")(v))
    _s_blocks = field_validator("taxi_blockages", mode="after")(lambda cls, v: _sorted_by("blockage_id")(v))
    _s_crew = field_validator("crew", mode="after")(lambda cls, v: _sorted_by("crew_id")(v))
    _s_conn = field_validator("connections", mode="after")(lambda cls, v: _sorted_by("group_id")(v))

    @model_validator(mode="after")
    def _cross(self) -> OperationalConfig:
        _unique(self.runways, "runway_id", "runway")
        _unique(self.runway_movements, "movement_id", "runway movement")
        _unique(self.gates, "gate_id", "gate")
        _unique(self.gate_occupancy, "occupancy_id", "gate occupancy")
        _unique(self.taxi_routes, "route_id", "taxi route")
        _unique(self.taxi_blockages, "blockage_id", "taxi blockage")
        _unique(self.crew, "crew_id", "crew")
        _unique(self.connections, "group_id", "connection group")
        cats = set(self.separation.categories)
        runway_ids = {r.runway_id for r in self.runways}
        gate_ids = {g.gate_id for g in self.gates}
        if self.subject.wake_category not in cats:
            raise ValueError(f"subject wake_category {self.subject.wake_category!r} not in separation categories")
        if self.subject.assigned_gate_id not in gate_ids:
            raise ValueError(f"subject assigned_gate_id {self.subject.assigned_gate_id!r} is not a configured gate")
        for m in self.runway_movements:
            if m.runway_id not in runway_ids:
                raise ValueError(f"movement {m.movement_id} references unknown runway {m.runway_id!r}")
            if m.wake_category not in cats:
                raise ValueError(f"movement {m.movement_id} has unknown wake category {m.wake_category!r}")
        for g in self.gates:
            unknown = set(g.compatible_categories) - cats
            if unknown:
                raise ValueError(f"gate {g.gate_id} lists unknown categories {sorted(unknown)}")
        for o in self.gate_occupancy:
            if o.gate_id not in gate_ids:
                raise ValueError(f"occupancy {o.occupancy_id} references unknown gate {o.gate_id!r}")
        segments: set[str] = set()
        for r in self.taxi_routes:
            if r.runway_id not in runway_ids:
                raise ValueError(f"route {r.route_id} references unknown runway {r.runway_id!r}")
            if r.gate_id not in gate_ids:
                raise ValueError(f"route {r.route_id} references unknown gate {r.gate_id!r}")
            segments.update(r.segment_ids)
        for b in self.taxi_blockages:
            if b.segment_id not in segments:
                raise ValueError(f"blockage {b.blockage_id} references unknown taxi segment {b.segment_id!r}")
        exposure_ids = [e.runway_id for e in self.turbulence.runway_exposure]
        if sorted(exposure_ids) != sorted(runway_ids) or len(set(exposure_ids)) != len(exposure_ids):
            raise ValueError("turbulence.runway_exposure must list every runway exactly once")
        return self

    # Convenience lookups (read-only)
    def runway(self, runway_id: str) -> Runway:
        return next(r for r in self.runways if r.runway_id == runway_id)

    def gate(self, gate_id: str) -> Gate:
        return next(g for g in self.gates if g.gate_id == gate_id)

    def route(self, route_id: str) -> TaxiRoute | None:
        return next((r for r in self.taxi_routes if r.route_id == route_id), None)

    def runway_exposure(self, runway_id: str) -> float:
        return next(e.exposure_index for e in self.turbulence.runway_exposure if e.runway_id == runway_id)


# --------------------------------------------------------------------------
# Provenance and the Scenario root
# --------------------------------------------------------------------------
class Evidence(ContractModel):
    evidence_id: EvidenceId
    kind: Literal["historical", "derived", "simulated"]
    field_path: StrictStr
    source_ref: StrictStr
    note: StrictStr


class Assumption(ContractModel):
    field_path: StrictStr
    note: StrictStr


class SimulationConfig(ContractModel):
    """Versioned simulation config file (configs/*.json) consumed by build_scenario."""

    schema_version: SchemaVersion
    config_version: Identifier
    scenario_id: Identifier
    simulation_epoch_utc: StrictStr
    earliest_landing_min: NonNegInt
    planning_horizon_min: PosInt
    slot_step_min: PosInt
    operations: OperationalConfig
    assumptions: tuple[Assumption, ...] = ()

    _utc = field_validator("simulation_epoch_utc")(lambda cls, v: _check_utc(v))


class Scenario(ContractModel):
    schema_version: SchemaVersion
    scenario_id: Identifier
    state_hash: Sha256Hex
    source_mode: Literal["historical_anchor", "synthetic"]
    historical_row: dict[str, HistoricalValue]
    historical_ref: dict[str, StrictStr | StrictInt]
    simulation_epoch_utc: StrictStr
    earliest_landing_min: NonNegInt
    planning_horizon_min: PosInt
    slot_step_min: PosInt
    operations: OperationalConfig
    provenance: list[Evidence]

    _utc = field_validator("simulation_epoch_utc")(lambda cls, v: _check_utc(v))

    @model_validator(mode="after")
    def _check(self, info: ValidationInfo) -> Scenario:
        ids = [e.evidence_id for e in self.provenance]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate evidence_id in provenance")
        missing = [e for e in REQUIRED_EVIDENCE_IDS if e not in ids]
        if missing:
            raise ValueError(f"provenance missing required evidence ids {missing}")
        if self.source_mode == "historical_anchor":
            absent = [k for k in HISTORICAL_REF_REQUIRED_KEYS if k not in self.historical_ref]
            if absent:
                raise ValueError(f"historical_anchor scenario missing historical_ref keys {absent}")
            if not self.historical_row:
                raise ValueError("historical_anchor scenario requires a historical_row")
            if self.historical_ref["row_hash"] != canonical_hash(self.historical_row):
                raise ValueError("historical_ref.row_hash does not match historical_row")
        else:
            if self.historical_ref.get("mode") != "synthetic":
                raise ValueError("synthetic scenario must set historical_ref.mode='synthetic'")
            fake = [k for k in ("source_sha256", "row_index", "row_hash") if k in self.historical_ref]
            if fake or self.historical_row:
                raise ValueError("synthetic scenario must not carry a historical row or reference")
        expected = compute_state_hash(self)
        if info.context and info.context.get(_SEAL_CONTEXT_KEY) is True:
            object.__setattr__(self, "state_hash", expected)
        elif self.state_hash != expected:
            raise ValueError(
                f"state_hash mismatch (stale or tampered scenario): got {self.state_hash}, expected {expected}"
            )
        return self

    @classmethod
    def seal(cls, **fields: Any) -> Scenario:
        """Build a Scenario computing state_hash from the other fields."""
        fields = dict(fields)
        fields["state_hash"] = "0" * 64  # placeholder, replaced by the validated hash
        sealed = cls.model_validate(fields, context={_SEAL_CONTEXT_KEY: True})
        # Round-trip through strict JSON validation so the result is a normal,
        # independently hash-checked Scenario.
        return cls.model_validate_json(sealed.model_dump_json())

    def evidence_ids(self) -> set[str]:
        return {e.evidence_id for e in self.provenance}

    @property
    def slots(self) -> range:
        return range(
            self.earliest_landing_min,
            self.earliest_landing_min + self.planning_horizon_min,
            self.slot_step_min,
        )


def _dump_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, (list, tuple)):
        return [_dump_json(v) for v in value]
    if isinstance(value, dict):
        return {k: _dump_json(v) for k, v in value.items()}
    return value


def compute_state_hash(scenario: Scenario | dict[str, Any]) -> str:
    """SHA256 over every Scenario field except state_hash (no run timestamps exist in it).

    Accepts a Scenario or its ``model_dump()`` / ``model_dump(mode="json")`` dict;
    all three give the same hash.
    """
    if isinstance(scenario, dict):
        missing = [n for n in Scenario.model_fields if n != "state_hash" and n not in scenario]
        if missing:
            raise ContractError(f"scenario dict missing fields {missing}")
        get = scenario.__getitem__
    else:
        get = lambda name: getattr(scenario, name)  # noqa: E731
    payload = {name: _dump_json(get(name)) for name in Scenario.model_fields if name != "state_hash"}
    return canonical_hash(payload)


def ensure_sealed(scenario: Scenario) -> None:
    """Raise StaleStateError if the scenario's mutable fields were altered after sealing."""
    if compute_state_hash(scenario) != scenario.state_hash:
        raise StaleStateError(f"scenario {scenario.scenario_id} was modified after sealing")


# --------------------------------------------------------------------------
# Plans, validation and assessment
# --------------------------------------------------------------------------
class Plan(ContractModel):
    plan_id: PlanId
    scenario_id: Identifier
    state_hash: Sha256Hex
    landing_min: NonNegInt  # wheels-on
    runway_id: Identifier
    gate_id: Identifier
    taxi_route_id: Identifier
    taxi_min: PosInt
    gate_in_min: Minute  # = landing_min + taxi_min (no implicit gate holding)
    service_end_min: Minute  # = gate_in_min + service duration

    @model_validator(mode="after")
    def _timing(self) -> Plan:
        if self.gate_in_min != self.landing_min + self.taxi_min:
            raise ValueError("gate_in_min must equal landing_min + taxi_min")
        if self.service_end_min <= self.gate_in_min:
            raise ValueError("service_end_min must exceed gate_in_min")
        return self


def make_plan_id(landing_min: int, runway_id: str, gate_id: str, route_id: str) -> str:
    """Stable, human-readable plan id; identical decision inputs give identical ids."""
    return f"P{landing_min:04d}_{runway_id}_{gate_id}_{route_id}"


class Violation(ContractModel):
    rule_id: Identifier
    reason: StrictStr
    observed: StrictStr
    required: StrictStr
    units: StrictStr
    evidence_ids: list[EvidenceId]


class ValidationReport(ContractModel):
    plan_id: PlanId
    feasible: Annotated[bool, Strict()]
    violations: list[Violation]
    metrics: dict[str, FiniteFloat]

    @model_validator(mode="after")
    def _consistent(self) -> ValidationReport:
        if self.feasible != (len(self.violations) == 0):
            raise ValueError("feasible must be True exactly when there are no violations")
        return self


class Assessment(ContractModel):
    plan: Plan
    validation: ValidationReport
    utilities: dict[Role, FiniteFloat]
    components: dict[Role, dict[str, FiniteFloat]]

    @model_validator(mode="after")
    def _check(self) -> Assessment:
        if set(self.utilities) != set(ROLES) or set(self.components) != set(ROLES):
            raise ValueError(f"utilities and components must have exactly the keys {ROLES}")
        if self.validation.plan_id != self.plan.plan_id:
            raise ValueError("validation.plan_id does not match plan.plan_id")
        if not self.validation.feasible:
            raise ValueError("assessments exist only for feasible plans")
        for role in ROLES:
            cost = self.components[role].get("cost")
            if cost is None or abs((1.0 - cost) - self.utilities[role]) > 1e-12:
                raise ValueError(f"{role}: utility must equal 1 - components['cost']")
        return self


# --------------------------------------------------------------------------
# Agent messaging, logging and run results (implemented by Members 2/3)
# --------------------------------------------------------------------------
RoundNo = Annotated[int, Strict(), Field(ge=1, le=2)]
LogRoundNo = Annotated[int, Strict(), Field(ge=0, le=2)]


class Objection(ContractModel):
    code: Literal["hard_constraint", "utility_below_fallback", "missing_evidence", "preference", "protocol_error"]
    plan_id: PlanId | None
    rule_id: Identifier | None
    evidence_ids: list[EvidenceId]
    reason: StrictStr


class AgentMessage(ContractModel):
    scenario_id: Identifier
    state_hash: Sha256Hex
    role: Role
    round: RoundNo
    action: Literal["propose", "accept", "reject"]
    plan_id: PlanId | None
    ranked_plan_ids: list[PlanId] = Field(max_length=MAX_RANKED_PLAN_IDS)
    objections: list[Objection]
    rationale: StrictStr
    evidence_ids: list[EvidenceId]

    @model_validator(mode="after")
    def _protocol(self) -> AgentMessage:
        if self.round == 1 and self.action not in ("propose", "reject"):
            raise ValueError("round 1 allows only propose/reject")
        if self.round == 2 and self.action not in ("accept", "reject"):
            raise ValueError("round 2 allows only accept/reject of the exact compromise")
        if self.action in ("propose", "accept") and self.plan_id is None:
            raise ValueError(f"action {self.action!r} requires plan_id")
        if len(set(self.ranked_plan_ids)) != len(self.ranked_plan_ids):
            raise ValueError("ranked_plan_ids must be unique")
        if len(self.rationale.split()) > MAX_RATIONALE_WORDS:
            raise ValueError(f"rationale exceeds {MAX_RATIONALE_WORDS} words")
        return self

    def check_known(self, scenario_id: str, state_hash: str, known_plan_ids: set[str]) -> None:
        """Raise if stale or referencing unknown/infeasible plan ids."""
        if scenario_id != self.scenario_id or state_hash != self.state_hash:
            raise StaleStateError("agent message refers to a different scenario/state hash")
        refs = set(self.ranked_plan_ids) | ({self.plan_id} if self.plan_id else set())
        refs |= {o.plan_id for o in self.objections if o.plan_id}
        unknown = refs - known_plan_ids
        if unknown:
            raise ContractError(f"unknown plan ids {sorted(unknown)}")


class LogEvent(ContractModel):
    sequence: NonNegInt
    timestamp_utc: StrictStr
    scenario_id: Identifier
    state_hash: Sha256Hex
    round: LogRoundNo
    actor: StrictStr
    event_type: StrictStr
    payload: dict[str, Any]

    _utc = field_validator("timestamp_utc")(lambda cls, v: _check_utc(v))


class RunResult(ContractModel):
    schema_version: SchemaVersion = SCHEMA_VERSION
    scenario_id: Identifier
    state_hash: Sha256Hex
    status: Literal["consensus", "fallback", "human_review"]
    reason_code: StrictStr
    selected_plan_id: PlanId | None
    fallback_plan_id: PlanId | None
    rounds_completed: Annotated[int, Strict(), Field(ge=0, le=2)]
    agent_message_count: Annotated[int, Strict(), Field(ge=0, le=6)]
    provider_mode: ProviderMode
    events: list[LogEvent]
    elapsed_ms: NonNegFloat
    human_approval_required: Literal[True]

    @model_validator(mode="after")
    def _status(self) -> RunResult:
        if self.status == "consensus":
            if self.selected_plan_id is None:
                raise ValueError("consensus requires selected_plan_id")
            if self.rounds_completed != 2 or self.agent_message_count != 6:
                raise ValueError("consensus requires 2 completed rounds and 6 agent responses")
        elif self.status == "fallback":
            if self.fallback_plan_id is None or self.selected_plan_id != self.fallback_plan_id:
                raise ValueError("fallback status must select exactly the fallback plan")
        elif self.selected_plan_id is not None:
            raise ValueError("human_review carries no actionable selected plan")
        seqs = [e.sequence for e in self.events]
        if seqs != sorted(set(seqs)):
            raise ValueError("event sequence numbers must be strictly increasing")
        for e in self.events:
            if e.scenario_id != self.scenario_id or e.state_hash != self.state_hash:
                raise ValueError("event scenario/state hash differs from run result")
        return self


PUBLIC_MODELS: dict[str, type[BaseModel]] = {
    "Scenario": Scenario,
    "SimulationConfig": SimulationConfig,
    "OperationalConfig": OperationalConfig,
    "Plan": Plan,
    "Evidence": Evidence,
    "Violation": Violation,
    "ValidationReport": ValidationReport,
    "Assessment": Assessment,
    "Objection": Objection,
    "AgentMessage": AgentMessage,
    "LogEvent": LogEvent,
    "RunResult": RunResult,
}


def export_json_schemas(out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, model in PUBLIC_MODELS.items():
        path = out_dir / f"{name}.schema.json"
        path.write_text(json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n")
        written.append(path)
    return written


if __name__ == "__main__":  # python -m airspace.contracts [out_dir]
    import sys

    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixtures/schemas")
    for p in export_json_schemas(target):
        print(p)
