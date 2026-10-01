"""Tests for the value-pattern identifier scan (synthetic data only)."""

import json

import pandas as pd
import pytest

from scan_identifiers import main, scan_series


def test_detects_email_phone_and_id(tmp_path):
    s = pd.Series(["nurse@example.com", "call +1 514-555-0123",
                   "record 987654321", "plain note"])
    findings = scan_series("notes", s)
    patterns = {f["pattern"] for f in findings}
    assert "email" in patterns
    assert "phone" in patterns
    assert "long_digit_string" in patterns
    # no values leak into the findings
    blob = json.dumps(findings)
    assert "nurse@example.com" not in blob
    assert "987654321" not in blob


def test_clean_column_has_no_findings():
    s = pd.Series(["70.1", "71.2", "69.8", "70.5"])
    assert scan_series("HR", s) == []


def test_numeric_columns_are_not_scanned_for_text():
    s = pd.Series([70.1, 71.2, 69.8])
    assert scan_series("HR", s) == []


def test_end_to_end_report_has_no_values(tmp_path):
    df = pd.DataFrame({
        "HR": [70.0, 71.0],
        "notes": ["patient John Smith stable", "routine"],
    })
    p = tmp_path / "sample.parquet"
    df.to_parquet(p, index=False)
    report = tmp_path / "report.json"
    rc = main(["--input", str(p), "--report", str(report)])
    assert rc == 2  # findings present
    text = report.read_text()
    assert "John Smith" not in text
    assert "sample.parquet" not in text  # file token only
    data = json.loads(text)
    assert data["total_findings"] >= 1
    assert data["results"][0]["file_token"]


def test_clean_file_exits_zero(tmp_path):
    df = pd.DataFrame({"HR": [70.0, 71.0], "SpO2": [98.0, 99.0]})
    p = tmp_path / "clean.parquet"
    df.to_parquet(p, index=False)
    report = tmp_path / "report.json"
    assert main(["--input", str(p), "--report", str(report)]) == 0
