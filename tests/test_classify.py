"""Name-only classification of the file families found in Azure (2026-09-23 inventory).

Names below are synthetic (no real patient identifiers).
"""
import pytest

from physio.classify import Classifier
from physio.common import DropboxHasher

C = Classifier()

CASES = [
    # path                                                                  source        stage       subject
    ("DEXREM/1/20250509091611.infinity.data.OR^^BLOC07.csv",                "Infinity",   "RAW",       "1"),
    ("IPAMS/26/ExtractedData/BetterCare/20260402075735.infinity.data.OR^^BLOC07.csv", "Infinity", "RAW", "26"),
    ("V-RAPS/3/1.2.826.0.1.3680043.2.403.36.1250211142801123.13.1.-1.csv",  "BetterCare", "RAW",       "3"),
    ("SILVR/4/NOL/Data_12.pmd.enc",                                          "NOL",        "RAW",       "4"),
    ("SILVR/4/2025-02-11 1428_ExcelData.csv",                                "NOL",        "EXTRACTED", "4"),
    ("PROMISES/9/2024-11-01 0820-1_PlotPDF.pdf",                             "NOL",        "EXTRACTED", "9"),
    ("PROMISES/9/2024-11-01_0820-1_PM09200550.med",                          "NOL",        "RAW",       "9"),
    ("DEXREM/1/M-KQ3k-05090917/DH05090917/L05090917.ara",                    "BIS",        "RAW",       "1"),
    ("DEXREM/1/M-KQ3k-05090917/BIS_KQ3k_09052025091701/BIS_KQ3k_20250509_1-2.pdf", "BIS", "EXTRACTED", "1"),
    ("IPAMS/2/EEG_aE6d_20260223_1-20.pdf",                                   "BIS",        "EXTRACTED", "2"),
    ("DEXREM/2/Perf_3_History(Device)_250509-091700.csv",                    "Pump",       "RAW",       "2"),
    ("POSBRAIN/5/20250819_1655__TPAD1.H3",                                   "Oximetry",   "RAW",       "5"),
    ("MONREPI/16/Patient 16_240328_084133.vital",                            "NMT",        "RAW",       "16"),
    ("MONREPI/16/Patient 16_240328_084133.csv",                              "NMT",        "RAW",       "16"),
    ("PROMISES/ExtractedData/bettercare/bettercare_combined4/PROMISES 51 22 juillet 2025_combined.csv",
                                                                             "BetterCare", "DERIVED",   "51"),
    ("SILVR/7/Enregistrement/note.m4a",                                      "Audio",      "DOCUMENT",  "7"),
]


@pytest.mark.parametrize("path,source,stage,subject", CASES)
def test_name_signatures(path, source, stage, subject):
    r = C.classify(path)
    assert (r.source, r.stage, r.subject) == (source, stage, subject), r.notes


def test_loose_file_gets_source_from_name():
    r = C.classify("DEXREM/1/20250509091611.infinity.data.OR^^BLOC07.csv")
    assert r.loose and r.source_evidence == "filename"
    assert r.canonical_path == "DEXREM/Database/RawData/1/Infinity/20250509091611.infinity.data.OR^^BLOC07.csv"
    assert r.session_start == "2025-05-09T09:16:11"


def test_name_beats_wrong_folder():
    r = C.classify("PROMISES/ExtractedData/The infinity/Included Patients/Patient 5 20240807 AB/L08071519/L08071519.ara")
    assert r.source == "BIS" and r.subject == "5"


def test_unconfigured_project_is_flagged():
    assert any("UNCONFIGURED" in n for n in C.classify("NEWSTUDY/1/x.csv").notes)


def test_dropbox_hash_empty_and_small():
    import hashlib
    h = DropboxHasher(); assert h.hexdigest() == hashlib.sha256(b"").hexdigest()
    h = DropboxHasher(); h.update(b"abc")
    assert h.hexdigest() == hashlib.sha256(hashlib.sha256(b"abc").digest()).hexdigest()


def test_dropbox_hash_block_boundary():
    import hashlib
    data = b"x" * (4 * 1024 * 1024 + 10)
    h = DropboxHasher()
    for i in range(0, len(data), 1_000_003):
        h.update(data[i:i + 1_000_003])
    want = hashlib.sha256(hashlib.sha256(data[:4194304]).digest()
                          + hashlib.sha256(data[4194304:]).digest()).hexdigest()
    assert h.hexdigest() == want
