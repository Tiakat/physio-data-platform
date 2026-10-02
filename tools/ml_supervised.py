"""Supervised inspector: ML-based signal filtration.

Trained on K's labeled segments (clean vs artifact). The model learns
subtle artifact patterns that rule-based filters miss.

Architecture:
- Training: K provides labeled segments → train per-signal models in Azure
- Inference: Model proposes filtering + confidence scores
- Audit: Every decision logged with confidence; low-confidence → human review
- Fallback: Rule-based engine remains as baseline for comparison

Usage:
  # Train (needs labeled data):
  python -m tools.ml_supervised --train --labels labels.csv --signal HR --out models/
  # Inference:
  python -m tools.ml_supervised --predict --model models/hr_model.pkl --input data.parquet --out filtered/
"""

from __future__ import annotations

import argparse
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score


def extract_window_features(signal: np.ndarray, window: int = 100) -> pd.DataFrame:
    """Extract features from sliding windows for artifact detection."""
    features = []
    for i in range(0, len(signal) - window, window // 2):
        w = signal[i:i + window]
        w = w[~np.isnan(w)]
        if len(w) < 10:
            continue
        features.append({
            "mean": np.mean(w),
            "std": np.std(w),
            "min": np.min(w),
            "max": np.max(w),
            "range": np.max(w) - np.min(w),
            "median": np.median(w),
            # Rate of change (artifact often = sudden jumps)
            "max_diff": np.max(np.abs(np.diff(w))) if len(w) > 1 else 0,
            # Flatness (sensor disconnect = flatline)
            "n_unique": len(np.unique(np.round(w, 2))),
        })
    return pd.DataFrame(features)


def train(signal_data: pd.DataFrame, labels: pd.Series, signal_name: str):
    """Train a per-signal artifact classifier."""
    X = extract_window_features(signal_data.values)
    # Align labels to windows (simplified — real version needs careful alignment)
    y = labels.iloc[:len(X)].values if len(labels) >= len(X) else None
    if y is None:
        raise ValueError("Not enough labels for the signal length")

    model = RandomForestClassifier(
        n_estimators=100,
        max_depth=10,
        random_state=42,
        n_jobs=-1,
    )
    scores = cross_val_score(model, X, y, cv=5)
    print(f"[{signal_name}] CV accuracy: {scores.mean():.3f} ± {scores.std():.3f}",
          flush=True)
    model.fit(X, y)
    return model, scores


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--train", action="store_true")
    ap.add_argument("--predict", action="store_true")
    ap.add_argument("--labels", help="CSV with labeled segments")
    ap.add_argument("--signal", help="Signal name (e.g., HR)")
    ap.add_argument("--model", help="Path to trained model for prediction")
    ap.add_argument("--input", help="Input parquet for prediction")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    if args.train:
        if not args.labels or not args.signal:
            print("Need --labels and --signal for training", flush=True)
            return 1
        print(f"[ml-supervised] training {args.signal}...", flush=True)
        # TODO: Load labels and signal data, train model
        print("[ml-supervised] framework ready — needs K's labeled segments",
              flush=True)

    elif args.predict:
        print("[ml-supervised] prediction mode — needs trained model", flush=True)

    manifest = {
        "status": "framework_built",
        "next": "needs K's labeled segments for training",
    }
    (out / "_manifest.json").write_text(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
