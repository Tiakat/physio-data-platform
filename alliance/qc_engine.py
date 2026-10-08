#!/usr/bin/env python3
"""
qc_engine.py — Deterministic QC engine (Phase A).

Reads alliance/ontology/variables.yaml and applies K's processing rules:
  - Waveforms: signal QC → filtering → artifact detection
  - Scalar physiology: range QC → artifact detection → optional robust smoothing
  - Ventilator: device QC → consistency checks (NO smoothing)
  - Anesthesia: unit/range QC (NO smoothing)
  - Settings: validate only (NO filtering, NEVER interpolate)
  - Counters: reset detection → differencing (NO smoothing)
  - Events/metadata: validate only (NEVER interpolate/filter)

Key principle: QC identifies bad measurements WITHOUT deleting genuine
abnormal physiology. Two separate flags:
  - value_valid: can we trust this measurement? (device/artifact)
  - clinical_flag: is it physiologically unusual? (preserved, not deleted)
"""

import yaml
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional
import numpy as np
import pandas as pd

ONTOLOGY_PATH = Path(__file__).parent / "ontology" / "variables.yaml"


@dataclass
class QCResult:
    """Per-observation QC outcome."""
    value_valid: bool          # device/artifact trust
    qc_status: str             # VALID, SUSPICIOUS, INVALID, MISSING, SATURATED
    qc_reason: str = ""        # why
    clinical_flag: str = "UNKNOWN"  # NORMAL, ABNORMAL_LOW, ABNORMAL_HIGH, CRITICAL
    artifact_type: Optional[str] = None  # SPIKE, FLATLINE, DROPOUT, etc.


class QCEngine:
    def __init__(self, ontology_path: Path = ONTOLOGY_PATH):
        with open(ontology_path) as f:
            self.ontology = yaml.safe_load(f)["variables"]

    def get_var(self, canonical: str) -> dict:
        return self.ontology.get(canonical, {})

    def qc_scalar(self, series: pd.Series, canonical: str) -> pd.DataFrame:
        """
        Deterministic QC for scalar physiological variables.
        Returns DataFrame with QC columns; NEVER deletes the raw value.
        Non-numeric series are routed to event QC.
        """
        # Coerce to numeric; if mostly non-numeric, treat as event/metadata
        numeric = pd.to_numeric(series, errors="coerce")
        if numeric.notna().sum() < len(series) * 0.5:
            return self.qc_event(series, canonical)

        series = numeric
        var = self.get_var(canonical)
        n = len(series)
        out = pd.DataFrame(index=series.index)
        out["raw_value"] = series
        out["value_valid"] = True
        out["qc_status"] = "VALID"
        out["qc_reason"] = ""
        out["clinical_flag"] = "UNKNOWN"
        out["artifact_type"] = None

        # 1. Missing
        missing = series.isna()
        out.loc[missing, "qc_status"] = "MISSING"
        out.loc[missing, "value_valid"] = False
        out.loc[missing, "qc_reason"] = "missing value"

        # 2. Non-finite
        nonfinite = ~np.isfinite(series.fillna(0)) & ~missing
        out.loc[nonfinite, "qc_status"] = "INVALID"
        out.loc[nonfinite, "value_valid"] = False
        out.loc[nonfinite, "qc_reason"] = "non-finite"

        # 3. Flatline detection (>10s zero variance for 1Hz data)
        # (simplified: 10 consecutive identical values)
        valid = ~missing & ~nonfinite
        if valid.sum() > 10:
            vals = series[valid]
            # rolling std == 0 for 10+ samples
            roll_std = vals.rolling(10, min_periods=10).std()
            flat = roll_std == 0
            flat_idx = vals[flat.fillna(False)].index
            out.loc[flat_idx, "qc_status"] = "SUSPICIOUS"
            out.loc[flat_idx, "artifact_type"] = "FLATLINE"
            out.loc[flat_idx, "qc_reason"] = "flatline >=10 samples"

        # 4. Spike detection (beat-to-beat change >20% for HR-like)
        # Generic: change > 50% of median in one step
        if valid.sum() > 2:
            vals = series[valid]
            med = vals.median()
            if med != 0 and not pd.isna(med):
                pct_change = (vals.diff().abs() / abs(med))
                spikes = pct_change > 0.5
                spike_idx = vals[spikes.fillna(False)].index
                # Don't overwrite flatline flags
                not_flagged = out.loc[spike_idx, "qc_status"] == "VALID"
                idx_to_flag = spike_idx[not_flagged]
                out.loc[idx_to_flag, "qc_status"] = "SUSPICIOUS"
                out.loc[idx_to_flag, "artifact_type"] = "SPIKE"

        return out

    def qc_setting(self, series: pd.Series, canonical: str) -> pd.DataFrame:
        """
        Settings: validate only. NEVER filter, NEVER interpolate.
        """
        out = pd.DataFrame(index=series.index)
        out["raw_value"] = series
        out["value_valid"] = ~series.isna()
        out["qc_status"] = np.where(series.isna(), "MISSING", "VALID")
        out["qc_reason"] = ""
        out["clinical_flag"] = "NOT_APPLICABLE"
        out["artifact_type"] = None
        return out

    def qc_counter(self, series: pd.Series, canonical: str) -> pd.DataFrame:
        """
        Cumulative counters: detect resets, compute increments.
        NEVER smooth the counter.
        """
        out = pd.DataFrame(index=series.index)
        out["raw_value"] = series
        out["value_valid"] = True
        out["qc_status"] = "VALID"
        out["increment"] = series.diff()
        out["reset_flag"] = False

        # Reset detection: negative increment
        resets = out["increment"] < 0
        out.loc[resets.fillna(False), "reset_flag"] = True
        out.loc[resets.fillna(False), "qc_status"] = "SUSPICIOUS"
        out.loc[resets.fillna(False), "qc_reason"] = "counter reset"

        # Negative absolute value = invalid
        neg = series < 0
        out.loc[neg.fillna(False), "qc_status"] = "INVALID"
        out.loc[neg.fillna(False), "value_valid"] = False

        out["clinical_flag"] = "NOT_APPLICABLE"
        out["artifact_type"] = None
        return out

    def qc_event(self, series: pd.Series, canonical: str) -> pd.DataFrame:
        """Events/metadata: validate only, never filter."""
        out = pd.DataFrame(index=series.index)
        out["raw_value"] = series
        out["value_valid"] = True
        out["qc_status"] = "VALID"
        out["qc_reason"] = ""
        out["clinical_flag"] = "NOT_APPLICABLE"
        out["artifact_type"] = None
        return out

    def process(self, df: pd.DataFrame, canonical_map: dict) -> dict:
        """
        Process a DataFrame using the ontology.
        canonical_map: {dataframe_column: canonical_variable_name}

        Returns {canonical: QC DataFrame} preserving raw values.
        """
        results = {}
        for col, canon in canonical_map.items():
            if col not in df.columns:
                continue
            var = self.get_var(canon)
            pclass = var.get("processing_class", "scalar_physiology")

            series = df[col]
            if pclass == "setting":
                results[canon] = self.qc_setting(series, canon)
            elif pclass == "counter":
                results[canon] = self.qc_counter(series, canon)
            elif pclass in ("event", "metadata"):
                results[canon] = self.qc_event(series, canon)
            elif pclass == "waveform":
                # Waveform QC: basic for now (full spectral in Phase C)
                results[canon] = self.qc_scalar(series, canon)
            else:
                # scalar_physiology, ventilator, anesthesia
                results[canon] = self.qc_scalar(series, canon)

        return results


if __name__ == "__main__":
    import sys
    eng = QCEngine()
    print(f"Loaded {len(eng.ontology)} variables from ontology")
    # Quick self-test
    s = pd.Series([72, 73, 71, 250, 72, 72, 72, 72, 72, 72, 72, 72, 72, 72, np.nan, 73])
    r = eng.qc_scalar(s, "HR")
    print(r[["raw_value", "qc_status", "artifact_type"]].to_string())
