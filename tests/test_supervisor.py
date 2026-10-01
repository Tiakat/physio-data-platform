"""Tests for the supervisor (stage: oversight, not processing).

Covers: template comparison (missing/unexpected), participant-number
folders, layout-note for non-template data_roots, path redaction
(de-identification), digest shape, run classification.
Pure logic — no Dropbox/Azure/network.
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub the dropbox SDK (not installed here; tests never touch the network).
_dropbox = types.ModuleType("dropbox")
_files = types.ModuleType("dropbox.files")
_exceptions = types.ModuleType("dropbox.exceptions")


class _FolderMetadata:
    pass


class _FileMetadata:
    pass


_files.FolderMetadata = _FolderMetadata
_files.FileMetadata = _FileMetadata
_dropbox.files = _files
_dropbox.exceptions = _exceptions
sys.modules["dropbox"] = _dropbox
sys.modules["dropbox.files"] = _files
sys.modules["dropbox.exceptions"] = _exceptions

from tools.supervisor import (  # noqa: E402
    audit_project,
    build_digest,
    classify_run,
    redact_path,
)


def _tree(*folders):
    return {f: {"name": f, "files": 0} for f in folders}


def test_template_conformant_project_has_no_findings():
    tree = _tree(
        "database", "documents",
        "database/rawdata", "database/rawdata/12",
        "database/photos",
        "database/extracteddata", "database/extracteddata/nol",
        "database/analyzeddata", "database/analyzeddata/12",
        "documents/soumission ethique",
    )
    f = audit_project(tree, ["Database/RawData"])
    assert f["missing"] == [], f["missing"]
    assert f["unexpected"] == [], f["unexpected"]
    assert f["layout_note"] == ""


def test_missing_sections_are_reported():
    tree = _tree("database", "database/rawdata", "database/rawdata/3")
    f = audit_project(tree, ["Database/RawData"])
    assert "documents/" in f["missing"]
    assert "database/photos/" in f["missing"]


def test_unexpected_folders_are_reported():
    tree = _tree(
        "database", "documents",
        "database/rawdata", "database/wrongplace",
        "database/extracteddata", "database/extracteddata/eeg",
    )
    f = audit_project(tree, ["Database/RawData"])
    assert any("wrongplace" in u for u in f["unexpected"])
    assert any("extracteddata/eeg" in u for u in f["unexpected"])


def test_non_numeric_folders_under_rawdata_are_unexpected():
    tree = _tree("database", "documents",
                 "database/rawdata", "database/rawdata/JeanDupont")
    f = audit_project(tree, ["Database/RawData"])
    assert any("JeanDupont" in u for u in f["unexpected"])


def test_non_template_data_roots_get_layout_note():
    tree = _tree("database", "documents")
    f = audit_project(tree, ["Included patients"])
    assert "Included patients" in f["layout_note"]


def test_unreachable_project_is_flagged_not_crashed():
    f = audit_project(None, ["Database"])
    assert f["unreachable"] is True


def test_redaction_keeps_template_and_numbers():
    assert redact_path("database/rawdata/12") == "database/rawdata/12"
    assert redact_path("database/extracteddata/nol") == \
        "database/extracteddata/nol"
    r = redact_path("Included patients/Jean Dupont")
    assert "Jean Dupont" not in r and "<label>" in r


def test_digest_is_deidentified():
    audit = {"DEXREM": {
        "unreachable": False,
        "missing": ["documents/"],
        "unexpected": ["Included patients/Jean Dupont/"],
        "stray_files": 2,
        "layout_note": "data_roots=['Included patients'] do not follow",
    }}
    d = build_digest(audit, {"files_ok": 10, "files_failed": 1,
                             "projects": {}}, [])
    p = d["projects"]["DEXREM"]
    assert p["missing"] == ["documents/"]  # template names are safe
    assert "Jean Dupont" not in json_dumps(p)
    assert p["unexpected_count"] == 1


def json_dumps(o):
    import json
    return json.dumps(o)


def test_classify_run_counts_ok_and_failed():
    state = {"legacy": {
        "DEXREM": {"files": {
            "a": {"status": "ok"}, "b": {"status": "failed"},
            "c": {"status": "ok"}}},
        "IPAMS": {"files": {}},
    }}
    s = classify_run(state)
    assert s["files_ok"] == 2
    assert s["files_failed"] == 1
    assert s["projects"]["DEXREM"] == {"ok": 2, "failed": 1}




def test_suggest_home_devices_and_docs():
    from tools.supervisor import suggest_home
    assert suggest_home("NOL") == "Database/ExtractedData/NOL/"
    assert suggest_home("bis") == "Database/ExtractedData/BIS/"
    assert suggest_home("Soumission ethique") == \
        "Documents/Soumission ethique/"
    assert suggest_home("Patient 12") == \
        "Database/RawData/12/  (or AnalyzedData — please decide)"
    assert suggest_home("random stuff") is None


def test_render_email_only_when_attention_needed():
    from tools.supervisor import render_email
    clean = {"DEXREM": {"unreachable": False, "missing": [],
                       "unexpected": [], "stray_files": 0,
                       "layout_note": ""}}
    assert render_email(clean, {"files_ok": 5, "files_failed": 0},
                        "20261001_1200") is None
    bad = {"DEXREM": {"unreachable": False, "missing": ["documents/"],
                      "unexpected": ["NOL/"],
                      "stray_files": 0, "layout_note": ""}}
    out = render_email(bad, {"files_ok": 5, "files_failed": 0},
                       "20261001_1200")
    assert out is not None
    subject, body = out
    assert "deviate" in subject and "1 project" in subject
    assert "== DEXREM ==" in body
    assert "NOL" in body and "Database/ExtractedData/NOL/" in body
    assert "Missing section: documents/" in body
    assert "nothing was moved" in body.lower() or \
        "Nothing was moved" in body


def test_health_no_history_no_findings():
    from tools.supervisor import check_project_health
    assert check_project_health("X", {}) == []
    assert check_project_health("X", {"run_history": []}) == []


def _rec(eligible, selected, ok_new, failed_new, ok_total):
    return {"ts": "20261001_1200", "eligible": eligible,
            "selected": selected, "ok_new": ok_new,
            "failed_new": failed_new, "ok_total": ok_total}


def test_health_stall_needs_two_consecutive_runs():
    from tools.supervisor import check_project_health
    one = {"run_history": [_rec(100, 0, 0, 0, 40)]}
    assert check_project_health("X", one) == []  # single run: no finding yet
    two = {"run_history": [_rec(100, 0, 0, 0, 40),
                           _rec(100, 0, 0, 0, 40)]}
    f = check_project_health("X", two)
    assert len(f) == 1 and "stall" in f[0] and "60" in f[0]


def test_health_complete_project_is_not_a_stall():
    from tools.supervisor import check_project_health
    entry = {"run_history": [_rec(50, 0, 0, 0, 50),
                             _rec(50, 0, 0, 0, 50)]}
    assert check_project_health("X", entry) == []


def test_health_failure_spike():
    from tools.supervisor import check_project_health
    clean_hist = [_rec(100, 20, 20, 0, 20 + 20 * i) for i in range(3)]
    spike = _rec(100, 10, 5, 5, 80)
    f = check_project_health("X", {"run_history": clean_hist + [spike]})
    assert len(f) == 1 and "spike" in f[0]
    # low failure rate: no finding
    ok_run = _rec(100, 10, 9, 1, 80)
    assert check_project_health(
        "X", {"run_history": clean_hist + [ok_run]}) == []


def test_run_health_checks_digest_and_email(tmp_path=None):
    import tempfile
    from datetime import datetime, timezone
    from tools.supervisor import run_health_checks
    tmp = tempfile.mkdtemp()
    # pre-existing audit email files (clean audit -> none written; make one)
    with open(os.path.join(tmp, "body.txt"), "w") as fh:
        fh.write("audit body\n")
    with open(os.path.join(tmp, "subject.txt"), "w") as fh:
        fh.write("audit subject")
    uploaded = {}

    def put_plain(name, data):
        uploaded[name] = data

    state = {"legacy": {"STALLED": {
        "files": {},
        "run_history": [_rec(100, 0, 0, 0, 40),
                        _rec(100, 0, 0, 0, 40)],
        "last_run": _rec(100, 0, 0, 0, 40),
    }}}
    started = datetime.now(timezone.utc)
    d = run_health_checks(None, "acct", [{"code": "STALLED"}],
                          state, lambda n, d: None, put_plain,
                          tmp, started, sample_parquets=False)
    assert d["projects"]["STALLED"]["ok_total"] == 40
    assert "STALLED" in d["findings"]
    assert any(n.startswith("supervisor/health_") for n in uploaded)
    body = open(os.path.join(tmp, "body.txt")).read()
    assert "audit body" in body and "== Ingest health ==" in body
    assert "stall" in body
    subj = open(os.path.join(tmp, "subject.txt")).read()
    assert subj == "audit subject + ingest health"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)