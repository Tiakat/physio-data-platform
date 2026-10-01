"""Review the header-only schema census.

Reads the latest reports/recon/schemas_<ts>/ CSVs from Azure and produces a
human-readable review: per-project coverage, identifier-candidate columns,
unmapped columns, duplicate-column hotspots, and schema-stability evidence.

Read-only with respect to Dropbox (never touches it). Writes one plaintext
review file to reports/recon/review_<ts>.md containing only counts and column
names -- no patient tokens, no file paths, no raw data.

Usage: python -m tools.review_dictionary
"""

from __future__ import annotations

import csv
import io
import re
import sys
from datetime import datetime, timezone

ACCOUNT = "labdataplatform"
REPORTS = "reports"

# Tokens that suggest a column carries identifying information. Matching is a
# *candidate* flag for human review, never an automatic classification.
# This set is the union of the review heuristics and the Stage 2 classifier's
# _IDENTIFIER_TOKENS (tools/stage2_discovery.py): both must agree, otherwise a
# column flagged in parquets is missed in the raw-header review.
IDENTIFIER_TOKENS = {
    "patientid", "patient_id", "subjectid", "subject_id", "mrn",
    "medicalrecordnumber", "name", "firstname", "lastname", "dob",
    "dateofbirth", "birthdate", "birth_date", "patientname",
    # bare tokens from the Stage 2 classifier:
    "patient", "subject", "guid", "uuid", "ipp", "nhs", "fullname",
    "surname",
}

_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(column: str) -> set[str]:
    return set(t for t in _SPLIT.split(column.lower()) if t)


def is_identifier_candidate(original_column: str) -> str | None:
    """Return the matched token if the column name looks identifier-like."""
    toks = _tokens(original_column)
    joined = original_column.lower().replace("_", "").replace("-", "").replace(" ", "")
    for tok in IDENTIFIER_TOKENS:
        if tok in toks or tok == joined:
            return tok
    # common compact forms: PatientID, SubjectID, DateOfBirth
    if joined in IDENTIFIER_TOKENS:
        return joined
    return None


def summarize_coverage(schema_rows: list[dict]) -> list[dict]:
    """Per (project, source): files, patients, union/stable columns, stability."""
    out = []
    for r in schema_rows:
        out.append({
            "project": r["project"],
            "source": r["source"],
            "n_files": int(r["n_files_scanned"] or 0),
            "n_patients": int(r["n_patients"] or 0),
            "n_union_columns": int(r["n_union_columns"] or 0),
            "n_stable_columns": int(r["n_stable_columns"] or 0),
            "stability": r["schema_stability"],
        })
    return sorted(out, key=lambda d: (d["project"], d["source"]))


def find_identifier_candidates(dict_rows: list[dict]) -> list[dict]:
    """Candidate identifier columns per project/source. Human review required."""
    out = []
    for r in dict_rows:
        tok = is_identifier_candidate(r["original_column"] or "")
        if tok:
            out.append({
                "project": r["project"],
                "source": r["source"],
                "original_column": r["original_column"],
                "matched_token": tok,
                "available_patients": int(r["available_patients"] or 0),
            })
    return sorted(out, key=lambda d: (d["project"], d["source"], d["original_column"]))


def summarize_unmapped(dict_rows: list[dict]) -> list[dict]:
    """Unmapped (unknown-meaning) columns per project/source."""
    agg: dict[tuple[str, str], dict] = {}
    for r in dict_rows:
        if (r["canonical_variable"] or "").strip():
            continue
        key = (r["project"], r["source"])
        d = agg.setdefault(key, {"project": r["project"], "source": r["source"],
                                 "n_unmapped": 0, "examples": []})
        d["n_unmapped"] += 1
        if len(d["examples"]) < 8:
            d["examples"].append(r["original_column"])
    return sorted(agg.values(), key=lambda d: -d["n_unmapped"])


def summarize_duplicates(dupe_rows: list[dict], top: int = 15) -> list[dict]:
    out = []
    for r in dupe_rows:
        out.append({
            "project": r["project"],
            "source": r["source"],
            "original_column": r["original_column"],
            "n_files": int(r["n_files_with_duplicates"] or 0),
            "max_in_one_file": int(r["max_duplicates_in_one_file"] or 0),
        })
    return sorted(out, key=lambda d: -d["n_files"])[:top]


def summarize_single_patient(dict_rows: list[dict]) -> list[dict]:
    """Columns seen in exactly one patient per project/source (instability evidence)."""
    agg: dict[tuple[str, str], int] = {}
    for r in dict_rows:
        if int(r["available_patients"] or 0) == 1:
            key = (r["project"], r["source"])
            agg[key] = agg.get(key, 0) + 1
    return [{"project": p, "source": s, "n_single_patient_columns": c}
            for (p, s), c in sorted(agg.items(), key=lambda kv: -kv[1])]


def build_markdown(ts: str, schemas_prefix: str, coverage: list[dict],
                   ident: list[dict], unmapped: list[dict],
                   dupes: list[dict], single: list[dict],
                   n_dict_rows: int) -> str:
    L = []
    L.append(f"# Dictionary review -- {ts}")
    L.append("")
    L.append(f"Source census: `reports/{schemas_prefix}/` ({n_dict_rows} unique "
             "(project, source, column) entries).")
    L.append("Column names are schema metadata. No patient tokens, file paths, "
             "or values are included in this review.")
    L.append("")
    L.append("## 1. Coverage per project/source")
    L.append("")
    L.append("| project | source | files | patients | union cols | stable cols | stability |")
    L.append("|---|---|---|---|---|---|---|")
    for c in coverage:
        L.append(f"| {c['project']} | {c['source']} | {c['n_files']} | "
                 f"{c['n_patients']} | {c['n_union_columns']} | "
                 f"{c['n_stable_columns']} | {c['stability']} |")
    L.append("")
    L.append("## 2. Identifier candidates (require human review)")
    L.append("")
    if not ident:
        L.append("No identifier-like column names detected.")
    else:
        L.append("These are *candidates* only -- flag for review, never "
                 "auto-delete, never auto-reclassify.")
        L.append("")
        L.append("| project | source | original column | matched token | patients |")
        L.append("|---|---|---|---|---|")
        for r in ident:
            L.append(f"| {r['project']} | {r['source']} | `{r['original_column']}` | "
                     f"{r['matched_token']} | {r['available_patients']} |")
    L.append("")
    L.append("## 3. Unmapped columns (unknown meaning -- review queue)")
    L.append("")
    if not unmapped:
        L.append("Every column mapped to a canonical variable.")
    else:
        L.append("| project | source | n unmapped | examples |")
        L.append("|---|---|---|---|")
        for u in unmapped:
            ex = ", ".join(f"`{e}`" for e in u["examples"])
            L.append(f"| {u['project']} | {u['source']} | {u['n_unmapped']} | {ex} |")
    L.append("")
    L.append("## 4. Duplicate-column hotspots (preserved, not collapsed)")
    L.append("")
    L.append("| project | source | column | files affected | max in one file |")
    L.append("|---|---|---|---|---|")
    for d in dupes:
        L.append(f"| {d['project']} | {d['source']} | `{d['original_column']}` | "
                 f"{d['n_files']} | {d['max_in_one_file']} |")
    L.append("")
    L.append("## 5. Single-patient columns (schema-instability evidence)")
    L.append("")
    if not single:
        L.append("No single-patient columns.")
    else:
        L.append("| project | source | n single-patient columns |")
        L.append("|---|---|---|")
        for s in single:
            L.append(f"| {s['project']} | {s['source']} | {s['n_single_patient_columns']} |")
    L.append("")
    L.append("## Reviewer notes")
    L.append("")
    L.append("- Identifier candidates (section 2) must be resolved before any "
             "de-identified release: confirm what each column holds, then decide "
             "drop-from-feed vs keep-with-justification. Nothing is deleted from "
             "Dropbox either way.")
    L.append("- Unmapped columns (section 3) are the human review queue for "
             "Phase 8. They are processed (raw preserved + QC flags), never "
             "discarded.")
    L.append("- Duplicate columns (section 4) must be disambiguated per file "
             "position, never collapsed.")
    return "\n".join(L) + "\n"


def _read_csv(payload: bytes) -> list[dict]:
    return list(csv.DictReader(io.StringIO(payload.decode("utf-8"))))


def main() -> int:
    from tools import azure_auth

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    svc = azure_auth.get_blob_service_client(ACCOUNT)

    prefixes = set()
    for b in svc.get_container_client(REPORTS).list_blobs(
            name_starts_with="recon/schemas_"):
        parts = b.name.split("/")
        if len(parts) >= 3:
            prefixes.add("/".join(parts[:2]))
    if not prefixes:
        print("[review] no schemas_* census found under reports/recon/", flush=True)
        return 1
    schemas_prefix = sorted(prefixes)[-1]
    print(f"[review] using census {schemas_prefix}", flush=True)

    def get(name: str) -> list[dict]:
        data = svc.get_blob_client(
            container=REPORTS,
            blob=f"{schemas_prefix}/{name}").download_blob().readall()
        rows = _read_csv(data)
        print(f"[review] loaded {name}: {len(rows)} rows", flush=True)
        return rows

    dict_rows = get("column_dictionary.csv")
    schema_rows = get("project_source_schema.csv")
    dupe_rows = get("duplicate_columns.csv")

    coverage = summarize_coverage(schema_rows)
    ident = find_identifier_candidates(dict_rows)
    unmapped = summarize_unmapped(dict_rows)
    dupes = summarize_duplicates(dupe_rows)
    single = summarize_single_patient(dict_rows)

    md = build_markdown(ts, schemas_prefix, coverage, ident, unmapped,
                        dupes, single, len(dict_rows))
    out_blob = f"recon/review_{ts}.md"
    svc.get_blob_client(container=REPORTS,
                        blob=out_blob).upload_blob(md.encode("utf-8"),
                                                   overwrite=True)
    print(f"[review] wrote reports/{out_blob}", flush=True)
    print(f"[review] findings: {len(ident)} identifier candidates, "
          f"{sum(u['n_unmapped'] for u in unmapped)} unmapped columns, "
          f"{sum(s['n_single_patient_columns'] for s in single)} "
          f"single-patient columns", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
