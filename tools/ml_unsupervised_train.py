"""Unsupervised inspector: train clustering on processed Azure parquets.

Loads processed parquets from Azure, extracts per-signal features,
runs KMeans clustering to find patient subgroups and outliers.

Usage:
  python -m tools.ml_unsupervised_train --project DEXREM --out ml_models/
"""

from __future__ import annotations

import argparse
import io
import json
import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler


def extract_patient_features(parquet_path: str) -> dict:
    """Extract summary features from one patient's processed parquet."""
    df = pd.read_parquet(parquet_path)
    features = {}
    for col in df.columns:
        if col.startswith("_"):
            continue
        try:
            s = pd.to_numeric(df[col], errors="coerce").dropna()
        except Exception:
            continue
        if len(s) < 10:
            continue
        # Use the filtered column if available, else raw
        features[f"{col}_mean"] = float(s.mean())
        features[f"{col}_std"] = float(s.std()) if len(s) > 1 else 0.0
        features[f"{col}_min"] = float(s.min())
        features[f"{col}_max"] = float(s.max())
    return features


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-clusters", type=int, default=5)
    ap.add_argument("--parquet-dir", help="Local dir with processed parquets")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # Load all processed parquets for the project.
    if not args.parquet_dir:
        print("[ml-unsup-train] need --parquet-dir", flush=True)
        return 1

    pdir = Path(args.parquet_dir)
    files = list(pdir.rglob("*.parquet"))
    print(f"[ml-unsup-train] {len(files)} parquets", flush=True)
    if not files:
        print("[ml-unsup-train] no files found", flush=True)
        return 1

    # Extract features per file.
    rows = []
    labels = []
    for f in files:
        try:
            feats = extract_patient_features(str(f))
            if feats:
                rows.append(feats)
                labels.append(f.name)
        except Exception as e:
            print(f"[ml-unsup-train] skip {f.name}: {e}", flush=True)

    if not rows:
        print("[ml-unsup-train] no features extracted", flush=True)
        return 1

    X = pd.DataFrame(rows).fillna(0)
    print(f"[ml-unsup-train] feature matrix: {X.shape}", flush=True)

    # Standardize and cluster.
    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)
    ncl = min(args.n_clusters, len(X))
    kmeans = KMeans(n_clusters=ncl, random_state=42, n_init=10)
    clusters = kmeans.fit_predict(Xs)

    # PCA for visualization.
    pca = PCA(n_components=2)
    Xp = pca.fit_transform(Xs)

    # Save.
    with open(out / "scaler.pkl", "wb") as f:
        pickle.dump(scaler, f)
    with open(out / "kmeans.pkl", "wb") as f:
        pickle.dump(kmeans, f)
    with open(out / "pca.pkl", "wb") as f:
        pickle.dump(pca, f)

    result = {
        "project": args.project,
        "n_files": len(files),
        "n_features": X.shape[1],
        "n_clusters": ncl,
        "assignments": {lbl: int(c) for lbl, c in zip(labels, clusters)},
        "pca_explained": pca.explained_variance_ratio_.tolist(),
    }
    (out / "clusters.json").write_text(json.dumps(result, indent=1))
    print(f"[ml-unsup-train] done: {ncl} clusters from {len(files)} files",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
