"""Supervised inspector: train artifact classifier using rule-based pseudo-labels.

The rule-based QC engine flags artifacts. We use those flags as initial
training labels — the ML learns to replicate the rules, then K's real
labels refine it when available.

Usage:
  python -m tools.ml_supervised_train --signal HR --parquet-dir processed/ --out ml_models/
  python -m tools.ml_supervised_train --signal all --parquet-dir processed/ --out ml_models/
"""

from __future__ import annotations

import argparse
import io
import json
import pickle
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import cross_val_score


def read_parquet_any(path) -> pd.DataFrame:
    """Read a parquet file, decrypting .enc blobs with PIPELINE_DATA_KEY."""
    data = Path(path).read_bytes()
    if str(path).endswith(".enc"):
        from tools.crypto import decrypt_bytes
        data = decrypt_bytes(data)
    return pd.read_parquet(io.BytesIO(data))


def signal_columns(df: pd.DataFrame) -> list:
    """Candidate artifact-classification targets: numeric signal columns."""
    cols = []
    for col in df.columns:
        low = col.lower()
        if low in ("timestamp", "time") or low.endswith("__qc") or low.startswith("_"):
            continue
        cols.append(col)
    return cols


def train_one(signal: str, X: np.ndarray, y: np.ndarray, out: Path) -> bool:
    """Train one artifact classifier; True if a model was saved."""
    if len(np.unique(y)) < 2:
        print(f"[ml-sup-train] {signal}: only one class -- skipped", flush=True)
        return False
    model = RandomForestClassifier(n_estimators=100, max_depth=12,
                                   random_state=42, n_jobs=-1)
    scores = cross_val_score(model, X, y, cv=3)
    print(f"[ml-sup-train] {signal}: CV accuracy "
          f"{scores.mean():.3f} +- {scores.std():.3f} "
          f"({len(X)} windows, {np.mean(y):.1%} artifact)", flush=True)
    model.fit(X, y)
    with open(out / f"{signal}_model.pkl", "wb") as f:
        pickle.dump(model, f)
    (out / f"{signal}_report.json").write_text(json.dumps({
        "signal": signal,
        "n_windows": len(X),
        "artifact_frac": float(np.mean(y)),
        "cv_mean": float(scores.mean()),
        "cv_std": float(scores.std()),
        "label_source": "rule_based_pseudo",
    }, indent=1))
    return True


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
    ap.add_argument("--signal", default="all",
                    help="Signal name e.g. HR, or 'all' for every signal column")
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-windows-per-signal", type=int, default=20000,
                    help="Cap training windows per signal (memory guard)")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    pdir = Path(args.parquet_dir)
    files = sorted(pdir.rglob("*filtered.parquet*"))
    print(f"[ml-sup-train] {len(files)} filtered parquets, signal={args.signal}",
          flush=True)
    if not files:
        print("[ml-sup-train] no files found", flush=True)
        return 1

    want_all = not args.signal or args.signal.lower() == "all"
    # Accumulate windows per signal (capped) in a single pass over files.
    acc_X = defaultdict(list)
    acc_y = defaultdict(list)
    counts = defaultdict(int)
    for f in files:
        try:
            df = read_parquet_any(f)
        except Exception as e:  # noqa: BLE001
            print(f"[ml-sup-train] skip {f.name}: {e}", flush=True)
            continue
        if want_all:
            cols = signal_columns(df)
        else:
            cols = [c for c in df.columns
                    if c == args.signal or c.startswith(args.signal + ".")][:1]
        for col in cols:
            if counts[col] >= args.max_windows_per_signal:
                continue
            try:
                sig = pd.to_numeric(df[col], errors="coerce").values
            except Exception:  # noqa: BLE001
                continue
            if np.isnan(sig).all():
                continue
            qc = np.isnan(sig).astype(int)
            X, _ = extract_windows(sig)
            y = pseudo_label_from_qc(qc)
            n = min(len(X), len(y))
            if n == 0:
                continue
            # Cap per-file contribution so giant files don't dominate.
            if n > 2000:
                rng = np.random.default_rng(0)
                idx = rng.choice(n, 2000, replace=False)
                X, y = X[idx], y[idx]
                n = 2000
            room = args.max_windows_per_signal - counts[col]
            acc_X[col].append(X[:room])
            acc_y[col].append(y[:room])
            counts[col] += min(n, room)

    if not acc_X:
        print("[ml-sup-train] no training data", flush=True)
        return 1

    trained, skipped = [], []
    for col in sorted(acc_X):
        X = np.vstack(acc_X[col])
        y = np.concatenate(acc_y[col])
        if train_one(col, X, y, out):
            trained.append(col)
        else:
            skipped.append(col)
    print(f"[ml-sup-train] done: {len(trained)} models trained, "
          f"{len(skipped)} skipped", flush=True)
    (out / "_summary.json").write_text(json.dumps({
        "trained": trained, "skipped": skipped,
        "label_source": "rule_based_pseudo",
    }, indent=1))
    return 0 if trained else 1


if __name__ == "__main__":
    sys.exit(main())
