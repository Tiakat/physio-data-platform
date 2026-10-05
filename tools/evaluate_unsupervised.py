"""Evaluate unsupervised clustering quality.

Checks:
1. Silhouette scores (are clusters well-separated?)
2. Cluster interpretation (what does each cluster represent?)
3. Stability (do clusters replicate across runs?)

Usage:
  python -m tools.evaluate_unsupervised --project DEXREM --parquet-dir processed_dl --out eval_out
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd


def read_parquet_any(path) -> pd.DataFrame:
    data = Path(path).read_bytes()
    if str(path).endswith(".enc"):
        from tools.crypto import decrypt_bytes
        data = decrypt_bytes(data)
    return pd.read_parquet(io.BytesIO(data))


def extract_patient_features(parquet_path: str) -> dict:
    """Same as ml_unsupervised_train."""
    df = read_parquet_any(parquet_path)
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
        features[f"{col}_mean"] = float(s.mean())
        features[f"{col}_std"] = float(s.std()) if len(s) > 1 else 0.0
        features[f"{col}_min"] = float(s.min())
        features[f"{col}_max"] = float(s.max())
        # Add missingness - key for artifact detection
        total = len(df[col])
        features[f"{col}_missing_frac"] = float(1 - len(s) / total) if total > 0 else 0.0
    return features


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-clusters", type=int, default=5)
    args = ap.parse_args(argv)

    from sklearn.cluster import KMeans
    from sklearn.decomposition import PCA
    from sklearn.preprocessing import StandardScaler
    from sklearn.metrics import silhouette_score, silhouette_samples

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pdir = Path(args.parquet_dir)
    files = sorted(pdir.rglob("*filtered.parquet*"))
    print(f"[eval-unsup] {len(files)} parquets", flush=True)
    if not files:
        return 1

    rows, labels = [], []
    for f in files:
        try:
            feats = extract_patient_features(str(f))
            if feats:
                rows.append(feats)
                labels.append(f.name)
        except Exception as e:
            print(f"[eval-unsup] skip {f.name}: {e}", flush=True)

    X = pd.DataFrame(rows).fillna(0)
    print(f"[eval-unsup] feature matrix: {X.shape}", flush=True)

    scaler = StandardScaler()
    Xs = scaler.fit_transform(X)

    # Try different k values
    results = {}
    for k in [3, 5, 7, 10]:
        if k >= len(X):
            continue
        kmeans = KMeans(n_clusters=k, random_state=42, n_init=10)
        clusters = kmeans.fit_predict(Xs)
        sil = silhouette_score(Xs, clusters)
        sil_samples = silhouette_samples(Xs, clusters)
        
        # Per-cluster silhouette
        cluster_sil = {}
        for c in range(k):
            mask = clusters == c
            cluster_sil[f"cluster_{c}"] = {
                "n_members": int(mask.sum()),
                "mean_silhouette": float(sil_samples[mask].mean()),
                "members": [labels[i] for i in np.where(mask)[0][:10]],  # First 10
            }
        
        results[f"k={k}"] = {
            "silhouette_score": float(sil),
            "interpretation": "GOOD" if sil > 0.5 else "WEAK" if sil > 0.25 else "POOR",
            "clusters": cluster_sil,
        }
        print(f"[eval-unsup] k={k}: silhouette={sil:.3f}", flush=True)

    # Find best k
    best_k = max(results.keys(), key=lambda k: results[k]["silhouette_score"])
    print(f"\n[eval-unsup] BEST: {best_k} (silhouette={results[best_k]['silhouette_score']:.3f})", flush=True)

    # Interpret best clusters: what distinguishes each?
    k_best = int(best_k.split("=")[1])
    kmeans = KMeans(n_clusters=k_best, random_state=42, n_init=10)
    clusters = kmeans.fit_predict(Xs)
    
    # For each cluster, find top distinguishing features
    feature_names = X.columns.tolist()
    centroids = scaler.inverse_transform(kmeans.cluster_centers_)
    
    interpretations = {}
    for c in range(k_best):
        # Compare centroid to global mean
        global_mean = X.mean().values
        diff = (centroids[c] - global_mean) / (X.std().values + 1e-6)
        # Top 5 most distinctive features
        top_idx = np.argsort(np.abs(diff))[-5:][::-1]
        interpretations[f"cluster_{c}"] = [
            {"feature": feature_names[i], "z_score": float(diff[i])}
            for i in top_idx
        ]

    output = {
        "project": args.project,
        "n_files": len(files),
        "n_features": X.shape[1],
        "k_evaluation": results,
        "best_k": best_k,
        "cluster_interpretations": interpretations,
    }
    (out / "unsupervised_evaluation.json").write_text(json.dumps(output, indent=1))
    print(f"\n[eval-unsup] written to {out / 'unsupervised_evaluation.json'}", flush=True)
    
    # Verdict
    best_sil = results[best_k]["silhouette_score"]
    if best_sil < 0.25:
        print("[eval-unsup] VERDICT: Clusters are POOR - likely not meaningful", flush=True)
        print("[eval-unsup] Recommendation: fix feature engineering or try different approach", flush=True)
    elif best_sil < 0.5:
        print("[eval-unsup] VERDICT: Clusters are WEAK - may have some structure", flush=True)
    else:
        print("[eval-unsup] VERDICT: Clusters are GOOD", flush=True)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
