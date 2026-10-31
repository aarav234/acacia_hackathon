# Report C — Candidate-generation evaluation

**Answer.**
* The production generator is **complete for its stated decision space**: configured slot
  grid × configured routes, with no duplicates and an explicit cap error instead of truncation.
* That decision space is **coarser than the contract permits**. Landing times are integer
  minutes, but the configs use a 5-minute grid, which on the holdout covers only **49.7%** of
  the integer-minute Pareto utility vectors.
* An **event grid** recovers 100% of them exactly, using about 69% of the integer-minute candidates.

## 1. Completeness of the production generator

| Check | Result |
|---|---|
| Generated = slots × configured routes (independent enumeration in the oracle) | identical in every scenario (2,703 legacy plans; all stratified variants) |
| Duplicate plan IDs / duplicate (slot, route) assignments | 0 across the 160 stratified configurations |
| Ordering / IDs | deterministic `(landing, runway, gate, route)`; `P{L:04d}_{R}_{G}_{T}` |
| Runway × gate pairs with no route (unrepresentable **by contract design**) | 22.4% of 1,480 pairs in the stratified suite (mean 17.7% per scenario); these are not "missing" but unconnected |
| Cap | raises `CandidateCapExceededError`, never truncates (tested). Example: DEN, horizon 160 min, 1-min grid → 2,400 > 2,000 → explicit error |

So **cap-induced omissions are impossible by construction**; the cost of hitting the cap is
a planning error rather than a silently incomplete optimum.

## 2. Coverage relative to the integer-minute space

All figures are means of per-variant ratios; 128 holdout variants; source
`results/eval_holdout_summary.json`.

| Grid | Candidates | Feasible | Integer-minute Pareto vectors covered | Strict bargains | Fallback differs from slot grid |
|---|---|---|---|---|---|
| Slot grid (production, 5 min) | 8,370 | 2,143 | **0.497** | 2 | — |
| Integer-minute (`slot_step_min = 1`) | 41,850 | 9,996 | 1.000 | 4 | 44 / 128 |
| Event grid | 28,698 | 3,763 | **1.000** | 4 | 44 / 128 (identical to integer-minute) |

**Selected plan, integer-minute minus slot grid** (mean over variants with a selection;
holdout):

| Metric | Change |
|---|---|
| Landing delay | **−1.26 min** |
| Incremental fuel | **−49.7 kg** |
| Turnaround delay | **−1.21 min** |
| Crew overtime | −0.30 min |

The cause is almost entirely the **fallback**: the earliest feasible minute is often
between grid points. These are synthetic-simulation differences under the toy model, not
real-world savings.

Dev and tuning show the same pattern:

| Partition | Slot-grid Pareto coverage | Fallback differs | Delay change |
|---|---|---|---|
| dev | 0.525 | 38/128 | −1.22 min |
| tuning | 0.594 | 14/64 | −0.90 min |

## 3. Event grid: an exact reduction (claim C-EVT)

**Construction.** For each route, the event grid contains the configuration-derived
minutes at which any of the following changes:
* runway windows;
* lead/follow separation regimes;
* spillover-window membership;
* gate windows and buffered occupancy;
* taxi blockage;
* crew availability and busy windows;
* connection deadlines;
* the exact rational fuel-reserve threshold.

**Argument.** Between consecutive events, feasibility, the spillover membership, the
missed-connection set and the set of covering crews are constant, and all 8 costs are
non-decreasing in landing time. Each interval's first minute therefore weakly dominates
the rest of it for every role. Since production tie-breaks prefer earlier landings, the
fallback, every Pareto vector and the Nash winner all lie on interval starts.

**Verification.**
* The brute-force interval starts are ⊆ the event grid in all 43 legacy scenarios.
  The first version missed 105 starts, all FUEL-RESERVE onsets; I added the exact threshold
  and re-verified.
* Fallback and compromise are identical to the full integer-minute grid in **320/320**
  stratified variants and 8/8 benchmark scenarios.
* Tests: `test_event_grid_is_an_exact_reduction_of_the_integer_minute_grid`.

## 4. Performance (`results/benchmark_assurance.json`; idle machine, median of 5, 8 scenarios)

| Grid | Mean candidates | Generate | Validate + score | Select | Heap peak |
|---|---|---|---|---|---|
| Slot | 57 | 0.36 ms | 15.8 ms | 0.05 ms | 0.33 MB |
| Integer-minute | 287 | 0.85 ms | 79.7 ms | 0.21 ms | 1.42 MB |
| Event | 217 | 1.04 ms | 61.8 ms | 0.12 ms | 1.13 MB |

Validation and scoring dominate, at about 0.28 ms per candidate (pydantic model
construction). The independent oracle is about 10× faster (45 ms vs 542 ms at 936
candidates), so a lean production fast path is possible but not needed at these sizes.

## 5. Variants considered and not adopted

| Variant | Reason |
|---|---|
| New decision variables (gate holding, extra taxi, alternative routes per pair) | Excluded by the contract ("no implicit gate holding") and by the lack of data |
| Dominance pruning | The event grid already is the safe, exact pruning. Further pruning across routes would need multi-route dominance proofs; not justified by the runtimes measured |
| Branch-and-bound / solver dependency | Not justified: worst measured exhaustive solve is 0.54 s at 936 candidates |
