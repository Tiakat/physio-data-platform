"""Open physiological data for portfolio demos.

Downloads open-access ECG data from PhysioNet (MIT-BIH Arrhythmia Database)
or generates realistic synthetic ECG when offline.

NO credentials required. NO patient data. Safe for public portfolios.

Usage:
    from tools.open_data import load_ecg_demo

    df = load_ecg_demo(n_seconds=60, source="synthetic")  # always works
    df = load_ecg_demo(n_seconds=60, source="mitdb", record="100")  # needs internet

PhysioNet MIT-BIH: https://physionet.org/content/mitdb/1.0.0/
Goldberger et al. (2000). PhysioBank, PhysioToolkit, and PhysioNet.
Circulation 101(23):e215-e220.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd


def synthetic_ecg(
    n_seconds: float = 60.0,
    fs_hz: float = 360.0,
    heart_rate_bpm: float = 72.0,
    seed: int = 42,
    add_artifacts: bool = True,
) -> pd.DataFrame:
    """Generate realistic synthetic single-lead ECG with optional artifacts.

    The waveform is built from Gaussian-shaped P/QRS/T complexes placed at
    realistic RR intervals with physiological heart-rate variability.
    Artifacts injected (when add_artifacts=True):
      - baseline wander (respiratory, 0.3 Hz)
      - muscle noise bursts (high-frequency)
      - electrode motion artifact (low-frequency transient)
      - a flatline dropout segment
      - a saturation/clipping segment
    """
    rng = np.random.default_rng(seed)
    n = int(n_seconds * fs_hz)
    t = np.arange(n) / fs_hz

    # RR intervals with realistic HRV (mean RR from heart rate + LF/HF wobble)
    mean_rr = 60.0 / heart_rate_bpm
    n_beats = int(n_seconds / mean_rr) + 2
    rr = mean_rr * (1 + 0.04 * np.sin(np.arange(n_beats) * 0.5)
                    + 0.02 * rng.normal(0, 1, n_beats))
    rr = np.clip(rr, 0.4, 1.5)
    beat_times = np.cumsum(rr)
    beat_times = beat_times[beat_times < n_seconds]

    # ECG morphology: P, Q, R, S, T as Gaussians (relative amplitudes/widths)
    # (mV scale, lead II-ish)
    ecg = np.zeros(n)

    def _add_wave(center_t, amp_mv, width_s):
        idx = int(center_t * fs_hz)
        w = int(width_s * fs_hz * 3)
        if idx - w < 0 or idx + w >= n:
            return
        xs = (np.arange(idx - w, idx + w + 1) - idx) / fs_hz
        ecg[idx - w: idx + w + 1] += amp_mv * np.exp(-0.5 * (xs / width_s) ** 2)

    for bt in beat_times:
        _add_wave(bt - 0.16, 0.12, 0.025)   # P
        _add_wave(bt - 0.035, -0.10, 0.012)  # Q
        _add_wave(bt, 1.0, 0.012)            # R
        _add_wave(bt + 0.035, -0.18, 0.014)  # S
        _add_wave(bt + 0.28, 0.28, 0.045)    # T

    # Physiological noise floor
    ecg += rng.normal(0, 0.008, n)

    qc = np.full(n, "VALID", dtype=object)

    if add_artifacts:
        # 1) Baseline wander (respiration)
        ecg += 0.12 * np.sin(2 * np.pi * 0.3 * t)

        # 2) Muscle-noise burst: 5 s of high-freq noise
        i0, i1 = int(10 * fs_hz), int(15 * fs_hz)
        ecg[i0:i1] += rng.normal(0, 0.09, i1 - i0)
        qc[i0:i1] = "NOISY"

        # 3) Electrode motion: slow transient 20-25 s
        j0, j1 = int(20 * fs_hz), int(25 * fs_hz)
        bump = 0.5 * np.sin(np.pi * (np.arange(j1 - j0) / (j1 - j0))) ** 2
        ecg[j0:j1] += bump
        qc[j0:j1] = "MOTION_ARTIFACT"

        # 4) Flatline dropout: 30-33 s (lead-off)
        k0, k1 = int(30 * fs_hz), int(33 * fs_hz)
        ecg[k0:k1] = 0.0
        qc[k0:k1] = "FLATLINE"

        # 5) Saturation/clipping: 40-42 s
        m0, m1 = int(40 * fs_hz), int(42 * fs_hz)
        ecg[m0:m1] = np.clip(ecg[m0:m1], -0.4, 0.4)
        over = np.abs(ecg[m0:m1]) >= 0.399
        qc[m0:m1][over] = "SATURATED"

    timestamps = pd.date_range("2026-01-01 08:00:00", periods=n,
                               freq=pd.Timedelta(seconds=1 / fs_hz))
    return pd.DataFrame({
        "timestamp": timestamps,
        "ECG_mV": ecg,
        "ECG__qc_expected": qc,
    })


def load_mitdb_record(
    record: str = "100",
    n_seconds: float | None = 60.0,
    cache_dir: str | Path | None = None,
) -> pd.DataFrame:
    """Download one MIT-BIH Arrhythmia Database record from PhysioNet.

    Requires the `wfdb` package (`pip install wfdb`) and internet access.
    Returns a DataFrame with timestamp + ECG_mV (lead MLII).

    Falls back to synthetic data with a warning if download fails.
    """
    cache = Path(cache_dir or Path.home() / ".cache" / "physio-portfolio")
    cache.mkdir(parents=True, exist_ok=True)
    parquet_path = cache / f"mitdb_{record}.parquet"

    if parquet_path.exists():
        return pd.read_parquet(parquet_path)

    try:
        import wfdb
    except ImportError:
        print("[open_data] wfdb not installed; using synthetic ECG instead "
              "(pip install wfdb for real MIT-BIH data).")
        return synthetic_ecg(n_seconds=n_seconds or 60.0, seed=hash(record) % 2**31)

    try:
        signals, fields = wfdb.rdsamp(record, pn_dir="mitdb")
    except Exception as exc:  # offline, PhysioNet down, etc.
        print(f"[open_data] MIT-BIH download failed ({exc}); "
              f"using synthetic ECG instead.")
        return synthetic_ecg(n_seconds=n_seconds or 60.0, seed=hash(record) % 2**31)

    fs = float(fields["fs"])
    # MLII is usually channel 0; fall back to first channel
    sig = signals[:, 0].astype(float)
    if n_seconds:
        sig = sig[: int(n_seconds * fs)]
    n = len(sig)
    timestamps = pd.date_range("2026-01-01 08:00:00", periods=n,
                               freq=pd.Timedelta(seconds=1 / fs))
    df = pd.DataFrame({"timestamp": timestamps, "ECG_mV": sig})
    df.to_parquet(parquet_path, index=False)
    print(f"[open_data] MIT-BIH record {record}: {n} samples @ {fs} Hz "
          f"(cached to {parquet_path})")
    return df


def load_ecg_demo(
    n_seconds: float = 60.0,
    source: str = "synthetic",
    record: str = "100",
    seed: int = 42,
) -> pd.DataFrame:
    """One-call loader for portfolio demos.

    source="synthetic" -- always works, no internet, reproducible.
    source="mitdb"     -- real open data from PhysioNet (falls back to synthetic).
    """
    if source == "mitdb":
        return load_mitdb_record(record=record, n_seconds=n_seconds)
    return synthetic_ecg(n_seconds=n_seconds, seed=seed, add_artifacts=True)
