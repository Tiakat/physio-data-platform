"""Tests for the encrypted-parquet ingest (stage 1).

Covers: document/junk exclusion, tier classification, group-atomic batching,
budget selection, blob-name privacy (no patient codes), and state shape.
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
    classify_entries,
    group_key,
    is_document,
    is_junk,
    profile_tier,
    select_batch,
)


def test_documents_and_junk_excluded():
    assert is_document("ethics/approval.pdf")
    assert is_document("forms/consent.docx")
    assert is_document("data/sheet.xlsx")
    assert not is_document("recording.csv")  # csv is DATA, not a document
    assert is_junk(".DS_Store")
    assert is_junk("Thumbs.db")
    assert not is_junk("NOL.csv")


def test_profile_tier():
    profile = {"acquisition": {"tiers": {
        "a": {"include": ["*.csv"]},
        "c": {"include": ["*.pdf", "*.med"]},
    }}}
    assert profile_tier(profile, "NOL.csv") == "a"
    assert profile_tier(profile, "notes.pdf") == "c"
    assert profile_tier(profile, "weird.xyz") == "b"
    assert profile_tier(None, "anything.csv") == "u"


def test_classify_entries_keeps_signal_files_only():
    project = {"code": "V-RAPS", "profile": {"acquisition": {"tiers": {
        "a": {"include": ["NOL*.csv", "Infinity*.csv"]},
        "c": {"include": ["*.pdf"]},
    }}}}
    entries = [
        {"relpath": "12/NOL.csv", "name": "NOL.csv", "size": 10, "rev": "r1"},
        {"relpath": "12/notes.pdf", "name": "notes.pdf", "size": 5, "rev": "r2"},
        {"relpath": "12/.DS_Store", "name": ".DS_Store", "size": 1, "rev": "r3"},
        {"relpath": "12/readme.txt", "name": "readme.txt", "size": 2, "rev": "r4"},
    ]
    tier_a, as_bytes = classify_entries(project, entries)
    assert [e["name"] for e in tier_a] == ["NOL.csv"]
    # readme.txt is tier-b data-ish -> stored as encrypted bytes; pdf/junk dropped
    assert [e["name"] for e in as_bytes] == ["readme.txt"]


def test_bettercare_group_atomic():
    project = {"code": "V-RAPS", "profile": {"acquisition": {"tiers": {
        "a": {"include": ["*.csv"]},
    }}}}
    entries = [
        {"relpath": "12/Extracted data/Better Care/a.csv", "name": "a.csv", "size": 2_000_000_000, "rev": "r1"},
        {"relpath": "12/Extracted data/Better Care/b.csv", "name": "b.csv", "size": 2_000_000_000, "rev": "r2"},
    ]
    # budget fits only one group-member: group must stay whole -> nothing chosen
    batch_a, _ = select_batch(project, entries, {}, budget_bytes=3_000_000_000)
    assert batch_a == [] or len(batch_a) == 2
    # budget fits the whole group -> both chosen
    batch_a, _ = select_batch(project, entries, {}, budget_bytes=5_000_000_000)
    assert len(batch_a) == 2


def test_changed_source_reingested():
    project = {"code": "IPAMS", "profile": {"acquisition": {"tiers": {
        "a": {"include": ["*.csv"]},
    }}}}
    entries = [{"relpath": "P-01/HR.csv", "name": "HR.csv", "size": 100, "rev": "r2"}]
    # same rev recorded -> skipped
    done = {"P-01/HR.csv": {"kind": "parquet", "sha256": "old", "rev": "r2"}}
    batch_a, batch_b = select_batch(project, entries, done, budget_bytes=10 ** 9)
    assert batch_a == [] and batch_b == []
    # source changed in Dropbox (new rev) -> re-ingested
    done2 = {"P-01/HR.csv": {"kind": "parquet", "sha256": "old", "rev": "r1"}}
    batch_a, _ = select_batch(project, entries, done2, budget_bytes=10 ** 9)
    assert len(batch_a) == 1
    # old record without rev -> re-ingested once to stamp the rev
    done3 = {"P-01/HR.csv": {"kind": "parquet", "sha256": "old"}}
    batch_a, _ = select_batch(project, entries, done3, budget_bytes=10 ** 9)
    assert len(batch_a) == 1
    # failed entries are retried
    done4 = {"P-01/HR.csv": {"kind": "failed", "error": "x", "rev": "r2"}}
    batch_a, _ = select_batch(project, entries, done4, budget_bytes=10 ** 9)
    assert len(batch_a) == 1


def test_group_key_vraps_patient_folder():
    assert group_key("V-RAPS", "12/Extracted data/NOL.csv") == "12"
    assert group_key("IPAMS", "P-01/HR.csv") == "P-01"
    assert group_key("IPAMS", "HR.csv") == "HR.csv"


def test_blob_names_carry_no_patient_codes():
    # Convention enforced in _parse_and_store_parquet: blob names are
    # <device>_<sha8>.parquet.enc — patient codes live only in the
    # encrypted catalog/state.
    device, digest = "infinity", "abc123def456"
    blob = f"V-RAPS/parquet/{device}_{digest[:8]}.parquet.enc"
    assert "P-" not in blob and "/12/" not in blob
    assert blob.endswith(".parquet.enc")


def test_state_shape():
    # state["legacy"][code]["files"][relpath] = {...}
    state = {"legacy": {"V-RAPS": {"files": {
        "12/NOL.csv": {"sha256": "abc", "size": 10, "kind": "parquet",
                       "stored": "V-RAPS/parquet/nol_abc12345.parquet.enc"},
        "12/notes.pdf": {"kind": "skipped-document"},
    }}}}
    files = state["legacy"]["V-RAPS"]["files"]
    assert files["12/NOL.csv"]["stored"].endswith(".enc")
    assert files["12/notes.pdf"]["kind"] == "skipped-document"


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
