#!/usr/bin/env python3
"""
train_qc_12var.py — Phase C step 2: scale from 3 to 12 variables.

Variables: HR, SPO2, RESP, ART_MEAN, ETCO2, VT, MVE, PEEP, PIP, PPLAT, FIO2, BIS
Adds cross-variable consistency features (MV=VT*RR/1000, PIP>=PPLAT>=PEEP).
"""

import numpy as np
import pandas as pd
import sys
sys.path.insert(0, "/home/hatch/workspace/build")
from synthetic_artifacts import corrupt
from train_qc_baseline import extract_window_features

from sklearn.model_selection import train_test_split
from sklearn.metrics import precision_score, recall_score, f1_score, roc_auc_score, confusion_matrix
import xgboost as xgb


def generate_clean_12var(n_seconds: int = 3600, seed: int = 0) -> pd.DataFrame:
    """Realistic 12-variable physiology with cross-variable relationships."""
    rng = np.random.default_rng(seed)
    t = np.arange(n_seconds)

    # Cardiovascular
    hr_base = 72 + 5*np.sin(t/300) + 2*np.sin(t/4)
    hr = hr_base + rng.normal(0, 1.5, n_seconds)
    art_mean = 85 + 0.3*(hr_base-72) + rng.normal(0, 2, n_seconds)

    # Oxygenation
    spo2 = np.clip(98 - 0.02*np.clip(hr_base-72,0,20) + rng.normal(0,0.3,n_seconds), 90, 100)
    fio2 = np.full(n_seconds, 50.0) + rng.normal(0, 1, n_seconds)  # 50% O2

    # Respiratory
    rr_base = 12 + 1.5*np.sin(t/240)
    resp = rr_base + rng.normal(0, 0.5, n_seconds)
    vt_base = 480 + 30*np.sin(t/180)
    vt = np.clip(vt_base + rng.normal(0, 15, n_seconds), 300, 700)
    mve = vt * rr_base / 1000 + rng.normal(0, 0.2, n_seconds)  # consistent!
    etco2 = 38 + 0.02*(vt_base-480)/10 + rng.normal(0, 1, n_seconds)

    # Ventilator pressures (consistent ordering)
    peep = np.full(n_seconds, 5.0)
    pplat = 14 + 2*np.sin(t/200) + rng.normal(0, 0.5, n_seconds)
    pip = pplat + 4 + rng.normal(0, 0.5, n_seconds)  # PIP > PPLAT > PEEP

    # BIS (anesthesia depth)
    bis = 48 + 5*np.sin(t/400) + rng.normal(0, 2, n_seconds)
    bis = np.clip(bis, 20, 80)

    return pd.DataFrame({
        "HR": hr, "SPO2": spo2, "RESP": resp, "ART_MEAN": art_mean,
        "ETCO2": etco2, "VT": vt, "MVE": mve, "PEEP": peep,
        "PIP": pip, "PPLAT": pplat, "FIO2": fio2, "BIS": bis,
    })


def extract_12var_features(window: pd.DataFrame) -> dict:
    """Standard features + cross-variable consistency features."""
    feats = extract_window_features(window)

    # MV consistency: MVE ≈ VT × RR / 1000
    if all(c in window.columns for c in ("VT", "RESP", "MVE")):
        vt_m = window["VT"].mean()
        rr_m = window["RESP"].mean()
        mve_m = window["MVE"].mean()
        if pd.notna(vt_m) and pd.notna(rr_m) and pd.notna(mve_m) and mve_m > 0:
            expected = vt_m * rr_m / 1000
            feats["mv_consistency_err"] = abs(mve_m - expected) / mve_m
        else:
            feats["mv_consistency_err"] = np.nan

    # Pressure ordering violations
    if all(c in window.columns for c in ("PIP", "PPLAT", "PEEP")):
        pip = window["PIP"].mean()
        pplat = window["PPLAT"].mean()
        peep = window["PEEP"].mean()
        feats["pip_lt_pplat"] = 1.0 if pd.notna(pip) and pd.notna(pplat) and pip < pplat else 0.0
        feats["pplat_lt_peep"] = 1.0 if pd.notna(pplat) and pd.notna(peep) and pplat < peep else 0.0

    return feats


def build_12var_dataset(n_patients=20, seconds_per=1800, window_sec=30, seed=0):
    rng = np.random.default_rng(seed)
    all_features = []
    for p in range(n_patients):
        clean = generate_clean_12var(seconds_per, seed=seed+p)
        corrupted = pd.DataFrame(index=clean.index)
        labels = pd.DataFrame(index=clean.index)
        for col in clean.columns:
            c, l = corrupt(clean[col], seed=seed*100+p*10+hash(col)%97)
            corrupted[col] = c
            labels[col] = l
        n_windows = seconds_per // window_sec
        for w in range(n_windows):
            s, e = w*window_sec, (w+1)*window_sec
            feats = extract_12var_features(corrupted.iloc[s:e])
            feats["patient_id"] = p
            feats["label"] = 1 if (labels.iloc[s:e] != "VALID").any().any() else 0
            all_features.append(feats)
    return pd.DataFrame(all_features)


def main():
    print("Building 12-variable dataset...")
    df = build_12var_dataset()
    print(f"  {len(df)} windows, {df['label'].mean()*100:.1f}% artifact")

    feature_cols = [c for c in df.columns if c not in ("label", "patient_id")]
    X = df[feature_cols].fillna(0)
    y = df["label"]

    patients = df["patient_id"].unique()
    train_pts, test_pts = train_test_split(patients, test_size=0.3, random_state=42)
    train_mask = df["patient_id"].isin(train_pts)
    X_train, X_test = X[train_mask], X[~train_mask]
    y_train, y_test = y[train_mask], y[~train_mask]

    print("Training XGBoost (12 vars)...")
    model = xgb.XGBClassifier(n_estimators=200, max_depth=6, learning_rate=0.05,
                               subsample=0.8, colsample_bytree=0.8,
                               eval_metric="logloss", random_state=42)
    model.fit(X_train, y_train)

    y_pred = model.predict(X_test)
    y_prob = model.predict_proba(X_test)[:, 1]

    print(f"\n  Precision: {precision_score(y_test, y_pred):.3f}")
    print(f"  Recall:    {recall_score(y_test, y_pred):.3f}")
    print(f"  F1:        {f1_score(y_test, y_pred):.3f}")
    print(f"  AUROC:     {roc_auc_score(y_test, y_prob):.3f}")
    print(f"\nConfusion matrix:")
    print(confusion_matrix(y_test, y_pred))

    imp = pd.Series(model.feature_importances_, index=feature_cols)
    print(f"\nTop 12 features:")
    print(imp.nlargest(12).to_string())

    # Check if consistency features are useful
    cons_feats = [c for c in feature_cols if "consistency" in c or "lt_" in c]
    if cons_feats:
        cons_imp = imp[cons_feats].sum()
        print(f"\nConsistency features total importance: {cons_imp:.3f}")

    model.save_model("/home/hatch/workspace/build/qc_xgb_12var.json")
    print("\nModel saved to qc_xgb_12var.json")

    f1 = f1_score(y_test, y_pred)
    print(f"\n{'✓ 12-VAR PASSED' if f1 > 0.85 else '✗ NEEDS WORK'} (F1={f1:.3f})")


if __name__ == "__main__":
    main()
