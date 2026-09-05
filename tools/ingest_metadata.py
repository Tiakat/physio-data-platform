"""
Project metadata workbook ingestion.

The Excel file is preserved exactly as sent, and separately parsed into a tidy
table so a researcher can ask "patients aged 60 to 70 with BMI over 30 who have
BetterCare" without opening Excel.

Two outputs, deliberately:
    demographics.csv    one row per patient, the common variables
    variables.csv       long format, one row per patient per variable, with the
                        original column heading kept verbatim

Provenance is recorded on every value: source workbook, its checksum, the sheet,
the row and the original column name. When somebody asks in a year where a BMI
came from, the answer is traceable to a cell.

    python -m tools.ingest_metadata --project IPAMS \
        --workbook /tmp/azure_sim/rawdata/IPAMS/_metadata/"Données IPAMS.xlsx"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sync_dropbox import load_rules, project_rules      # noqa: E402

# The workbook uses long French headings. Each canonical field is matched by a
# distinctive fragment, case and accent insensitive. Add to this list rather
# than renaming columns in the source workbook.
CANONICAL = {
    "study_id": ["study id", "studyid", "no patient", "patient id", "id patient"],
    "included": ["inclusion"],
    "age": ["age"],
    "sex": ["sexe", "gender"],
    "asa": ["classe asa", "asa"],
    "surgery_type": ["type de chirurgie", "chirurgie"],
    "height_cm": ["taille"],
    "weight_kg": ["poids"],
    "bmi": ["imc", "bmi"],
    "hypertension": ["hta"],
    "diabetes": ["db ("],
    "dyslipidaemia": ["dlp"],
    "atrial_fibrillation": ["fa ("],
    "renal_failure": ["irc"],
    "copd": ["mpoc"],
    "coronary_disease": ["mcas"],
    "arrhythmia": ["arythmie"],
    "stroke": ["avc"],
    "vascular_disease": ["mvas"],
    "sleep_apnoea": ["sahs"],
    "hypothyroid": ["hypot4"],
    "smoking": ["tabac"],
    "anxiety": ["anxiete"],
    "depression": ["depression"],
    "asthma": ["asthme"],
    "anaemia": ["anemie"],
    "chronic_pain": ["douleur chronique"],
    "alcohol": ["roh"],
    "surgery_minutes": ["total time of surgery", "duree chirurgie"],
}


def fold(text) -> str:
    """Lower case, strip accents, collapse whitespace."""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", s).strip().lower()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def map_columns(columns) -> dict[str, str]:
    """Original heading -> canonical field. First match wins, longest fragment
    first so 'classe asa' beats 'asa'."""
    folded = {c: fold(c) for c in columns}
    out: dict[str, str] = {}
    for field, fragments in CANONICAL.items():
        for fragment in sorted(fragments, key=len, reverse=True):
            hit = next((c for c, f in folded.items()
                        if fragment in f and c not in out), None)
            if hit:
                out[hit] = field
                break
    return out


def find_header_row(path: Path, sheet, max_scan=10) -> int:
    """
    These workbooks sometimes carry a title row above the headings. Pick the
    first row in which several canonical fields can be recognised.
    """
    best_row, best_score = 0, -1
    for row in range(max_scan):
        try:
            probe = pd.read_excel(path, sheet_name=sheet, header=row, nrows=1)
        except Exception:                                        # noqa: BLE001
            continue
        score = len(map_columns(probe.columns))
        if score > best_score:
            best_row, best_score = row, score
    return best_row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--workbook", required=True)
    ap.add_argument("--sheet", default=0)
    ap.add_argument("--out", default="metadata")
    args = ap.parse_args()

    rules = project_rules(load_rules(), args.project)
    fmt = rules.get("patient_id_format", "P_{n:0>3}")
    path = Path(args.workbook)
    digest = sha256(path)

    header_row = find_header_row(path, args.sheet)
    table = pd.read_excel(path, sheet_name=args.sheet, header=header_row)
    table = table.dropna(how="all").dropna(axis=1, how="all")
    mapping = map_columns(table.columns)

    if "study_id" not in mapping.values():
        raise SystemExit(
            "No patient identifier column recognised. Headings found:\n  "
            + "\n  ".join(str(c) for c in list(table.columns)[:25])
            + "\n\nAdd the right fragment to CANONICAL['study_id'] and rerun.")

    id_column = next(c for c, f in mapping.items() if f == "study_id")

    demographics, long_rows, skipped = [], [], 0
    for excel_row, record in table.iterrows():
        raw_id = record[id_column]
        if pd.isna(raw_id):
            skipped += 1
            continue
        match = re.search(r"(\d+)", str(raw_id))
        if not match:
            skipped += 1
            continue
        patient = fmt.format(n=int(match.group(1)))

        row = {"patient_id": patient, "source_row": int(excel_row) + header_row + 2}
        for original, field in mapping.items():
            if field == "study_id":
                continue
            value = record.get(original)
            if pd.isna(value):
                continue
            row[field] = value
            long_rows.append({
                "patient_id": patient, "variable": field, "value": value,
                "source_column": str(original), "source_row": row["source_row"],
            })

        # Everything not in CANONICAL is still captured, so no study variable is
        # silently lost. It keeps its original heading as the variable name.
        for original in table.columns:
            if original in mapping:
                continue
            value = record.get(original)
            if pd.isna(value) or str(value).strip() == "":
                continue
            long_rows.append({
                "patient_id": patient, "variable": f"extra::{fold(original)[:60]}",
                "value": value, "source_column": str(original),
                "source_row": row["source_row"],
            })
        demographics.append(row)

    out_dir = Path(args.out) / args.project
    out_dir.mkdir(parents=True, exist_ok=True)
    demo = pd.DataFrame(demographics)
    long = pd.DataFrame(long_rows)
    demo.to_csv(out_dir / "demographics.csv", index=False)
    long.to_csv(out_dir / "variables.csv", index=False)

    provenance = {
        "project": args.project,
        "workbook": path.name,
        "sha256": digest,
        "sheet": str(args.sheet),
        "header_row": header_row,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "patients": len(demo),
        "rows_skipped_no_id": skipped,
        "canonical_columns": {str(k): v for k, v in mapping.items()},
        "unmapped_columns": [str(c) for c in table.columns if c not in mapping],
    }
    (out_dir / "provenance.json").write_text(json.dumps(provenance, indent=2,
                                                        default=str), encoding="utf-8")

    print(f"\n{args.project}   {path.name}")
    print(f"  checksum              {digest[:16]}")
    print(f"  header row            {header_row}")
    print(f"  patients extracted    {len(demo)}")
    print(f"  rows without an id    {skipped}")
    print(f"  recognised columns    {len(mapping) - 1}")
    print(f"  unmapped columns      {len(provenance['unmapped_columns'])} "
          f"(kept in variables.csv under extra::)")

    if not demo.empty:
        interesting = [c for c in ("age", "sex", "bmi", "asa", "surgery_minutes")
                       if c in demo.columns]
        if interesting:
            print("\n  Summary of the variables that drive the statistics")
            print(demo[interesting].describe().round(1).to_string())

    print(f"\n  written to {out_dir}/")


if __name__ == "__main__":
    main()
