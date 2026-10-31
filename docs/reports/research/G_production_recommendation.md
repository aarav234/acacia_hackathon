# Report G — Production recommendation

**None of these changes has been applied to production.** Every item that changes
behaviour needs explicit approval.

## Acceptance-criteria check of the current production engine

| Criterion | Status |
|---|---|
| A. Constraint correctness | **Met**: every selected plan passes the validator (296/296 stratified, 34/34 legacy) |
| B. Mathematical correctness | **Met**: equal to the exhaustive exact oracle in 11,481 + 320 runs; property tests pass |
| C. Reproducibility | **Met**: bit-identical sensitivity reproduction; stable seeds |
| D. Candidate coverage | **Documented limitation**: complete for the configured grid; the 5-min grid covers about 50% of integer-minute Pareto vectors; cap → explicit error |
| E. Coefficient defensibility | **Met only as assumptions**: weights are labelled design assumptions; not empirically calibrated |
| F. Evaluation independence | **Met** for this study (pre-registered holdout run once) |
| G. Numerical stability | **Met**, with documented scale-dependence of ε and tie-breaks |
| H. Backwards compatibility | **Met** (no contract change) |
| I. Practical benefit | see below |
| J. Reporting honesty | synthetic findings only; see Report F §6 |

## Ranked recommendations

### 1. Confirmed correctness defects

**None found in the production engine.** One documentation defect is already corrected:
Report 03's "ATC-optimal 19/34" relied on a tolerance-free comparison, and an erratum now
points to Report A.

### 2. High-confidence mathematical improvements (optional; need approval)

**2a. Document ε's role.** ε = 1e-9 absorbs binary-float decimal ties (30 observed).
Keep it. Option: parse weights and scales as exact decimals (`Fraction(str(x))`) so that
true decimal ties are exactly zero.
* Code: `scoring.score` and `bargaining` would use decimal-exact rationals.
* Evidence: 0 selection changes observed.
* Risk: low.
* Contract: none.
* Approval: optional, because it is behaviour-neutral on all evaluated data.

**2b. Scale-consistent tolerance and tie-breaks (rule A2).**
ε_i = ε_rel·range_i, with tie-breaks on normalised gains.
* Evidence: invariant in the counterexamples; identical to A in 296/296 variants.
* Benefit: removes a theoretical scale-dependence.
* Risk: changes bargaining semantics in rare tie or boundary cases.
* Tests: 1 new property test exists.
* Contract: none (internal rule).
* **Approval required** (bargaining semantics).

### 3. High-impact candidate-generation enhancement

**3a. Use `slot_step_min = 1` in the simulation configs.** This is a config change, not a
contract change.
* Evidence (holdout):
  - Pareto coverage rises from 0.497 to 1.0.
  - The fallback moves earlier in 44/128 variants.
  - The selected plan's mean raw outcome improves by −1.26 min delay, −49.7 kg fuel and
    −1.21 min turnaround (synthetic).
* Cost: about 5× candidates; the measured solve rises from 16 to 80 ms.
* Risk: committed fixtures, demo plan IDs and the example assessments change, so Members
  2 and 3 must update any hard-coded IDs. The cap is reached sooner: 2,000 / routes, i.e. a horizon of roughly
  133–167 min at 12–15 routes (a measured DEN probe hit it at 160 min with 15 routes).
* **Approval required** (changes the demo outcomes).

**3b. Adopt the event-grid generator for 3a.** This is exact (320/320 identical to the
integer-minute choice).
* Benefit: 24% fewer candidates and a 22% faster validate+score stage; less cap pressure.
* Risk: a new production code path. Plans stay on the 1-min grid, so `PLAN-SLOT` stays valid.
* Tests: the exactness test exists.
* **Approval required.** Do 3a first; adopt 3b only if runtime or the cap matters.

### 4. Defensible calibration changes

**None.** There is no class-E evidence (Report D). Keep the weights; publish the implied
trade-off rates; review the passenger-connection rate once a cost source is approved.

### 5. Experimental, not yet justified for production

* **Weak-IR or Kalai–Smorodinsky selector.** It fixes the weakly dominated fallback, but
  this occurred in 1 legacy and 2 dev variants and 0 holdout. Lower-risk first step: an
  additive "weak Pareto improvement available" flag shown to the human reviewer.
  Contract-additive; needs Members 2/3.
* **Utilitarian or worst-case-d selectors.** **Not recommended**: they violate individual
  rationality relative to the safe default in about 40–50% of variants.
* **A lean exact fast path** (the oracle is about 10× faster). Not needed at current sizes.

## Tests added in this pass (`tests/test_engine.py`; 156 total pass under `-W error`)

Candidate-order invariance, dominated-candidate injection, weight-independence of
feasibility, event-grid exactness, research/production Nash equality, affine (in)variance
counterexamples, weak-IR boundary handling, selector feasibility, decimal-tie noise vs ε,
diagnosis labels, and the fixed stratified partition.
