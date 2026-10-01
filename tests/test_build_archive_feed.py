"""Tests for tools/build_archive_feed.py — the de-identified archive feed."""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.build_archive_feed import build_feed, K_ANONYMITY


def _digest(projects):
    return {"generated_utc": "20251001_1500", "projects": projects}


def _proj(n_files=10, **kw):
    p = {"n_parquet_files": n_files, "sampled": 5,
         "total_rows_sampled": 1_800_000, "schema_drift": 3,
         "n_unknown": 12,
         "missingness_kinds": {"complete": 8, "gappy": 2},
         "sampling": {"dev": {"nominal_hz_range": [200.0, 200.0]}},
         "signal_detail": {
             "HR": {"files_seen": 5, "mean_missing_frac": 0.12,
                    "worst_kind": "sparse_isolated"},
             "RARE": {"files_seen": 2, "mean_missing_frac": 0.0,
                      "worst_kind": "complete"},
         }}
    p.update(kw)
    return p


def test_small_project_suppressed():
    feed = build_feed(_digest({"TINY": _proj(n_files=3)}))
    e = feed["projects"]["TINY"]
    assert e["suppressed"] is True
    assert e["n_exams"] is None
    assert e["signals"] == {}


def test_rare_signal_dropped_below_k():
    feed = build_feed(_digest({"DEXREM": _proj()}))
    sigs = feed["projects"]["DEXREM"]["signals"]
    assert "HR" in sigs
    assert "RARE" not in sigs  # files_seen=2 < k=5


def test_hours_estimate_math():
    # 1.8M rows over 5 sampled files, HR in all 10 files -> all rows,
    # at 200 Hz -> 9000 s -> 2.5 h
    feed = build_feed(_digest({"DEXREM": _proj()}))
    hrs = feed["projects"]["DEXREM"]["signals"]["HR"]["total_hours"]
    assert hrs == pytest.approx(2.5)


def test_qc_verdicts_suppressed_counts():
    feed = build_feed(_digest({"DEXREM": _proj()}))
    qc = feed["projects"]["DEXREM"]["qc_verdicts"]
    assert qc["columns_complete"] == 8
    assert qc["columns_gappy"] is None  # 2 < k=5 -> suppressed
    assert qc["unknown_columns"] == 12


def test_feed_shape_matches_website_contract():
    feed = build_feed(_digest({"DEXREM": _proj()}))
    assert "generated_at" in feed
    assert feed["privacy"]["k_anonymity"] == K_ANONYMITY
    e = feed["projects"]["DEXREM"]
    for key in ("n_exams", "suppressed", "total_rows", "qc_verdicts",
                "ett_versions", "signals"):
        assert key in e
    s = e["signals"]["HR"]
    for key in ("n_exams", "mean_duration_s", "total_hours",
                "null_fraction"):
        assert key in s


def test_no_patient_level_content():
    feed = build_feed(_digest({"DEXREM": _proj()}))
    blob = json.dumps(feed).lower()
    for token in ("patient_id", "subject_id", "exam_guid", "session_guid",
                  "date_of_birth", "patient_code"):
        assert token not in blob
