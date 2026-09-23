import csv

from physio import restructure

HDR = ["project", "full_path", "size_bytes", "content_md5", "dropbox_content_hash", "layout"]


def plan(tmp_path, blobs):
    inv = tmp_path / "inv.csv"
    with open(inv, "w", newline="") as f:
        w = csv.writer(f); w.writerow(HDR); w.writerows(blobs)
    return {r["legacy_path"]: r for r in restructure.make_plan(str(inv), tmp_path / "plan.csv")}


def test_legacy_to_canonical_and_redundant_folder_dropped(tmp_path):
    p = plan(tmp_path, [["DEXREM", "DEXREM/17/DEXREM 17/1.2.826.0.1.3680043.2.403.36.1250912093151555.13.1-1.csv", 5, "m", "h", "legacy"]])
    r = next(iter(p.values()))
    assert r["action"] == "copy"
    assert r["target_path"] == "DEXREM/Database/RawData/17/BetterCare/1.2.826.0.1.3680043.2.403.36.1250912093151555.13.1-1.csv"


def test_same_bytes_twice_is_duplicate_different_bytes_keep_subpath(tmp_path):
    n = "1.2.826.0.1.3680043.2.403.36.1250725070401149.13.6-1.csv"
    p = plan(tmp_path, [
        ["P", f"PROMISES/ExtractedData/bettercare/54/{n}", 10, "a", "H1", "legacy"],
        ["P", f"PROMISES/ExtractedData/bettercare/BetterCare1/Patient 54 x/{n}", 10, "a", "H1", "legacy"],
        ["P", f"PROMISES/ExtractedData/The infinity/Included Patients/PROMISES 54 y/{n}", 20, "b", "H2", "legacy"],
    ])
    acts = sorted(r["action"] for r in p.values())
    assert acts == ["copy", "copy", "duplicate"]
    assert all("_from_legacy" in r["target_path"] for r in p.values())


def test_canonical_blobs_untouched(tmp_path):
    assert plan(tmp_path, [["IPAMS", "IPAMS/Database/RawData/1/NOL/x.pmd.enc", 1, "", "", "canonical"]]) == {}
