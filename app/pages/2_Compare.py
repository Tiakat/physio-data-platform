"""
Compare patients and projects.

This is the page the laboratory actually asked for: pick a group of patients,
get a comparison table and a figure, with the denominator always visible.
"""

import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (DEVICE_LABEL, describe_series, load_catalog, load_signals,
                    load_summary, pretty, projects, unit)

st.set_page_config(page_title="Compare", layout="wide")
st.title("Compare")

available = projects()
if not available:
    st.stop()

chosen = st.sidebar.multiselect("Projects", available, default=available[:1])
if not chosen:
    st.info("Choose at least one project.")
    st.stop()

# ------------------------------------------------------------------ build cohort
frames = []
for code in chosen:
    s = load_summary(code)
    if s.empty:
        continue
    s = s.copy()
    s["project"] = code
    frames.append(s)
if not frames:
    st.stop()
cohort = pd.concat(frames, ignore_index=True)

st.sidebar.subheader("Filters")
only_ready = st.sidebar.checkbox("Only patients with complete data", value=True)
min_minutes = st.sidebar.slider("Minimum recorded minutes", 0, 600, 20, step=10)
hide_review = st.sidebar.checkbox("Hide recordings awaiting review", value=False)

filtered = cohort.copy()
before = len(filtered)
if only_ready and "complete_for_primary" in filtered:
    filtered = filtered[filtered["complete_for_primary"]]
filtered = filtered[filtered["duration_min"] >= min_minutes]
if hide_review:
    filtered = filtered[filtered["review"] == 0]

st.caption(f"{len(filtered)} of {before} patients selected. "
           f"{before - len(filtered)} excluded by the filters above.")
if filtered.empty:
    st.warning("No patient matches. Relax a filter.")
    st.stop()

# ------------------------------------------------------------------ cohort table
st.subheader("Selected patients")
show = filtered[["project", "patient", "duration_min", "parsed", "devices",
                 "review", "bytes_mb"]].rename(columns={
    "project": "Project", "patient": "Patient", "duration_min": "Minutes",
    "parsed": "Recordings", "devices": "Available", "review": "To review",
    "bytes_mb": "MB"})
st.dataframe(show, width="stretch", hide_index=True)

# ------------------------------------------------------------------ measurement
st.subheader("Compare a measurement")

catalogs = {code: load_catalog(code) for code in chosen}
variables = set()
for code, cat in catalogs.items():
    if "variables" in cat:
        for v in cat["variables"].dropna():
            variables.update(v if isinstance(v, list) else [])
variables = sorted(variables)
if not variables:
    st.info("No parsed measurements available yet.")
    st.stop()

default_ix = variables.index("ART_MEAN") if "ART_MEAN" in variables else 0
variable = st.selectbox("Measurement", variables, index=default_ix, format_func=pretty)

records = []
keys = set(zip(filtered["project"], filtered["patient"]))
for code, cat in catalogs.items():
    if cat.empty or "parquet" not in cat:
        continue
    sub = cat[(cat["parse_status"] == "parsed") & cat["parquet"].notna()]
    for _, f in sub.iterrows():
        if (code, f["patient"]) not in keys:
            continue
        frame = load_signals(code, f["parquet"])
        if variable not in frame.columns:
            continue
        stats = describe_series(frame[variable])
        if not stats:
            continue
        records.append({"Project": code, "Patient": f["patient"],
                        "Source": DEVICE_LABEL.get(f["device"], f["device"]),
                        **stats})

if not records:
    st.warning(f"No selected patient has {pretty(variable)}.")
    st.stop()

table = pd.DataFrame(records)
st.caption(f"{table['Patient'].nunique()} of {len(filtered)} selected patients have "
           f"{pretty(variable)}. The rest are not shown and are not counted below.")

nice = table.rename(columns={"n": "N", "mean": "Mean", "sd": "SD", "median": "Median",
                             "p25": "P25", "p75": "P75", "min": "Min", "max": "Max",
                             "missing_pct": "Missing %"})
st.dataframe(nice, width="stretch", hide_index=True)

c1, c2 = st.columns(2)
with c1:
    fig = px.box(table, x="Project", y="median", points="all",
                 hover_data=["Patient"],
                 labels={"median": f"{pretty(variable)} {unit(variable)}"})
    fig.update_layout(height=420, margin=dict(l=0, r=0, t=30, b=0),
                      title=f"Median {pretty(variable)} per patient")
    st.plotly_chart(fig, width="stretch")
with c2:
    ordered = table.sort_values("median")
    fig = px.bar(ordered, x="median", y="Patient", orientation="h", color="Project",
                 labels={"median": f"{pretty(variable)} {unit(variable)}"})
    fig.update_layout(height=max(420, 22 * len(ordered)),
                      margin=dict(l=0, r=0, t=30, b=0),
                      title=f"{pretty(variable)} by patient")
    st.plotly_chart(fig, width="stretch")

# ------------------------------------------------------------------ group test
if table["Project"].nunique() >= 2:
    st.subheader("Group comparison")
    st.caption("Non parametric test on the per patient median, because the number "
               "of patients is small and distributions are usually skewed.")
    try:
        from scipy import stats as sstats
        groups = [g["median"].dropna().values for _, g in table.groupby("Project")]
        names = list(table.groupby("Project").groups.keys())
        if len(groups) == 2 and all(len(g) >= 3 for g in groups):
            u, p = sstats.mannwhitneyu(groups[0], groups[1], alternative="two-sided")
            st.write(f"**{names[0]}** median {np.median(groups[0]):.1f} "
                     f"(n={len(groups[0])}) versus **{names[1]}** median "
                     f"{np.median(groups[1]):.1f} (n={len(groups[1])}), "
                     f"Mann-Whitney p = {p:.3f}")
        elif len(groups) > 2:
            h, p = sstats.kruskal(*[g for g in groups if len(g) >= 3])
            st.write(f"Kruskal-Wallis across {len(groups)} projects, p = {p:.3f}")
    except Exception as exc:                                       # noqa: BLE001
        st.caption(f"Test not run: {exc}")

st.download_button("Download this table (CSV)",
                   nice.to_csv(index=False).encode("utf-8"),
                   file_name=f"compare_{variable}.csv", mime="text/csv")
