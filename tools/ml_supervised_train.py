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


def train_one(signal: str, X: np.ndarray, y: np.ndarray, out: Path,
              label_source: str = "rule_based_pseudo") -> bool:
    """Train one artifact classifier with train/val/test splits.

    Split: 70% train / 15% validation / 15% test (stratified).
    5-fold CV on train, tune on validation, final eval on held-out test.
    """
    from sklearn.model_selection import train_test_split, StratifiedKFold
    from sklearn.metrics import accuracy_score, f1_score, matthews_corrcoef
    if len(np.unique(y)) < 2:
        print(f"[ml-sup-train] {signal}: only one class -- skipped", flush=True)
        return False
    # Stratified splits
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.15, random_state=42, stratify=y)
    X_tr2, X_va, y_tr2, y_va = train_test_split(
        X_tr, y_tr, test_size=0.176, random_state=42, stratify=y_tr)  # 15% of total
    # 5-fold CV on train
    model = RandomForestClassifier(n_estimators=100, max_depth=12,
                                   random_state=42, n_jobs=-1)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_tr2, y_tr2, cv=cv, scoring="f1")
    # Train on full train split, validate, then final fit
    model.fit(X_tr2, y_tr2)
    va_pred = model.predict(X_va)
    va_f1 = f1_score(y_va, va_pred, zero_division=0)
    # Final model on train+val, evaluate on held-out test
    model.fit(X_tr, y_tr)
    te_pred = model.predict(X_te)
    te_acc = accuracy_score(y_te, te_pred)
    te_f1 = f1_score(y_te, te_pred, zero_division=0)
    te_mcc = matthews_corrcoef(y_te, te_pred)
    print(f"[ml-sup-train] {signal}: CV F1 {cv_scores.mean():.3f}+-{cv_scores.std():.3f} | "
          f"val F1 {va_f1:.3f} | TEST acc {te_acc:.3f} F1 {te_f1:.3f} MCC {te_mcc:.3f} "
          f"({len(X)} windows, {np.mean(y):.1%} artifact)", flush=True)
    safe = "".join(c if c.isalnum() or c in ("-", "_", ".") else "_"
                    for c in signal)
    with open(out / f"{safe}_model.pkl", "wb") as f:
        pickle.dump(model, f)
    # Flag unreliable models
    reliability = "RELIABLE" if te_f1 >= 0.8 else "UNRELIABLE"
    if te_f1 < 0.8:
        print(f"[ml-sup-train] WARNING: {signal} test F1 {te_f1:.3f} < 0.8 - flagged UNRELIABLE", flush=True)
    (out / f"{safe}_report.json").write_text(json.dumps({
        "signal": signal,
        "n_windows": len(X),
        "n_train": len(X_tr2), "n_val": len(X_va), "n_test": len(X_te),
        "artifact_frac": float(np.mean(y)),
        "cv_f1_mean": float(cv_scores.mean()),
        "cv_f1_std": float(cv_scores.std()),
        "val_f1": float(va_f1),
        "test_accuracy": float(te_acc),
        "test_f1": float(te_f1),
        "test_mcc": float(te_mcc),
        "label_source": label_source,
        "reliability": reliability,
        "uses_morphology": is_art_signal(signal),
    }, indent=1))
    return True


def extract_labeled_windows(signal: np.ndarray, window: int = 100,
                            step: int = 50,
                            timestamps: np.ndarray | None = None,
                            real_segments: list | None = None,
                            signal_name: str = ""
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
        base_feats = [
            np.mean(wv), np.std(wv), np.min(wv), np.max(wv),
            np.max(wv) - np.min(wv), np.median(wv),
            np.max(diffs), len(np.unique(np.round(wv, 2))),
        ]
        # Add ART morphology features for arterial pressure signals
        if is_art_signal(signal_name):
            morph_feats = extract_art_morphology_features(wv)
            # Pad base feats to match non-ART length, then append morphology
            feats.append(base_feats + morph_feats)
        else:
            # Pad with zeros to keep feature dimension consistent
            feats.append(base_feats + [0.0] * 12)
        labels.append(lab)
    return np.array(feats), np.array(labels)

def extract_art_morphology_features(wv: np.ndarray, fs_hz: float = 1.0) -> list:
    """ART-specific morphology features.
    
    Captures arterial pressure waveform characteristics that distinguish
    real physiology from artifacts:
    - Dicrotic notch: small secondary peak after systolic peak
    - Pulse pressure: systolic - diastolic
    - Upstroke slope: max dP/dt during systole
    - Beat regularity: variation in peak-to-peak intervals
    """
    feats = []
    if len(wv) < 20:
        return [0.0] * 12
    
    # Basic pulse pressure
    sys_p = float(np.max(wv))
    dia_p = float(np.min(wv))
    pp = sys_p - dia_p
    feats.extend([sys_p, dia_p, pp])
    
    # Upstroke slope (max positive derivative)
    diffs = np.diff(wv)
    pos_diffs = diffs[diffs > 0]
    max_upstroke = float(np.max(pos_diffs)) if len(pos_diffs) > 0 else 0.0
    mean_upstroke = float(np.mean(pos_diffs)) if len(pos_diffs) > 0 else 0.0
    feats.extend([max_upstroke, mean_upstroke])
    
    # Find peaks (simplified)
    from scipy.signal import find_peaks
    try:
        peaks, _ = find_peaks(wv, distance=10, prominence=pp * 0.1 if pp > 0 else 1)
        n_beats = len(peaks)
        feats.append(float(n_beats))
        
        if n_beats >= 2:
            # Beat-to-beat interval regularity
            intervals = np.diff(peaks)
            feats.append(float(np.mean(intervals)))
            feats.append(float(np.std(intervals) / (np.mean(intervals) + 1e-6)))  # CV
            
            # Dicrotic notch detection: look for secondary peak between main peaks
            notch_count = 0
            for i in range(len(peaks) - 1):
                segment = wv[peaks[i]:peaks[i+1]]
                if len(segment) > 10:
                    # Find local maxima in the downstroke
                    sub_peaks, _ = find_peaks(segment, distance=5)
                    # Notch is a small peak after the main peak, before the valley
                    if len(sub_peaks) > 0:
                        notch_count += 1
            feats.append(float(notch_count) / max(n_beats - 1, 1))  # Notch ratio
        else:
            feats.extend([0.0, 0.0, 0.0])
        
        # Systolic/diastolic ratio (should be ~1.5-2.0 for normal ART)
        feats.append(float(sys_p / (dia_p + 1e-6)))
        
        # Waveform area (integral)
        feats.append(float(np.trapz(wv - dia_p)))
        
    except ImportError:
        # scipy not available, use basic features
        feats.extend([0.0] * 6)
    except Exception:
        feats.extend([0.0] * 6)
    
    return feats


def is_art_signal(signal_name: str) -> bool:
    """Check if signal is arterial pressure (needs morphology features)."""
    low = signal_name.lower()
    return any(kw in low for kw in ["art", "rad ", "rad_", "bra ", "bra_", 
                                     "abp", "arterial", "ibp", "nbp"])






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
            X, y = extract_labeled_windows(sig, timestamps=ts, real_segments=segs, signal_name=col)
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
    ls = "mixed_real_and_pseudo" if real_labels else "rule_based_pseudo"
    for col in sorted(acc_X):
        X = np.vstack(acc_X[col])
        y = np.concatenate(acc_y[col])
        if train_one(col, X, y, out, label_source=ls):
            trained.append(col)
        else:
            skipped.append(col)
    print(f"[ml-sup-train] done: {len(trained)} models trained, "
          f"{len(skipped)} skipped", flush=True)
    (out / "_summary.json").write_text(json.dumps({
        "trained": trained, "skipped": skipped,
        "label_source": "mixed_real_and_pseudo" if real_labels else "rule_based_pseudo",
        "split": "70/15/15 train/val/test stratified, 5-fold CV on train",
        "labels_csv": args.labels_csv,
    }, indent=1))
    return 0 if trained else 1


if __name__ == "__main__":
    sys.exit(main())
