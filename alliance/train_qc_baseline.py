#!/usr/bin/env python3
"""
train_qc_baseline.py — Phase C step 1: XGBoost artifact detector (3-variable POC).

Pipeline:
  1. Generate realistic clean physiology (HR, SpO2, ART_MEAN with relationships)
  2. Inject synthetic artifacts (known labels from synthetic_artifacts.py)
  3. Extract 30-second window features
  4. Train XGBoost binary classifier: VALID vs ARTIFACT
  5. Evaluate: precision, recall, F1, AUROC

This is the proof of concept. If this loop closes cleanly on 3 variables,
we scale to 12, then add waveforms, then the multimodal encoder.
"""

import numpy as np
import pandas as pd
import sys
sys.path.insert(0, "/home/hatch/workspace/build")
from synthetic_artifacts import corrupt

from sklearn.model_selection import train_test_split
from sklearn.metrics import (precision_score, recall_score, f1_score,
                             roc_auc_score, classification_report,
                             confusion_matrix)
import xgboost as xgb


def generate_clean_physiology(n_seconds: int = 3600, seed: int = 0) -> pd.DataFrame:
    """
    Generate realistic clean signals with physiological relationships:
      - HR: baseline ~72, respiratory sinus arrhythmia, slow drift
      - SpO2: ~98, dips slightly when HR rises (exertion proxy)
      - ART_MEAN: ~85, correlates with HR (cardiac output proxy)
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n_seconds)

    # Slow drift + respiratory modulation
    hr_base = 72 + 5 * np.sin(t / 300) + 2 * np.sin(t / 4)  # RSA at ~0.25 Hz
    hr = hr_base + rng.normal(0, 1.5, n_seconds)

    # SpO2: stable, slight inverse with HR exertion
    spo2_base = 98 - 0.02 * np.clip(hr_base - 72, 0, 20)
    spo2 = np.clip(spo2_base + rng.normal(0, 0.3, n_seconds), 90, 100)

    # ART_MEAN: correlates with HR
    art_base = 85 + 0.3 * (hr_base - 72) + 3 * np.sin(t / 200)
    art = art_base + rng.normal(0, 2, n_seconds)

    return pd.DataFrame({"HR": hr, "SPO2": spo2, "ART_MEAN": art})


def extract_window_features(window: pd.DataFrame) -> dict:
    """Temporal features for one 30-second window (per variable)."""
    feats = {}
    for col in window.columns:
        s = window[col].dropna()
        p = f"{col}_"
        feats[p + "mean"] = s.mean() if len(s) else np.nan
        feats[p + "std"] = s.std() if len(s) > 1 else 0
        feats[p + "min"] = s.min() if len(s) else np.nan
        feats[p + "max"] = s.max() if len(s) else np.nan
        feats[p + "range"] = feats[p + "max"] - feats[p + "min"] if len(s) else 0
        # slope (linear fit)
        if len(s) > 1:
            x = np.arange(len(s))
            feats[p + "slope"] = np.polyfit(x, s.values, 1)[0]
        else:
            feats[p + "slope"] = 0
        # MAD (robust spread)
        feats[p + "mad"] = (s - s.median()).abs().median() if len(s) else 0
        # missing fraction
        feats[p + "missing_frac"] = window[col].isna().mean()
        # jump count (>3 MAD jumps)
        if len(s) > 1:
            jumps = s.diff().abs() > (3 * feats[p + "mad"] + 1e-6)
            feats[p + "n_jumps"] = jumps.sum()
        else:
            feats[p + "n_jumps"] = 0

    # Cross-variable: HR-SpO2 correlation (should be slightly negative)
    valid = window[["HR", "SPO2"]].dropna()
    feats["hr_spo2_corr"] = valid["HR"].corr(valid["SPO2"]) if len(valid) > 5 else 0

    return feats


def build_dataset(n_patients: int = 20, seconds_per: int = 1800,
                  window_sec: int = 30, seed: int = 0) -> pd.DataFrame:
    """
    Build labeled dataset. Each patient gets clean data + corruption.
    Labels: 0=VALID, 1=ARTIFACT (any artifact type in window).
    """
    rng = np.random.default_rng(seed)
    all_features = []

    for p in range(n_patients):
        clean = generate_clean_physiology(seconds_per, seed=seed + p)

        # Corrupt each variable independently
        corrupted = pd.DataFrame(index=clean.index)
        labels = pd.DataFrame(index=clean.index)
        for col in clean.columns:
            c, l = corrupt(clean[col], seed=seed * 100 + p * 10 + hash(col) % 100)
            corrupted[col] = c
            labels[col] = l

        # Window-level label: ARTIFACT if any variable has artifact in window
        n_windows = seconds_per // window_sec
        for w in range(n_windows):
            s, e = w * window_sec, (w + 1) * window_sec
            win_data = corrupted.iloc[s:e]
            win_labels = labels.iloc[s:e]

            feats = extract_window_features(win_data)
            feats["patient_id"] = p
            # Binary label
            has_artifact = (win_labels != "VALID").any().any()
            feats["label"] = 1 if has_artifact else 0
            # Also store dominant artifact type for analysis
            art_types = win_labels[win_labels != "VALID"].stack().unique()
            feats["artifact_type"] = art_types[0] if len(art_types) else "VALID"
            all_features.append(feats)

    return pd.DataFrame(all_features)


def main():
    print("Building dataset (20 patients × 1800s, 30s windows)...")
    df = build_dataset()
    print(f"  {len(df)} windows, {df['label'].mean()*100:.1f}% artifact")

    feature_cols = [c for c in df.columns
                    if c not in ("label", "patient_id", "artifact_type")]
    X = df[feature_cols].fillna(0)
    y = df["label"]

    # PATIENT-LEVEL split (critical: no patient in both train and test)
    patients = df["patient_id"].unique()
    train_pts, test_pts = train_test_split(patients, test_size=0.3,
                                            random_state=42)
    train_mask = df["patient_id"].isin(train_pts)
    X_train, X_test = X[train_mask], X[~train_mask]
    y_train, y_test = y[train_mask], y[~train_mask]
    print(f"  Train patients: {sorted(train_pts)}, Test: {sorted(test_pts)}")
    print(f"  Train: {len(X_train)}, Test: {len(X_test)}")

    print("\nTraining XGBoost...")
    model = xgb.XGBClassifier(
        n_estimators=200,
        max_depth=6,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        eval_metric="logloss",
        random_state=42,
    )
    model.fit(X_train, y_train)

    print("\nEvaluating...")
    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]

    print(f"\n  Precision: {precision_score(y_test, y_pred):.3f}")
    print(f"  Recall:    {recall_score(y_test, y_pred):.3f}")
    print(f"  F1:        {f1_score(y_test, y_pred):.3f}")
    print(f"  AUROC:     {roc_auc_score(y_test, y_prob):.3f}")
    print(f"\nConfusion matrix (rows=true, cols=pred):")
    print(confusion_matrix(y_test, y_pred))

    # Feature importance
    imp = pd.Series(model.feature_importances_, index=feature_cols)
    print(f"\nTop 10 features:")
    print(imp.nlargest(10).to_string())

    # Save model
    model.save_model("/home/hatch/workspace/build/qc_xgb_3var.json")
    print("\nModel saved to qc_xgb_3var.json")

    # Success criterion: F1 > 0.85 on held-out patients
    f1 = f1_score(y_test, y_pred)
    if f1 > 0.85:
        print(f"\n✓ PROOF OF CONCEPT PASSED (F1={f1:.3f} > 0.85)")
    else:
        print(f"\n✗ NEEDS WORK (F1={f1:.3f} <= 0.85)")


if __name__ == "__main__":
    main()
