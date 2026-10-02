"""Tests for the signal-specific processing engine (Phases 9-11).

The dictionary (profiles/_variables.yaml) is the single source of truth for
ranges, zero semantics and aliases; signal configs declare behaviour only.
"""

import numpy as np
import pandas as pd
import pytest
import yaml

from signal_processing import (
    apply_filter,
    apply_validity,
    detect_flatline,
    detect_saturation,
    detect_spike,
    find_config,
    get_var_spec,
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


@pytest.fixture()
def dictionary(tmp_path):
    """A minimal master dictionary with the Infinity alias from the field."""
    variables = {
        "variables": {
            "HR": {"label": "Heart rate", "unit": "bpm", "min": 20,
                   "max": 250, "zero_is_valid": False,
                   "aliases": ["HR (/min^^ISO+)"]},
            "ART_MEAN": {"label": "Arterial mean", "unit": "mmHg",
                         "min": 40, "max": 160, "zero_is_valid": False,
                         "aliases": ["ART M (mm(hg)^^ISO+)", "ART M"]},
        },
        "global_missing_codes": [-1401],
    }
    p = tmp_path / "_variables.yaml"
    p.write_text(yaml.safe_dump(variables), encoding="utf-8")
    return p


@pytest.fixture()
def configs_and_index(tmp_path, dictionary):
    (tmp_path / "hr.yaml").write_text(
        "signal: HR\nstatus: draft\nchannels: [HR]\n"
        "filter: {method: none}\n",
        encoding="utf-8")
    (tmp_path / "pressure_params.yaml").write_text(
        "signal: PRESSURE_PARAMS\nstatus: draft\n"
        "channels: [ART_MEAN]\n"
        "filter: {method: robust_smoothing, params: {window_s: 10.0}}\n",
        encoding="utf-8")
    return load_signal_configs(str(tmp_path), str(dictionary))


# ---- validity: dictionary is the authority --------------------------------

def test_validity_range_flags_and_nans():
    s = pd.Series([70.0, 4000.0, 10.0, 80.0])
    _, flags = apply_validity(s, _cfg(), None, [])
    assert list(flags) == ["VALID", "INVALID_RANGE", "INVALID_RANGE", "VALID"]


def test_zero_invalid_becomes_invalid_range():
    s = pd.Series([70.0, 0.0])
    cleaned, flags = apply_validity(s, _cfg(), None, [])
    assert flags[1] == "INVALID_RANGE"
    assert np.isnan(cleaned.iloc[1])


def test_zero_valid_is_kept():
    cfg = _cfg(validity={"unit": "index", "zero_is_valid": True})
    cleaned, flags = apply_validity(pd.Series([0.0, 50.0]), cfg, None, [])
    assert list(flags) == ["VALID", "VALID"]
    assert cleaned.iloc[0] == 0.0


def test_dictionary_spec_drives_validity_not_yaml():
    # YAML says 20-250; dictionary says 40-160 for ART_MEAN.
    spec = {"min": 40, "max": 160, "zero_is_valid": False}
    s = pd.Series([100.0, 200.0])
    _, flags = apply_validity(s, _cfg(), spec, [])
    assert list(flags) == ["VALID", "INVALID_RANGE"]


def test_missing_codes_become_missing():
    s = pd.Series([70.0, -1401.0, 80.0])
    cleaned, flags = apply_validity(s, _cfg(), None, [-1401])
    assert flags[1] == "MISSING"
    assert np.isnan(cleaned.iloc[1])


# ---- alias resolution ------------------------------------------------------

def test_infinity_unit_embedded_name_resolves(configs_and_index):
    configs, index = configs_and_index
    cfg, canon = find_config("ART M (mm(hg)^^ISO+)", configs, index)
    assert cfg["signal"] == "PRESSURE_PARAMS"
    assert canon == "ART_MEAN"


def test_alias_unit_stripped_fallback(configs_and_index):
    configs, index = configs_and_index
    # An unseen unit variant still resolves through the stripped form.
    cfg, canon = find_config("ART M (mmHg)", configs, index)
    assert canon == "ART_MEAN"


def test_find_config_strips_duplicate_suffix(configs_and_index):
    configs, index = configs_and_index
    cfg, canon = find_config("HR.1", configs, index)
    assert cfg["signal"] == "HR" and canon == "HR"
    cfg, canon = find_config("HR.11", configs, index)
    assert cfg["signal"] == "HR"
    assert find_config("Nope", configs, index) == (None, None)


def test_get_var_spec_returns_dictionary_entry(configs_and_index):
    _, index = configs_and_index
    spec = get_var_spec("ART_MEAN", index)
    assert spec["min"] == 40 and spec["max"] == 160
    assert get_var_spec("NOPE", index) is None


def test_range_conflict_reported_dictionary_wins(tmp_path, dictionary):
    (tmp_path / "hr.yaml").write_text(
        "signal: HR\nstatus: draft\nchannels: [HR]\n"
        "validity: {range: [0, 999]}\n",
        encoding="utf-8")
    _, index = load_signal_configs(str(tmp_path), str(dictionary))
    assert len(index["conflicts"]) == 1
    assert index["conflicts"][0]["dictionary_range"] == [20, 250]


def test_unimplemented_detector_reported_at_load(tmp_path, dictionary):
    (tmp_path / "x.yaml").write_text(
        "signal: X\nstatus: draft\nchannels: [HR]\n"
        "artifact_detection:\n"
        "  - {name: flush_artifact, method: square wave}\n",
        encoding="utf-8")
    _, index = load_signal_configs(str(tmp_path), str(dictionary))
    assert index["unimplemented_detectors"][0]["detector"] == "flush_artifact"


def test_unimplemented_filter_reported_at_load(tmp_path, dictionary):
    (tmp_path / "x.yaml").write_text(
        "signal: X\nstatus: draft\nchannels: [HR]\n"
        "filter: {method: wavelet_denoise}\n",
        encoding="utf-8")
    _, index = load_signal_configs(str(tmp_path), str(dictionary))
    assert index["unimplemented_filters"][0]["method"] == "wavelet_denoise"


# ---- detectors / filters (unchanged behaviour) ------------------------------

def test_flatline_detected():
    s = pd.Series(np.concatenate([np.linspace(70, 80, 50),
                                  np.full(100, 72.0)]))
    hit = detect_flatline(s, fs_hz=10.0, window_s=2.0)
    assert hit[60:130].all()
    assert not hit[:40].any()


def test_spike_detected_not_threshold_alone():
    rng = np.random.default_rng(0)
    s = pd.Series(70 + rng.normal(0, 1, 300))
    s.iloc[150] = 4000.0
    hit = detect_spike(s, fs_hz=10.0, window_s=10.0, n_sigma=5.0)
    assert hit[150]
    assert hit.sum() < 10


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
    resid_in = np.abs(noisy - clean).mean()
    resid_out = np.abs(out.to_numpy() - clean).mean()
    assert resid_out < resid_in * 0.5


def test_robust_smoothing_clips_spike_keeps_level():
    s = pd.Series([70.0] * 50 + [4000.0] + [70.0] * 50)
    cfg = _cfg(filter={"method": "robust_smoothing",
                       "params": {"window_s": 10.0, "n_sigma": 3.0}})
    out = apply_filter(s, fs_hz=1.0, cfg=cfg)
    assert out.iloc[50] < 100.0
    assert abs(out.iloc[0] - 70.0) < 1.0


def test_filter_never_resurrects_invalid_samples():
    s = pd.Series([70.0] * 50 + [4000.0] + [70.0] * 50)
    cfg = _cfg(filter={"method": "robust_smoothing",
                       "params": {"window_s": 10.0, "n_sigma": 3.0}})
    filt, qc = process_series("HR", s, 1.0, cfg, None, [])
    assert filt.isna().sum() >= 1
    assert (qc == "INVALID_RANGE").sum() >= 1


# ---- frame-level behaviour --------------------------------------------------

def test_unknown_column_goes_to_review_queue_not_dropped(configs_and_index):
    configs, index = configs_and_index
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=10, freq="s"),
        "HR": [70.0] * 10,
        "MysterySignal": [1.0] * 10,
    })
    filt, qc, review = process_frame(df, "timestamp", configs, index, [],
                                     fs_overrides={"HR": 1.0},
                                     source="infinity")
    assert "MysterySignal" in filt.columns
    assert (qc["MysterySignal__qc"] == "UNREVIEWED").all()
    assert any(e["column"] == "MysterySignal" and
               e["reason"] == "no_dictionary_entry" for e in review)
    assert not any(e["column"] == "HR" for e in review)


def test_duplicate_columns_each_processed(configs_and_index):
    configs, index = configs_and_index
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=20, freq="s"),
        "HR": [70.0] * 20,
        "HR.1": [4000.0] * 20,
    })
    filt, qc, review = process_frame(
        df, "timestamp", configs, index, [],
        fs_overrides={"HR": 1.0, "HR.1": 1.0}, source="bettercare")
    assert "HR" in filt.columns and "HR.1" in filt.columns
    assert (qc["HR__qc"] == "VALID").all()
    assert (qc["HR.1__qc"] == "INVALID_RANGE").all()
    assert review == []


def test_explicit_time_index_measures_true_fs(configs_and_index):
    # Regression: the driver once dropped the time column and measured
    # fs=1e9 Hz from the row index. An explicit time base must win.
    configs, index = configs_and_index
    tidx = pd.DatetimeIndex(pd.date_range("2025-01-01", periods=50,
                                          freq="s"))
    df = pd.DataFrame({"HR": [70.0] * 50})  # no time column at all
    filt, qc, review = process_frame(df, None, configs, index, [],
                                     source="infinity", time_index=tidx)
    assert review == []
    assert (qc["HR__qc"] == "VALID").all()


def test_missing_time_col_warns_loudly(configs_and_index, capsys):
    configs, index = configs_and_index
    df = pd.DataFrame({"HR": [70.0] * 10})
    process_frame(df, "nope", configs, index, [], fs_overrides={"HR": 1.0})
    assert "not found in columns" in capsys.readouterr().out


def test_waveform_without_dictionary_entry_flagged_fallback(tmp_path,
                                                            dictionary):
    (tmp_path / "ecg.yaml").write_text(
        "signal: ECG\nstatus: draft\nchannels: [ECG]\n"
        "validity: {range: [-5, 5]}\nfilter: {method: none}\n",
        encoding="utf-8")
    configs, index = load_signal_configs(str(tmp_path), str(dictionary))
    assert index["unresolved_channels"][0]["channel"] == "ECG"
    df = pd.DataFrame({
        "timestamp": pd.date_range("2025-01-01", periods=20, freq="5ms"),
        "ECG": [0.5] * 20,
    })
    _, qc, _ = process_frame(df, "timestamp", configs, index, [],
                             fs_overrides={"ECG": 200.0})
    # Config fallback range used, but never reported as clean VALID.
    assert (qc["ECG__qc"] == "LOW_QUALITY").all()


def test_measure_fs_hz():
    idx = pd.DatetimeIndex(pd.date_range("2025-01-01", periods=100,
                                         freq="5ms"))
    s = pd.Series(np.ones(100), index=idx)
    assert abs(measure_fs_hz(idx, s) - 200.0) < 1.0


def test_gap_flagged_not_silently_bridged():
    s = pd.Series([70.0] * 10 + [np.nan] * 30 + [70.0] * 10)
    _, qc = process_series("HR", s, 1.0, _cfg(), None, [], max_gap_s=10.0)
    assert (qc == "GAP").sum() == 30


def test_nyquist_invalid_bandpass_is_noop():
    # Regression: bandpass with cutoffs above Nyquist must not crash
    # (scipy raises "Filter not stable"); it becomes a no-op.
    from signal_processing import _butter_filter
    x = np.sin(np.linspace(0, 10, 500))
    y = _butter_filter(x, fs_hz=1.0, low_hz=0.5, high_hz=40.0,
                       kind="bandpass")
    assert np.allclose(y, x, equal_nan=True)


def test_nyquist_invalid_highpass_is_noop():
    from signal_processing import _butter_filter
    x = np.sin(np.linspace(0, 10, 500))
    y = _butter_filter(x, fs_hz=1.0, low_hz=5.0, kind="highpass")
    assert np.allclose(y, x, equal_nan=True)
