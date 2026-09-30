"""Store: write processed outputs as Parquet.

Layout (all paths under ``outputs.root``)::

    <project>/<config_fingerprint>/<patient_id>/signals.parquet   # cleaned signals
    <project>/<config_fingerprint>/<patient_id>/features.parquet  # per-window metrics
    <project>/<config_fingerprint>/<patient_id>/figures/          # per-patient PNGs
    <project>/<config_fingerprint>/cohort_stats.parquet           # cross-patient stats
    <project>/<config_fingerprint>/run_manifest.json              # provenance

Keying everything by ``config_fingerprint`` means re-running with a changed
config writes a new directory instead of silently mixing old and new results.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd


def patient_dir(out_root: str | Path, project: str, fingerprint: str,
                patient_id: str) -> Path:
    d = Path(out_root) / project / fingerprint / patient_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_signals(out_root: str | Path, project: str, fingerprint: str,
                  patient_id: str, df: pd.DataFrame) -> Path:
    """Cleaned signals: one row per sample. Columns: t_s, <channel...>.
    Long recordings should be written in chunks by the caller."""
    d = patient_dir(out_root, project, fingerprint, patient_id)
    p = d / "signals.parquet"
    df.to_parquet(p, index=False)
    return p


def write_features(out_root: str | Path, project: str, fingerprint: str,
                   patient_id: str, df: pd.DataFrame) -> Path:
    """Per-window/per-phase metrics: one row per window, columns are metrics."""
    d = patient_dir(out_root, project, fingerprint, patient_id)
    p = d / "features.parquet"
    df.to_parquet(p, index=False)
    return p


def write_cohort_stats(out_root: str | Path, project: str, fingerprint: str,
                       df: pd.DataFrame) -> Path:
    """Cross-patient statistics table for the project."""
    d = Path(out_root) / project / fingerprint
    d.mkdir(parents=True, exist_ok=True)
    p = d / "cohort_stats.parquet"
    df.to_parquet(p, index=False)
    return p


def read_parquet(path: str | Path) -> pd.DataFrame:
    return pd.read_parquet(path)
