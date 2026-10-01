"""Tests for the demographic recon helpers (no Dropbox needed)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.recon_demographics import location_class, looks_demographic


def test_location_class_documents():
    assert location_class("/Liam/Projets actifs/IPAMS",
                          "/Liam/Projets actifs/IPAMS/Documents/demo.xlsx") \
        == "documents"


def test_location_class_patient_tree():
    assert location_class("/Liam/Projets actifs/IPAMS",
                          "/Liam/Projets actifs/IPAMS/Database/RawData/f.xlsx") \
        == "patient_tree"


def test_location_class_other():
    assert location_class("/Liam/Projets actifs/IPAMS",
                          "/Liam/Projets actifs/IPAMS/random/f.xlsx") \
        == "other"


def test_looks_demographic_french_and_english():
    assert looks_demographic("Demographie_patients.xlsx")
    assert looks_demographic("patient demographics.xls")
    assert looks_demographic("caracteristiques.xlsx")
    assert not looks_demographic("extracted_signals.xlsx")
    assert not looks_demographic("report.pdf")
