"""BOOTSTRAP STUB — OWNED BY MEMBER 1. DO NOT MERGE.

normalized = raw/scale; C_i = sum(w*normalized); U_i = 1 - C_i (no clipping).
"""
from __future__ import annotations

from airspace.contracts import ROLES, Assessment, Plan, Scenario
from airspace.engine.constraints import validate_plan

COMPONENT_METRIC = {
    "atc": {"landing_delay": "atc_landing_delay_min", "spillover": "atc_spillover_proxy"},
    "pilot": {"incremental_fuel": "pilot_extra_fuel_kg", "connection": "pilot_connection_pax_missed",
              "turbulence": "pilot_turbulence_proxy"},
    "ground": {"turnaround_delay": "ground_turnaround_delay_min",
               "crew_overtime": "ground_crew_overtime_min", "gate_change": "ground_gate_change"},
}


def assess_candidates(scenario: Scenario, plans: list[Plan]) -> list[Assessment]:
    out = []
    for p in plans:
        rep = validate_plan(scenario, p)
        if not rep.feasible:
            continue
        utilities, components = {}, {}
        for role in ROLES:
            cfg = scenario.operations.utility[role]
            comp = {k: rep.metrics[m] / cfg.scales[k] for k, m in COMPONENT_METRIC[role].items()}
            components[role] = comp
            utilities[role] = 1.0 - sum(cfg.weights[k] * comp[k] for k in comp)
        out.append(Assessment(plan=p, validation=rep, utilities=utilities, components=components))
    return out
