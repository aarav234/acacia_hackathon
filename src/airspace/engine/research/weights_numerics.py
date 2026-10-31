"""Weight-space, scale and numerical-robustness experiments (dev + tuning + legacy only).

    PYTHONPATH=src python -m airspace.engine.research.weights_numerics --out <dir>

The holdout partition is deliberately excluded (see preregistration.md).
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import time
import zlib
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path

from airspace.contracts import CONTRACT_DEFAULT_WEIGHTS, ROLE_METRICS
from airspace.data.fixtures import load_fixture_scenario
from airspace.data.scenario_suite import build_suite, build_suite_scenario, stratified_specs, with_fuel_reading
from airspace.engine.oracle import independent_oracle
from airspace.engine.research.grid import refine
from airspace.engine.research.selectors import ROLES, Table, strict_nash, strict_nash_scaled
from airspace.engine.sensitivity import _renorm

SEED = 20261102
BASE = {r: dict(CONTRACT_DEFAULT_WEIGHTS[r]) for r in ROLES}
SCALES = None  # filled per scenario


def scenarios() -> list[tuple[str, object]]:
    out = [(n, load_fixture_scenario(n)) for n in ("success", "no_feasible", "no_positive_gain")]
    out += [(sp.scenario_id, s) for sp, s in build_suite()]
    for part in ("dev", "tuning"):
        for sp in stratified_specs(part):
            for inc in (True, False):
                v = with_fuel_reading(sp, inc)
                out.append((v.scenario_id, build_suite_scenario(v)))
    return out


def table(s, weights=None, scales=None) -> Table:
    return Table.from_oracle(independent_oracle(s, weights=weights, scales=scales))


def base_scales(s):
    return {m.metric: m.scale for r in ROLES for m in getattr(s.operations.utility, r).metrics}


def eps_sweep(items):
    eps_values = [0, 1e-15, 1e-12, 1e-9, 1e-6, 1e-3, 1e-2]
    changes = Counter()
    bargains = Counter()
    for _, s in items:
        t = table(s)
        if t.fallback is None:
            continue
        ref = strict_nash(t)
        for e in eps_values:
            pick = strict_nash(t, Fraction(e))
            changes[e] += pick != ref
            bargains[e] += pick is not None
    return {"changes_vs_1e-9": {str(k): v for k, v in changes.items()}, "bargains": {str(k): v for k, v in bargains.items()}}


def decimal_weights(items):
    """Utilities from exact decimal weights Fraction('0.6') vs the stored binary floats."""
    sign_flips = sel_changes = near_zero = 0
    for _, s in items:
        tf = table(s)
        if tf.fallback is None:
            continue
        wd = {r: {m: Fraction(str(w)) for m, w in BASE[r].items()} for r in ROLES}
        td = table(s, weights=wd)
        d_f, d_d = tf.u[tf.fallback], td.u[td.fallback]
        for p in tf.ids:
            for r in ROLES:
                gf, gd = tf.u[p][r] - d_f[r], td.u[p][r] - d_d[r]
                if gd == 0 and gf != 0:
                    near_zero += 1
                if (gf > 0) != (gd > 0):
                    sign_flips += 1
        sel_changes += strict_nash(tf, Fraction(0)) != strict_nash(td, Fraction(0))
    return {"exact_decimal_ties_seen_as_nonzero_in_binary": near_zero, "gain_sign_flips_at_eps0": sign_flips,
            "eps0_selection_changes": sel_changes}


def near_ties(items, samples=40):
    """Relative gap between the best and second-best eligible Nash products (sampled weights)."""
    gaps = []
    rng = random.Random(SEED)
    for _, s in items:
        for i in range(samples):
            w = BASE if i == 0 else {r: _renorm(dict(zip(ROLE_METRICS[r], (rng.gammavariate(1.0, 1.0) for _ in ROLE_METRICS[r])))) for r in ROLES}
            t = table(s, weights=w)
            if t.fallback is None:
                continue
            prods = sorted((math.prod(t.gains(p).values()) for p in t.ids if all(v > Fraction(1e-9) for v in t.gains(p).values())), reverse=True)
            if len(prods) >= 2:
                gaps.append(float((prods[0] - prods[1]) / prods[0]))
    gaps.sort()
    return {"tables_with_2plus_eligible": len(gaps), "min_relative_gap": gaps[0] if gaps else None,
            "share_gap_below_1e-6": sum(g < 1e-6 for g in gaps) / len(gaps) if gaps else None,
            "median_relative_gap": statistics.median(gaps) if gaps else None, "exact_ties": sum(g == 0 for g in gaps)}


def affine(items, trials=50):
    rng = random.Random(SEED + 1)
    res = Counter()
    for _, s in items:
        for i in range(8):
            w = BASE if i == 0 else {r: _renorm(dict(zip(ROLE_METRICS[r], (rng.gammavariate(1.0, 1.0) for _ in ROLE_METRICS[r])))) for r in ROLES}
            t = table(s, weights=w)
            if t.fallback is None or sum(all(v > 0 for v in t.gains(p).values()) for p in t.ids) < 1:
                continue
            a0, b0 = strict_nash(t), strict_nash_scaled(t)
            for _ in range(trials // 8):
                a = {r: Fraction(rng.choice([1, 3, 10, 1000, 10**6])) / rng.choice([1, 7, 10**4]) for r in ROLES}
                b = {r: Fraction(rng.randint(-5, 5)) for r in ROLES}
                t2 = Table(t.ids, {p: {r: a[r] * t.u[p][r] + b[r] for r in ROLES} for p in t.ids}, t.order, t.fallback, t.raw)
                res["trials"] += 1
                res["A_changed"] += strict_nash(t2) != a0
                res["A2_changed"] += strict_nash_scaled(t2) != b0
    return dict(res)


def perturbation_stability(items, n_joint=60):
    """Selection stability on the slot grid vs the integer-minute grid (same perturbations)."""
    rng = random.Random(SEED + 2)
    out = defaultdict(lambda: defaultdict(list))
    for name, s in items:
        s1 = refine(s, 1)
        sc = base_scales(s)
        configs = []
        for r in ROLES:
            for m in ROLE_METRICS[r]:
                for f in (0.5, 1.5):
                    w = {k: dict(v) for k, v in BASE.items()}
                    w[r][m] *= f
                    w[r] = _renorm(w[r])
                    configs.append(("ofat_pm50", w, None))
        for _ in range(n_joint):
            configs.append(("joint_scale_loguniform_x0.5_x2", BASE, {m: v * math.exp(rng.uniform(math.log(0.5), math.log(2))) for m, v in sc.items()}))
        for grid, scen in (("slot", s), ("integer_minute", s1)):
            base = strict_nash(table(scen))
            tb = table(scen)
            if tb.fallback is None:
                continue
            base_pick = base or tb.fallback
            for fam, w, scl in configs:
                t = table(scen, weights=w, scales=scl)
                pick = strict_nash(t) or t.fallback
                out[fam][grid].append(pick == base_pick)
    return {fam: {g: {"runs": len(v), "stability": round(statistics.fmean(v), 4)} for g, v in grids.items()} for fam, grids in out.items()}


def decision_maps(items, step=0.05):
    """Distinct selections over a weight grid for one role at a time (others at baseline)."""
    maps = {}
    n = round(1 / step)
    for name, s in items:
        tb = table(s)
        if tb.fallback is None:
            continue
        per_role = {}
        for role in ROLES:
            ms = ROLE_METRICS[role]
            picks = Counter()
            pts = [(i / n, 1 - i / n) for i in range(n + 1)] if len(ms) == 2 else \
                  [(i / n, j / n, (n - i - j) / n) for i in range(n + 1) for j in range(n + 1 - i)]
            for pt in pts:
                w = {k: dict(v) for k, v in BASE.items()}
                w[role] = dict(zip(ms, pt))
                t = table(s, weights=w)
                picks[strict_nash(t) or t.fallback] += 1
            per_role[role] = {"grid_points": len(pts), "distinct_selections": len(picks),
                              "baseline_selection_share": picks[strict_nash(tb) or tb.fallback] / len(pts)}
        maps[name] = per_role
    return maps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    items = scenarios()
    dev_like = [(n, s) for n, s in items if not n.startswith("strat-tuning")]
    sensitive = [(n, s) for n, s in items if n in {"success", "suite-mia-tight_fuel", "suite-den-tight_fuel",
                                                    "suite-mia-generous_fuel", "suite-mia-large_delay", "suite-sjc-crew_restricted"}]
    res = {"scenario_count": len(items), "seed": SEED,
           "eps_sweep": eps_sweep(items), "decimal_weights": decimal_weights(items),
           "near_ties": near_ties([x for x in dev_like if not x[0].startswith("strat")] + dev_like[43:43 + 64], samples=20),
           "affine": affine(items), "perturbation_stability": perturbation_stability(dev_like[:43] + dev_like[43:43 + 64:2]),
           "decision_maps": decision_maps(sensitive)}
    res["elapsed_s"] = round(time.perf_counter() - t0, 1)
    (args.out / "weights_numerics.json").write_text(json.dumps(res, indent=1, default=str) + "\n")
    print(json.dumps(res, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
