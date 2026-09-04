"""
Shared helpers for the researcher interface.

Design rule for every page: a researcher sees projects, patients, numbers and
figures. They never see a column name, a file path, a status code or a
traceback. Anything technical lives behind an expander marked for the data
scientist.
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import streamlit as st

STORE = Path("local_store")

PRETTY = {
    "ART_SYS": "Radial systolic", "ART_DIA": "Radial diastolic",
    "ART_MEAN": "Radial mean", "PA_SYS": "Brachial systolic",
    "PA_DIA": "Brachial diastolic", "PA_MEAN": "Brachial mean",
    "NBP_SYS": "Cuff systolic", "NBP_MEAN": "Cuff mean",
    "HR": "Heart rate", "SPO2": "Oxygen saturation", "ETCO2": "End tidal CO2",
    "TEMP": "Temperature", "CVP": "Central venous pressure",
    "BIS": "Depth of anaesthesia (BIS)", "NOL": "Nociception index (NOL)",
    "PEEP": "PEEP", "TV": "Tidal volume", "MAC": "MAC",
    "ET_SEVO": "End tidal sevoflurane", "ET_DES": "End tidal desflurane",
}
UNITS = {
    "ART_SYS": "mmHg", "ART_DIA": "mmHg", "ART_MEAN": "mmHg",
    "PA_SYS": "mmHg", "PA_DIA": "mmHg", "PA_MEAN": "mmHg",
    "NBP_SYS": "mmHg", "NBP_MEAN": "mmHg", "HR": "bpm", "SPO2": "%",
    "ETCO2": "mmHg", "TEMP": "C", "CVP": "mmHg", "BIS": "", "NOL": "",
}
DEVICE_LABEL = {
    "infinity": "Monitor (Infinity)", "bettercare": "Waveforms (BetterCare)",
    "nol": "Nociception (NOL)", "bis": "Depth of anaesthesia (BIS)",
    "pump": "Infusion pumps", "other": "Other",
}


def pretty(name: str) -> str:
    return PRETTY.get(name, name.replace("_", " ").title())


def unit(name: str) -> str:
    return UNITS.get(name, "")


def projects() -> list[str]:
    if not STORE.exists():
        return []
    return sorted(p.name for p in STORE.iterdir()
                  if p.is_dir() and (p / "catalog.json").exists())


@st.cache_data(show_spinner=False)
def load_catalog(project: str) -> pd.DataFrame:
    path = STORE / project / "catalog.json"
    if not path.exists():
        return pd.DataFrame()
    return pd.DataFrame(json.loads(path.read_text(encoding="utf-8")))


@st.cache_data(show_spinner=False)
def load_summary(project: str) -> pd.DataFrame:
    path = STORE / project / "summary.csv"
    if not path.exists():
        return pd.DataFrame()
    return pd.read_csv(path)


@st.cache_data(show_spinner=False)
def load_signals(project: str, parquet_rel: str) -> pd.DataFrame:
    path = STORE / project / parquet_rel
    if not path.exists():
        return pd.DataFrame()
    return pd.read_parquet(path)


def project_picker(key="project"):
    available = projects()
    if not available:
        st.warning("No processed project found yet. Run the pipeline first:\n\n"
                   "`python -m tools.run_local --project IPAMS --root <folder>`")
        st.stop()
    return st.sidebar.selectbox("Project", available, key=key)


def coverage_note(summary: pd.DataFrame) -> str:
    """
    The sentence that must appear next to every statistic. Never present a
    number without saying how many patients it came from and who was left out.
    """
    if summary.empty or "complete_for_primary" not in summary:
        return ""
    total = len(summary)
    ready = int(summary["complete_for_primary"].sum())
    if ready == total:
        return f"All {total} patients have the data this analysis requires."
    return (f"{ready} of {total} patients have the data this analysis requires. "
            f"{total - ready} are excluded from the figures below and listed under "
            f"Data availability.")


def describe_series(series: pd.Series) -> dict:
    clean = series.dropna()
    if clean.empty:
        return {}
    return {
        "n": int(clean.size),
        "mean": round(float(clean.mean()), 1),
        "sd": round(float(clean.std()), 1),
        "median": round(float(clean.median()), 1),
        "p25": round(float(clean.quantile(0.25)), 1),
        "p75": round(float(clean.quantile(0.75)), 1),
        "min": round(float(clean.min()), 1),
        "max": round(float(clean.max()), 1),
        "missing_pct": round(float(series.isna().mean()) * 100, 1),
    }
