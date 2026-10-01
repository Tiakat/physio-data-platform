"""Tests for tools/review_dictionary.py pure analysis functions."""

import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from review_dictionary import (  # noqa: E402
    is_identifier_candidate,
    summarize_coverage,
    find_identifier_candidates,
    summarize_unmapped,
    summarize_duplicates,
    summarize_single_patient,
    build_markdown,
)


def test_identifier_candidate_patient_id():
    assert is_identifier_candidate("PatientID") == "patientid"


def test_identifier_candidate_dob():
    assert is_identifier_candidate("DateOfBirth") == "dateofbirth"


def test_identifier_candidate_negative():
    assert is_identifier_candidate("HR") is None
    assert is_identifier_candidate("ART M (mm(hg)^^ISO+)") is None
    assert is_identifier_candidate("SpO2") is None


def test_identifier_candidate_bare_patient_token():
    # Stage 2 flags the bare "patient" token; the review must not miss it.
    assert is_identifier_candidate("patient") == "patient"
    assert is_identifier_candidate("Patient") == "patient"


def test_identifier_candidate_case_insensitive():
    # Either the bare token or the joined form may match first; the flag is
    # what matters, not which spelling of it.
    assert is_identifier_candidate("patient_id") in {"patient", "patientid"}


def test_find_identifier_candidates_sorted():
    rows = [
        {"project": "B", "source": "s", "original_column": "MRN",
         "available_patients": "3"},
        {"project": "A", "source": "s", "original_column": "HR",
         "available_patients": "3"},
        {"project": "A", "source": "s", "original_column": "PatientName",
         "available_patients": "2"},
    ]
    out = find_identifier_candidates(rows)
    assert [r["original_column"] for r in out] == ["PatientName", "MRN"]


def test_summarize_unmapped_counts_and_examples():
    rows = [
        {"project": "P", "source": "s", "original_column": "mystery1",
         "canonical_variable": ""},
        {"project": "P", "source": "s", "original_column": "mystery2",
         "canonical_variable": ""},
        {"project": "P", "source": "s", "original_column": "HR",
         "canonical_variable": "HR"},
    ]
    out = summarize_unmapped(rows)
    assert len(out) == 1
    assert out[0]["n_unmapped"] == 2
    assert set(out[0]["examples"]) == {"mystery1", "mystery2"}


def test_summarize_duplicates_top_n():
    rows = [
        {"project": "P", "source": "s", "original_column": "HR",
         "n_files_with_duplicates": "100", "max_duplicates_in_one_file": "12"},
        {"project": "P", "source": "s", "original_column": "RESP",
         "n_files_with_duplicates": "50", "max_duplicates_in_one_file": "4"},
    ]
    out = summarize_duplicates(rows, top=1)
    assert len(out) == 1
    assert out[0]["original_column"] == "HR"


def test_summarize_single_patient():
    rows = [
        {"project": "P", "source": "s", "original_column": "weird",
         "available_patients": "1"},
        {"project": "P", "source": "s", "original_column": "HR",
         "available_patients": "9"},
    ]
    out = summarize_single_patient(rows)
    assert out == [{"project": "P", "source": "s",
                    "n_single_patient_columns": 1}]


def test_summarize_coverage_parses_ints():
    rows = [{"project": "P", "source": "s", "n_files_scanned": "10",
             "n_patients": "3", "n_union_columns": "54",
             "n_stable_columns": "40", "schema_stability": "variable"}]
    out = summarize_coverage(rows)
    assert out[0]["n_files"] == 10
    assert out[0]["stability"] == "variable"


def test_build_markdown_no_tokens_or_paths():
    md = build_markdown(
        "20261001_1500", "recon/schemas_x",
        [{"project": "P", "source": "s", "n_files": 1, "n_patients": 1,
          "n_union_columns": 2, "n_stable_columns": 2, "stability": "stable"}],
        [], [], [], [], 2)
    assert "pt_" not in md
    assert "review" in md.lower()
