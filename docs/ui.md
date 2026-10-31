# Duty-manager console (Streamlit, Hallmark / Wayfind)

Draft UI for Member 3 to adopt, change or replace. It runs on the real engine (Member 1)
and the bounded negotiation (Member 2); it adds no decision logic of its own.

```
pip install "streamlit>=1.40" "pydantic>=2.6,<3" "pandas>=2.0" "numpy>=1.26" openai
streamlit run app.py
```
Live mode reads `AIRSPACE_LLM_PROVIDER` and `OPENAI_API_KEY` from the environment. Without a key it
shows an error state and never switches to mock.

## Hallmark picks (v2.0.0)
- **Screen:** an app screen. Scenario, mode and deadline controls; an outcome plate with the duty-manager
  approval beside it; the negotiation board; compromise against fallback; what each role said; the audit log.
- **Theme:** Wayfind, dealt 1 of 3 (seed `airport-disruption-decision-support-console-6e58·74a57d09`). It brings
  cool grey paper, blue-black ink and Chivo Mono throughout, with square 2px borders. Cobalt marks the live and
  primary action; amber marks figures on ink.
- **Signature:** the negotiation board, set as a departure board on the ink band. Each role's round-1 proposal,
  round-2 vote and engine-computed gain against the fallback, with amber figures.
- **States:**
  - empty: no run yet;
  - loading: a skeleton board;
  - error: live unavailable, or no trace to replay;
  - full: consensus, fallback, or human review. Human review says "No agent was called" and shows no invented figures.
- **Honesty:** the masthead names the historical anchor row and says every operating value is simulated. Model
  text is labelled as data and HTML-escaped. Human approval is logged apart from the agents' votes, and nothing is executed.
- **Sweep:** `sloplint --scope app --url` reports 0 FAIL and 0 REFLEX in both the static and render tiers. Its one
  WARN came from the sandbox, which blocked Google Fonts, so those screenshots used a fallback face. The
  fresh-context finish review was not run.

## Files
`app.py` (Streamlit wiring), `ui/render.py` (pure, escaped HTML views, tested in `tests/test_ui_render.py`) ·
`ui/tokens.css` and `ui/base.css` (Hallmark scaffold), `ui/theme.css` (stamp, then page CSS) ·
`.streamlit/config.toml` (Wayfind values for Streamlit's native theme).
