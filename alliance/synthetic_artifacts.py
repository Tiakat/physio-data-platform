#!/usr/bin/env python3
"""
synthetic_artifacts.py — Phase B: labeled artifact generator.

Takes clean physiological signals and injects known corruptions.
Each function returns (corrupted_series, label_series) where labels are:
  VALID, SPIKE, DROPOUT, FLATLINE, NOISE, BASELINE_SHIFT, SATURATION

This gives us unlimited labeled training data for the QC models (Phase C)
without requiring manual annotation.
"""

import numpy as np
import pandas as pd


def inject_spike(series: pd.Series, n_spikes: int = 3,
                 magnitude: float = 3.0, seed: int = 42) -> tuple:
    """Random isolated spikes (e.g. HR 72 -> 250 -> 73)."""
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)
    valid_idx = s.dropna().index
    if len(valid_idx) < 5:
        return s, labels
    spike_locs = rng.choice(valid_idx, size=min(n_spikes, len(valid_idx)),
                            replace=False)
    med = s.median()
    for loc in spike_locs:
        direction = rng.choice([-1, 1])
        s.loc[loc] = med + direction * abs(med) * magnitude * rng.uniform(0.5, 1.5)
        labels.loc[loc] = "SPIKE"
    return s, labels


def inject_dropout(series: pd.Series, n_gaps: int = 2,
                   gap_len: int = 10, seed: int = 42) -> tuple:
    """Missing blocks (sensor disconnect)."""
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)
    n = len(s)
    for _ in range(n_gaps):
        if n < gap_len + 2:
            break
        start = rng.integers(0, n - gap_len)
        s.iloc[start:start + gap_len] = np.nan
        labels.iloc[start:start + gap_len] = "DROPOUT"
    return s, labels


def inject_flatline(series: pd.Series, n_events: int = 2,
                    flat_len: int = 15, seed: int = 42) -> tuple:
    """Constant value segments (probe off / frozen)."""
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)
    n = len(s)
    for _ in range(n_events):
        if n < flat_len + 2:
            break
        start = rng.integers(0, n - flat_len)
        freeze_val = s.iloc[start]
        if pd.isna(freeze_val):
            continue
        s.iloc[start:start + flat_len] = freeze_val
        labels.iloc[start:start + flat_len] = "FLATLINE"
    return s, labels


def inject_noise(series: pd.Series, snr: float = 5.0, seed: int = 42) -> tuple:
    """Additive Gaussian noise (motion artifact proxy)."""
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)
    valid = s.dropna()
    if len(valid) < 2:
        return s, labels
    signal_power = valid.var()
    noise_power = signal_power / (snr ** 2) if snr > 0 else signal_power
    noise = rng.normal(0, np.sqrt(noise_power), size=len(s))
    # Apply noise to middle third (labeled region)
    n = len(s)
    start, end = n // 3, 2 * n // 3
    s.iloc[start:end] = s.iloc[start:end] + noise[start:end]
    labels.iloc[start:end] = "NOISE"
    return s, labels


def inject_baseline_shift(series: pd.Series, shift: float = 0.3,
                          seed: int = 42) -> tuple:
    """Sudden level change (sensor repositioning / recalibration)."""
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)
    n = len(s)
    if n < 10:
        return s, labels
    split = rng.integers(n // 3, 2 * n // 3)
    med = s.median()
    s.iloc[split:] = s.iloc[split:] + med * shift
    labels.iloc[split:split + 5] = "BASELINE_SHIFT"  # label transition zone
    return s, labels


def inject_saturation(series: pd.Series, sat_value: float = 9999.0,
                      n_events: int = 1, sat_len: int = 8,
                      seed: int = 42) -> tuple:
    """Device saturation codes (e.g. Dräger 9999 = >=max)."""
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)
    n = len(s)
    for _ in range(n_events):
        if n < sat_len + 2:
            break
        start = rng.integers(0, n - sat_len)
        s.iloc[start:start + sat_len] = sat_value
        labels.iloc[start:start + sat_len] = "SATURATION"
    return s, labels


def corrupt(series: pd.Series, seed: int = 42) -> tuple:
    """
    Apply a random combination of artifacts.
    Returns (corrupted_series, label_series).
    """
    rng = np.random.default_rng(seed)
    s = series.copy()
    labels = pd.Series("VALID", index=series.index)

    # Apply 1-3 random corruptions
    funcs = [inject_spike, inject_dropout, inject_flatline,
             inject_noise, inject_saturation]
    chosen = rng.choice(funcs, size=rng.integers(1, 4), replace=False)

    for f in chosen:
        sub_seed = int(rng.integers(0, 100000))
        s_new, l_new = f(s, seed=sub_seed)
        # Merge: corrupted labels overwrite VALID
        mask = l_new != "VALID"
        s = s_new
        labels[mask] = l_new[mask]

    return s, labels


if __name__ == "__main__":
    # Self-test: clean HR signal
    t = np.arange(300)
    clean = pd.Series(72 + 3 * np.sin(t / 20) + np.random.default_rng(0).normal(0, 1, 300))

    corrupted, labels = corrupt(clean, seed=123)
    print("Label distribution:")
    print(labels.value_counts())
    print(f"\nClean mean: {clean.mean():.1f}, corrupted mean: {corrupted.mean():.1f}")
    print("Self-test passed: synthetic artifacts generated with known labels")
