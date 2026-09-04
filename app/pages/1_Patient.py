"""One patient. Summary statistics and figures, no columns, no code."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DEVICE_LABEL, describe_series, load_catalog, load_signals,
                    load_summary, pretty, project_picker, unit)

st.set_page_config(page_title="Patient", layout="wide")
st.title("Patient")

project = project_picker("proj_patient")
summary = load_summary(project)
catalog = load_catalog(project)
if summary.empty:
    st.stop()

patient = st.sidebar.selectbox("Patient", summary["patient"].tolist())
row = summary[summary["patient"] == patient].iloc[0]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Recorded time", f"{row['duration_min']:.0f} min")
c2.metric("Recordings", int(row["parsed"]))
c3.metric("Data size", f"{row['bytes_mb']:.0f} MB")
c4.metric("Needs review", int(row["review"]))

devices = [d for d in str(row.get("devices", "")).split(",") if d]
st.caption("Available: " + (", ".join(DEVICE_LABEL.get(d, d) for d in devices)
                            if devices else "no parsed recordings"))
if not row.get("complete_for_primary", True):
    st.info("This patient does not have every recording the main analysis needs. "
            "They remain in the project and are available for analyses that use "
            "what they do have.")

files = catalog[(catalog["patient"] == patient) &
                (catalog["parse_status"] == "parsed") &
                (catalog["parquet"].notna())] if "parquet" in catalog else pd.DataFrame()
if files.empty:
    st.warning("No parsed signal for this patient.")
    st.stop()

# ------------------------------------------------------------------ summary table
st.subheader("Summary of measurements")
rows = []
for _, f in files.iterrows():
    frame = load_signals(project, f["parquet"])
    for col in frame.columns:
        stats = describe_series(frame[col])
        if not stats:
            continue
        rows.append({"Measurement": pretty(col), "Unit": unit(col),
                     "Source": DEVICE_LABEL.get(f["device"], f["device"]),
                     **{k.replace("_", " ").title(): v for k, v in stats.items()}})
if rows:
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

# ------------------------------------------------------------------ figures
st.subheader("Recordings over time")
for _, f in files.iterrows():
    frame = load_signals(project, f["parquet"])
    if frame.empty:
        continue
    label = DEVICE_LABEL.get(f["device"], f["device"])
    with st.expander(f"{label}   {f['duration_min']:.0f} min", expanded=True):
        choices = list(frame.columns)
        default = [c for c in ("ART_MEAN", "PA_MEAN", "NOL", "HR") if c in choices] \
                  or choices[:2]
        picked = st.multiselect("Show", choices, default=default,
                                format_func=pretty, key=f"pick_{f['sha256'][:8]}")
        if picked:
            fig = go.Figure()
            for col in picked:
                fig.add_trace(go.Scatter(x=frame.index, y=frame[col], name=pretty(col),
                                         mode="lines", line=dict(width=1)))
            fig.update_layout(height=380, margin=dict(l=0, r=0, t=10, b=0),
                              yaxis_title="", xaxis_title="Time",
                              legend=dict(orientation="h", y=1.1))
            st.plotly_chart(fig, width="stretch")

        flags = f.get("qc_flags") or []
        if isinstance(flags, list) and flags:
            st.caption("Quality notes: " +
                       "; ".join(x.get("detail", "") for x in flags))
