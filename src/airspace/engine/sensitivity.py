"""Weight/scale sensitivity, invariance and baseline experiments (Member 1).

Every weight configuration re-seals the scenario with the new utility block and
re-runs the full engine (candidates, validation, utilities, fresh disagreement
point, gains, selection). Every run is checked against the independent oracle.
Cross-configuration comparisons use RAW operational metrics only, never Nash
products or utilities from different utility definitions.

Nothing here selects production weights. The perturbations are hypothetical
design-space probes, not estimates of real stakeholder preferences.

    PYTHONPATH=src python -m airspace.engine.sensitivity --out <dir>
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

from airspace.contracts import CONTRACT_DEFAULT_WEIGHTS, ROLE_METRICS, ROLES, Scenario
from airspace.engine.oracle import independent_oracle
from airspace.engine.pipeline import solve_scenario

SEED = 20261031
RAW_KEYS = {
    "landing_delay_min": "atc", "spillover_proxy_min": "atc", "incremental_fuel_kg": "pilot",
    "connection_penalty_pax": "pilot", "turbulence_exposure_index": "pilot", "turnaround_delay_min": "ground",
    "crew_overtime_min": "ground", "gate_change_indicator": "ground",
}


# ---- configuration families ------------------------------------------------------
def _renorm(ws: dict[str, float]) -> dict[str, float]:
    total = sum(ws.values())
    keys = list(ws)
    out = {k: ws[k] / total for k in keys[:-1]}
    out[keys[-1]] = 1.0 - sum(out.values())  # exact simplex closure
    return out


def weight_families(seed: int = SEED, n_dirichlet: int = 100) -> dict[str, list[dict]]:
    base = {r: dict(CONTRACT_DEFAULT_WEIGHTS[r]) for r in ROLES}
    fam: dict[str, list[dict]] = {"baseline": [base]}
    ofat = []
    for role in ROLES:
        for metric in ROLE_METRICS[role]:
            for f in (0.8, 0.9, 1.1, 1.2):
                w = {r: dict(base[r]) for r in ROLES}
                w[role][metric] *= f
                w[role] = _renorm(w[role])
                ofat.append(w)
    fam["ofat_pm10_pm20"] = ofat
    rng = random.Random(seed)
    for name, conc in (("dirichlet_local_k50", 50.0), ("dirichlet_uniform_k1", None)):
        samples = []
        for _ in range(n_dirichlet):
            w = {}
            for role in ROLES:
                alphas = [conc * base[role][m] if conc else 1.0 for m in ROLE_METRICS[role]]
                draws = [rng.gammavariate(a, 1.0) for a in alphas]
                w[role] = _renorm(dict(zip(ROLE_METRICS[role], draws)))
            samples.append(w)
        fam[name] = samples
    vertices = []
    for a in ROLE_METRICS["atc"]:
        for p in ROLE_METRICS["pilot"]:
            for g in ROLE_METRICS["ground"]:
                pick = {"atc": a, "pilot": p, "ground": g}
                vertices.append({r: {m: (1.0 if m == pick[r] else 0.0) for m in ROLE_METRICS[r]} for r in ROLES})
    fam["simplex_vertices"] = vertices
    return fam


def reweighted(s: Scenario, weights: dict, scale_factors: dict[str, float] | None = None) -> Scenario:
    data = s.model_dump(mode="json")
    util = data["operations"]["utility"]
    for role in ROLES:
        for spec in util[role]["metrics"]:
            spec["weight"] = weights[role][spec["metric"]]
            if scale_factors:
                spec["scale"] = spec["scale"] * scale_factors.get(spec["metric"], 1.0)
    data.pop("state_hash")
    return Scenario.seal(**data)


# ---- exact selection on a utility table (for counterfactual d / affine checks) ----
def select_exact(util: dict[str, dict[str, Fraction]], d: dict[str, Fraction], order: dict[str, tuple],
                 eps: Fraction, cost: dict[str, dict[str, Fraction]] | None = None) -> str | None:
    cost = cost or {p: {r: 1 - u[r] for r in ROLES} for p, u in util.items()}
    best, best_key = None, None
    for p, u in util.items():
        g = {r: u[r] - d[r] for r in ROLES}
        if not all(v > eps for v in g.values()):
            continue
        key = (-(g["atc"] * g["pilot"] * g["ground"]), -min(g.values()), sum(cost[p].values()), order[p])
        if best_key is None or key < best_key:
            best, best_key = p, key
    return best


# ---- experiment runner ----------------------------------------------------------------
def _raw(o, pid):
    if pid is None:
        return None
    a = o.assessment(pid)
    return {m: a.components[RAW_KEYS[m]][f"raw.{m}"] for m in RAW_KEYS}


def run_configs(scenarios: list[tuple[str, str, Scenario]], families: dict[str, list[dict]],
                scale_runs: list[tuple[str, dict]]) -> list[dict]:
    rows = []
    jobs = [(fam, i, w, None) for fam, ws in families.items() for i, w in enumerate(ws)]
    jobs += [("scale_ofat_x0.5_x2", i, families["baseline"][0], sf) for i, (_, sf) in enumerate(scale_runs)]
    for sid, cat, base in scenarios:
        for fam, i, w, sf in jobs:
            s = base if (fam == "baseline" and sf is None) else reweighted(base, w, sf)
            t0 = time.perf_counter()
            o = solve_scenario(s)
            t_engine = time.perf_counter() - t0
            ref = independent_oracle(s)
            agree = ((o.fallback.plan_id if o.fallback else None) == ref.fallback_id
                     and (o.compromise.plan_id if o.compromise else None) == ref.compromise_id
                     and {a.plan.plan_id for a in o.assessments} == set(ref.feasible_ids))
            sel = o.compromise.plan_id if o.compromise else (o.fallback.plan_id if o.fallback else None)
            rows.append({
                "scenario": sid, "category": cat, "family": fam, "config": i,
                "scale_change": None if sf is None else scale_runs[i][0],
                "reason": o.reason, "fallback": o.fallback.plan_id if o.fallback else None,
                "compromise": o.compromise.plan_id if o.compromise else None, "selected": sel,
                "feasible": len(o.assessments), "candidates": o.candidate_count, "eligible": len(ref.eligible_ids),
                "fallback_pareto": (ref.fallback_id in ref.pareto_ids) if ref.fallback_id else None,
                "oracle_agrees": agree, "engine_s": t_engine,
                "raw_selected": _raw(o, sel), "raw_fallback": _raw(o, o.fallback.plan_id if o.fallback else None),
                "role_best": dict(o.role_best_ids),
                "gains": o.summary().get("compromise_gains"),
            })
    return rows


def scale_runs() -> list[tuple[str, dict]]:
    return [(f"{m}x{f:g}", {m: f}) for m in RAW_KEYS for f in (0.5, 2.0)]


# ---- analyses -------------------------------------------------------------------------
def blocking_roles(s: Scenario) -> dict:
    """At baseline weights: which roles can strictly gain over the fallback at all?"""
    ref = independent_oracle(s)
    if ref.fallback_id is None:
        return {"feasible": 0}
    d = ref.utilities[ref.fallback_id]
    can = {r: any(ref.utilities[p][r] - d[r] > Fraction(1e-9) for p in ref.feasible_ids) for r in ROLES}
    atc_opt = all(ref.utilities[p]["atc"] <= d["atc"] for p in ref.feasible_ids)
    return {"feasible": len(ref.feasible_ids), "role_can_gain": can, "fallback_atc_optimal": atc_opt,
            "fallback_pareto": ref.fallback_id in ref.pareto_ids, "eligible": len(ref.eligible_ids)}


def baselines(s: Scenario) -> dict | None:
    """Fallback, role-best, utilitarian (min sum C_i) and Nash, in raw metrics (baseline weights)."""
    o = solve_scenario(s)
    if not o.assessments:
        return None
    ref = independent_oracle(s)
    order = {a.plan.plan_id: (a.plan.landing_min, a.plan.gate_in_min, a.plan.plan_id) for a in o.assessments}
    util_sum = min(o.assessments, key=lambda a: (sum(Fraction(a.components[r]["cost"]) for r in ROLES), order[a.plan.plan_id]))
    picks = {"fallback": o.fallback.plan_id, "nash": o.compromise.plan_id if o.compromise else o.fallback.plan_id,
             "utilitarian_sum": util_sum.plan.plan_id, **{f"{r}_best": o.role_best_ids[r] for r in ROLES}}
    return {k: {"plan": p, "raw": _raw(o, p), "pareto": p in ref.pareto_ids,
                "utilities": {r: o.assessment(p).utilities[r] for r in ROLES}} for k, p in picks.items()}


def disagreement_counterfactuals(s: Scenario) -> dict | None:
    ref = independent_oracle(s)
    if ref.fallback_id is None:
        return None
    order = {p: (ref.plans[p]["landing"], ref.plans[p]["gate_in"], p) for p in ref.feasible_ids}
    u = {p: ref.utilities[p] for p in ref.feasible_ids}
    eps = Fraction(1e-9)
    out = {"contract_fallback_d": select_exact(u, u[ref.fallback_id], order, eps)}
    worst = {r: min(v[r] for v in u.values()) for r in ROLES}
    out["counterfactual_worst_case_d"] = select_exact(u, worst, order, eps)
    for e in (0.0, 1e-6, 1e-3, 1e-2):
        out[f"epsilon_{e:g}"] = select_exact(u, u[ref.fallback_id], order, Fraction(e))
    return out


def affine_check(s: Scenario, rng: random.Random, trials: int = 20) -> dict | None:
    """U_i -> a_i U_i + b_i (a_i > 0) applied to utilities AND d. Count selection changes."""
    ref = independent_oracle(s)
    if ref.fallback_id is None:
        return None
    order = {p: (ref.plans[p]["landing"], ref.plans[p]["gate_in"], p) for p in ref.feasible_ids}
    u = {p: ref.utilities[p] for p in ref.feasible_ids}
    base = select_exact(u, u[ref.fallback_id], order, Fraction(1e-9))
    base_eps0 = select_exact(u, u[ref.fallback_id], order, Fraction(0))
    changed = changed_eps0 = 0
    for _ in range(trials):
        a = {r: Fraction(rng.choice([1, 2, 5, 10, 100])) / rng.choice([1, 3, 7]) for r in ROLES}
        b = {r: Fraction(rng.randint(-5, 5)) for r in ROLES}
        u2 = {p: {r: a[r] * v[r] + b[r] for r in ROLES} for p, v in u.items()}
        cost2 = {p: {r: 1 - v[r] for r in ROLES} for p, v in u2.items()}
        changed += select_exact(u2, u2[ref.fallback_id], order, Fraction(1e-9), cost2) != base
        changed_eps0 += select_exact(u2, u2[ref.fallback_id], order, Fraction(0), cost2) != base_eps0
    return {"trials": trials, "changed_eps1e-9": changed, "changed_eps0": changed_eps0, "eligible": len(ref.eligible_ids)}


def summarise(rows: list[dict]) -> dict:
    by_fam = defaultdict(list)
    for r in rows:
        by_fam[r["family"]].append(r)
    base_sel = {r["scenario"]: r["selected"] for r in rows if r["family"] == "baseline"}
    out = {}
    for fam, rs in by_fam.items():
        feas = [r for r in rs if r["feasible"]]
        stable = [r["selected"] == base_sel[r["scenario"]] for r in feas]
        deltas = defaultdict(list)
        for r in feas:
            for m in ("landing_delay_min", "incremental_fuel_kg", "turnaround_delay_min", "crew_overtime_min"):
                deltas[m].append(r["raw_selected"][m] - r["raw_fallback"][m])
        out[fam] = {
            "runs": len(rs), "runs_with_feasible_plan": len(feas),
            "reasons": dict(Counter(r["reason"] for r in rs)),
            "positive_bargain_rate_given_feasible": round(sum(r["reason"] == "compromise_found" for r in feas) / len(feas), 4) if feas else None,
            "selection_equals_baseline_rate": round(sum(stable) / len(stable), 4) if stable else None,
            "scenarios_with_any_compromise": len({r["scenario"] for r in feas if r["compromise"]}),
            "mean_selected_minus_fallback_raw": {m: round(statistics.fmean(v), 4) for m, v in deltas.items()},
            "oracle_agreement": f"{sum(r['oracle_agrees'] for r in rs)}/{len(rs)}",
        }
    return out


def main(argv=None) -> int:
    from airspace.data.fixtures import load_fixture_scenario
    from airspace.data.scenario_suite import build_suite

    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--dirichlet", type=int, default=100)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    scenarios = [(n, f"fixture_{n}", load_fixture_scenario(n)) for n in ("success", "no_feasible", "no_positive_gain")]
    scenarios += [(spec.scenario_id, spec.category, s) for spec, s in build_suite()]
    fams = weight_families(SEED, args.dirichlet)
    t0 = time.perf_counter()
    rows = run_configs(scenarios, fams, scale_runs())
    elapsed = time.perf_counter() - t0
    rng = random.Random(SEED)
    result = {
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "seed": SEED,
                        "dirichlet_samples_per_family": args.dirichlet},
        "scenario_count": len(scenarios), "config_counts": {k: len(v) for k, v in fams.items()} | {"scale_ofat_x0.5_x2": len(scale_runs())},
        "total_runs": len(rows), "elapsed_s": round(elapsed, 2),
        "engine_ms_per_run": {"mean": round(1000 * statistics.fmean(r["engine_s"] for r in rows), 3),
                              "max": round(1000 * max(r["engine_s"] for r in rows), 3)},
        "families": summarise(rows),
        "blocking_roles": {sid: blocking_roles(s) for sid, _, s in scenarios},
        "baselines": {sid: baselines(s) for sid, _, s in scenarios},
        "disagreement_counterfactuals": {sid: disagreement_counterfactuals(s) for sid, _, s in scenarios},
        "affine": {sid: affine_check(s, rng) for sid, _, s in scenarios},
    }
    (args.out / "sensitivity_runs.json").write_text(json.dumps(rows, default=str) + "\n", encoding="utf-8")
    (args.out / "sensitivity_summary.json").write_text(json.dumps(result, indent=1, default=str) + "\n", encoding="utf-8")
    print(json.dumps({k: result[k] for k in ("scenario_count", "config_counts", "total_runs", "elapsed_s", "engine_ms_per_run")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
