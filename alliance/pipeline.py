#!/usr/bin/env python3
"""
pipeline.py — Integrated ML processing pipeline.

Runs all stages end-to-end:
  1. Load data (DataFrame with canonical columns)
  2. Deterministic QC (qc_engine.py)
  3. Cross-variable consistency (consistency.py)
  4. XGBoost artifact detection (pretrained model)
  5. Output: QC report with value_valid + clinical_flag per observation

Usage:
  python pipeline.py --input data.csv --output qc_report.csv
  python pipeline.py --test  (runs on synthetic data)
"""

import argparse
import sys
from pathlib import Path
import pandas as pd
import numpy as np
import xgboost as xgb

sys.path.insert(0, str(Path(__file__).parent))
from qc_engine import QCEngine
from consistency import run_all_consistency
from train_qc_12var import extract_12var_features, generate_clean_12var
from synthetic_artifacts import corrupt


class PhysioPipeline:
    def __init__(self, ontology_path=None, xgb_model_path=None):
        self.qc_engine = QCEngine()
        self.xgb_model = None
        if xgb_model_path and Path(xgb_model_path).exists():
            self.xgb_model = xgb.XGBClassifier()
            self.xgb_model.load_model(xgb_model_path)
            print(f"Loaded XGBoost model from {xgb_model_path}")

    def run_deterministic_qc(self, df: pd.DataFrame,
                             canonical_map: dict) -> dict:
        """Stage 1: per-variable deterministic QC."""
        print("  Stage 1: Deterministic QC...")
        return self.qc_engine.process(df, canonical_map)

    def run_consistency(self, df: pd.DataFrame) -> pd.DataFrame:
        """Stage 2: cross-variable consistency."""
        print("  Stage 2: Consistency checks...")
        return run_all_consistency(df)

    def run_ml_qc(self, df: pd.DataFrame, window_sec: int = 30) -> pd.DataFrame:
        """Stage 3: XGBoost artifact detection on windows."""
        if self.xgb_model is None:
            print("  Stage 3: SKIPPED (no XGBoost model loaded)")
            return pd.DataFrame()

        print("  Stage 3: ML artifact detection...")
        n_windows = len(df) // window_sec
        results = []
        feature_cols = None

        for w in range(n_windows):
            s, e = w * window_sec, (w + 1) * window_sec
            window = df.iloc[s:e]
            feats = extract_12var_features(window)
            if feature_cols is None:
                feature_cols = [c for c in feats.keys()
                                if c not in ("patient_id",)]
            X = pd.DataFrame([{c: feats.get(c, 0) for c in feature_cols}]).fillna(0)
            prob = self.xgb_model.predict_proba(X)[0, 1]
            pred = int(prob > 0.5)
            results.append({
                "window": w,
                "start_idx": s, "end_idx": e,
                "artifact_prob": prob,
                "artifact_pred": pred,
            })

        return pd.DataFrame(results)

    def process(self, df: pd.DataFrame, canonical_map: dict) -> dict:
        """Run full pipeline. Returns dict of all stage outputs."""
        print(f"\nProcessing {len(df)} rows × {len(df.columns)} columns...")

        qc_results = self.run_deterministic_qc(df, canonical_map)
        consistency = self.run_consistency(df)
        ml_results = self.run_ml_qc(df)

        # Summary
        n_violations = consistency["any_violation"].sum() if len(consistency) else 0
        n_ml_artifacts = (ml_results["artifact_pred"] == 1).sum() if len(ml_results) else 0

        print(f"\n--- Pipeline Summary ---")
        print(f"  Rows processed: {len(df)}")
        print(f"  Consistency violations: {n_violations}")
        print(f"  ML-detected artifact windows: {n_ml_artifacts}")

        return {
            "qc": qc_results,
            "consistency": consistency,
            "ml": ml_results,
        }


def run_test():
    """End-to-end test on synthetic data with known artifacts."""
    print("=" * 60)
    print("PIPELINE INTEGRATION TEST (synthetic data)")
    print("=" * 60)

    # Generate clean 12-var data, inject known artifacts
    clean = generate_clean_12var(n_seconds=600, seed=42)
    corrupted = pd.DataFrame(index=clean.index)
    true_labels = {}
    for col in clean.columns:
        c, l = corrupt(clean[col], seed=42 + hash(col) % 100)
        corrupted[col] = c
        true_labels[col] = (l != "VALID").sum()
    print(f"\nInjected artifacts per variable:")
    for col, n in true_labels.items():
        if n > 0:
            print(f"  {col}: {n} corrupted samples")

    canonical_map = {c: c for c in corrupted.columns}

    pipeline = PhysioPipeline(
        xgb_model_path="/home/hatch/workspace/build/qc_xgb_12var.json"
    )
    results = pipeline.process(corrupted, canonical_map)

    # Verify: did ML catch the artifacts?
    ml = results["ml"]
    if len(ml) > 0:
        detected = (ml["artifact_pred"] == 1).sum()
        total = len(ml)
        print(f"\n  ML detected artifacts in {detected}/{total} windows")
        # We injected artifacts into every variable, so most windows should flag
        if detected / total > 0.5:
            print("  ✓ ML correctly identifies corrupted windows")
        else:
            print("  ✗ ML missing too many artifacts")

    # Verify: did consistency catch violations?
    cons = results["consistency"]
    if cons["any_violation"].any():
        print("  ✓ Consistency checks flagged violations")

    print("\n" + "=" * 60)
    print("INTEGRATION TEST COMPLETE")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Physio ML Pipeline")
    parser.add_argument("--input", help="Input CSV file")
    parser.add_argument("--output", help="Output QC report CSV")
    parser.add_argument("--test", action="store_true",
                        help="Run integration test on synthetic data")
    parser.add_argument("--model",
                        default="/home/hatch/workspace/build/qc_xgb_12var.json",
                        help="XGBoost model path")
    args = parser.parse_args()

    if args.test:
        run_test()
        return

    if not args.input:
        parser.error("--input required (or use --test)")

    df = pd.read_csv(args.input)
    canonical_map = {c: c for c in df.columns}  # assume already canonical

    pipeline = PhysioPipeline(xgb_model_path=args.model)
    results = pipeline.process(df, canonical_map)

    if args.output:
        # Save ML results
        if len(results["ml"]):
            results["ml"].to_csv(args.output, index=False)
            print(f"\nQC report saved to {args.output}")


if __name__ == "__main__":
    main()
