"""Filter demo: synthetic HR + SpO2 through the real processing engine.

SYNTHETIC DATA ONLY -- no patient data. Demonstrates exactly what the
real-data output looks like: filtered CSV (raw + filtered + QC flags)
and a raw-vs-filtered graph.

Usage:
  python tools/demo_filter.py --repo-root . --out demo_out/

Outputs:
  demo_out/hr_spo2_filtered.csv   timestamp, HR_raw, HR_filtered, HR__qc,
                                  SPO2_raw, SPO2_filtered, SPO2__qc
  demo_out/hr_spo2_graph.png      raw vs filtered, QC flags shaded
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
from signal_processing import load_signal_configs, process_frame


def synth_data(n=300, seed=7):
    rng = np.random.default_rng(seed)
    t = pd.date_range("2026-01-01 08:00:00", periods=n, freq="1s")

    # HR: ~72 bpm with respiratory-ish wobble
    hr = 72 + 3 * np.sin(np.arange(n) / 12.0) + rng.normal(0, 1.2, n)
    hr[40:46] = 300.0            # out-of-range burst -> INVALID_RANGE
    hr[120:135] = 72.0           # flatline -> FLATLINE
    hr[200:215] = np.nan         # 15 s dropout -> GAP
    hr[250] = 4000.0             # single wild spike -> INVALID_RANGE

    # SpO2: ~98% with small noise
    spo2 = 98 + rng.normal(0, 0.4, n)
    spo2[90:100] = 0.0           # probe-off -> INVALID_RANGE (0 invalid)
    spo2[160:166] = np.nan       # short gap
    spo2[230:240] = spo2[230:240] - 8.0  # sudden desat-like drop

    return pd.DataFrame({"timestamp": t, "HR": hr, "SPO2": spo2})


def main(argv=None):
    ap = argparse.ArgumentParser(description="Synthetic HR/SpO2 filter demo.")
    ap.add_argument("--repo-root", default=".",
                    help="repo checkout (configs/, profiles/)")
    ap.add_argument("--out", default="demo_out", help="output directory")
    args = ap.parse_args(argv)

    root = Path(args.repo_root)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    configs, index = load_signal_configs(
        root / "configs" / "signals", root / "profiles" / "_variables.yaml")
    missing_codes = list(index.get("global_missing_codes", []))
    # Inspector transparency: surface everything the engine reports.
    for w in index["unimplemented_detectors"]:
        print(f"[warn] detector {w['detector']!r} declared by {w['config']} "
              f"is NOT implemented -- skipping.")
    for w in index["unimplemented_filters"]:
        print(f"[warn] filter {w['method']!r} declared by {w['config']} "
              f"is NOT implemented -- no filtering.")
    for w in index["conflicts"]:
        print(f"[warn] range conflict on {w['channel']}: config "
              f"{w['config_range']} vs dictionary {w['dictionary_range']} "
              f"-- dictionary wins.")
    drafts = [s for s, c in configs.items() if c.get("status") == "draft"]
    if drafts:
        print(f"[warn] draft configs in use: {drafts} -- intervals "
              f"await clinical review.")
    # Keep only HR + SPO2 for the demo.
    configs = {k: v for k, v in configs.items()
               if (v.get("signal") or k).upper() in ("HR", "SPO2")}

    df = synth_data()
    filtered, qc, review = process_frame(
        df, time_col="timestamp", configs=configs, index=index,
        missing_codes=missing_codes, fs_overrides={"HR": 1.0, "SPO2": 1.0},
        source="demo_synthetic")

    combo = pd.DataFrame({
        "timestamp": filtered["timestamp"],
        "HR_raw": df["HR"].to_numpy(),
        "HR_filtered": filtered["HR"].to_numpy(),
        "HR__qc": qc["HR__qc"].to_numpy(),
        "SPO2_raw": df["SPO2"].to_numpy(),
        "SPO2_filtered": filtered["SPO2"].to_numpy(),
        "SPO2__qc": qc["SPO2__qc"].to_numpy(),
    })
    csv_path = out / "hr_spo2_filtered.csv"
    combo.to_csv(csv_path, index=False)

    # --- graph ---------------------------------------------------------
    fig, axes = plt.subplots(2, 1, figsize=(12, 7), sharex=True)
    for i, (col, unit, color) in enumerate((("HR", "bpm", "tab:red"),
                                          ("SPO2", "%", "tab:blue"))):
        ax = axes[i]
        ax.plot(combo["timestamp"], combo[f"{col}_raw"], color="0.7",
                lw=0.8, label=f"{col} raw")
        ax.plot(combo["timestamp"], combo[f"{col}_filtered"], color=color,
                lw=1.4, label=f"{col} filtered")
        bad = combo[f"{col}__qc"] != "VALID"
        ax.scatter(combo["timestamp"][bad], combo[f"{col}_raw"][bad],
                   color="black", s=14, zorder=5, label="flagged")
        ax.set_ylabel(f"{col} ({unit})")
        ax.legend(loc="upper right", fontsize=8)
        ax.grid(alpha=0.3)
    axes[0].set_title("SYNTHETIC demo — HR and SpO2: raw vs filtered "
                      "(black dots = QC-flagged samples)")
    axes[1].set_xlabel("time")
    fig.autofmt_xdate()
    fig.tight_layout()
    png_path = out / "hr_spo2_graph.png"
    fig.savefig(png_path, dpi=110)
    plt.close(fig)

    n_flag_hr = int((combo["HR__qc"] != "VALID").sum())
    n_flag_spo2 = int((combo["SPO2__qc"] != "VALID").sum())
    print(f"[demo] rows={len(combo)} flagged HR={n_flag_hr} "
          f"SPO2={n_flag_spo2} review_items={len(review)}")
    print(f"[demo] csv -> {csv_path}")
    print(f"[demo] png -> {png_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
