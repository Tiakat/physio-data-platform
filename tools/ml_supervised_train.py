"""Supervised inspector: train artifact classifier using rule-based pseudo-labels.

The rule-based QC engine flags artifacts. We use those flags as initial
training labels — the ML learns to replicate the rules, then K's real
labels refine it when available.

Usage:
  python -m tools.ml_supervised_train --signal HR --parquet-dir processed/ --out ml_models/
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


def extract_windows(signal: np.ndarray, window: int = 100,
                    step: int = 50) -> tuple[np.ndarray, list]:
    """Extract sliding window features."""
    feats = []
    idxs = []
    for i in range(0, len(signal) - window, step):
        w = signal[i:i + window]
        mask = ~np.isnan(w)
        w = w[mask]
        if len(w) < 10:
            continue
        diffs = np.abs(np.diff(w)) if len(w) > 1 else np.array([0])
        feats.append([
            np.mean(w), np.std(w), np.min(w), np.max(w),
            np.max(w) - np.min(w), np.median(w),
            np.max(diffs), len(np.unique(np.round(w, 2))),
        ])
        idxs.append(i)
    return np.array(feats), idxs


def pseudo_label_from_qc(qc_flags: np.ndarray, window: int = 100,
                        step: int = 50) -> np.ndarray:
    """Convert per-sample QC flags to per-window pseudo-labels.

    A window is labeled 'artifact' if >20% of its samples were QC-flagged.
    """
    labels = []
    for i in range(0, len(qc_flags) - window, step):
        w = qc_flags[i:i + window]
        # QC flag: 1 = artifact/invalid, 0 = clean
        frac = np.mean(w) if len(w) > 0 else 0
        labels.append(1 if frac > 0.2 else 0)
    return np.array(labels)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", required=True, help="Signal name e.g. HR")
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pdir = Path(args.parquet_dir)
    files = list(pdir.rglob("*.parquet"))
    print(f"[ml-sup-train] {len(files)} parquets, signal={args.signal}",
          flush=True)

    all_X, all_y = [], []
    for f in files:
        try:
            df = pd.read_parquet(f)
            # Find the signal column (handle duplicates like HR, HR.1)
            cols = [c for c in df.columns
                    if c == args.signal or c.startswith(args.signal + ".")]
            if not cols:
                continue
            col = cols[0]  # use first match
            sig = pd.to_numeric(df[col], errors="coerce").values

            # QC flags: look for a QC column, else derive from NaN in filtered
            # For now, use NaN as pseudo-artifact (filtered out by rules)
            qc = np.isnan(sig).astype(int)

            X, _ = extract_windows(sig)
            y = pseudo_label_from_qc(qc)
            n = min(len(X), len(y))
            if n > 0:
                all_X.append(X[:n])
                all_y.append(y[:n])
        except Exception as e:
            print(f"[ml-sup-train] skip {f.name}: {e}", flush=True)

    if not all_X:
        print("[ml-sup-train] no training data", flush=True)
        return 1

    X = np.vstack(all_X)
    y = np.concatenate(all_y)
    print(f"[ml-sup-train] {len(X)} windows, "
          f"{np.mean(y):.1%} pseudo-artifact", flush=True)

    if len(np.unique(y)) < 2:
        print("[ml-sup-train] only one class — need more varied data",
              flush=True)
        return 1

    model = RandomForestClassifier(n_estimators=100, max_depth=12,
                                   random_state=42, n_jobs=-1)
    scores = cross_val_score(model, X, y, cv=3)
    print(f"[ml-sup-train] CV accuracy: {scores.mean():.3f} ± {scores.std():.3f}",
          flush=True)
    model.fit(X, y)

    with open(out / f"{args.signal}_model.pkl", "wb") as f:
        pickle.dump(model, f)

    (out / f"{args.signal}_report.json").write_text(json.dumps({
        "signal": args.signal,
        "n_windows": len(X),
        "artifact_frac": float(np.mean(y)),
        "cv_mean": float(scores.mean()),
        "cv_std": float(scores.std()),
        "label_source": "rule_based_pseudo",
    }, indent=1))
    print(f"[ml-sup-train] done for {args.signal}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
