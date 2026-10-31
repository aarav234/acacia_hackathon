# Airspace: disruption negotiation coordinator

A hackathon **decision-support simulation**. Three AI role agents (ATC, Pilot, Ground Ops)
negotiate a landing plan for one disrupted inbound flight. A deterministic engine enforces the
safety rules and computes a Nash-product compromise, and a human duty manager approves the result.
This is not certified ATC, not a clearance system and not autonomous; nothing is executed.

## Run
```
python -m venv .venv && .venv\Scripts\activate          # Windows (macOS/Linux: source .venv/bin/activate)
pip install "pydantic>=2.6,<3" "pandas>=2.0" "numpy>=1.26" pytest streamlit openai
# PowerShell: $env:PYTHONPATH="src;tests"   bash: export PYTHONPATH=src:tests
python -m pytest -q                                      # tests
streamlit run app.py                                     # duty-manager console
```
Live mode: set `AIRSPACE_LLM_PROVIDER=openai` and `OPENAI_API_KEY`. Mock and Replay need no key.
Live never falls back to mock on its own.

## Where things are
| Area | Owner | Docs |
|---|---|---|
| Contracts, data, engine (`src/airspace/{contracts.py,data,engine}`) | Member 1 | `docs/shared_contract.md`, `docs/data_and_model.md` |
| Agents and bounded negotiation (`src/airspace/{agents,negotiation}`) | Member 2 | `docs/negotiation.md` |
| Console (`app.py`, `ui/`), evaluation, packaging | Member 3 | `docs/ui.md` |

Interface proposals and open items are in `docs/contract_changes.md`.
Packaging (`pyproject.toml`) is still to come from Member 3; until it lands, set `PYTHONPATH` as shown above.
