"""Stratified research evaluation (production vs grid and selector research variants).

    PYTHONPATH=src python -m airspace.engine.research.evaluate --partition dev --out <dir>

Every comparison across weight vectors uses RAW metrics or utilities under the fixed
baseline weights, never Nash products from different utility definitions.
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import statistics
import sys
import time
import tracemalloc
import zlib
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

from airspace.contracts import CONTRACT_DEFAULT_WEIGHTS, ROLE_METRICS
from airspace.data.scenario_suite import build_suite_scenario, stratified_specs, with_fuel_reading
from airspace.engine.bargaining import choose_compromise
from airspace.engine.candidates import generate_candidates
from airspace.engine.constraints import validate_plan
from airspace.engine.errors import CandidateCapExceededError
from airspace.engine.fallback import choose_fallback
from airspace.engine.oracle import independent_oracle
from airspace.engine.research.diagnosis import diagnose
from airspace.engine.research.grid import event_candidates, refine
from airspace.engine.research.selectors import ROLES, SELECTORS, Table, pareto_efficient, recommended
from airspace.engine.scoring import evaluate_candidates
from airspace.engine.sensitivity import _renorm

SEED = 20261101
RAW = ("landing_delay_min", "spillover_proxy_min", "incremental_fuel_kg", "connection_penalty_pax",
       "turbulence_exposure_index", "turnaround_delay_min", "crew_overtime_min", "gate_change_indicator")


def _weights(rng: random.Random, conc: float | None):
    base = CONTRACT_DEFAULT_WEIGHTS
    out = {}
    for r in ROLES:
        a = [conc * base[r][m] if conc else 1.0 for m in ROLE_METRICS[r]]
        out[r] = _renorm(dict(zip(ROLE_METRICS[r], (rng.gammavariate(x, 1.0) for x in a))))
    return out


def _production(s, plans):
    t0 = time.perf_counter()
    ev = evaluate_candidates(s, plans)
    t1 = time.perf_counter()
    fb = choose_fallback(ev.assessments)
    cp = choose_compromise(ev.assessments, fb.plan_id) if fb else None
    t2 = time.perf_counter()
    return ev, fb, cp, (t1 - t0, t2 - t1)


def evaluate_spec(spec, weight_samples: int = 30) -> dict:
    s = build_suite_scenario(spec)
    out: dict = {"scenario": spec.scenario_id, "family": spec.category, "airport": spec.airport,
                 "fuel_includes_reserve": spec.assumptions.spare_fuel_includes_final_reserve}
    # ---- production (5-min grid) + oracle + compliance --------------------------------
    t0 = time.perf_counter()
    plans = generate_candidates(s)
    t_gen = time.perf_counter() - t0
    ev, fb, cp, (t_eval, t_sel) = _production(s, plans)
    ref = independent_oracle(s)
    sel = cp.plan_id if cp else (fb.plan_id if fb else None)
    rules = Counter(v.rule_id for r in ev.rejected for v in r.violations)
    out["production"] = {
        "candidates": len(plans), "feasible": len(ev.assessments), "rejections_by_rule": dict(rules),
        "fallback": fb.plan_id if fb else None, "compromise": cp.plan_id if cp else None, "selected": sel,
        "selected_passes_validator": (validate_plan(s, next(p for p in plans if p.plan_id == sel)).feasible if sel else None),
        "oracle_agrees": (ref.fallback_id == (fb.plan_id if fb else None) and ref.compromise_id == (cp.plan_id if cp else None)
                          and set(ref.feasible_ids) == {a.plan.plan_id for a in ev.assessments}),
        "seconds": {"generate": t_gen, "validate_and_score": t_eval, "select": t_sel},
    }
    t = Table.from_oracle(ref)
    # ---- integer-minute grid and event grid -------------------------------------------
    s1 = refine(s, 1)
    try:
        p1 = generate_candidates(s1)
        ev1, fb1, cp1, _ = _production(s1, p1)
        pe = event_candidates(s1)
        tt = time.perf_counter()
        eve, fbe, cpe, _ = _production(s1, pe)
        t_event = time.perf_counter() - tt
        ref1 = independent_oracle(s1)
        t1 = Table.from_oracle(ref1)
        par1 = {p for p in t1.ids if pareto_efficient(t1, p)}
        par1_vecs = {tuple(t1.u[p][r] for r in ROLES) for p in par1}
        ev_ids = {p.plan_id for p in pe}
        ids5 = {p.plan_id for p in plans}
        out["grids"] = {
            "candidates": {"slot_grid": len(plans), "integer_minute": len(p1), "event": len(pe)},
            "feasible": {"slot_grid": len(ev.assessments), "integer_minute": len(ev1.assessments), "event": len(eve.assessments)},
            "pareto_integer_minute": len(par1),
            "pareto_vectors_covered": {
                "slot_grid": len({tuple(t1.u[p][r] for r in ROLES) for p in par1 if p in ids5}) / len(par1_vecs) if par1_vecs else None,
                "event": len({tuple(t1.u[p][r] for r in ROLES) for p in par1 if p in ev_ids}) / len(par1_vecs) if par1_vecs else None},
            "event_matches_integer_minute": ((fbe.plan_id if fbe else None) == (fb1.plan_id if fb1 else None)
                                             and (cpe.plan_id if cpe else None) == (cp1.plan_id if cp1 else None)),
            "integer_minute_fallback": fb1.plan_id if fb1 else None,
            "integer_minute_compromise": cp1.plan_id if cp1 else None,
            "integer_minute_selected_raw": _raw_of(t1, (cp1 or fb1).plan_id) if fb1 else None,
            "event_seconds": t_event,
        }
    except CandidateCapExceededError as exc:
        out["grids"] = {"cap_error": str(exc)}
    # ---- selectors on the production slot grid ----------------------------------------
    out["selectors"] = {}
    if t.fallback:
        d = t.u[t.fallback]
        for name in SELECTORS:
            p = recommended(t, name)
            g = {r: t.u[p][r] - d[r] for r in ROLES}
            out["selectors"][name] = {"plan": p, "is_fallback": p == t.fallback, "pareto": pareto_efficient(t, p),
                                      "individually_rational": all(v >= 0 for v in g.values()),
                                      "gains": {r: float(v) for r, v in g.items()}, "raw": _raw_of(t, p)}
        # weight-stability of each selector (utilities recomputed per sample; d recomputed per sample)
        rng = random.Random(SEED + zlib.crc32(spec.scenario_id.encode()))  # stable across processes
        stab = defaultdict(int)
        bargains = 0
        samples = [("k50", 50.0)] * weight_samples + [("uniform", None)] * weight_samples
        for _, conc in samples:
            w = _weights(rng, conc)
            tw = Table.from_oracle(independent_oracle(s, weights=w))
            bargains += SELECTORS["A_strict_nash"](tw) is not None
            for name in SELECTORS:
                stab[name] += recommended(tw, name) == out["selectors"][name]["plan"]
        out["weight_stability"] = {n: v / len(samples) for n, v in stab.items()}
        out["weight_bargain_rate"] = bargains / len(samples)
    out["diagnosis"] = diagnose(s, out.get("weight_bargain_rate"))
    return out


def _raw_of(t: Table, p):
    return {m: float(t.raw[p][m]) for m in RAW}


def summarise(rows: list[dict]) -> dict:
    feas = [r for r in rows if r["production"]["feasible"]]
    summ = {
        "variants": len(rows), "with_feasible": len(feas),
        "outcomes": dict(Counter("compromise" if r["production"]["compromise"] else ("fallback" if r["production"]["fallback"] else "human_review") for r in rows)),
        "oracle_agreement": f"{sum(r['production']['oracle_agrees'] for r in rows)}/{len(rows)}",
        "selected_plans_pass_validator": f"{sum(1 for r in feas if r['production']['selected_passes_validator'])}/{len(feas)}",
        "rejections_by_rule": dict(sum((Counter(r["production"]["rejections_by_rule"]) for r in rows), Counter())),
        "labels": dict(Counter(l.split(":")[0] for r in rows for l in r["diagnosis"]["labels"])),
        "blocking_roles": dict(Counter(l.split(":")[1] for r in rows for l in r["diagnosis"]["labels"] if l.startswith("ROLE_CANNOT_GAIN"))),
    }
    g = [r["grids"] for r in rows if "candidates" in r.get("grids", {})]
    summ["grids"] = {
        "cap_errors": sum("cap_error" in r.get("grids", {}) for r in rows),
        "event_matches_integer_minute": f"{sum(x['event_matches_integer_minute'] for x in g)}/{len(g)}",
        "candidates_total": {k: sum(x["candidates"][k] for x in g) for k in ("slot_grid", "integer_minute", "event")},
        "feasible_total": {k: sum(x["feasible"][k] for x in g) for k in ("slot_grid", "integer_minute", "event")},
        "mean_pareto_vector_coverage": {k: statistics.fmean(x["pareto_vectors_covered"][k] for x in g if x["pareto_vectors_covered"][k] is not None)
                                        for k in ("slot_grid", "event")},
        "integer_minute_bargains": sum(x["integer_minute_compromise"] is not None for x in g),
        "integer_minute_fallback_differs": sum(1 for r in rows if "candidates" in r.get("grids", {}) and r["grids"]["integer_minute_fallback"] != r["production"]["fallback"]),
        "slot_vs_integer_minute_mean_raw_delta_selected": _delta(rows),
    }
    sel = defaultdict(lambda: defaultdict(list))
    for r in feas:
        for name, v in r["selectors"].items():
            sel[name]["fallback"].append(v["is_fallback"])
            sel[name]["pareto"].append(v["pareto"])
            sel[name]["ir"].append(v["individually_rational"])
            for m in RAW:
                sel[name][m].append(v["raw"][m])
            sel[name]["stability"].append(r["weight_stability"][name])
    summ["selectors"] = {n: {"share_fallback": round(statistics.fmean(v["fallback"]), 4), "share_pareto": round(statistics.fmean(v["pareto"]), 4),
                             "share_individually_rational": round(statistics.fmean(v["ir"]), 4),
                             "mean_weight_stability": round(statistics.fmean(v["stability"]), 4),
                             "mean_raw": {m: round(statistics.fmean(v[m]), 3) for m in RAW}} for n, v in sel.items()}
    secs = [r["production"]["seconds"] for r in rows]
    summ["production_ms"] = {k: round(1000 * statistics.median(x[k] for x in secs), 3) for k in secs[0]}
    return summ


def _delta(rows):
    d = defaultdict(list)
    for r in rows:
        gr = r.get("grids", {})
        if "candidates" in gr and r["production"]["selected"] and gr["integer_minute_selected_raw"]:
            for m in ("landing_delay_min", "incremental_fuel_kg", "turnaround_delay_min", "crew_overtime_min"):
                d[m].append(gr["integer_minute_selected_raw"][m] - r["selectors"]["A_strict_nash"]["raw"][m])
    return {m: round(statistics.fmean(v), 3) for m, v in d.items()}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--partition", choices=["dev", "tuning", "holdout"], required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--weight-samples", type=int, default=30)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    specs = [with_fuel_reading(sp, inc) for sp in stratified_specs(args.partition) for inc in (True, False)]
    tracemalloc.start()
    t0 = time.perf_counter()
    rows = [evaluate_spec(sp, args.weight_samples) for sp in specs]
    elapsed = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    result = {"environment": {"python": sys.version.split()[0], "platform": platform.platform()},
              "partition": args.partition, "seed": SEED, "weight_samples_per_family": args.weight_samples,
              "elapsed_s": round(elapsed, 1), "python_heap_peak_mb": round(peak / 2**20, 1), "summary": summarise(rows)}
    (args.out / f"eval_{args.partition}_rows.json").write_text(json.dumps(rows, default=str) + "\n")
    (args.out / f"eval_{args.partition}_summary.json").write_text(json.dumps(result, indent=1, default=str) + "\n")
    print(json.dumps(result, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
