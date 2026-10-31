# Contract change proposals and interpretations (Member 1)

None of these renames or removes a Shared Contract v1 public name or field.
Members 2/3: please confirm or object in review.

## Additive

1. **`select_flight` extra keyword-only arguments.** The signature is
   `select_flight(csv_path, *, row_index=None, airlines_path=None, airports_path=None,
   airport_id_map_path=None, chunk_rows=200_000)`. Calls written against the contract
   behave exactly as specified. Lookups default to `airlines.csv`/`airports.csv`
   next to `csv_path`. The airport id map (CSV `SOURCE_ID,IATA_CODE`) must be supplied
   explicitly and is never inferred.
2. **Provenance key in the returned row.** `select_flight` returns the 31 columns plus
   `"__airspace_provenance__"` (namespace `airspace.ingest/v1`). `build_scenario`
   refuses dicts that lack it. `synthetic_row()` produces the synthetic form.
3. **Extra engine helpers:** `evaluate_candidates` (feasible plus rejected reports),
   `pareto_front`, `role_best`, `gains`, `solve_scenario`, `oracle`. No other solver exists.
4. **`Scenario.seal(**fields)`** computes `state_hash`. `ensure_sealed(scenario)` detects
   post-load mutation.
5. **`AgentMessage.check_known(...)`** is a convenience check for stale or unknown IDs that the coordinator can call.
6. **`UtilityConfig.spillover_window_min`** sets the window used by the ATC spillover proxy.

## Responses to Member 2's proposals (feat/agents-negotiation)

* **CC-2 (metric naming): adopted.** Every `ValidationReport.metrics` key starts with
  `atc_`, `pilot_` or `ground_`; published as `contracts.METRIC_ROLE_PREFIXES`.
* **CC-3 (`compute_state_hash`): adopted.** `compute_state_hash(scenario)` accepts a
  `Scenario` or its `model_dump()` / `model_dump(mode="json")` dict and returns the same hash.
* **CC-4 (rule IDs): adopted.** `contracts.RULE_IDS` lists all 13 rule IDs.
* **RunResult.schema_version** now defaults to `"1.0"`, as the stub did, so the coordinator can omit it.
* **CC-1 (`EngineAPI.default()`): confirmed.** The `airspace.engine.{candidates,constraints,
  scoring,fallback,bargaining}` functions it imports exist with the contract signatures.
* **Still needed on Member 2's side:** replace `airspace.engine._stub_fixture` (it does not
  exist in the real engine) with `airspace.data.fixtures.load_fixture_scenario("success")`
  or `build_config(...)`, drop stub commit `c9f326b` when rebasing, and use real 64-hex
  state hashes in tests (the contract validates the format).

## Interpretations needing confirmation

* **RunResult consensus ⇒ `rounds_completed == 2` and `agent_message_count == 6`.**
  Consensus needs three valid round-2 acceptances, and round 2 happens only after
  three round-1 messages. If Member 2 counts invalid or late messages differently, raise it here.
* **Assessment cost key.** `components[role]["cost"]` must exist and equal `1 - utilities[role]`.
* **Gate buffer convention.** Every use, existing or proposed, occupies `[start, end + buffer)`.
* **Fuel snapshot** is taken at simulation minute 0. `approach_burn_kg` covers flying to
  wheels-on at `earliest_landing_min`. Holding burn applies to minutes after that.

## Packaging request for Member 3 (owner of `pyproject.toml`)

Member 1 code needs Python ≥ 3.11, `pydantic>=2.6,<3`, `pandas>=2.0`, `numpy>=1.26`,
and `pytest>=8` (dev). Please add `[tool.pytest.ini_options] pythonpath = ["src"]` or an
editable install. No root manifest was created on `feat/data-engine`, to avoid a conflict.

---

# Member 2 proposals (feat/agents-negotiation) — status after rebase onto feat/data-engine

| ID | Proposal | Status |
|---|---|---|
| CC-1 | optional kw-only `negotiate(..., engine=, state_probe=, epsilon=, clock=)` | Member 1 confirmed `EngineAPI.default()` imports; **Member 3 to confirm** |
| CC-2 | `atc_`/`pilot_`/`ground_` metric prefixes | adopted by Member 1 (`METRIC_ROLE_PREFIXES`); Member 2 now imports it |
| CC-3 | `compute_state_hash` export | adopted; coordinator recomputes it and calls `ensure_sealed` |
| CC-4 | `RULE_IDS` | adopted; role contexts now list the domain-relevant subset of `RULE_IDS` |
| CC-5 | optional `live = ["anthropic>=0.75"]` extra + env names `ANTHROPIC_API_KEY`, `AIRSPACE_LLM_MODEL`, `AIRSPACE_LLM_MAX_TOKENS`, `AIRSPACE_LLM_TIMEOUT_S`, `AIRSPACE_REPLAY_TRACE` | **Member 3 to confirm** |

Member 2 confirms Member 1's interpretation: consensus ⇒ `rounds_completed == 2` and
`agent_message_count == 6` (count = agent requests issued, including invalid/timed-out ones;
consensus is impossible unless all six were valid).

## Integration flags from Member 2 (after running on feat/data-engine, 2026-10-31)

All 151 tests pass together (3 skipped because `flights.csv` is not in the repo). Items to change:

**For Member 1**
1. **File-handle leak in `data/ingest.py::select_flight`.** The pandas chunk reader is closed
   only after the loop completes (`reader.close()` near line 351). An early return or a raise
   inside the loop leaves `flights.csv` open. Three `tests/test_data.py` tests fail under
   `pytest -W error` with `ResourceWarning: unclosed file`. Suggested fix:
   `with pd.read_csv(..., chunksize=chunk_rows) as reader:` (or `try/finally`).
2. **Please confirm:** `hist:*` and `derived:clock.*` evidence (including `ARRIVAL_TIME` and
   `WHEELS_ON`) are for provenance only. Member 2 sends none of them to any role context; a test asserts this.
3. **Informational:** the ATC separation metrics are absent when no movement leads or follows.
   The agents handle the missing keys. Ground has only `ground_covering_crew_count` in
   `ValidationReport.metrics`; its cost drivers come through `components["ground"]["raw.*"]`, which is the intended route.

**For Member 3 (packaging PR still missing)**
1. No `pyproject.toml`, `src/airspace/__init__.py` or `.gitignore` exists on any branch yet.
   Please add `[tool.pytest.ini_options] pythonpath = ["src", "tests"]`. `tests` is needed
   because `tests/test_*.py` import the helper `tests/fixtures_negotiation.py`.
2. `.gitignore` must include `__pycache__/`, `*.pyc`, `.env`, `runs/`, `*.csv` (but not the CSVs under `fixtures/`), `.venv/`.
3. Dependencies: Member 1's list plus the optional extra `live = ["anthropic>=0.75"]` (CC-5).
4. **There is still no `main` branch.** GitHub's default branch is currently
   `feat/agents-negotiation`. Create `main` from Member 1's bootstrap commit `8d8d498`
   (or your packaging PR) and make it the default.
