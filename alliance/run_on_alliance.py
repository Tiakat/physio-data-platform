#!/usr/bin/env python3
"""
run_on_alliance.py — Run ML pipeline on encrypted Alliance data.

Decrypts files IN MEMORY using the Alliance Fernet key.
Plaintext is NEVER written to disk.

Usage:
  python run_on_alliance.py --input-dir <encrypted_dir> --output-dir <dir> \
      --project DEXREM [--patient "Patient 5"] [--model models/qc_xgb_12var.json]
"""

import argparse
import os
import sys
from pathlib import Path
import pandas as pd
import numpy as np

# Add alliance dir to path
sys.path.insert(0, str(Path(__file__).parent))

from cryptography.fernet import Fernet


def load_key() -> bytes:
    """Load Alliance Fernet key from secure location."""
    key_file = os.environ.get("ALLIANCE_KEY_FILE",
                               os.path.expanduser("~/.alliance_key"))
    with open(key_file, "rb") as f:
        return f.read().strip()


def decrypt_to_memory(encrypted_path: Path, fernet: Fernet) -> bytes:
    """Decrypt file, return plaintext bytes. Never touches disk."""
    with open(encrypted_path, "rb") as f:
        ciphertext = f.read()
    return fernet.decrypt(ciphertext)


def parse_infinity_csv(plaintext: bytes) -> pd.DataFrame:
    """
    Parse decrypted Infinity CSV data.
    Returns DataFrame with canonical column names.
    """
    import io
    try:
        df = pd.read_csv(io.BytesIO(plaintext), low_memory=False)
    except Exception:
        # Try with different encoding/separator
        try:
            df = pd.read_csv(io.BytesIO(plaintext), sep=";",
                             encoding="latin-1", low_memory=False)
        except Exception as e:
            print(f"    Warning: could not parse ({e})")
            return pd.DataFrame()

    # Basic cleanup: strip whitespace from columns
    df.columns = df.columns.str.strip()

    # Convert numeric columns (coerce errors to NaN)
    for col in df.columns:
        # Skip obvious non-numeric (timestamps, strings)
        if df[col].dtype == object:
            # Try converting, keep original if mostly non-numeric
            converted = pd.to_numeric(df[col], errors="coerce")
            if converted.notna().sum() > len(df) * 0.5:
                df[col] = converted

    return df


def map_to_canonical(df: pd.DataFrame, ontology: dict) -> pd.DataFrame:
    """
    Map raw column names to canonical variables using ontology aliases.
    For now: exact match on canonical name (case-insensitive).
    """
    canonical_map = {}
    for col in df.columns:
        col_upper = col.upper().strip()
        # Direct match
        if col_upper in ontology:
            canonical_map[col] = col_upper
        # Try without common suffixes
        for suffix in [" (^^ISO+)", "^^ISO+)"]:
            base = col_upper.replace(suffix, "").strip()
            if base in ontology:
                canonical_map[col] = base
                break

    # Rename and keep only mapped columns
    mapped = df.rename(columns=canonical_map)
    keep = [c for c in mapped.columns if c in ontology]
    return mapped[keep]


def process_file(encrypted_path: Path, fernet: Fernet, ontology: dict,
                 pipeline) -> dict:
    """Decrypt, parse, map, and run pipeline on one file."""
    print(f"  Processing: {encrypted_path.name}")

    # Decrypt in memory
    plaintext = decrypt_to_memory(encrypted_path, fernet)

    # Parse
    df = parse_infinity_csv(plaintext)
    if df.empty or len(df) < 10:
        print(f"    Skipped: too few rows ({len(df)})")
        return {"status": "skipped", "reason": "too_few_rows"}

    # Map to canonical
    mapped = map_to_canonical(df, ontology)
    if mapped.empty or len(mapped.columns) < 2:
        print(f"    Skipped: too few mapped columns ({len(mapped.columns)})")
        return {"status": "skipped", "reason": "too_few_columns"}

    print(f"    Rows: {len(mapped)}, Mapped columns: {len(mapped.columns)}")

    # Run pipeline stages that work on DataFrames
    # (QC engine + consistency; ML windowing needs 1Hz resampling)
    canonical_map = {c: c for c in mapped.columns}
    qc_results = pipeline.run_deterministic_qc(mapped, canonical_map)
    consistency = pipeline.run_consistency(mapped)

    # Clear plaintext from memory
    del plaintext, df, mapped

    n_violations = consistency["any_violation"].sum() if len(consistency) else 0

    return {
        "status": "processed",
        "n_rows": len(consistency),
        "n_columns": len(qc_results),
        "n_violations": int(n_violations),
    }


def main():
    p = argparse.ArgumentParser(description="Run ML pipeline on Alliance")
    p.add_argument("--input-dir", required=True, help="Encrypted input directory")
    p.add_argument("--output-dir", required=True, help="Output directory")
    p.add_argument("--project", required=True, help="Project name")
    p.add_argument("--patient", default=None, help="Specific patient (optional)")
    p.add_argument("--model", default=None, help="XGBoost model path")
    p.add_argument("--max-files", type=int, default=0,
                   help="Max files to process (0=all, for testing)")
    args = p.parse_args()

    # Load key and ontology
    print("Loading Alliance key...")
    fernet = Fernet(load_key())
    print("  ✓ Key loaded")

    import yaml
    ontology_path = Path(__file__).parent / "ontology" / "variables.yaml"
    # Fallback: look in repo
    if not ontology_path.exists():
        ontology_path = Path(os.path.expanduser(
            "~/physio-data-platform/alliance/ontology/variables.yaml"))
    with open(ontology_path) as f:
        ontology = yaml.safe_load(f)["variables"]
    print(f"  ✓ Ontology loaded ({len(ontology)} variables)")

    # Initialize pipeline (import here to avoid circular imports)
    sys.path.insert(0, "/home/hatch/workspace/build")
    # Also try repo path
    sys.path.insert(0, os.path.expanduser("~/physio-data-platform/alliance"))
    try:
        from pipeline import PhysioPipeline
    except ImportError:
        # Fallback: minimal pipeline without ML
        print("  Warning: full pipeline not available, using QC-only mode")
        from qc_engine import QCEngine
        from consistency import run_all_consistency
        class PhysioPipeline:
            def __init__(self, **kw):
                self.qc = QCEngine()
            def run_deterministic_qc(self, df, cm):
                return self.qc.process(df, cm)
            def run_consistency(self, df):
                return run_all_consistency(df)

    pipeline = PhysioPipeline()

    # Find files
    input_dir = Path(args.input_dir)
    pattern = f"*{args.patient}*" if args.patient else "*"
    # Look for encrypted files (.enc extension or all files)
    all_files = sorted(input_dir.rglob(pattern))
    # Filter to files (not dirs), prefer .enc
    enc_files = [f for f in all_files if f.is_file()]
    if args.max_files > 0:
        enc_files = enc_files[:args.max_files]

    print(f"\nFound {len(enc_files)} files to process")
    if not enc_files:
        print("No files found. Check --input-dir and --patient.")
        return 1

    # Process each file
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, fpath in enumerate(enc_files):
        print(f"\n[{i+1}/{len(enc_files)}]", end=" ")
        try:
            result = process_file(fpath, fernet, ontology, pipeline)
            result["file"] = str(fpath.relative_to(input_dir))
            results.append(result)
        except Exception as e:
            print(f"    ERROR: {e}")
            results.append({"file": str(fpath.relative_to(input_dir)),
                            "status": "error", "reason": str(e)[:200]})

    # Save summary (encrypt the output too)
    summary_df = pd.DataFrame(results)
    summary_path = output_dir / f"pipeline_summary_{args.project}.csv"
    # Save plaintext summary (not patient data, just file-level stats)
    summary_df.to_csv(summary_path, index=False)

    n_ok = (summary_df["status"] == "processed").sum()
    n_skip = (summary_df["status"] == "skipped").sum()
    n_err = (summary_df["status"] == "error").sum()

    print(f"\n{'='*60}")
    print(f"COMPLETE: {n_ok} processed, {n_skip} skipped, {n_err} errors")
    print(f"Summary: {summary_path}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
