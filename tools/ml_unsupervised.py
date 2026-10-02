"""Unsupervised inspector: clustering on processed physiological signals.

Finds natural patient subgroups and flags outlier segments for human review.
No labels needed — works on the processed parquets in Azure.

Usage:
  python -m tools.ml_unsupervised --project DEXREM --out ml_out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.decomposition import PCA
from sklearn.preprocessing import StandardScaler


def extract_features(df: pd.DataFrame) -> pd.DataFrame:
    """Extract per-signal summary features for clustering."""
    features = {}
    for col in df.columns:
        if col.startswith("_"):  # skip metadata
            continue
        s = pd.to_numeric(df[col], errors="coerce").dropna()
        if len(s) < 10:
            continue
        features[f"{col}_mean"] = s.mean()
        features[f"{col}_std"] = s.std()
        features[f"{col}_min"] = s.min()
        features[f"{col}_max"] = s.max()
        features[f"{col}_median"] = s.median()
    return pd.DataFrame([features])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--n-clusters", type=int, default=5)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # TODO: Load processed parquets from Azure for the project.
    # For now, this is the framework — wire to Azure in the workflow.
    print(f"[ml-unsupervised] project={args.project}", flush=True)
    print("[ml-unsupervised] framework ready — needs Azure parquet listing", flush=True)

    # Placeholder: the actual clustering runs in GitHub Actions with Azure access.
    manifest = {
        "project": args.project,
        "n_clusters": args.n_clusters,
        "status": "framework_built",
        "next": "wire to Azure processed parquets",
    }
    (out / "_manifest.json").write_text(json.dumps(manifest, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
