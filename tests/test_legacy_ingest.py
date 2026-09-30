"""Tests for the encrypted-parquet ingest (stage 1).

Covers: document/junk exclusion, exclude-folder patterns, incremental
selection (new/changed/failed vs unchanged), budget selection, blob-name
privacy (no patient codes), and state shape.
Pure logic — no Dropbox/Azure/network.
"""

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Stub the dropbox SDK (not installed on this machine; tests never touch it).
_dropbox = types.ModuleType("dropbox")
_files = types.ModuleType("dropbox.files")
_exceptions = types.ModuleType("dropbox.exceptions")


class _FolderMetadata:
    def __init__(self, name=""):
        self.name = name


class _FileMetadata:
    pass


class _ApiError(Exception):
    pass


_files.FolderMetadata = _FolderMetadata
_files.FileMetadata = _FileMetadata
_exceptions.ApiError = _ApiError
_dropbox.files = _files
_dropbox.exceptions = _exceptions
sys.modules["dropbox"] = _dropbox
sys.modules["dropbox.files"] = _files
sys.modules["dropbox.exceptions"] = _exceptions

from tools.legacy_pipeline import (  # noqa: E402
    DOCUMENT_EXTENSIONS,
    _excluded,
    _parquet_blob_name,
    _select_new,
    is_document,
    is_junk,
)


def test_documents_and_junk_excluded():
    assert is_document("ethics/approval.pdf")
    assert is_document("forms/consent.docx")
    assert is_document("data/sheet.xlsx")
    assert not is_document("recording.csv")  # csv is DATA, not a document
    assert is_junk(".DS_Store")
    assert is_junk("Thumbs.db")
    assert not is_junk("NOL.csv")


def test_exclude_folders():
    pats = ["Patient non inclus*"]
    assert _excluded("Database/Patient non inclus 5/NOL.csv", pats)
    assert _excluded("Database/patient non inclus 12/x.csv", pats)
    assert not _excluded("Database/Patient 3/NOL.csv", pats)
    assert not _excluded("Database/RawData/1/x.csv", [])


def test_select_new_skips_unchanged():
    entries = [
        {"relpath": "a/HR.csv", "name": "HR.csv", "size": 100, "rev": "r2"},
        {"relpath": "b/HR.csv", "name": "HR.csv", "size": 100, "rev": "r1"},
    ]
    done = {"a/HR.csv": {"status": "ok", "rev": "r2", "kind": "parquet"}}
    sel, used = _select_new(entries, done, 10 ** 9)
    assert [e["relpath"] for e in sel] == ["b/HR.csv"]
    assert used == 100


def test_select_new_reingests_changed_and_failed():
    entries = [{"relpath": "P-01/HR.csv", "name": "HR.csv", "size": 100, "rev": "r2"}]
    # changed rev -> re-ingest
    sel, _ = _select_new(entries, {"P-01/HR.csv": {"status": "ok", "rev": "r1"}}, 10 ** 9)
    assert len(sel) == 1
    # failed -> retry even with same rev
    sel, _ = _select_new(entries, {"P-01/HR.csv": {"status": "failed", "rev": "r2"}}, 10 ** 9)
    assert len(sel) == 1
    # record without rev -> re-ingest once to stamp it
    sel, _ = _select_new(entries, {"P-01/HR.csv": {"status": "ok"}}, 10 ** 9)
    assert len(sel) == 1


def test_select_new_respects_budget():
    entries = [
        {"relpath": "a.csv", "name": "a.csv", "size": 2_000_000_000, "rev": "r1"},
        {"relpath": "b.csv", "name": "b.csv", "size": 2_000_000_000, "rev": "r1"},
    ]
    sel, used = _select_new(entries, {}, 3_000_000_000)
    assert len(sel) == 1 and used == 2_000_000_000
    sel, _ = _select_new(entries, {}, 5_000_000_000)
    assert len(sel) == 2


def test_blob_names_carry_no_patient_codes():
    # Blob names are <device>_<sha8>.parquet.enc — patient codes live only
    # in the encrypted catalog/state, never in blob names.
    blob = _parquet_blob_name("V-RAPS", "infinity", "abc123def456")
    assert blob == "V-RAPS/parquet/infinity_abc123de.parquet.enc"
    assert "P-" not in blob and "/12/" not in blob
    assert blob.endswith(".parquet.enc")


def test_state_shape():
    # state["legacy"][code]["files"][relpath] = {...}
    state = {"legacy": {"V-RAPS": {"files": {
        "Database/RawData/12/NOL.csv": {
            "status": "ok", "rev": "r2", "sha256": "abc", "size": 10,
            "kind": "parquet",
            "stored": "V-RAPS/parquet/nol_abc12345.parquet.enc"},
    }}}}
    files = state["legacy"]["V-RAPS"]["files"]
    rec = files["Database/RawData/12/NOL.csv"]
    assert rec["stored"].endswith(".enc")
    assert rec["status"] == "ok"


TESTS = [v for k, v in sorted(globals().items())
         if k.startswith("test_") and callable(v)]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"PASS {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"{len(TESTS) - failed}/{len(TESTS)} passed")
    sys.exit(1 if failed else 0)
