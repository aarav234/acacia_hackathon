# Report A — Reproducibility audit

## Baseline

| Item | Value |
|---|---|
| Base commit | `6f0f7f31c5cacd240861012bad7da06f21ecfe7a` (`origin/m1/hardening-review`), working tree clean |
| Research branch | `m1/math-assurance` (local only) |
| Environment | Python 3.13.9, macOS-15.7.3 arm64 (Apple M2, 8 cores, 16 GB) |
| Packages | pydantic 2.12.4, pandas 2.3.3, numpy 2.3.5, pytest 9.1.0, openpyxl 3.1.5 |
| Cross-check environment | Python 3.11.13, pydantic 2.14.2, pandas 3.1.0, numpy 2.4.6, no openpyxl |

Seeds:

| Experiment | Seed |
|---|---|
| 40-scenario suite | `20261031 + 101·i` |
| Sensitivity | `20261031` |
| Stratified suite | `7,000,000 + 1000·cell + k` |
| Selector weight probe | `20261101 + crc32(id)` |
| Weights/numerics | `20261102` |

Dataset SHA256 (all match the hashes recorded in earlier reports):

| File | Bytes | sha256 |
|---|---|---|
| flights.csv | 592,406,591 | `afa4e421…3a83` |
| airlines.csv | 359 | `ec54f453…cca3` |
| airports.csv | 23,867 | `23c4cea9…e2` |
| challenge2_operational_constraints.xlsx | 27,711 | `f889ae9e…8211` |

Config versions: `sim-success-v1`, `sim-no-feasible-v1`, `sim-no-positive-gain-v1`, `sim-challenge2-v1`.

## Reproduced claims

| Prior claim (Reports 03/04) | Reproduced? | Evidence |
|---|---|---|
| 145 tests pass with data; 139 + 6 skipped without | **Yes, exactly** | pytest `-W error` |
| Python 3.11: 133 + 12 skipped | **Yes** | venv311 |
| 40 suite scenarios, 34 feasible (incl. fixtures), 2 compromises | **Yes** | diagnosis run |
| 2,703 candidates checked rule-by-rule | **Yes** | `test_independent_oracle_agrees_rule_by_rule` |
| 11,481 oracle-checked sensitivity runs | **Yes, bit-identical** | `sensitivity_summary.json` regenerated; identical to the committed file except the timing fields |
| ±10–20% one-at-a-time: no selection change | **Yes** | same file |
| 6 of 34 feasible scenarios weight-sensitive | **Yes** | same file |
| Fallback Pareto-efficient in 31/34 | **Yes** | diagnosis label `FALLBACK_EFFICIENT` = 31 |
| Benchmarks (solve 8.7 → 545.7 ms; ingestion 7.45 s) | **Yes, within ~2%** | `results/benchmark_assurance.json` (8.5 → 541.8 ms; 7.40 s) |

## Discrepancies found (none hidden)

1. **"Fallback ATC-optimal in 19/34" (Report 03 §5) vs "ATC cannot gain" in 20/34.**
   The cause is the tolerance definition. In `suite-mia-large_delay`, plan `P0030_R1_G4_T14`
   changes the ATC cost by 0.6·10/30 − 0.4·5/10, which is exactly 0 in decimal arithmetic but
   +1.85e-17 in exact arithmetic over the stored binary floats. Report 03 compared with no
   tolerance; the engine and this audit use ε = 1e-9. Both numbers are correct under their own
   definitions. Report 03 now carries an erratum note pointing here.
2. **Maximum per-run engine time:** 108 ms committed vs 188 ms reproduced. The reproduction
   overlapped the test runs at its start. Means agree (17.11 vs 17.09 ms). Maxima from
   non-isolated runs are not reliable.
3. **Stage timings inside `research.evaluate`** were measured with `tracemalloc` active and
   are inflated about 7× (validate+score median 112 ms vs 15.8 ms in the clean benchmark).
   Only `results/benchmark_assurance.json` is used for timing claims.
4. **Remote branch naming changed outside this work:** a teammate branch now appears as
   `Feat/airspace-ui`. It was not touched.
