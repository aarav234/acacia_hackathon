"""Render an engine-generated worked example (markdown) for a scenario.

    PYTHONPATH=src python -m airspace.engine.report [fixtures/success_scenario.json]
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

from airspace.contracts import ROLE_METRICS, ROLES, METRIC_UNITS, Scenario
from airspace.data.scenario import load_scenario
from airspace.engine.bargaining import gains
from airspace.engine.pipeline import solve_scenario


def worked_example(scenario: Scenario) -> str:
    out = solve_scenario(scenario)
    lines = [
        f"Scenario `{scenario.scenario_id}` (source_mode={scenario.source_mode}), "
        f"state_hash `{scenario.state_hash[:16]}…`",
        "",
        f"* candidates: {out.candidate_count}; feasible: {len(out.assessments)}; rejected: {len(out.rejected)}",
    ]
    counts = Counter(v.rule_id for r in out.rejected for v in r.violations)
    lines.append("* violations by rule: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    lines.append(f"* outcome: **{out.reason}**")
    if not out.fallback:
        return "\n".join(lines) + "\n"
    shown = {"fallback": out.fallback.plan_id}
    for role, pid in out.role_best_ids.items():
        shown[f"{role}-best"] = pid
    if out.compromise:
        shown["compromise"] = out.compromise.plan_id
    d = out.assessment(out.fallback.plan_id).utilities
    lines += ["", "Raw metrics (units) → weighted normalized components, per plan:", ""]
    header = "| metric | units | " + " | ".join(f"{k}<br>`{v}`" for k, v in shown.items()) + " |"
    lines += [header, "|" + "---|" * (len(shown) + 2)]
    for role in ROLES:
        for metric in ROLE_METRICS[role]:
            spec = scenario.operations.utility.spec(role, metric)
            cells = []
            for pid in shown.values():
                c = out.assessment(pid).components[role]
                cells.append(f"{c['raw.' + metric]:g} → {c['weighted.' + metric]:.6f}")
            lines.append(
                f"| {role}.{metric} (w={spec.weight:g}, scale={spec.scale:g}) | {METRIC_UNITS[metric]} | "
                + " | ".join(cells) + " |"
            )
    for role in ROLES:
        cells = []
        for pid in shown.values():
            a = out.assessment(pid)
            cells.append(f"C={a.components[role]['cost']:.6f}, U={a.utilities[role]:.6f}")
        lines.append(f"| **{role}** C / U | – | " + " | ".join(cells) + " |")
    gcells, pcells = [], []
    for pid in shown.values():
        g = gains(out.assessment(pid), d)
        gcells.append(", ".join(f"{float(g[r]):+.6f}" for r in ROLES))
        eligible = all(v > 1e-9 for v in g.values())
        pcells.append(f"{float(g['atc'] * g['pilot'] * g['ground']):.9f}" if eligible else "not eligible")
    lines.append("| gains U_i − d_i (atc, pilot, ground) | – | " + " | ".join(gcells) + " |")
    lines.append("| Nash product | – | " + " | ".join(pcells) + " |")
    lines += ["", f"d (fallback utilities) = " + ", ".join(f"{r}={d[r]:.6f}" for r in ROLES)]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("fixtures/success_scenario.json")
    print(worked_example(load_scenario(target)))
