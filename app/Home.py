"""Researcher home. Project overview and data availability at a glance."""

import pandas as pd
import plotly.express as px
import streamlit as st

from common import (DEVICE_LABEL, coverage_note, load_catalog, load_summary,
                    project_picker, projects)

st.set_page_config(page_title="Research Data Platform", page_icon="🩺", layout="wide")
st.title("Research Data Platform")

available = projects()
if not available:
    st.warning("No processed project yet. Run:\n\n"
               "`python -m tools.run_local --project IPAMS --root <folder>`")
    st.stop()

# ------------------------------------------------------------------ all projects
st.subheader("All projects")
rows = []
for code in available:
    s = load_summary(code)
    c = load_catalog(code)
    if s.empty:
        continue
    rows.append({
        "Project": code,
        "Patients": len(s),
        "Ready for analysis": int(s.get("complete_for_primary", pd.Series(dtype=bool)).sum()),
        "Recorded hours": round(s["duration_min"].sum() / 60, 1),
        "Size (GB)": round(s["bytes_mb"].sum() / 1000, 2),
        "Needs review": int(s["review"].sum()),
    })
overview = pd.DataFrame(rows)
st.dataframe(overview, width="stretch", hide_index=True)

st.divider()

# ------------------------------------------------------------------ one project
project = project_picker()
summary = load_summary(project)
catalog = load_catalog(project)

st.subheader(project)
st.caption(coverage_note(summary))

c1, c2, c3, c4 = st.columns(4)
c1.metric("Patients", len(summary))
c2.metric("Ready for analysis", int(summary.get("complete_for_primary",
                                                pd.Series(dtype=bool)).sum()))
c3.metric("Recorded hours", f"{summary['duration_min'].sum()/60:,.0f}")
c4.metric("Recordings needing review", int(summary["review"].sum()))

# Data availability, the honest answer to "some patients do not have everything"
st.subheader("Data availability")
st.caption("Which recordings exist for each patient. A missing device is a fact "
           "about the patient, not an error, and never removes them from the project.")

has_cols = [c for c in summary.columns if c.startswith("has_")]
if has_cols:
    matrix = summary.set_index("patient")[has_cols].astype(int)
    matrix.columns = [DEVICE_LABEL.get(c[4:], c[4:]) for c in has_cols]
    fig = px.imshow(matrix.T, aspect="auto", color_continuous_scale=["#e8e8e8", "#2a6f97"],
                    labels=dict(x="Patient", y="", color="Present"))
    fig.update_coloraxes(showscale=False)
    fig.update_layout(height=90 + 34 * len(matrix.columns), margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, width="stretch")

    counts = pd.DataFrame({
        "Recording type": matrix.columns,
        "Patients with it": matrix.sum().values,
        "Patients without": len(matrix) - matrix.sum().values,
    })
    st.dataframe(counts, width="stretch", hide_index=True)

with st.expander("Technical detail, for the data scientist"):
    st.write("File status counts")
    st.dataframe(catalog["parse_status"].value_counts().rename_axis("status")
                 .reset_index(name="files"), hide_index=True)
    failed = catalog[catalog["parse_status"] == "failed"]
    if not failed.empty:
        st.write("Failures")
        st.dataframe(failed[["file", "error"]], hide_index=True)
    notsup = catalog[catalog["parse_status"] == "not_supported"]
    if not notsup.empty:
        st.write(f"{len(notsup)} files kept but not parsed (vendor or binary formats). "
                 "These are stored and findable; a parser can be added later.")
