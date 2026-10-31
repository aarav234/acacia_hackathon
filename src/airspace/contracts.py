"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE.

Contract-conformant stand-in for ``airspace.contracts`` (Shared Contract v1,
section 3) so Member 2's agents/negotiation slice can be built and tested
before Member 1's bootstrap PR lands. Public model names and field meanings
follow the contract verbatim. ``OperationalConfig`` and its nested resource
models are a *toy* placeholder: Member 1 publishes the real nested fields.

Delete this file (and the stub commit) when rebasing onto the real bootstrap.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

SCHEMA_VERSION = "1.0"

Role = Literal["atc", "pilot", "ground"]
ProviderMode = Literal["live", "mock", "replay"]
ROLES: tuple[Role, ...] = ("atc", "pilot", "ground")


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


# --------------------------------------------------------------------------
# Toy nested operational config (placeholder for Member 1's real models)
# --------------------------------------------------------------------------
class Window(_Strict):
    start_min: int
    end_min: int  # half-open [start, end)


class Movement(_Strict):
    runway_id: str
    time_min: int
    category: str


class Runway(_Strict):
    runway_id: str
    open_windows: list[Window]
    turbulence_exposure: float  # simulated proxy per minute airborne


class Gate(_Strict):
    gate_id: str
    compatible_categories: list[str]
    occupied: list[Window]
    buffer_min: int


class TaxiRoute(_Strict):
    taxi_route_id: str
    runway_id: str
    gate_id: str
    taxi_min: int
    blocked: list[Window]


class Fuel(_Strict):
    snapshot_kg: float
    approach_burn_kg: float
    airborne_burn_kg_per_min: float
    taxi_burn_kg_per_min: float
    min_reserve_kg: float


class Crew(_Strict):
    available: Window
    overtime_after_min: int


class Connection(_Strict):
    deadline_min: int
    passengers: int


class RoleWeights(_Strict):
    weights: dict[str, float]
    scales: dict[str, float]


class OperationalConfig(_Strict):
    aircraft_category: str
    separation_min: dict[str, int]  # "LEAD>FOLLOW" -> minutes
    runways: list[Runway]
    existing_movements: list[Movement]
    gates: list[Gate]
    planned_gate_id: str
    scheduled_gate_in_min: int
    service_duration_min: int
    taxi_routes: list[TaxiRoute]
    fuel: Fuel
    crew: Crew
    connections: list[Connection]
    utility: dict[str, RoleWeights]
    catalogue_cap: int = 2000


# --------------------------------------------------------------------------
# Canonical contract models
# --------------------------------------------------------------------------
class Evidence(_Strict):
    evidence_id: str
    kind: Literal["historical", "derived", "simulated"]
    field_path: str
    source_ref: str
    note: str


class Scenario(_Strict):
    scenario_id: str
    schema_version: str = SCHEMA_VERSION
    state_hash: str
    source_mode: Literal["historical_anchor", "synthetic"]
    historical_row: dict[str, str | int | float | None]
    historical_ref: dict[str, str | int]
    simulation_epoch_utc: str
    earliest_landing_min: int
    planning_horizon_min: int
    slot_step_min: int
    operations: OperationalConfig
    provenance: list[Evidence]


class Plan(_Strict):
    plan_id: str
    scenario_id: str
    state_hash: str
    landing_min: int
    runway_id: str
    gate_id: str
    taxi_route_id: str
    taxi_min: int
    gate_in_min: int
    service_end_min: int


class Violation(_Strict):
    rule_id: str
    reason: str
    observed: str
    required: str
    units: str
    evidence_ids: list[str]


class ValidationReport(_Strict):
    plan_id: str
    feasible: bool
    violations: list[Violation]
    metrics: dict[str, float]


class Assessment(_Strict):
    plan: Plan
    validation: ValidationReport
    utilities: dict[Role, float]
    components: dict[Role, dict[str, float]]

    @model_validator(mode="after")
    def _check(self) -> "Assessment":
        if set(self.utilities) != set(ROLES) or set(self.components) != set(ROLES):
            raise ValueError("utilities/components must have exactly atc, pilot, ground")
        for v in self.utilities.values():
            if not math.isfinite(v):
                raise ValueError("non-finite utility")
        return self


class Objection(_Strict):
    code: Literal["hard_constraint", "utility_below_fallback", "missing_evidence",
                  "preference", "protocol_error"]
    plan_id: str | None
    rule_id: str | None
    evidence_ids: list[str]
    reason: str


class AgentMessage(_Strict):
    scenario_id: str
    state_hash: str
    role: Role
    round: Literal[1, 2]
    action: Literal["propose", "accept", "reject"]
    plan_id: str | None
    ranked_plan_ids: list[str] = Field(max_length=3)
    objections: list[Objection]
    rationale: str
    evidence_ids: list[str]

    @field_validator("rationale")
    @classmethod
    def _words(cls, v: str) -> str:
        if len(v.split()) > 120:
            raise ValueError("rationale exceeds 120 words")
        return v


class LogEvent(_Strict):
    sequence: int
    timestamp_utc: str
    scenario_id: str
    state_hash: str
    round: Literal[0, 1, 2]
    actor: str
    event_type: str
    payload: dict


class RunResult(_Strict):
    schema_version: str = SCHEMA_VERSION
    scenario_id: str
    state_hash: str
    status: Literal["consensus", "fallback", "human_review"]
    reason_code: str
    selected_plan_id: str | None
    fallback_plan_id: str | None
    rounds_completed: int
    agent_message_count: int
    provider_mode: ProviderMode
    events: list[LogEvent]
    elapsed_ms: float
    human_approval_required: bool = True

    @field_validator("human_approval_required")
    @classmethod
    def _always_true(cls, v: bool) -> bool:
        if v is not True:
            raise ValueError("human_approval_required is always true")
        return v


def compute_state_hash(scenario_without_hash: dict) -> str:
    """Stub hash over frozen decision inputs (excludes state_hash itself)."""
    payload = {k: v for k, v in scenario_without_hash.items() if k != "state_hash"}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()
