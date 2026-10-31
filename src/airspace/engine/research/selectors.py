"""Research bargaining selectors on exact utility tables (counterfactuals only).

Production selection is ``airspace.engine.bargaining.choose_compromise`` and is
unchanged. Every rule here is evaluated on the same feasible set F, using
exact rationals from the independent oracle. Notation: d = U(fallback),
g_i(x) = U_i(x) - d_i, ideal_i = max_{x in F} U_i(x), range_i = ideal_i - d_i,
h_i = g_i / range_i (normalised gain, defined for range_i > 0).

A  strict_nash          {g_i > eps for all i}; max prod g; production tie-breaks.
A' strict_nash_scaled   {g_i > eps_rel * range_i for all i}; max prod g; then max min h,
                        max sum h, earliest. Invariant to role-specific positive affine maps.
B  weak_ir_lex          {g_i >= 0 for all i, some g_i > 0}; max #{g_i > 0}; then max prod of
                        the positive gains; then max min positive gain; then earliest.
                        (Ordinary prod g is 0 on the boundary and cannot rank it.)
D1 kalai_smorodinsky    {g >= 0}; max min_{i: range_i > 0} h_i; then max sum h; then earliest.
D2 leximin_normalised   {g >= 0}; lexicographic max of sorted h; then earliest.
E  utilitarian          max sum U_i over F (equal 1/3 stakeholder weights; no IR).
E' utilitarian_ir       max sum U_i over {g >= 0}.
C  strict_nash with a counterfactual d (e.g. per-role worst utility) - labelled, not proposed.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction

ROLES = ("atc", "pilot", "ground")
EPS = Fraction(1e-9)


@dataclass(frozen=True)
class Table:
    ids: tuple[str, ...]
    u: dict[str, dict[str, Fraction]]
    order: dict[str, tuple]
    fallback: str | None
    raw: dict[str, dict]

    @classmethod
    def from_oracle(cls, ref) -> "Table":
        order = {p: (ref.plans[p]["landing"], ref.plans[p]["gate_in"], p) for p in ref.feasible_ids}
        raw = {p: {m: v for r in ROLES for m, v in ref.plans[p]["raw"][r].items()} for p in ref.feasible_ids}
        return cls(tuple(ref.feasible_ids), {p: ref.utilities[p] for p in ref.feasible_ids}, order, ref.fallback_id, raw)

    def gains(self, p, d=None):
        d = d or self.u[self.fallback]
        return {r: self.u[p][r] - d[r] for r in ROLES}

    def ranges(self):
        d = self.u[self.fallback]
        return {r: max(self.u[p][r] for p in self.ids) - d[r] for r in ROLES}


def _argbest(cands, key):
    best = None
    for p in cands:
        if best is None or key(p) < key(best):
            best = p
    return best


def strict_nash(t: Table, eps: Fraction = EPS, d=None):
    if t.fallback is None:
        return None
    d = d or t.u[t.fallback]
    pool = [p for p in t.ids if all(v > eps for v in t.gains(p, d).values())]

    def key(p):
        g = t.gains(p, d)
        return (-(g["atc"] * g["pilot"] * g["ground"]), -min(g.values()),
                sum(1 - t.u[p][r] for r in ROLES), t.order[p])
    return _argbest(pool, key)


def strict_nash_scaled(t: Table, eps_rel: Fraction = EPS):
    if t.fallback is None:
        return None
    rng = t.ranges()
    if any(v <= 0 for v in rng.values()):
        return None
    pool = [p for p in t.ids if all(t.gains(p)[r] > eps_rel * rng[r] for r in ROLES)]

    def key(p):
        g = t.gains(p)
        h = [g[r] / rng[r] for r in ROLES]
        return (-(g["atc"] * g["pilot"] * g["ground"]), -min(h), -sum(h), t.order[p])
    return _argbest(pool, key)


def weak_ir_lex(t: Table):
    if t.fallback is None:
        return None
    pool = [p for p in t.ids if all(v >= 0 for v in t.gains(p).values()) and any(v > 0 for v in t.gains(p).values())]

    def key(p):
        pos = [v for v in t.gains(p).values() if v > 0]
        prod = Fraction(1)
        for v in pos:
            prod *= v
        return (-len(pos), -prod, -min(pos), t.order[p])
    return _argbest(pool, key)


def _ir_pool(t: Table):
    return [p for p in t.ids if all(v >= 0 for v in t.gains(p).values())]


def kalai_smorodinsky(t: Table):
    if t.fallback is None:
        return None
    rng = t.ranges()
    roles = [r for r in ROLES if rng[r] > 0]
    if not roles:
        return t.fallback

    def key(p):
        h = [t.gains(p)[r] / rng[r] for r in roles]
        return (-min(h), -sum(h), t.order[p])
    return _argbest(_ir_pool(t), key)


def leximin_normalised(t: Table):
    if t.fallback is None:
        return None
    rng = t.ranges()
    roles = [r for r in ROLES if rng[r] > 0]
    if not roles:
        return t.fallback
    return _argbest(_ir_pool(t), lambda p: (tuple(-v for v in sorted(t.gains(p)[r] / rng[r] for r in roles)), t.order[p]))


def utilitarian(t: Table, ir: bool = False):
    if t.fallback is None:
        return None
    pool = _ir_pool(t) if ir else list(t.ids)
    return _argbest(pool, lambda p: (-sum(t.u[p].values()), t.order[p]))


def worst_case_d(t: Table):
    return {r: min(t.u[p][r] for p in t.ids) for r in ROLES}


SELECTORS = {
    "A_strict_nash": strict_nash,
    "A2_strict_nash_scaled": strict_nash_scaled,
    "B_weak_ir_lex": weak_ir_lex,
    "D1_kalai_smorodinsky": kalai_smorodinsky,
    "D2_leximin_normalised": leximin_normalised,
    "E_utilitarian": utilitarian,
    "E2_utilitarian_ir": lambda t: utilitarian(t, ir=True),
    "C_strict_nash_worst_case_d": lambda t: strict_nash(t, d=worst_case_d(t)) if t.fallback else None,
}


def recommended(t: Table, name: str):
    """Plan the selector would recommend: its pick, else the fallback (production semantics)."""
    pick = SELECTORS[name](t)
    return pick if pick is not None else t.fallback


# ---- Pareto relations on a table ---------------------------------------------------
def weakly_improves(t: Table, x, y) -> bool:
    """x is a weak Pareto improvement over y: >= for all roles, > for at least one."""
    return all(t.u[x][r] >= t.u[y][r] for r in ROLES) and any(t.u[x][r] > t.u[y][r] for r in ROLES)


def strictly_improves(t: Table, x, y) -> bool:
    return all(t.u[x][r] > t.u[y][r] for r in ROLES)


def pareto_efficient(t: Table, x) -> bool:
    return not any(weakly_improves(t, y, x) for y in t.ids)
