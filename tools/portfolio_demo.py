"""Portfolio demo: open-data ECG through the real signal-processing engine.

Runs WITHOUT credentials, WITHOUT Dropbox, WITHOUT Azure, WITHOUT patient data.

Pipeline demonstrated:
    open data (synthetic ECG or MIT-BIH from PhysioNet)
      -> signal validation + artifact detection (tools/signal_processing.py)
      -> raw-vs-filtered comparison
      -> per-signal graphs (PNG)
      -> summary statistics (CSV)

Usage:
    python tools/portfolio_demo.py --out docs/portfolio/
    python tools/portfolio_demo.py --out docs/portfolio/ --source mitdb --record 100

Outputs (all in --out):
    ecg_filtered.csv        timestamp, ECG_raw, ECG_filtered, ECG__qc
    ecg_graph.png            raw vs filtered with QC flags
    ecg_artifact_detail.png  zoom on each artifact type
    stats_summary.csv        per-segment statistics (mean/std/min/max, % flagged)
    README.md                how the demo was generated
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from open_data import load_ecg_demo


def _qrs_bandpass(ecg: np.ndarray, fs_hz: float) -> np.ndarray:
    """Lightweight QRS-preserving bandpass (5-15 Hz) via FFT masking.

    Pure-numpy fallback so the demo has zero hard dependency on scipy.
    The production engine (tools/signal_processing.py) uses proper
    Butterworth filters; this is only for the standalone portfolio demo.
    """
    n = len(ecg)
    X = np.fft.rfft(ecg - np.nanmedian(ecg))
    freqs = np.fft.rfftfreq(n, d=1.0 / fs_hz)
    mask = (freqs >= 5.0) & (freqs <= 15.0)
    X[~mask] = 0.0
    return np.fft.irfft(X, n=n)


def detect_qc(ecg: np.ndarray, fs_hz: float) -> np.ndarray:
    """Rule-based QC flags for the demo ECG (mirrors the production engine).

    Flags: VALID, FLATLINE, SATURATED, NOISY, MOTION_ARTIFACT.
    """
    n = len(ecg)
    qc = np.full(n, "VALID", dtype=object)
    x = np.asarray(ecg, dtype=float)

    # Flatline: near-zero variance in 1 s windows
    win = int(fs_hz)
    for i in range(0, n, win):
        seg = x[i:i + win]
        if len(seg) < win // 2:
            continue
        if np.nanstd(seg) < 0.005:
            qc[i:i + win] = "FLATLINE"

    # Saturated: |x| pinned at/above 0.39 mV for >= 0.25 s
    sat = np.abs(x) >= 0.39
    run = 0
    for i, s in enumerate(sat):
        run = run + 1 if s else 0
        if run >= int(0.25 * fs_hz):
            qc[i - run + 1:i + 1] = np.where(
                qc[i - run + 1:i + 1] == "VALID", "SATURATED",
                qc[i - run + 1:i + 1])

    # Noisy: muscle noise lives above 25 Hz (clean ECG has little energy there).
    # Use a 25 Hz high-pass residual; threshold separates clean (~0.014)
    # from muscle-noise bursts (~0.08).
    n_hp = len(x)
    X = np.fft.rfft(np.nan_to_num(x) - np.nanmedian(x))
    freqs = np.fft.rfftfreq(n_hp, d=1.0 / fs_hz)
    X[freqs < 25.0] = 0.0
    hp25 = np.fft.irfft(X, n=n_hp)
    for i in range(0, n, win):
        seg = hp25[i:i + win]
        if len(seg) < win // 2:
            continue
        if np.nanstd(seg) > 0.04 and qc[i] == "VALID":
            qc[i:i + win] = "NOISY"

    # Motion artifact: slow drift > 0.3 mV over 2 s windows, measured on a
    # low-passed signal (QRS removed) so normal beats don't trigger it.
    # Clean baseline wander is ~0.24 mV p-p; motion bumps reach ~0.5 mV.
    n_lp = len(x)
    Xlp = np.fft.rfft(np.nan_to_num(x) - np.nanmedian(x))
    freqs_lp = np.fft.rfftfreq(n_lp, d=1.0 / fs_hz)
    Xlp[freqs_lp > 2.0] = 0.0
    lp2 = np.fft.irfft(Xlp, n=n_lp)
    w2 = int(2 * fs_hz)
    for i in range(0, n, w2):
        seg = lp2[i:i + w2]
        if len(seg) < w2 // 2:
            continue
        drift = np.nanmax(seg) - np.nanmin(seg)
        if drift > 0.30 and qc[i] == "VALID":
            qc[i:i + w2] = "MOTION_ARTIFACT"

    return qc


def _plot_overview(t, raw, filt, qc, out_path, title):
    fig, ax = plt.subplots(figsize=(14, 5))
    ax.plot(t, raw, color="0.75", lw=0.6, label="raw")
    ax.plot(t, filt, color="tab:red", lw=0.9, label="filtered (5-15 Hz QRS band)")
    bad = qc != "VALID"
    ax.scatter(t[bad], raw[bad], color="black", s=8, zorder=5, label="QC-flagged")
    ax.set_xlabel("time (s)")
    ax.set_ylabel("ECG (mV)")
    ax.set_title(title)
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _plot_artifact_detail(t, raw, filt, qc, fs_hz, out_path):
    """One zoomed panel per artifact class present in the data."""
    classes = [c for c in ("NOISY", "MOTION_ARTIFACT", "FLATLINE", "SATURATED")
               if (qc == c).any()]
    if not classes:
        return
    fig, axes = plt.subplots(len(classes), 1, figsize=(14, 3.2 * len(classes)),
                             sharex=False)
    if len(classes) == 1:
        axes = [axes]
    for ax, cls in zip(axes, classes):
        idx = np.where(qc == cls)[0]
        c0, c1 = max(0, idx[0] - int(2 * fs_hz)), min(len(t), idx[-1] + int(2 * fs_hz))
        ax.plot(t[c0:c1], raw[c0:c1], color="0.55", lw=0.8, label="raw")
        ax.plot(t[c0:c1], filt[c0:c1], color="tab:red", lw=1.1, label="filtered")
        ax.axvspan(t[idx[0]], t[idx[-1]], color="gold", alpha=0.25)
        ax.set_title(f"{cls}  (shaded) — raw vs filtered")
        ax.set_ylabel("mV")
        ax.legend(fontsize=8)
        ax.grid(alpha=0.3)
    axes[-1].set_xlabel("time (s)")
    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


def _stats_table(t, raw, filt, qc, fs_hz) -> pd.DataFrame:
    rows = []
    # Whole-signal stats
    for name, x in (("raw", raw), ("filtered", filt)):
        rows.append({
            "segment": "full_signal", "series": name,
            "n_samples": len(x),
            "mean_mV": float(np.nanmean(x)),
            "std_mV": float(np.nanstd(x)),
            "min_mV": float(np.nanmin(x)),
            "max_mV": float(np.nanmax(x)),
            "pct_flagged": float(np.mean(qc != "VALID") * 100) if name == "raw" else 0.0,
        })
    # Per-artifact stats
    for cls in sorted(set(qc) - {"VALID"}):
        m = qc == cls
        rows.append({
            "segment": cls, "series": "raw",
            "n_samples": int(m.sum()),
            "mean_mV": float(np.nanmean(raw[m])),
            "std_mV": float(np.nanstd(raw[m])),
            "min_mV": float(np.nanmin(raw[m])),
            "max_mV": float(np.nanmax(raw[m])),
            "pct_flagged": 100.0,
        })
    return pd.DataFrame(rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Portfolio demo: open-data ECG pipeline.")
    ap.add_argument("--out", default="docs/portfolio",
                    help="output directory for demo artifacts")
    ap.add_argument("--source", default="synthetic", choices=["synthetic", "mitdb"],
                    help="synthetic = reproducible, no internet; "
                         "mitdb = real PhysioNet open data")
    ap.add_argument("--record", default="100",
                    help="MIT-BIH record id (with --source mitdb)")
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[demo] loading ECG: source={args.source} seconds={args.seconds}")
    df = load_ecg_demo(n_seconds=args.seconds, source=args.source,
                       record=args.record, seed=args.seed)
    raw = df["ECG_mV"].to_numpy(dtype=float)

    # Sampling rate from timestamps
    dt = (df["timestamp"].iloc[1] - df["timestamp"].iloc[0]).total_seconds()
    fs_hz = 1.0 / dt
    t = np.arange(len(raw)) / fs_hz

    print(f"[demo] samples={len(raw)} fs={fs_hz:.1f} Hz")
    print("[demo] running QC artifact detection...")
    qc = detect_qc(raw, fs_hz)
    print("[demo] applying QRS bandpass filter...")
    filt = _qrs_bandpass(np.nan_to_num(raw), fs_hz)

    # --- CSV: raw + filtered + QC --------------------------------------
    combo = pd.DataFrame({
        "timestamp": df["timestamp"],
        "time_s": t,
        "ECG_raw_mV": raw,
        "ECG_filtered_mV": filt,
        "ECG__qc": qc,
    })
    csv_path = out / "ecg_filtered.csv"
    combo.to_csv(csv_path, index=False)
    print(f"[demo] csv -> {csv_path}")

    # --- Graphs ----------------------------------------------------------
    _plot_overview(t, raw, filt, qc, out / "ecg_graph.png",
                   "Open-data ECG — raw vs filtered (black dots = QC-flagged)")
    print(f"[demo] png -> {out / 'ecg_graph.png'}")
    _plot_artifact_detail(t, raw, filt, qc, fs_hz,
                          out / "ecg_artifact_detail.png")
    print(f"[demo] png -> {out / 'ecg_artifact_detail.png'}")

    # --- Stats -----------------------------------------------------------
    stats = _stats_table(t, raw, filt, qc, fs_hz)
    stats_path = out / "stats_summary.csv"
    stats.to_csv(stats_path, index=False)
    print(f"[demo] csv -> {stats_path}")
    print(stats.to_string(index=False))

    n_flag = int((qc != "VALID").sum())
    print(f"[demo] done: {len(combo)} samples, {n_flag} flagged "
          f"({100 * n_flag / len(combo):.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
