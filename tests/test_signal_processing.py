"""Tests for the signal-specific processing engine (Phases 9-11)."""

import numpy as np
import pandas as pd

from signal_processing import (
    apply_filter,
    apply_validity,
    detect_artifacts,
    detect_flatline,
    detect_saturation,
    detect_spike,
    find_config,
    load_signal_configs,
    measure_fs_hz,
    process_frame,
    process_series,
)


def _cfg(**over):
    base = {
        "signal": "TEST",
        "status": "active",
        "validity": {"unit": "bpm", "range": [20, 250],
                     "zero_is_valid": False},
        "artifact_detection": [],
        "filter": {"method": "none"},
    }
    base.update(over)
    return base


def test_validity_range_flags_and_nans():
    s = pd.Series([70.0, 4000.0, 10.0, 80.0])
    _, flags = apply_validity(s, _cfg(), [])
    assert list(flags) == ["VALID", "INVALID_RANGE", "INVALID_RANGE", "VALID"]


def test_zero_invalid_becomes_invalid_range():
    s = pd.Series([70.0, 0.0])
    cleaned, flags = apply_validity(s, _cfg(), [])
    assert flags[1] == "INVALID_RANGE"
    assert np.isnan(cleaned.iloc[1])


def test_zero_valid_is_kept():
    cfg = _cfg(validity={"unit": "index", "range": [0, 100],
                         "zero_is_valid": True})
    cleaned, flags = apply_validity(pd.Series([0.0, 50.0]), cfg, [])
    assert list(flags) == ["VALID", "VALID"]
    assert cleaned.iloc[0] == 0.0


def test_missing_codes_become_missing():
    s = pd.Series([70.0, -1401.0, 80.0])
    cleaned, flags = apply_validity(s, _cfg(), [-1401])
    assert flags[1] == "MISSING"
    assert np.isnan(cleaned.iloc[1])


def test_flatline_detected():
    s = pd.Series(np.concatenate([np.linspace(70, 80, 50),
                                  np.full(100, 72.0)]))
    hit = detect_flatline(s, fs_hz=10.0, window_s=2.0)
    # Interior of the flat run is flagged; edges shorter than a full
    # centered window are conservatively left alone.
    assert hit[60:130].all()
    assert not hit[:40].any()


def test_spike_detected_not_threshold_alone():
    rng = np.random.default_rng(0)
    s = pd.Series(70 + rng.normal(0, 1, 300))
    s.iloc[150] = 4000.0  # artifact spike
    hit = detect_spike(s, fs_hz=10.0, window_s=10.0, n_sigma=5.0)
    assert hit[150]
    assert hit.sum() < 10  # only the spike, not the noise


def test_saturation_detected():
    s = pd.Series([1.0, 2.0, 5.0, 5.0, 5.0, 5.0, 5.0, 5.0, 3.0, 1.0])
    hit = detect_saturation(s, min_consecutive=5)
    assert hit[2:8].all()
    assert not hit[0]


def test_lowpass_reduces_high_frequency_noise():
    fs = 200.0
    t = np.arange(0, 5, 1 / fs)
    clean = np.sin(2 * np.pi * 1.0 * t)
    noisy = clean + 0.5 * np.sin(2 * np.pi * 60.0 * t)
    s = pd.Series(noisy)
    cfg = _cfg(filter={"method": "lowpass",
                       "params": {"high_hz": 25.0, "order": 4}})
    out = apply_filter(s, fs, cfg)
    # Residual high-frequency energy must drop substantially.
    resid_in = np.abs(noisy - clean).mean()
    resid_out = np.abs(out.to_numpy() - clean).mean()
    assert resid_out < resid_in * 0.5


def test_robust_smoothing_clips_spike_keeps_level():
    s = pd.Series([70.0] * 50 + [4000.0] + [70.0] * 50)
    cfg = _cfg(filter={"method": "robust_smoothing",
                       "params": {"window_s": 10.0, "n_sigma": 3.0}})
    out = apply_filter(s, fs_hz=1.0, cfg=cfg)
    assert out.iloc[50] < 100.0  # spike replaced by local median
    assert abs(out.iloc[0] - 70.0) < 1.0


def test_filter_never_resurrects_invalid_samples():
    s = pd.Series([70.0, 4000.0, 72.0, 71.0] * 25)
    cfg = _cfg(filter={"method": "robust_smoothing",
                       "params": {"window_s": 10.0, "n_sigma": 3.0}})
    filt, qc = process_series("HR", s, 1.0, cfg, [])
    assert filt.isna().sum() >= 25  # invalid samples stay NaN
    assert (qc == "INVALID_RANGE").sum() >= 25


def test_unknown_column_goes_to_review_queue_not_dropped():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10, freq="s"),
        "HR": [70.0] * 10,
        "MysterySignal": [1.0] * 10,
    })
    configs = {"HR": _cfg(signal="HR", channels=["HR"])}
    filt, qc, review = process_frame(df, "timestamp", configs, [],
                                     fs_overrides={"HR": 1.0},
                                     source="infinity")
    assert "MysterySignal" in filt.columns  # preserved, untouched
    assert any(e["column"] == "MysterySignal" and
               e["reason"] == "no_dictionary_entry" for e in review)
    assert not any(e["column"] == "HR" for e in review)


def test_duplicate_columns_each_processed():
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=20, freq="s"),
        "HR": [70.0] * 20,
        "HR.1": [4000.0] * 20,  # aberrant duplicate channel
    })
    configs = {"HR": _cfg(signal="HR", channels=["HR"])}
    filt, qc, review = process_frame(
        df, "timestamp", configs, [],
        fs_overrides={"HR": 1.0, "HR.1": 1.0}, source="bettercare")
    assert "HR" in filt.columns and "HR.1" in filt.columns  # both kept
    assert (qc["HR__qc"] == "VALID").all()
    assert (qc["HR.1__qc"] == "INVALID_RANGE").all()  # each judged alone
    assert review == []


def test_find_config_strips_duplicate_suffix():
    configs = {"HR": {"signal": "HR", "channels": ["HR"]}}
    assert find_config("HR.1", configs)["signal"] == "HR"
    assert find_config("HR.11", configs)["signal"] == "HR"
    assert find_config("Nope", configs) is None


def test_measure_fs_hz():
    idx = pd.DatetimeIndex(pd.date_range("2025-01-01", periods=100,
                                         freq="5ms"))
    s = pd.Series(np.ones(100), index=idx)
    assert abs(measure_fs_hz(idx, s) - 200.0) < 1.0


def test_load_signal_configs_reads_directory(tmp_path):
    (tmp_path / "hr.yaml").write_text(
        "signal: HR\nstatus: draft\nchannels: [HR]\n", encoding="utf-8")
    configs = load_signal_configs(tmp_path)
    assert configs["HR"]["channels"] == ["HR"]


def test_gap_flagged_not_silently_bridged():
    s = pd.Series([70.0] * 10 + [np.nan] * 30 + [70.0] * 10)
    cfg = _cfg()
    _, qc = process_series("HR", s, 1.0, cfg, [], max_gap_s=10.0)
    assert (qc == "GAP").sum() == 30
