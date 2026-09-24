from physio.organize import Planner, folder_renames

P = Planner()
S = "/Liam/Projets actifs"


def plan(study, rels, hashes=None):
    files = [{"study": study, "relative_path": r, "dropbox_path": f"{S}/{study}/{r}",
              "size_bytes": 1, "content_hash": (hashes or {}).get(r, r)} for r in rels]
    return {r["from"].split(f"{study}/", 1)[1]: r for r in P.plan(files)}


def to(r):
    return r["to"].split("/", 4)[-1] if r["to"] else None


def test_data_files_go_to_participant_device_folder():
    p = plan("PVB abdo", ["Included patients/Patient 11 2026-06-15 XX/11/2026-06-15_0923_PM09200551/2026-06-15 0923_ExcelData.csv",
                          "Included patients/Patient 5 2026-02-23 XX/IMG_1.jpeg"])
    assert to(p["Included patients/Patient 11 2026-06-15 XX/11/2026-06-15_0923_PM09200551/2026-06-15 0923_ExcelData.csv"]) \
        == "Database/RawData/11/ExtractedData/NOL/2026-06-15_0923_PM09200551/2026-06-15 0923_ExcelData.csv"
    assert to(p["Included patients/Patient 5 2026-02-23 XX/IMG_1.jpeg"]) == "Database/RawData/5/Photos/IMG_1.jpeg"


def test_documents_rules():
    p = plan("X", ["Documents/Documents version finale/Formulaire consentement v3.docx",
                   "Documents/Documents version finale/CRF v2.pdf",
                   "Documents/Documents version finale/Protocole v4.docx",
                   "Documents/CER/Lettre d'approbation.pdf",
                   "Documents/réponse CER juillet 2025/reponse.docx",
                   "entente/Contrat signé.pdf",
                   "amendement 2/a.docx", "amendement mai 2024/b.docx",
                   "Biblio/article.pdf"])
    assert to(p["Documents/Documents version finale/Formulaire consentement v3.docx"]).endswith("Version finale approuvée/FIC/Formulaire consentement v3.docx")
    assert "/CRF/" in to(p["Documents/Documents version finale/CRF v2.pdf"])
    assert "/Protocol/" in to(p["Documents/Documents version finale/Protocole v4.docx"])
    assert to(p["Documents/CER/Lettre d'approbation.pdf"]).endswith("Documents/Soumission ethique/Lettre d'approbation.pdf")
    assert to(p["Documents/réponse CER juillet 2025/reponse.docx"]).endswith("Soumission initiale/réponse CER juillet 2025/reponse.docx")
    assert to(p["entente/Contrat signé.pdf"]).endswith("Documents/Contrats legaux/Contrat signé.pdf")
    assert to(p["amendement 2/a.docx"]).endswith("Soumission ethique/Amendement 2/a.docx")
    assert to(p["amendement mai 2024/b.docx"]).endswith("Soumission ethique/Amendement 3/b.docx")
    assert p["Biblio/article.pdf"]["status"] == "leave"


def test_never_touch_and_frozen_studies():
    p = plan("POEGEA", ["Included patients/Patient 8 x/SOURCE DO NOT MODIFY/2022-02-16 1314_ExcelData.csv"])
    assert next(iter(p.values()))["status"] == "leave"
    p = plan("PROMISES", ["Documents/stats/.Rproj.user/x/y.json"])
    assert next(iter(p.values()))["status"] == "leave"


def test_identical_copies_are_duplicates_different_are_conflicts():
    a = "Database/RawData/ExtractedData/The infinity/Included Patients/Patient 4 x/2024-08-05 0808_ExcelData.csv"
    b = "Database/RawData/4/NOL/2024-08-05 0808_ExcelData.csv"
    p = plan("PROMISES", [a, b], {a: "H", b: "H"})
    assert sorted(r["status"] for r in p.values()) == ["duplicate", "move"]
    p = plan("PROMISES", [a, b], {a: "H1", b: "H2"})
    assert {r["status"] for r in p.values()} == {"conflict"}


def test_case_only_folder_rename():
    p = plan("V-RAPS", ["Database/RawData/14/ExtractedData/Bettercare/1.2.826.0.1.3680043.2.403.36.1250508110546277.13.1-1.csv"])
    r = list(p.values())
    assert r[0]["status"] == "case"
    assert folder_renames(r) == [(f"{S}/V-RAPS/Database/RawData/14/ExtractedData/Bettercare",
                                  f"{S}/V-RAPS/Database/RawData/14/ExtractedData/BetterCare")]
