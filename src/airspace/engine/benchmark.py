"""Reproducible performance benchmark for the Member 1 engine and ingestion.

    PYTHONPATH=src python -m airspace.engine.benchmark --out <dir> [--flights flights.csv]
"""

from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

from airspace.data.scenario_suite import ChallengeAssumptions, ScenarioSpec, build_suite_scenario
from airspace.engine.oracle import independent_oracle
from airspace.engine.pipeline import solve_scenario


def _time(fn, repeat: int = 5) -> dict:
    samples = []
    for _ in range(repeat):
        t = time.perf_counter()
        out = fn()
        samples.append(time.perf_counter() - t)
    return {"median_ms": round(1000 * statistics.median(samples), 3), "min_ms": round(1000 * min(samples), 3), "result": out}


def engine_scaling() -> list[dict]:
    rows = []
    for horizon in (15, 45, 90, 180, 360):
        spec = ScenarioSpec(scenario_id=f"bench-den-h{horizon}", airport="DEN", category="benchmark", seed=1,
                            assumptions=replace(ChallengeAssumptions(), planning_horizon_min=horizon,
                                                spare_fuel_includes_final_reserve=False))
        s = build_suite_scenario(spec)
        r = _time(lambda: solve_scenario(s))
        o = r.pop("result")
        sel = [solve_scenario(s).summary() for _ in range(3)]
        rows.append({"horizon_min": horizon, "candidates": o.candidate_count, "feasible": len(o.assessments),
                     **r, "deterministic_over_3_runs": all(x == sel[0] for x in sel),
                     "oracle_ms": _time(lambda: independent_oracle(s), repeat=3)["median_ms"]})
    return rows


def ingestion(flights: Path) -> dict:
    from airspace.data.ingest import select_flight

    first = _time(lambda: select_flight(flights), repeat=3)
    first.pop("result")
    last_index = 5_819_078  # final data row: forces a full chunked scan
    t = time.perf_counter()
    try:
        select_flight(flights, row_index=last_index)
        outcome = "qualifying"
    except Exception as exc:  # the last row is not required to qualify
        outcome = type(exc).__name__
    full_s = time.perf_counter() - t
    return {"first_qualifying_row": first, "full_scan_to_last_row_s": round(full_s, 2), "last_row_outcome": outcome,
            "full_scan_rows_per_s": round((last_index + 1) / full_s), "file_bytes": flights.stat().st_size}


def grid_stage_comparison(repeat: int = 5) -> list[dict]:
    """Per-stage production timing on the slot, integer-minute and event grids (no tracemalloc)."""
    import tracemalloc

    from airspace.data.scenario_suite import stratified_specs
    from airspace.engine.bargaining import choose_compromise
    from airspace.engine.candidates import generate_candidates
    from airspace.engine.fallback import choose_fallback
    from airspace.engine.research.grid import event_candidates, refine
    from airspace.engine.scoring import evaluate_candidates

    rows = []
    specs = [sp for sp in stratified_specs("dev") if sp.scenario_id.endswith("k0") and sp.category in ("normal", "wake_dense")]
    for spec in specs:
        s5 = build_suite_scenario(spec)
        s1 = refine(s5, 1)
        for grid, scen, gen in (("slot", s5, generate_candidates), ("integer_minute", s1, generate_candidates), ("event", s1, event_candidates)):
            g = []; v = []; c = []
            for _ in range(repeat):
                t0 = time.perf_counter(); plans = gen(scen); t1 = time.perf_counter()
                ev = evaluate_candidates(scen, plans); t2 = time.perf_counter()
                fb = choose_fallback(ev.assessments)
                cp = choose_compromise(ev.assessments, fb.plan_id) if fb else None
                t3 = time.perf_counter()
                g.append(t1 - t0); v.append(t2 - t1); c.append(t3 - t2)
            tracemalloc.start()
            evaluate_candidates(scen, gen(scen))
            peak = tracemalloc.get_traced_memory()[1]
            tracemalloc.stop()
            rows.append({"scenario": spec.scenario_id, "grid": grid, "candidates": len(plans), "feasible": len(ev.assessments),
                         "generate_ms": round(1000 * statistics.median(g), 3), "validate_score_ms": round(1000 * statistics.median(v), 3),
                         "select_ms": round(1000 * statistics.median(c), 3), "heap_peak_mb": round(peak / 2**20, 2),
                         "fallback": fb.plan_id if fb else None, "compromise": cp.plan_id if cp else None})
    return rows


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--flights", type=Path)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    result = {"environment": {"python": sys.version.split()[0], "platform": platform.platform(),
                              "machine": platform.machine()},
              "engine_scaling": engine_scaling(), "grid_stage_comparison": grid_stage_comparison()}
    if args.flights and args.flights.exists():
        result["ingestion"] = ingestion(args.flights)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    result["peak_rss_mb"] = round(rss / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
    (args.out / "benchmark.json").write_text(json.dumps(result, indent=1) + "\n")
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
