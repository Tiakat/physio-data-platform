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


def load_labels(path: str) -> dict:
    """Load real labels: (patient_key, signal) -> list of (start_s, end_s, label)."""
    import csv, re
    labels = {}
    with open(path, newline='', encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            proj = row['project'].strip()
            pat = row['patient'].strip().lower()
            # Normalize "patient 10" -> "patient 10"
            m = re.search(r'patient\s*(\d+)', pat)
            pat_key = f"patient {m.group(1)}" if m else pat
            sig = row['signal'].strip()
            key = (proj.upper(), pat_key, sig)
            seg = (float(row['start_s']), float(row['end_s']),
                   1 if row['label'].strip().lower() == 'artifact' else 0)
            labels.setdefault(key, []).append(seg)
    # Sort segments by start time for efficient lookup
    for k in labels:
        labels[k].sort()
    n_seg = sum(len(v) for v in labels.values())
    print(f"[ml-sup-train] loaded {n_seg} real label segments for {len(labels)} (project,patient,signal) keys", flush=True)
    return labels


def patient_from_path(path: Path) -> str | None:
    """Extract 'patient N' from a file path."""
    import re
    m = re.search(r'patient\s*(\d+)', str(path).lower())
    return f"patient {m.group(1)}" if m else None


def project_from_path(path: Path) -> str | None:
    """Extract project code from level2 path."""
    parts = Path(path).parts
    for i, p in enumerate(parts):
        if p == 'level2' and i + 1 < len(parts):
            return parts[i + 1].upper()
    return None


def label_for_window(segments: list, win_start_s: float, win_end_s: float) -> int | None:
    """Majority label for a window overlapping labeled segments. None if no overlap."""
    votes = []
    for s, e, lab in segments:
        # Overlap?
        if e >= win_start_s and s <= win_end_s:
            overlap = min(e, win_end_s) - max(s, win_start_s)
            if overlap > 0:
                votes.append((overlap, lab))
    if not votes:
        return None
    # Weighted by overlap duration
    art_w = sum(w for w, l in votes if l == 1)
    clean_w = sum(w for w, l in votes if l == 0)
    return 1 if art_w >= clean_w else 0


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
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_"
                    for c in signal)
    with open(out / f"{safe}_model.pkl", "wb") as f:
        pickle.dump(model, f)
    (out / f"{safe}_report.json").write_text(json.dumps({
        "signal": signal,
        "n_windows": len(X),
        "artifact_frac": float(np.mean(y)),
        "cv_mean": float(scores.mean()),
        "cv_std": float(scores.std()),
        "label_source": "rule_based_pseudo",
    }, indent=1))
    return True


def extract_labeled_windows(signal: np.ndarray, window: int = 100,
                            step: int = 50,
                            timestamps: np.ndarray | None = None,
                            real_segments: list | None = None
                            ) -> tuple[np.ndarray, np.ndarray]:
    """Sliding window features with aligned labels.

    If real_segments (from --labels-csv) and timestamps are provided,
    windows overlapping labeled segments use the real label (majority
    by overlap). Otherwise falls back to pseudo-labels: artifact (1)
    when >20% of samples are NaN (filtered out by rule-based QC),
    else clean (0). Windows with fewer than 10 valid samples are
    skipped entirely so X and y stay aligned.
    """
    feats = []
    labels = []
    n_real = 0
    for i in range(0, len(signal) - window, step):
        w = signal[i:i + window]
        nan_frac = float(np.mean(np.isnan(w))) if len(w) > 0 else 0.0
        mask = ~np.isnan(w)
        wv = w[mask]
        if len(wv) < 10:
            continue
        # Try real label first
        lab = None
        if real_segments and timestamps is not None:
            try:
                ws = float(timestamps[i])
                we = float(timestamps[i + window - 1])
                lab = label_for_window(real_segments, ws, we)
                if lab is not None:
                    n_real += 1
            except Exception:
                pass
        if lab is None:
            lab = 1 if nan_frac > 0.2 else 0
        diffs = np.abs(np.diff(wv)) if len(wv) > 1 else np.array([0])
        feats.append([
            np.mean(wv), np.std(wv), np.min(wv), np.max(wv),
            np.max(wv) - np.min(wv), np.median(wv),
            np.max(diffs), len(np.unique(np.round(wv, 2))),
        ])
        labels.append(lab)
    return np.array(feats), np.array(labels)




def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--signal", default="all",
                    help="Signal name e.g. HR, or 'all' for every signal column")
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-windows-per-signal", type=int, default=20000,
                    help="Cap training windows per signal (memory guard)")
    ap.add_argument("--labels-csv", default=None,
                    help="Path to real labels CSV (project,patient,signal,start_s,end_s,label). Overrides pseudo-labels where available.")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    real_labels = load_labels(args.labels_csv) if args.labels_csv else {}

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
        # Patient/project for real-label lookup
        pat = patient_from_path(f)
        proj = project_from_path(f)
        # Timestamps in seconds (for label alignment)
        ts = None
        for tc in df.columns:
            if tc.lower() in ("timestamp", "time", "t"):
                try:
                    tcol = pd.to_datetime(df[tc], errors="coerce")
                    if tcol.notna().any():
                        # Seconds from midnight (matches labels format)
                        ts = (tcol.dt.hour * 3600 + tcol.dt.minute * 60
                              + tcol.dt.second + tcol.dt.microsecond / 1e6).values
                    else:
                        ts = pd.to_numeric(df[tc], errors="coerce").values
                except Exception:
                    pass
                break
        for col in cols:
            if counts[col] >= args.max_windows_per_signal:
                continue
            try:
                sig = pd.to_numeric(df[col], errors="coerce").values
            except Exception:  # noqa: BLE001
                continue
            if np.isnan(sig).all():
                continue
            segs = real_labels.get((proj, pat, col)) if (proj and pat) else None
            X, y = extract_labeled_windows(sig, timestamps=ts, real_segments=segs)
            n = len(X)
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
        "label_source": "mixed_real_and_pseudo" if real_labels else "rule_based_pseudo",
        "labels_csv": args.labels_csv,
    }, indent=1))
    return 0 if trained else 1


if __name__ == "__main__":
    sys.exit(main())
