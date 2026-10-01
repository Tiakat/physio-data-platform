"""Pure-logic tests for tools/stage2_discovery.py (no Azure, no Dropbox)."""

import io
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Stub heavy deps: discovery imports azure lazily inside functions, but the
# module top level must import cleanly.
for name in ["dropbox"]:
    sys.modules.setdefault(name, types.ModuleType(name))

from tools.stage2_discovery import (  # noqa: E402
    _device_of,
    assess_linkage,
    classify_column,
)

ONTOLOGY = {
    "HR": {"label": "Heart rate", "unit": "bpm", "min": 20, "max": 250},
    "ART_SYS": {"label": "Arterial systolic", "unit": "mmHg",
                "min": 40, "max": 300},
}


def test_signal_from_ontology():
    cls, detail = classify_column("HR", ONTOLOGY)
    assert cls == "signal" and detail["unit"] == "bpm"


def test_identifier_tokens():
    for col in ["patient_id", "subject_guid", "mrn_number", "DOB"]:
        cls, _ = classify_column(col, ONTOLOGY)
        assert cls == "identifier", col


def test_identifier_token_boundaries():
    # "filename" contains "name" but is one token: must NOT match.
    assert classify_column("filename", ONTOLOGY)[0] != "identifier"
    assert classify_column("valid", ONTOLOGY)[0] != "identifier"


def test_demographic_metadata_qc_derived():
    assert classify_column("Age", ONTOLOGY)[0] == "demographic"
    assert classify_column("device", ONTOLOGY)[0] == "metadata"
    assert classify_column("qc_flag", ONTOLOGY)[0] == "qc"
    assert classify_column("hr_filt", ONTOLOGY)[0] == "derived"


def test_timestamp_and_unknown():
    assert classify_column("timestamp", ONTOLOGY)[0] == "timestamp"
    assert classify_column("PLETH_SOMETHING_WEIRD", ONTOLOGY)[0] == "unknown"


def test_ontology_beats_heuristic():
    # If HR were also a token somewhere, the curated ontology still wins.
    cls, _ = classify_column("HR", {**ONTOLOGY,
                                    "HR": {"unit": "bpm"}})
    assert cls == "signal"


def test_device_of_blob_name():
    assert _device_of("infinity_abc12345.parquet.enc") == "infinity"
    assert _device_of("bettercare_def67890.parquet.enc") == "bettercare"


def test_linkage_assessment_flags_identifiers():
    rep = {"by_class": {"signal": 10, "identifier": 2}}
    link = assess_linkage(rep)
    assert link["finding"] is not None
    rep2 = {"by_class": {"signal": 10}}
    assert assess_linkage(rep2)["finding"] is None


if __name__ == "__main__":
    fns = [(n, f) for n, f in sorted(globals().items())
           if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in fns:
        try:
            fn()
            print(f"PASS {name}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL {name}: {exc}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)


def test_discover_project_survives_raw_channel_names(monkeypatch):
    """Regression: ParquetSchema (pq.ParquetFile.schema) has no .field()
    method, so the old code raised AttributeError on the first column of
    every file; the half-recorded column then made
    columns['ECG I']['files_seen_in'] raise KeyError('ECG I'), which
    escaped the per-file handler and killed discovery for all 6 projects
    in the first production run (2026-10-01)."""
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    import tools.stage2_discovery as sd_mod

    table = pa.table({"ECG I": [1.0, 2.0, 3.0],
                      "HR": [70.0, 71.0, 72.0]})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    payload = buf.getvalue()
    monkeypatch.setattr(sd_mod, "_download_parquet",
                        lambda account, blob: payload)

    files = {"one": {"status": "ok", "kind": "parquet",
                     "stored": "DEXREM/parquet/infinity_ab12cd34.parquet.enc"}}
    rep = sd_mod.discover_project("acct", "DEXREM", files, ONTOLOGY,
                                  sample_n=5)

    assert rep["sampled"] == 1
    assert rep["errors"] == []
    assert rep["n_columns"] == 2
    assert rep["columns"]["ECG I"]["class"] == "unknown"
    assert rep["columns"]["ECG I"]["files_seen_in"] == 1
    assert rep["columns"]["HR"]["class"] == "signal"
    assert rep["columns"]["HR"]["detail"]["unit"] == "bpm"
