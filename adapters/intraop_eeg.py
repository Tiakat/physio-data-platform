"""Adapter: intraoperative EEG (BIS Vista) via Tiakat/intraop-eeg-pipeline.

Reads the raw recording, runs ``eegpipe.process_recording`` (filtering,
artifact rejection, spectral + PAC metrics, phase summaries), and writes the
platform-standard outputs: signals.parquet, features.parquet, figures/*.png.

Requires: ``pip install "eegpipe @ git+https://github.com/Tiakat/intraop-eeg-pipeline.git"``
(see requirements.txt).

The clinical landmarks (anesthetic phases) come from a sidecar JSON next to
the raw file: ``<stem>.landmarks.json`` -> {"induction_s": ..., ...}.
If absent, phases default to a single "unknown" phase.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd


def _load_landmarks(raw_path: str) -> dict:
    sidecar = Path(raw_path).with_suffix("").with_suffix(".landmarks.json")
    # handle names like "patient01.csv" -> "patient01.landmarks.json"
    stem = Path(raw_path).name
    for suffix in (".csv", ".r2a", ".h_a", ".spa", ".edf"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    sidecar = Path(raw_path).parent / f"{stem}.landmarks.json"
    if sidecar.exists():
        return json.loads(sidecar.read_text())
    return {}


def _read_recording(raw_path: str, cfg) -> tuple[np.ndarray, dict]:
    """Return (eeg_uv [n_channels, n_samples], meta). Supports the BIS Vista
    binary export via eegpipe; falls back to CSV (t_s, ch1, ch2, ...)."""
    from eegpipe import bis_vista  # noqa

    p = Path(raw_path)
    if p.suffix in (".r2a",):
        raise NotImplementedError(
            "BIS Vista binary reading: wire eegpipe.bis_vista.read_export() "
            "to your export layout here")
    # CSV fallback: first column t_s, then one column per channel
    df = pd.read_csv(raw_path)
    t = df.iloc[:, 0].to_numpy()
    fs = float(cfg.sampling_rate_hz)
    chans = [c for c in df.columns[1:]]
    eeg = df[chans].to_numpy(dtype=float)  # (n_samples, n_channels)
    return eeg, {"t_s": t, "fs": fs, "channels": chans}


def process_patient(raw_path: str, cfg, out_dir: str, log) -> dict:
    from eegpipe.pipeline import process_recording
    from physio_platform import store

    eeg_uv, meta = _read_recording(raw_path, cfg)
    fs = meta["fs"]
    landmarks = _load_landmarks(raw_path)
    log.info("processing %s: %s samples @ %.1f Hz, landmarks=%s",
             raw_path, eeg_uv.shape[1], fs, sorted(landmarks) or "none")

    res = process_recording(eeg_uv, landmarks, fs=fs,
                            seed=cfg.processing.seed)

    pid = Path(raw_path).stem
    fp = cfg.fingerprint

    if cfg.outputs.write_clean_signals:
        t = np.arange(eeg_uv.shape[0]) / fs
        sig = pd.DataFrame({"t_s": t})
        for i, ch in enumerate(meta["channels"]):
            sig[ch] = res.cleaned[:, i]
        store.write_signals(cfg.outputs.root, cfg.name, fp, pid, sig)

    if cfg.outputs.write_features:
        feats = res.metrics.copy()
        feats["patient_id"] = pid
        store.write_features(cfg.outputs.root, cfg.name, fp, pid, feats)

    # figures: eegpipe's plotting helpers (each saves to a path)
    try:
        from eegpipe import plots
        figdir = Path(out_dir) / "figures"
        figdir.mkdir(exist_ok=True)
        plots.fig_overview(res, eeg_uv, str(figdir / "overview.png"))
        plots.fig_traces_by_phase(res, str(figdir / "traces_by_phase.png"))
        plots.fig_spectra(res, str(figdir / "spectra.png"))
        plots.fig_evolution(res, str(figdir / "evolution.png"))
        plots.fig_pac(res, str(figdir / "pac.png"))
    except Exception as e:  # figures are nice-to-have, not fatal
        log.warning("figure generation skipped: %s", e)

    return {"n_samples": int(eeg_uv.shape[0]),
            "duration_s": float(eeg_uv.shape[0] / fs),
            "n_channels": int(eeg_uv.shape[1]),
            "n_rejected_epochs": int((res.rejection["reject"]).sum())
            if "reject" in res.rejection else 0}
