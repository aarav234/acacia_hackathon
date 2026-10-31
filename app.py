"""Airspace duty-manager console (Streamlit).

    streamlit run app.py

Runs Member 1's engine and Member 2's bounded negotiation on Member 1's fixtures.
Every result is a recommendation: human approval is recorded separately from the
agents' votes and nothing is executed. Modes are labelled and never switch on
their own; live without a key is an error, never a quiet mock.
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT / "src", ROOT):  # until Member 3's packaging lands
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import streamlit as st  # noqa: E402

from airspace.agents import LiveProviderUnavailable, build_agents  # noqa: E402
from airspace.agents.replay import export_trace  # noqa: E402
from airspace.data.fixtures import load_fixture_scenario  # noqa: E402
from airspace.negotiation import negotiate, record_human_decision  # noqa: E402
from ui import render as R  # noqa: E402

SCENARIOS = {
    "AA1057 into MIA": "success",
    "No shared gain": "no_positive_gain",
    "Nothing feasible": "no_feasible",
}
MODES = {"Mock": "mock", "Replay": "replay", "Live": "live"}

st.set_page_config(page_title="Airspace: disruption negotiation console", layout="wide",
                   initial_sidebar_state="collapsed")


def _css() -> str:  # read every rerun: cheap, and edits show without a restart
    ui = ROOT / "ui"
    return "\n".join((ui / f).read_text(encoding="utf-8") for f in ("tokens.css", "base.css", "theme.css"))


@st.cache_resource
def _scenario(name: str):
    return load_fixture_scenario(name)


st.html(f"<style>{_css()}</style>")
ss = st.session_state
for k, v in (("result", None), ("run_key", None), ("error", None), ("trace_path", None)):
    ss.setdefault(k, v)

scen_label = ss.get("scenario") or "AA1057 into MIA"
scenario = _scenario(SCENARIOS[scen_label])
st.html(R.masthead(scenario))

# ---------------------------------------------------------------- controls
c1, c2, c3, c4 = st.columns([6, 3, 2, 2], vertical_alignment="bottom", gap="medium")
with c1:
    st.segmented_control("Scenario", list(SCENARIOS), default="AA1057 into MIA", key="scenario")
with c2:
    st.segmented_control("Mode", list(MODES), default="Mock", key="mode")
with c3:
    timeout = st.number_input("Deadline (s)", min_value=2, max_value=60, value=15, step=1)
mode = MODES[ss.get("mode") or "Mock"]
key = (SCENARIOS[scen_label], scenario.state_hash, mode)

# a result belongs to one scenario state and mode: anything else is stale, never reused
if ss.result is not None and ss.run_key != key:
    ss.result, ss.error = None, None

if mode == "replay":
    up = st.file_uploader("Saved trace (JSON from a previous run)", type=["json"])
    if up is not None:
        tmp = Path(tempfile.gettempdir()) / f"airspace-trace-{up.file_id}.json"
        tmp.write_bytes(up.getvalue())
        ss.trace_path = str(tmp)
elif mode == "live":
    st.caption("Live mode calls the model provider set by AIRSPACE_LLM_PROVIDER with the key in your "
               "environment. Six calls at most. Without a key the run stops; it never falls back to mock.")

with c4:
    run = st.button("Run again" if ss.result else "Run negotiation",
                    type="secondary" if ss.result else "primary", use_container_width=True)

# ---------------------------------------------------------------- run
board_slot = st.empty()
if run:
    ss.result, ss.error = None, None
    board_slot.html(R.loading_state())
    try:
        if mode == "replay" and not ss.trace_path:
            raise FileNotFoundError("no trace uploaded")
        agents = build_agents(mode, replay_path=ss.trace_path if mode == "replay" else None)
        ss.result = asyncio.run(negotiate(scenario, agents, per_call_timeout_s=float(timeout)))
        ss.run_key = key
        st.rerun()  # redraw controls for the new state (Run again, approval primary)
    except LiveProviderUnavailable as exc:
        ss.error = ("Live model unavailable", str(exc),
                    "Set the key in your environment and restart, or choose Mock or Replay.")
    except FileNotFoundError:
        ss.error = ("No trace to replay", "Replay needs a trace saved from an earlier run.",
                    "Upload the JSON trace above, or run in Mock or Live first and download its trace.")
    except ValueError as exc:  # e.g. a trace from another scenario state
        ss.error = ("Run refused", str(exc), "Check the trace and scenario, then run again.")
    board_slot.empty()

# ---------------------------------------------------------------- states
if ss.error:
    st.html(R.error_state(*ss.error))
elif ss.result is None:
    st.html(R.empty_state())
else:
    view = R.build_view(ss.result, scenario)
    left, right = st.columns([3, 2], gap="large")
    with left:
        st.html(R.outcome(view))
    with right:
        st.html('<h2>Duty manager</h2><p class="muted">Approval is yours. The agents\' agreement is '
                'evidence, not authority, and nothing is sent anywhere.</p>')
        if view.human:
            st.html(R.human_line(view))
        else:
            who = st.text_input("Approver", value="duty-manager", max_chars=40)
            a, b = st.columns(2)
            can_approve = ss.result.selected_plan_id is not None
            if a.button("Approve plan", type="primary", disabled=not can_approve, use_container_width=True):
                ss.result = record_human_decision(ss.result, approver_id=who or "duty-manager", decision="approved")
                st.rerun()
            if b.button("Reject" if can_approve else "Escalate", use_container_width=True):
                ss.result = record_human_decision(ss.result, approver_id=who or "duty-manager",
                                                  decision="rejected" if can_approve else "escalated")
                st.rerun()
    for block in (R.board(view), R.comparison(view), R.statements(view)):
        if block.strip():  # st.html refuses an empty body; absent sections are simply not drawn
            st.html(block)

    with st.expander(f"Audit log, {len(ss.result.events)} events"):
        st.dataframe(
            [{"Seq": ev.sequence, "Time (UTC)": ev.timestamp_utc, "Round": ev.round, "Actor": ev.actor,
              "Event": ev.event_type} for ev in ss.result.events],
            hide_index=True, use_container_width=True)
    d1, d2, _ = st.columns([2, 2, 4])
    d1.download_button("Download trace for replay", json.dumps(export_trace(ss.result), indent=2),
                       file_name=f"trace-{ss.result.scenario_id}-{ss.result.provider_mode}.json",
                       mime="application/json", use_container_width=True)
    d2.download_button("Download full result", ss.result.model_dump_json(indent=2),
                       file_name=f"result-{ss.result.scenario_id}.json", mime="application/json",
                       use_container_width=True)
