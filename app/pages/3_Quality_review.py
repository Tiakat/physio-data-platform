"""Recordings the automatic checks flagged. One click to approve or reject."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DEVICE_LABEL, STORE, load_catalog, load_signals, pretty,
                    project_picker)

st.set_page_config(page_title="Quality review", layout="wide")
st.title("Quality review")

project = project_picker("proj_qc")
catalog = load_catalog(project)
if catalog.empty:
    st.stop()

queue = catalog[(catalog.get("qc_verdict") == "REVIEW")] if "qc_verdict" in catalog \
    else pd.DataFrame()
st.caption(f"{len(queue)} recordings flagged by the automatic checks. "
           "Everything else passed and needs no action.")
if queue.empty:
    st.success("Nothing to review.")
    st.stop()

decisions_path = STORE / project / "qc_reviews.json"
decisions = json.loads(decisions_path.read_text()) if decisions_path.exists() else {}

labels = [f"{r['patient']}  ·  {DEVICE_LABEL.get(r['device'], r['device'])}"
          f"  ·  {r.get('duration_min', 0):.0f} min"
          + ("   [reviewed]" if r["sha256"] in decisions else "")
          for _, r in queue.iterrows()]
choice = st.selectbox("Flagged recordings", range(len(labels)),
                      format_func=lambda i: labels[i])
row = queue.iloc[choice]

st.subheader(f"{row['patient']} · {DEVICE_LABEL.get(row['device'], row['device'])}")
for f in (row.get("qc_flags") or []):
    icon = "🔴" if f["severity"] == "error" else "🟠"
    st.write(f"{icon} **{f['flag'].replace('_',' ').title()}** — {f['detail']}")

if row.get("parquet"):
    frame = load_signals(project, row["parquet"])
    if not frame.empty:
        picked = st.multiselect("Show", list(frame.columns),
                                default=list(frame.columns)[:3], format_func=pretty)
        if picked:
            fig = go.Figure()
            for col in picked:
                fig.add_trace(go.Scatter(x=frame.index, y=frame[col], name=pretty(col),
                                         mode="lines", line=dict(width=1)))
            fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                              legend=dict(orientation="h", y=1.1))
            st.plotly_chart(fig, width="stretch")

st.divider()
reviewer = st.text_input("Your name", value=st.session_state.get("reviewer", ""))
comment = st.text_area("Comment", placeholder="Why this decision")
c1, c2, c3 = st.columns(3)


def record(decision):
    if not reviewer:
        st.error("Enter your name first, so the decision is attributable.")
        return
    st.session_state["reviewer"] = reviewer
    decisions[row["sha256"]] = {
        "decision": decision, "reviewer": reviewer, "comment": comment,
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "patient": row["patient"], "device": row["device"], "file": row["file"],
    }
    decisions_path.write_text(json.dumps(decisions, indent=2), encoding="utf-8")
    st.success(f"Recorded: {decision}")


if c1.button("Approve", width="stretch"):
    record("APPROVE")
if c2.button("Reject", width="stretch"):
    record("REJECT")
if c3.button("Reprocess", width="stretch"):
    record("REPROCESS")

if row["sha256"] in decisions:
    d = decisions[row["sha256"]]
    st.info(f"Already reviewed: **{d['decision']}** by {d['reviewer']} on "
            f"{d['at'][:10]}. {d.get('comment','')}")
