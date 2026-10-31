# Report 1 — Ingestion bug fix and provenance (Member 1)

## Outcome

| Item | Status |
|---|---|
| Reader leak reproduced | **Yes**, on the exception paths only |
| Fix | `with pd.read_csv(..., chunksize=...) as reader:` in `select_flight` |
| Published | Commit `709eab2`, fast-forward pushed to `origin/feat/data-engine` (only `ingest.py`, `test_data.py`) |
| Warnings-as-errors | Before: **3 failed / 100 passed**. After: **113 passed** (real data), **110 passed + 3 skipped** (no CSVs), **113 passed** on Python 3.11 |
| Historical behaviour preserved | Yes: the default selection is still data row 70 and equals the committed anchor; explicit-row semantics are unchanged |
| `hist:*` / `derived:clock.*` provenance-only | Confirmed; locked in with 4 new tests (no code change needed) |

## 1. Reproduction

`select_flight` called `reader.close()` once, after the loop. I wrapped
`pandas.read_csv` with a spy that records each chunked reader, then checked
`reader.handles.handle.closed` directly, with GC disabled during each call, so
the result doesn't depend on garbage collection:

| # | Path | Before fix | After fix |
|---|---|---|---|
| 1 | Target found → early return (`break`) | closed | closed |
| 2 | Exception during chunk processing | **open** | closed |
| 3 | Exception during row validation (explicit non-qualifying / malformed row) | **open** | closed |
| 4 | Requested row does not exist | closed | closed |
| 5 | Normal iterator exhaustion | closed | closed |
| 6 | Three sequential calls | closed | closed |

**Correction to the original report.** An early return did *not* leak, because
`break` reaches `reader.close()`. The leak happened only when an exception
escaped the loop. Under `-W error`, pytest turns the resulting
`ResourceWarning` into `PytestUnraisableExceptionWarning` at GC time, which is
why it surfaced in whichever test triggered collection:
`test_no_qualifying_row`, `test_malformed_values_rejected…` and
`test_real_explicit_nonqualifying_row_raises`.

## 2. Fix

`src/airspace/data/ingest.py`: the chunked reader is opened with its context
manager, and the loop body is re-indented unchanged. The parsing options
(`dtype=str`, `keep_default_na=False`, `usecols`, `chunksize`), the selection
order and the return value are all identical. The header probe
(`nrows=0`), `_read_lookup` and `file_sha256` already used context managers.

## 3. Regression tests (`tests/test_data.py`)

The fixture `tracked_readers` spies on every chunked reader and asserts that
its handle is closed, independent of GC:

* `test_reader_closed_after_early_return`
* `test_reader_closed_when_chunk_processing_raises` (injected `RuntimeError`)
* `test_reader_closed_when_explicit_row_fails_validation[0..3]` (predicate, nulls, cancelled, diverted)
* `test_reader_closed_when_explicit_row_has_malformed_value`
* `test_reader_closed_when_requested_row_missing`
* `test_reader_closed_after_normal_exhaustion`
* `test_repeated_sequential_ingestion_is_closed_and_identical` (5 calls, identical results)

**Mutation check.** Against the pre-fix `ingest.py`, the 6 exception-path tests
fail and the 4 others pass. With the fix, all pass.

## 4. Provenance audit: `hist:*` and `derived:clock.*`

* **Created in one place.** `airspace.data.scenario._historical_parts`:
  - `hist:row`: the note says "Context only; not a decision input"
  - `hist:join.{airline,origin_airport,destination_airport}`: the note is `UNVERIFIED …` when unresolved
  - `derived:clock.<HHMM>`: "display only, never subtracted across airports nor used as a UTC instant"
* **Field paths** point only to `historical_row.*` and `historical_ref.*`.
* **Never consumed by the engine.** An AST scan of `airspace/engine/*.py` finds no
  access to `historical_row`, `historical_ref` or `provenance`.
* **Never cited by a hard rule.** All 13 rules cite only `sim:*` evidence, and no
  `Violation` across the three fixtures cites a `hist:`/`derived:clock.` ID.
* **Leakage test.** Editing `ARRIVAL_DELAY`, `ARRIVAL_TIME`, `WHEELS_ON` and
  `WEATHER_DELAY` changes the state hash but not the candidates, utilities or selection.

None of these IDs can therefore act as a live sensor reading, verified aircraft
state, live airport fact, runway-availability evidence or enforceable safety
rule *inside Member 1's code*. Regression tests added to `tests/test_engine.py`:
`test_no_rule_cites_historical_or_clock_provenance`,
`test_violations_never_cite_provenance_only_evidence[*]`,
`test_engine_source_never_reads_historical_fields` and
`test_historical_and_clock_evidence_is_labelled_provenance`.

**Note for Member 2 (not changed here).** The coordinator routes evidence to
roles by `field_path` keywords. `hist:*`/`derived:clock.*` can be recognised by
their `kind` (`historical`/`derived`) and by the `historical_*` field-path
prefix. If agents may cite evidence IDs in a `hard_constraint` objection, the
coordinator should reject `hist:`/`derived:clock.` citations as safety
evidence. A shared `PROVENANCE_ONLY_PREFIXES` constant in `contracts.py` would be
an additive contract change; it is proposed, not applied.
