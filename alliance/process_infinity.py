#!/usr/bin/env python3
"""
process_infinity.py — Unified Infinity monitor processor for all projects.

Based on PROMISES blood-pressure methodology + Dextrem pipeline patterns:
  1. Decrypt Infinity .csv.enc files in memory (never plaintext at rest)
  2. Parse semicolon-delimited Infinity CSV
  3. Artifact filtering with extended window (PROMISES sai-csv-filtered logic)
  4. ART vs NBP gradient analysis (where both present, threshold 10 mmHg)
  5. Per-patient statistics + QC metrics
  6. Output: cleaned parquet (encrypted) + stats JSON

Usage:
  python process_infinity.py --project DEXREM --patient "Patient 5" [--force]
  python process_infinity.py --project DEXREM --all [--force]
"""

import argparse
import io
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Paths & key
# ---------------------------------------------------------------------------
BASE = Path("/project/def-molo/katia5/dropbox_enc")
OUT_BASE = Path("/project/def-molo/katia5/processed")
KEY_PATH = Path.home() / ".alliance_key"

def get_fernet():
    from cryptography.fernet import Fernet
    key = KEY_PATH.read_text().strip()
    return Fernet(key.encode())

# ---------------------------------------------------------------------------
# Column mapping (Infinity monitor standard, identical across projects)
# ---------------------------------------------------------------------------
# Full column name -> short canonical name
COL_MAP = {
    "HR (/min^^ISO+)": "HR",
    "PLS (/min^^ISO+)": "PLS",
    "SpO2 (%^^ISO+)": "SpO2",
    "ART S (mm(hg)^^ISO+)": "ART_S",
    "ART D (mm(hg)^^ISO+)": "ART_D",
    "ART M (mm(hg)^^ISO+)": "ART_M",
    "NBP S (mm(hg)^^ISO+)": "NBP_S",
    "NBP D (mm(hg)^^ISO+)": "NBP_D",
    "NBP M (mm(hg)^^ISO+)": "NBP_M",
    "RR (/min^^ISO+)": "RR",
    "RRc (/min^^ISO+)": "RRc",
    "etCO2 (mm(hg)^^ISO+)": "etCO2",
    "inCO2 (mm(hg)^^ISO+)": "inCO2",
    "FiO2 (%^^ISO+)": "FiO2",
    "etO2 (%^^ISO+)": "etO2",
}

GRADIENT_THRESHOLD = 10  # mmHg (PROMISES standard)
EXTENSION_BEFORE = 10    # samples (PROMISES sai-csv-filtered)
EXTENSION_AFTER = 10

# Physiological plausibility ranges (from literature norms)
RANGES = {
    "HR": (20, 250),
    "SpO2": (50, 100),
    "ART_S": (30, 300),
    "ART_D": (15, 200),
    "ART_M": (20, 250),
    "NBP_S": (30, 300),
    "NBP_D": (15, 200),
    "NBP_M": (20, 250),
    "RR": (2, 80),
    "etCO2": (5, 100),
}


def decrypt_read(enc_path: Path, fernet) -> pd.DataFrame:
    """Decrypt .enc file in memory, parse Infinity CSV."""
    raw = fernet.decrypt(enc_path.read_bytes())
    text = raw.decode("utf-8", errors="ignore")
    # Infinity CSV: semicolon-delimited, first row is header
    df = pd.read_csv(io.StringIO(text), sep=";", low_memory=False)
    # Strip quotes from column names
    df.columns = [c.strip('"') for c in df.columns]
    return df


def canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """Rename to canonical short names, parse datetime, coerce numerics."""
    # Rename known columns
    rename = {k: v for k, v in COL_MAP.items() if k in df.columns}
    df = df.rename(columns=rename)

    # Parse datetime
    if "OBSERVATION_DATETIME" in df.columns:
        df["datetime"] = pd.to_datetime(
            df["OBSERVATION_DATETIME"], format="%Y%m%d%H%M%S", errors="coerce"
        )
        df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)

    # Coerce numeric signal columns
    for col in list(COL_MAP.values()):
        if col in df.columns:
            # Infinity uses *** for invalid, strip whitespace
            s = df[col].astype(str).str.strip()
            s = s.replace({"***": np.nan, "": np.nan, "   ": np.nan})
            df[col] = pd.to_numeric(s, errors="coerce")

    return df


def flag_artifacts(df: pd.DataFrame) -> pd.Series:
    """
    Flag artifact samples: out-of-range values per signal.
    Returns boolean Series (True = artifact).
    """
    artifact = pd.Series(False, index=df.index)
    for col, (lo, hi) in RANGES.items():
        if col in df.columns:
            bad = (df[col] < lo) | (df[col] > hi)
            artifact |= bad.fillna(False)
    return artifact


def expand_artifact_regions(df: pd.DataFrame, artifact: pd.Series,
                            before: int = EXTENSION_BEFORE,
                            after: int = EXTENSION_AFTER) -> pd.Series:
    """
    PROMISES sai-csv-filtered logic: expand artifact regions by ±N samples,
    removing entire windows around each artifact.
    Returns boolean mask of samples to KEEP.
    """
    art_idx = np.where(artifact.values)[0]
    if len(art_idx) == 0:
        return pd.Series(True, index=df.index)

    remove = set()
    n = len(df)
    for idx in art_idx:
        for off in range(-before, after + 1):
            j = idx + off
            if 0 <= j < n:
                remove.add(j)

    keep = pd.Series(True, index=df.index)
    keep.iloc[list(remove)] = False
    return keep


def compute_gradients(df: pd.DataFrame) -> pd.DataFrame:
    """
    ART vs NBP gradient (PROMISES methodology).
    Adds gradient columns where both invasive and non-invasive exist.
    """
    for comp in ["S", "D", "M"]:
        art_col = f"ART_{comp}"
        nbp_col = f"NBP_{comp}"
        if art_col in df.columns and nbp_col in df.columns:
            df[f"grad_{comp}"] = (df[art_col] - df[nbp_col]).abs()
            df[f"high_grad_{comp}"] = df[f"grad_{comp}"] >= GRADIENT_THRESHOLD
    return df


def patient_stats(df_clean: pd.DataFrame, df_raw: pd.DataFrame,
                  project: str, patient: str) -> dict:
    """Per-patient summary statistics."""
    stats = {
        "project": project,
        "patient": patient,
        "n_raw": len(df_raw),
        "n_clean": len(df_clean),
        "pct_kept": round(100 * len(df_clean) / max(len(df_raw), 1), 2),
        "signals": {},
    }
    for col in list(COL_MAP.values()):
        if col in df_clean.columns:
            s = df_clean[col].dropna()
            if len(s) > 0:
                stats["signals"][col] = {
                    "n": int(s.count()),
                    "mean": round(float(s.mean()), 2),
                    "std": round(float(s.std()), 2),
                    "min": round(float(s.min()), 2),
                    "max": round(float(s.max()), 2),
                    "median": round(float(s.median()), 2),
                }
    # Gradient summary
    for comp in ["S", "D", "M"]:
        gc = f"grad_{comp}"
        hc = f"high_grad_{comp}"
        if gc in df_clean.columns:
            g = df_clean[gc].dropna()
            if len(g) > 0:
                stats[f"gradient_{comp}"] = {
                    "mean": round(float(g.mean()), 2),
                    "pct_high": round(100 * df_clean[hc].mean(), 2),
                    "n_high": int(df_clean[hc].sum()),
                }
    stats["processed_at"] = datetime.now().isoformat()
    return stats


def process_patient(project: str, patient: str, fernet, force: bool = False) -> dict:
    """Process one patient's Infinity data."""
    inf_dir = BASE / project / "Database" / "ExtractedData" / "Infinity" / patient
    out_dir = OUT_BASE / project / "Infinity" / patient
    out_dir.mkdir(parents=True, exist_ok=True)

    stats_path = out_dir / "_stats.json"
    if stats_path.exists() and not force:
        return {"patient": patient, "status": "skipped (exists)"}

    enc_files = sorted(inf_dir.glob("*.enc"))
    if not enc_files:
        return {"patient": patient, "status": "no files"}

    # Decrypt + parse all files, concatenate
    dfs = []
    for ef in enc_files:
        try:
            df = decrypt_read(ef, fernet)
            df = canonicalize(df)
            if len(df) > 0:
                dfs.append(df)
        except Exception as e:
            print(f"  WARN {ef.name}: {e}", file=sys.stderr)

    if not dfs:
        return {"patient": patient, "status": "parse failed"}

    df_raw = pd.concat(dfs, ignore_index=True)
    if "datetime" in df_raw.columns:
        df_raw = df_raw.sort_values("datetime").reset_index(drop=True)

    # Artifact detection + expanded removal (PROMISES logic)
    artifact = flag_artifacts(df_raw)
    keep = expand_artifact_regions(df_raw, artifact)
    df_clean = df_raw[keep].reset_index(drop=True)

    # Gradients
    df_clean = compute_gradients(df_clean)

    # Save cleaned (encrypted parquet)
    import pyarrow as pa
    import pyarrow.parquet as pq
    buf = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df_clean, preserve_index=False), buf)
    enc_data = fernet.encrypt(buf.getvalue())
    (out_dir / "infinity_clean.parquet.enc").write_bytes(enc_data)

    # Stats
    stats = patient_stats(df_clean, df_raw, project, patient)
    stats_path.write_text(json.dumps(stats, indent=2))

    return {"patient": patient, "status": "done",
            "n_raw": stats["n_raw"], "n_clean": stats["n_clean"]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--patient", default=None)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    fernet = get_fernet()

    if args.all:
        inf_base = BASE / args.project / "Database" / "ExtractedData" / "Infinity"
        patients = sorted([p.name for p in inf_base.glob("Patient *")])
    elif args.patient:
        patients = [args.patient]
    else:
        ap.error("specify --patient or --all")

    print(f"Processing {args.project}: {len(patients)} patients")
    for pat in patients:
        try:
            r = process_patient(args.project, pat, fernet, force=args.force)
            print(f"  {r['patient']}: {r['status']}" +
                  (f" ({r.get('n_clean')}/{r.get('n_raw')})" if r.get('n_clean') else ""))
        except Exception as e:
            print(f"  {pat}: FAILED {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
