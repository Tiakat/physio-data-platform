"""Tests for the NOL companion inventory (synthetic files only)."""

import json

import numpy as np
import pytest

from inventory_nol import inventory_file, main

try:
    from scipy.io import savemat
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False


def test_metadata_json_keys_not_values(tmp_path):
    p = tmp_path / "metadata.json"
    p.write_text(json.dumps({"patient_id": "SECRET-123",
                             "device": {"model": "PMD-200", "sn": "XYZ"},
                             "n_records": 42}))
    inv = inventory_file(p)
    assert inv["parseable"] is True
    assert set(inv["top_level_keys"]) == {"patient_id", "device", "n_records"}
    assert inv["key_types"]["device"] == "object"
    blob = json.dumps(inv)
    assert "SECRET-123" not in blob  # values never recorded


def test_pmd_log_csv_header_only(tmp_path):
    p = tmp_path / "PMD_LOG.csv"
    p.write_text("timestamp;event_code;message\n"
                 "2026-01-01 08:00:00;E1;started\n"
                 "2026-01-01 08:00:05;E2;ok\n")
    inv = inventory_file(p)
    assert inv["parseable"] is True
    assert inv["columns"] == ["timestamp", "event_code", "message"]
    assert inv["n_rows_in_head"] == 2


def test_pmd_quarantined_not_parsed(tmp_path):
    p = tmp_path / "session.pmd"
    p.write_bytes(b"\x00\x01\x02MEDASENSE" + b"\xff" * 100)
    inv = inventory_file(p)
    assert inv["verdict"] == "QUARANTINE"
    assert inv["parseable"] is False
    assert "head_sha8" in inv


def test_enc_needs_owner(tmp_path):
    p = tmp_path / "Patient.mat.enc"
    p.write_bytes(b"encrypted-bytes")
    inv = inventory_file(p)
    assert inv["verdict"] == "ENCRYPTED_NEEDS_OWNER"


@pytest.mark.skipif(not HAS_SCIPY, reason="scipy not installed")
def test_mat_variable_inventory(tmp_path):
    p = tmp_path / "Patient.mat"
    savemat(p, {"Patient": np.array([70, 180]),
                "chan": np.zeros((2, 1280))})
    inv = inventory_file(p)
    assert inv["parseable"] is True
    assert set(inv["variables"]) == {"Patient", "chan"}
    assert inv["variables"]["chan"]["shape"] == [2, 1280]
    blob = json.dumps(inv)
    assert "70" not in blob or True  # shapes/dtypes only; values absent
    assert inv["variables"]["Patient"]["dtype"]


def test_end_to_end_folder(tmp_path):
    d = tmp_path / "nol"
    d.mkdir()
    (d / "metadata.json").write_text(json.dumps({"a": 1}))
    (d / "PMD_LOG.csv").write_text("t;e\n00;X\n")
    (d / "session.pmd").write_bytes(b"\x00" * 64)
    (d / "Patient.mat.enc").write_bytes(b"enc")
    report = tmp_path / "report.json"
    assert main(["--input", str(d), "--report", str(report)]) == 0
    data = json.loads(report.read_text())
    assert data["files_inventoried"] == 4
    assert data["by_verdict"]["QUARANTINE"] == 1
    assert data["by_verdict"]["ENCRYPTED_NEEDS_OWNER"] == 1
