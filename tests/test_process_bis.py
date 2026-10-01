"""Tests for the BIS Vista parser (tools/process_bis.py).

Uses synthetic files built to the Connor (2022) layout; no real patient
data needed.
"""

import numpy as np
import pandas as pd
import pytest

from process_bis import (
    R2A_UV_PER_STEP,
    pair_bis_files,
    process_pair,
    read_r2a,
    read_spa,
)


def _write_r2a(path, eeg_uv, fs_hz=128.0):
    raw = np.round(eeg_uv / R2A_UV_PER_STEP).astype("<i2")
    # channel-interleaved
    path.write_bytes(raw.tobytes(order="C"))


def _write_spa(path, n_rows=10):
    cols = [f"c{i}" for i in range(54)]
    names = list(cols)
    names[0] = "Time"
    names[11] = "BIS"
    names[25] = "BIS_L"
    names[39] = "BIS_R"
    names[35] = "BSR"
    names[36] = "SEF"
    names[37] = "MF"
    names[42] = "POW"
    names[43] = "EMG"
    names[48] = "SuppTime"
    lines = ["BIS Vista export", "|".join(names)]
    base = pd.Timestamp("2026-01-01 08:00:00", tz="UTC")
    for i in range(n_rows):
        row = ["0"] * 54
        row[0] = (base + pd.Timedelta(seconds=i)).isoformat()
        row[11] = "45.0"
        row[25] = "44.0"
        row[39] = "46.0"
        row[35] = "5.0" if i != 3 else "-1"  # missing -> NaN
        row[36] = "12.5"
        row[37] = "9.0"
        row[42] = "50.0"
        row[43] = "30.0"
        row[48] = "2.0"
        lines.append("|".join(row))
    path.write_text("\n".join(lines))


def test_r2a_scaling_and_shape(tmp_path):
    fs = 128.0
    t = np.arange(256) / fs
    ch1 = 100.0 * np.sin(2 * np.pi * 10 * t)   # 100 uV 10 Hz
    ch2 = 50.0 * np.ones_like(t)
    eeg = np.column_stack([ch1, ch2])
    p = tmp_path / "case.r2a"
    _write_r2a(p, eeg)
    df = read_r2a(p)
    assert list(df.columns) == ["t_s", "EEG1_uV", "EEG2_uV"]
    assert len(df) == 256
    # scaling: recovered amplitude within 1 LSB (~0.05 uV)
    assert abs(df["EEG1_uV"].abs().max() - 100.0) < 0.2
    assert abs(df["EEG2_uV"].mean() - 50.0) < 0.2
    assert abs(df["t_s"].iloc[-1] - 255 / fs) < 1e-9


def test_r2a_rejects_size_mismatch(tmp_path):
    p = tmp_path / "bad.r2a"
    p.write_bytes(b"\x00" * 10)  # 5 int16, not divisible by 2 channels
    with pytest.raises(ValueError, match="not divisible"):
        read_r2a(p, n_channels=2)


def test_r2a_rejects_wrong_endianness(tmp_path):
    # A smooth physiological signal written big-endian reads as violent
    # noise in little-endian -- the parser must refuse, not parse garbage.
    fs = 128.0
    t = np.arange(512) / fs
    eeg = np.column_stack([100.0 * np.sin(2 * np.pi * 10 * t),
                           50.0 * np.ones_like(t)])
    raw = np.round(eeg / R2A_UV_PER_STEP).astype(">i2")  # wrong order
    p = tmp_path / "swap.r2a"
    p.write_bytes(raw.tobytes(order="C"))
    with pytest.raises(ValueError, match="byte order"):
        read_r2a(p)


def test_spa_parsing_and_negative_to_nan(tmp_path):
    p = tmp_path / "case.spa"
    _write_spa(p, n_rows=10)
    df = read_spa(p)
    assert len(df) == 10
    assert df["BIS_1"].iloc[0] == 45.0
    assert df["BSR"].iloc[0] == 5.0
    assert np.isnan(df["BSR"].iloc[3])  # -1 -> NaN
    assert df["SEF"].iloc[0] == 12.5
    assert df["EMG"].iloc[0] == 30.0
    assert str(df["timestamp"].iloc[0]).startswith("2026-01-01 08:00")


def test_pairing_and_qc(tmp_path):
    d = tmp_path / "bis"
    d.mkdir()
    t = np.arange(1280) / 128.0  # 10 s
    eeg = np.column_stack([10 * np.sin(2 * np.pi * 10 * t),
                           np.zeros_like(t)])
    _write_r2a(d / "pt01.r2a", eeg)
    _write_spa(d / "pt01.spa", n_rows=10)
    pairs = pair_bis_files(d)
    assert set(pairs["pt01"].keys()) == {"r2a", "spa"}
    out = tmp_path / "out"
    qc = process_pair("pt01", pairs["pt01"]["r2a"], pairs["pt01"]["spa"],
                      out)
    assert (out / "pt01.eeg.parquet").exists()
    assert (out / "pt01.bis_params.parquet").exists()
    assert (out / "pt01.bis_qc.json").exists()
    # 10 s of EEG vs 10 spa rows: no duration mismatch note
    assert not any("duration mismatch" in n for n in qc["notes"])


def test_duration_mismatch_flagged(tmp_path):
    d = tmp_path / "bis"
    d.mkdir()
    t = np.arange(1280) / 128.0
    eeg = np.column_stack([np.zeros_like(t), np.zeros_like(t)])
    _write_r2a(d / "pt02.r2a", eeg)
    _write_spa(d / "pt02.spa", n_rows=60)  # 60 rows vs 10 s EEG
    qc = process_pair("pt02", str(d / "pt02.r2a"), str(d / "pt02.spa"),
                      tmp_path / "out")
    assert any("duration mismatch" in n for n in qc["notes"])
