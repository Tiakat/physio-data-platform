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


def test_empty_digest_guard_fires(monkeypatch, tmp_path):
    """Guard: if projects have parquet files but discovery yields nothing,
    the run must say so loudly (finding + flag) instead of writing a
    clean-looking empty digest. Regression for the 2026-10-01 run that
    exited success with an empty digest."""
    import tools.stage2_discovery as sd_mod

    def boom(account, code, files, ontology, sample_n=20):
        raise RuntimeError("simulated per-project discovery failure")

    monkeypatch.setattr(sd_mod, "discover_project", boom)

    (tmp_path / "profiles").mkdir()
    (tmp_path / "profiles" / "_variables.yaml").write_text(
        "HR:\n  label: Heart rate\n  unit: bpm\n  min: 20\n  max: 250\n",
        encoding="utf-8")

    state = {"legacy": {"DEXREM": {"files": {
        "a": {"status": "ok", "kind": "parquet",
              "stored": "DEXREM/parquet/infinity_ab12cd34.parquet.enc"}}}}}
    saved = {}

    def put_plain(name, data):
        saved[name] = data

    result = sd_mod.run_discovery(
        "acct", state, tmp_path,
        put_encrypted=lambda n, d: None,
        put_plain=put_plain,
        sample_n=5)

    assert result["empty_guard_fired"] is True
    assert result["digest"]["projects"] == {}
    assert any("no projects discovered" in line
               for line in result["issue_lines"])


def test_measure_timebase_detects_rate_and_gaps():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    import tools.stage2_discovery as sd_mod

    t = np.arange(0, 10, 0.005)  # 200 Hz elapsed seconds
    t = np.concatenate([t, [30.0, 30.005]])  # a real gap, then resume
    tb = sd_mod._measure_timebase(pd.DataFrame({"timestamp": t}), "timestamp")
    assert tb["nominal_hz"] == 200.0
    assert tb["gap_fraction"] > 0
    assert tb["total_gap_time_s"] > 15


def test_measure_timebase_epoch_ms_and_garbage():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    import tools.stage2_discovery as sd_mod

    t = (1_700_000_000_000 + np.arange(0, 60000, 1000)).astype(float)
    tb = sd_mod._measure_timebase(pd.DataFrame({"timestamp": t}), "timestamp")
    assert tb["nominal_hz"] == 1.0
    assert "epoch_ms" in tb["time_unit"]

    bad = sd_mod._measure_timebase(
        pd.DataFrame({"timestamp": ["a", "b", "c"]}), "timestamp")
    assert bad is None


def test_measure_missingness_taxonomy():
    np = pytest.importorskip("numpy")
    pd = pytest.importorskip("pandas")
    import tools.stage2_discovery as sd_mod

    n = 500
    df = pd.DataFrame({
        "complete": np.ones(n),
        "sparse": [np.nan if i % 100 == 0 else 1.0 for i in range(n)],
        "gappy": [np.nan if 10 <= i < 25 else 1.0 for i in range(n)],
        "gone": [np.nan] * n,
    })
    m = sd_mod._measure_missingness(
        df, ["complete", "sparse", "gappy", "gone"])
    assert m["complete"]["kind"] == "complete"
    assert m["sparse"]["kind"] == "sparse_isolated"
    assert m["gappy"]["kind"] == "gappy"
    assert m["gappy"]["longest_gap_run"] == 15
    assert m["gone"]["kind"] == "entirely_missing"


def test_discover_project_reports_sampling_and_missingness(monkeypatch):
    """Phases 6-7: the project report carries sampling + missingness, and
    mixed time bases within one device are surfaced."""
    np = pytest.importorskip("numpy")
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    import tools.stage2_discovery as sd_mod

    t = np.arange(0, 5, 0.005)  # 200 Hz
    table = pa.table({"timestamp": t,
                      "HR": np.where(np.arange(len(t)) % 200 == 0,
                                     np.nan, 72.0)})
    buf = io.BytesIO()
    pq.write_table(table, buf)
    payload = buf.getvalue()
    monkeypatch.setattr(sd_mod, "_download_parquet",
                        lambda account, blob: payload)

    files = {"one": {"status": "ok", "kind": "parquet",
                     "stored": "DEXREM/parquet/bettercare_ab12cd34.parquet.enc"}}
    rep = sd_mod.discover_project("acct", "DEXREM", files, ONTOLOGY,
                                  sample_n=5)
    assert rep["errors"] == []
    assert rep["sampling"]["DEXREM/parquet/bettercare"][
        "nominal_hz_range"] == [200.0, 200.0]
    assert (rep["missingness"]["HR"]["worst_kind"]
            == "sparse_isolated")
