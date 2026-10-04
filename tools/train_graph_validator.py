"""Graph validator trainer: learn per-signal-family normal vs artifact morphology.

K's requirement: the validator must learn what a *normal* trace looks like for
each signal family -- HR is pulsatile 60-100 bpm, SpO2 is flat ~98%, ART shows
a pulsatile pressure wave, NBP is intermittent (cuff inflates periodically),
ECG is fast and spiky, temperature drifts slowly -- and flag windows whose
morphology looks like artifact.

Design notes:
- We train on the underlying filtered parquet data, NOT the PNG files. The
  PNGs in the `graphs` container are renderings of this same data; morphology
  features computed on the data are exact, auditable, and unaffected by
  rendering choices (axis limits, decimation, colours).
- One RandomForest per signal *family*, not per column: every HR-like column
  across all 10 projects trains a single HR validator, so "what HR looks
  like" is learned globally and generalises across projects/devices.
- Labels: K's real artifact labels (data/labels/promises_labels.csv) override
  rule-based pseudo-labels wherever they overlap in project/patient/family
  and time. Pseudo-labels distill the deterministic QC rules (heavy NaN =
  rule-based filter removed it; flatline inside a pulsatile signal =
  artifact; mostly outside the family's physiological range = artifact).
  The ML learns to replicate the rules; K's real labels refine it -- the
  same philosophy as tools/ml_supervised_train.py.
- 70/15/15 stratified train/val/test split + 5-fold CV on train, same
  convention as tools/ml_supervised_train.py.
- Each family report records the *learned* normal morphology (median/IQR of
  mean, std, peak count over clean windows) so K can audit what "normal"
  the model actually learned. Age/weight-adjusted norms are future work
  pending demographics integration.

Usage:
  python -m tools.train_graph_validator --parquet-dir processed_dl --out ml_out
  python -m tools.train_graph_validator --parquet-dir processed_dl --out ml_out \\
      --families HR,SPO2,ART --labels-csv data/labels/promises_labels.csv
"""

from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.signal import find_peaks
from sklearn.ensemble import RandomForestClassifier
from sklearn.metrics import accuracy_score, f1_score, matthews_corrcoef
from sklearn.model_selection import StratifiedKFold, cross_val_score, train_test_split

from tools.ml_supervised_train import (
    label_for_window,
    load_labels,
    patient_from_path,
    project_from_path,
    read_parquet_any,
)


# ---------------------------------------------------------------------------
# Signal families: prefix match (priority order), physiological range, traits.
# Traits encode K's morphology priors: what "normal" looks like per family.
# ---------------------------------------------------------------------------
# (family, prefixes, (lo, hi), traits)
FAMILY_DEFS = [
    ("ECG",   ["ECG"],                        (-5.0, 5.0),    {"pulsatile": True, "fast": True}),
    ("HR",    ["HR"],                         (30.0, 200.0),  {"pulsatile": True}),
    ("PLETH", ["PLETH", "PPG"],               (0.0, 100.0),   {"pulsatile": True}),
    ("SPO2",  ["SPO2", "SP_O2", "O2SAT"],      (70.0, 100.0),  {"flat_ok": True}),
    ("ART",   ["ART", "ABP"],                 (20.0, 260.0),  {"pulsatile": True}),
    ("NBP",   ["NBP", "NIBP"],                (20.0, 260.0),  {"intermittent": True}),
    ("RESP",  ["RESP", "RR", "AW", "CO2", "ETCO2", "AIR", "PAW", "TV", "VT"],
                                                        (0.0, 120.0),   {"oscillatory": True}),
    ("TEMP",  ["TEMP", "T1", "T2"],           (30.0, 42.0),   {"flat_ok": True, "slow": True}),
    ("BIS",   ["BIS"],                        (0.0, 100.0),   {"flat_ok": True, "slow": True}),
    ("NOL",   ["NOL"],                        (0.0, 100.0),   {"flat_ok": True, "slow": True}),
    ("PRESS", ["CVP", "PAP", "PA", "PRES"],    (0.0, 120.0),  {"pulsatile": True}),
    ("OTHER", [],                             (None, None),   {}),
]
FAMILY_TRAITS = {fam: traits for fam, _, _, traits in FAMILY_DEFS}
FAMILY_RANGE = {fam: rng for fam, _, rng, _ in FAMILY_DEFS}


def _norm_col(col: str) -> str:
    """Uppercase, strip unit annotations like ' (mmHg)' and '^^ISO+' junk."""
    c = col.upper()
    c = re.sub(r"\([^)]*\)", "", c)
    c = c.replace("^^ISO+", "").strip()
    return c


def classify_family(col: str) -> str:
    """Map a raw column name to a signal family (first prefix match wins)."""
    c = _norm_col(col)
    for fam, prefixes, _, _ in FAMILY_DEFS:
        for p in prefixes:
            # Match whole token start: "HR", "HR.1", "HR_SOMETHING" but not "HRV".
            if c == p or re.match(rf"^{re.escape(p)}(?![A-Z])", c):
                return fam
    return "OTHER"


FEATURE_NAMES = [
    "nan_frac", "mean", "std", "median", "min", "max", "range", "iqr",
    "n_peaks", "peak_regularity", "flatline_frac", "unique_ratio",
    "max_abs_step", "spike_count", "smoothness", "trend_slope",
    "in_norm_frac", "zero_frac", "neg_frac",
]


def window_features(w: np.ndarray, lo, hi) -> list | None:
    """Morphology features for one window. None if too few valid samples."""
    mask = ~np.isnan(w)
    nan_frac = float(1.0 - mask.mean())
    wv = w[mask]
    if len(wv) < 10:
        return None
    mean = float(np.mean(wv))
    std = float(np.std(wv))
    med = float(np.median(wv))
    mn = float(np.min(wv))
    mx = float(np.max(wv))
    rng = mx - mn
    q75, q25 = np.percentile(wv, [75, 25])
    iqr = float(q75 - q25)

    # Pulsatility: peak count + regularity of inter-peak intervals.
    try:
        peaks, _ = find_peaks(wv, distance=3)
        n_peaks = len(peaks)
        if n_peaks >= 3:
            ipis = np.diff(peaks)
            peak_reg = float(1.0 - (np.std(ipis) / (np.mean(ipis) + 1e-9)))
            peak_reg = max(0.0, min(1.0, peak_reg))
        else:
            peak_reg = 0.0
    except Exception:  # noqa: BLE001
        n_peaks, peak_reg = 0, 0.0

    # Flatline: longest run of near-identical consecutive samples.
    eps = max(1e-9, rng * 1e-4)
    diffs = np.abs(np.diff(wv)) if len(wv) > 1 else np.array([0.0])
    flat = np.concatenate([[False], diffs < eps])
    maxrun = cur = 0
    for f in flat:
        cur = cur + 1 if f else 0
        maxrun = max(maxrun, cur)
    flatline_frac = float(maxrun / len(wv))
    unique_ratio = float(len(np.unique(wv)) / len(wv))

    # Spikes / smoothness.
    max_abs_step = float(np.max(diffs)) if len(diffs) else 0.0
    med_step = float(np.median(diffs)) if len(diffs) else 0.0
    spike_count = float(np.sum(diffs > 5 * med_step + 1e-9))
    d2 = np.abs(np.diff(wv, n=2)) if len(wv) > 2 else np.array([0.0])
    smoothness = float(np.mean(d2) / (rng + 1e-9))
    x = np.arange(len(wv))
    slope = float(np.polyfit(x, wv, 1)[0]) if len(wv) > 1 else 0.0
    trend_slope = float(slope * 100 / (std + 1e-9))  # drift per 100 samples, in std units

    # Physiological plausibility.
    in_norm_frac = float(np.mean((wv >= lo) & (wv <= hi))) if lo is not None else 1.0
    zero_frac = float(np.mean(wv == 0))
    neg_frac = float(np.mean(wv < 0))

    return [nan_frac, mean, std, med, mn, mx, rng, iqr, float(n_peaks),
            peak_reg, flatline_frac, unique_ratio, max_abs_step,
            spike_count, smoothness, trend_slope, in_norm_frac,
            zero_frac, neg_frac]


def pseudo_label(feat: dict, traits: dict) -> int:
    """Distill deterministic QC rules into a pseudo-label (1 = artifact)."""
    if feat["nan_frac"] > 0.2:                      # rule-based filter removed it
        return 1
    if traits.get("pulsatile") and feat["flatline_frac"] > 0.9:
        return 1                                    # flatline in a pulsatile signal
    if feat["in_norm_frac"] < 0.3:                  # mostly outside physiology
        return 1
    return 0


def family_label_index(labels: dict) -> dict:
    """Re-index (project, patient, signal) label segments by signal family."""
    idx = defaultdict(list)
    for (proj, pat, sig), segs in labels.items():
        fam = classify_family(sig)
        idx[(proj, pat, fam)].extend(segs)
    for k in idx:
        idx[k].sort()
    return idx


def _timestamps_seconds(df: pd.DataFrame) -> np.ndarray | None:
    """Row timestamps in seconds (for real-label alignment)."""
    for tc in df.columns:
        if tc.lower() in ("timestamp", "time", "t"):
            try:
                tcol = pd.to_datetime(df[tc], errors="coerce")
                if tcol.notna().any():
                    return (tcol.dt.hour * 3600 + tcol.dt.minute * 60
                            + tcol.dt.second + tcol.dt.microsecond / 1e6).values
                return pd.to_numeric(df[tc], errors="coerce").values
            except Exception:  # noqa: BLE001
                return None
    return None


def train_family(family: str, X: np.ndarray, y: np.ndarray, out: Path,
                 label_source: str) -> bool:
    """Train one family validator: 70/15/15 split, 5-fold CV, held-out test."""
    if len(np.unique(y)) < 2:
        print(f"[graph-val] {family}: only one class -- skipped", flush=True)
        return False
    X_tr, X_te, y_tr, y_te = train_test_split(
        X, y, test_size=0.15, random_state=42, stratify=y)
    X_tr2, X_va, y_tr2, y_va = train_test_split(
        X_tr, y_tr, test_size=0.176, random_state=42, stratify=y_tr)
    model = RandomForestClassifier(n_estimators=100, max_depth=12,
                                   random_state=42, n_jobs=-1)
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    cv_scores = cross_val_score(model, X_tr2, y_tr2, cv=cv, scoring="f1")
    model.fit(X_tr2, y_tr2)
    va_f1 = f1_score(y_va, model.predict(X_va), zero_division=0)
    model.fit(X_tr, y_tr)
    te_pred = model.predict(X_te)
    te_acc = accuracy_score(y_te, te_pred)
    te_f1 = f1_score(y_te, te_pred, zero_division=0)
    te_mcc = matthews_corrcoef(y_te, te_pred)
    print(f"[graph-val] {family}: CV F1 {cv_scores.mean():.3f}+-{cv_scores.std():.3f} | "
          f"val F1 {va_f1:.3f} | TEST acc {te_acc:.3f} F1 {te_f1:.3f} MCC {te_mcc:.3f} "
          f"({len(X)} windows, {np.mean(y):.1%} artifact)", flush=True)

    # Learned "normal": morphology of clean windows, for K's audit.
    clean = X[y == 0]
    ci = {n: i for i, n in enumerate(FEATURE_NAMES)}
    def _stat(j):
        v = clean[:, j]
        return {"median": float(np.median(v)),
                "iqr": [float(np.percentile(v, 25)), float(np.percentile(v, 75))]}
    learned_normal = {k: _stat(ci[k]) for k in
                      ("mean", "std", "n_peaks", "peak_regularity",
                       "flatline_frac", "in_norm_frac")}

    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in family)
    with open(out / f"{safe}_validator.pkl", "wb") as f:
        pickle.dump(model, f)
    (out / f"{safe}_report.json").write_text(json.dumps({
        "family": family,
        "traits": FAMILY_TRAITS[family],
        "physiological_range": FAMILY_RANGE[family],
        "features": FEATURE_NAMES,
        "feature_importance": dict(sorted(
            zip(FEATURE_NAMES, (float(v) for v in model.feature_importances_)),
            key=lambda kv: kv[1], reverse=True)),
        "learned_normal": learned_normal,
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
    }, indent=1))
    return True


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet-dir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--families", default="all",
                    help="Comma-separated families e.g. HR,SPO2,ART or 'all'")
    ap.add_argument("--labels-csv", default=None,
                    help="Real labels CSV (project,patient,signal,start_s,end_s,label)")
    ap.add_argument("--max-windows-per-family", type=int, default=12000)
    ap.add_argument("--max-windows-per-file", type=int, default=1200)
    ap.add_argument("--window", type=int, default=100)
    ap.add_argument("--step", type=int, default=50)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    real = load_labels(args.labels_csv) if args.labels_csv else {}
    fam_labels = family_label_index(real)

    want = ({f.strip().upper() for f in args.families.split(",")}
            if args.families.lower() != "all" else None)

    files = sorted(Path(args.parquet_dir).rglob("*filtered.parquet*"))
    print(f"[graph-val] {len(files)} filtered parquets", flush=True)
    if not files:
        print("[graph-val] no files found", flush=True)
        return 1

    acc_X = defaultdict(list)
    acc_y = defaultdict(list)
    counts = defaultdict(int)
    n_real_used = 0

    for f in files:
        try:
            df = read_parquet_any(f)
        except Exception as e:  # noqa: BLE001
            print(f"[graph-val] skip {f.name}: {e}", flush=True)
            continue
        pat = patient_from_path(f)
        proj = project_from_path(f)
        ts = _timestamps_seconds(df)
        per_file = defaultdict(int)
        for col in df.columns:
            if col.lower() in ("timestamp", "time", "t") or col.lower().endswith("__qc"):
                continue
            fam = classify_family(col)
            if fam == "OTHER" or (want and fam not in want):
                continue
            if counts[fam] >= args.max_windows_per_family:
                continue
            try:
                sig = pd.to_numeric(df[col], errors="coerce").values.astype(float)
            except Exception:  # noqa: BLE001
                continue
            if np.isnan(sig).all():
                continue
            lo, hi = FAMILY_RANGE[fam]
            traits = FAMILY_TRAITS[fam]
            segs = fam_labels.get((proj, pat, fam)) if (proj and pat) else None
            W, S = args.window, args.step
            for i in range(0, len(sig) - W, S):
                if per_file[fam] >= args.max_windows_per_file:
                    break
                if counts[fam] >= args.max_windows_per_family:
                    break
                w = sig[i:i + W]
                feats = window_features(w, lo, hi)
                if feats is None:
                    continue
                fd = dict(zip(FEATURE_NAMES, feats))
                lab = None
                if segs and ts is not None:
                    try:
                        lab = label_for_window(segs, float(ts[i]),
                                               float(ts[min(i + W - 1, len(ts) - 1)]))
                        if lab is not None:
                            n_real_used += 1
                    except Exception:  # noqa: BLE001
                        lab = None
                if lab is None:
                    lab = pseudo_label(fd, traits)
                acc_X[fam].append(feats)
                acc_y[fam].append(lab)
                counts[fam] += 1
                per_file[fam] += 1

    if not acc_X:
        print("[graph-val] no training data", flush=True)
        return 1
    print(f"[graph-val] windows per family: {dict(counts)}; "
          f"real-label windows used: {n_real_used}", flush=True)

    ls = "mixed_real_and_pseudo" if real else "rule_based_pseudo"
    trained, skipped = [], []
    for fam in sorted(acc_X):
        X = np.array(acc_X[fam])
        y = np.array(acc_y[fam])
        if train_family(fam, X, y, out, label_source=ls):
            trained.append(fam)
        else:
            skipped.append(fam)
    print(f"[graph-val] done: {len(trained)} family validators trained, "
          f"{len(skipped)} skipped", flush=True)
    (out / "_summary.json").write_text(json.dumps({
        "trained": trained, "skipped": skipped,
        "windows_per_family": dict(counts),
        "real_label_windows": n_real_used,
        "label_source": ls,
        "split": "70/15/15 train/val/test stratified, 5-fold CV on train",
        "labels_csv": args.labels_csv,
        "families": {fam: {"traits": FAMILY_TRAITS[fam],
                           "range": FAMILY_RANGE[fam]} for fam in trained},
    }, indent=1))
    return 0 if trained else 1


if __name__ == "__main__":
    sys.exit(main())
