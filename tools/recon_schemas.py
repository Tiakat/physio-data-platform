"""Recon: header-only schema census of every source CSV in Dropbox.

Read-only. For each of the 10 data projects, walks the Dropbox tree and
downloads only the first HEAD_BYTES of every tier-A source CSV (infinity /
bettercare / nol / pump patterns from the project profile) plus any other
.csv exports (POSBRAIN, Patient-level vital CSVs, ...). Extracts the raw
column headers -- duplicates preserved with an occurrence index, never
collapsed -- and builds the global column inventory:

  all_columns.csv           every (project, source, file, column) occurrence
  column_dictionary.csv     one row per (project, source, original column)
  project_source_schema.csv per project x source: file/patient counts,
                            union columns, schema stability
  column_variants.csv       original column names grouped by canonical variable
  duplicate_columns.csv     files where a column name repeats (HR, HR.1, ...)

Privacy: no file contents beyond the header row, no real paths, no patient
codes. Patients appear only as opaque tokens and as counts.

Canonical mapping reuses the parsers' own resolve_columns() so the
inventory stays consistent with what lands in the standardized parquets.
Processing-rule fields (filter_method, normalization, statistics, graph)
are emitted as "pending": they are designed in Phase 5-7, never invented
by this scan.

Plaintext CSVs: reports/recon/schemas_<ts>/
"""

from __future__ import annotations

import csv
import hashlib
import io
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

HEAD_BYTES = 65536
WORKERS = 6

FALLBACK_DELIMITER = {
    "infinity": ";",
    "bettercare": ";",
    "nol": ",",
    "pump": ";",
}
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def head_bytes(dbx, dropbox_path: str, n: int = HEAD_BYTES) -> bytes:
    """Download only the first n bytes of a Dropbox file (streaming)."""
    _, resp = dbx.files_download(dropbox_path)
    try:
        buf = bytearray()
        for chunk in resp.iter_content(16384):
            buf += chunk
            if len(buf) >= n:
                break
        return bytes(buf[:n])
    finally:
        resp.close()


def decode_head(raw: bytes) -> str | None:
    for enc in ENCODINGS:
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return None


def parse_header_line(line: str, delimiter: str | None) -> list[str] | None:
    """Parse one header line with csv; sniff delimiter when not given."""
    candidates = [delimiter] if delimiter else [";", ",", "\t", "|"]
    best: list[str] | None = None
    for delim in candidates:
        try:
            row = next(csv.reader(io.StringIO(line), delimiter=delim))
        except Exception:
            continue
        row = [c.strip() for c in row]
        if len(row) >= 2 and (best is None or len(row) > len(best)):
            best = row
    return best


def extract_columns(text: str, delimiter: str | None,
                    header_row: int) -> tuple[list[str] | None, str]:
    """Return (columns, failure_reason). Reason is '' on success."""
    lines = text.splitlines()
    if not lines:
        return None, "empty_file"
    if len(lines) <= header_row:
        return None, "short_head"
    cols = parse_header_line(lines[header_row], delimiter)
    if not cols:
        return None, "blank_header"
    return cols, ""


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import sync_dropbox_cloud
    from tools.daily_pipeline import ACCOUNT, REPORTS
    from tools import azure_auth
    from tools.legacy_pipeline import list_dropbox_tree, select_projects
    from tools.run_local import find_patient
    from backbone.config import load_profile
    from backbone.parsers._common import resolve_columns

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    dbx = sync_dropbox_cloud.get_dropbox_client()
    projects, _ = select_projects(dbx)

    occurrences: list[dict] = []      # rows for all_columns.csv
    failed = 0
    fail_reasons: dict[str, int] = {}
    fail_extensions = {}  # extension -> count (privacy-safe: no paths)
    skipped_binary = 0
    lock = threading.Lock()

    def scan_file(code: str, profile: dict, tier_a: list[tuple[str, str]],
                  root: str, rel: str, name: str, dbx_path: str):
        nonlocal failed

        def record_failure(reason: str):
            nonlocal failed
            with lock:
                failed += 1
                fail_reasons[reason] = fail_reasons.get(reason, 0) + 1
                ext = Path(name).suffix.lower() or "<noext>"
                fail_extensions[ext] = fail_extensions.get(ext, 0) + 1
        device = None
        for pattern, dev in tier_a:
            if fnmatch(name, pattern):
                device = dev
                break
        if device is None:
            if name.lower().endswith(".csv"):
                device = "other_csv"
            else:
                return
        dev_cfg = (profile.get("devices") or {}).get(device, {})
        delimiter = dev_cfg.get("delimiter",
                                FALLBACK_DELIMITER.get(device))
        header_row = (dev_cfg.get("parser_options") or {}).get(
            "block_header_row", 0)
        try:
            raw = head_bytes(dbx, dbx_path)
        except Exception:
            record_failure("download_error")
            return
        text = decode_head(raw)
        if text is None:
            record_failure("decode_error")
            return
        cols, why = extract_columns(text, delimiter, header_row)
        if not cols:
            record_failure(f"no_header:{why}")
            return
        try:
            patient = find_patient(profile, Path(rel))
        except Exception:
            patient = None
        patient_token = ("pt_" + _sha8(f"{code}:{patient}")
                         if patient else "pt_unknown")
        file_token = "f_" + _sha8(f"{code}:{rel}")
        # Duplicate raw names are kept: occurrence index counts repeats.
        seen: dict[str, int] = {}
        mapping = resolve_columns(cols, profile)
        rows = []
        for pos, col in enumerate(cols):
            seen[col] = seen.get(col, 0) + 1
            rows.append({
                "project": code,
                "source": device,
                "patient_token": patient_token,
                "file_token": file_token,
                "position": pos,
                "original_column": col,
                "occurrence": seen[col],
                "canonical_variable": mapping.get(col, ""),
            })
        with lock:
            occurrences.extend(rows)

    for proj in sorted(projects, key=lambda p: p["code"]):
        code = proj["code"]
        profile = load_profile(code)
        if not profile:
            print(f"[schemas] {code}: no profile, skipped", flush=True)
            continue
        tier_a = [(t["pattern"], t["device"])
                  for t in (profile.get("tiers") or {}).get("A_parsed", [])]
        excl = proj.get("exclude_folders") or []
        jobs = []
        for root in proj["data_roots"]:
            base = (proj["dropbox_base"] if root == "."
                    else f"{proj['dropbox_base']}/{root}")
            try:
                entries = list_dropbox_tree(dbx, base)
            except Exception as exc:  # noqa: BLE001
                print(f"[schemas] {code}: cannot list {base}: {exc}",
                      flush=True)
                continue
            for e in entries:
                rel = (f"{root}/{e['relpath']}" if root != "."
                       else e["relpath"])
                if any(rel == x or rel.startswith(x + "/") for x in excl):
                    continue
                name = e["name"]
                if not name.lower().endswith(".csv"):
                    with lock:
                        skipped_binary += 1
                    continue
                dbx_path = f"{base}/{e['relpath']}"
                jobs.append((code, profile, tier_a, root, rel, name,
                             dbx_path))
        print(f"[schemas] {code}: {len(jobs)} csv files to scan",
              flush=True)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            list(pool.map(lambda j: scan_file(*j), jobs))
        print(f"[schemas] {code}: done "
              f"({len([o for o in occurrences if o['project'] == code])} "
              f"column occurrences)", flush=True)

    # ---- aggregate ------------------------------------------------------
    by_col: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    patients_per_src: dict[tuple[str, str], set[str]] = defaultdict(set)
    files_per_src: dict[tuple[str, str], set[str]] = defaultdict(set)
    canon_of: dict[tuple[str, str, str], str] = {}
    dupes: dict[tuple[str, str, str], dict] = defaultdict(
        lambda: {"files": set(), "max_in_file": 0})

    for o in occurrences:
        key = (o["project"], o["source"], o["original_column"])
        by_col[key].add(o["patient_token"])
        patients_per_src[(o["project"], o["source"])].add(o["patient_token"])
        files_per_src[(o["project"], o["source"])].add(o["file_token"])
        canon_of[key] = o["canonical_variable"]
        if o["occurrence"] > 1:
            d = dupes[key]
            d["files"].add(o["file_token"])
            d["max_in_file"] = max(d["max_in_file"], o["occurrence"])

    all_columns_rows = occurrences

    dict_rows = []
    for (code, source, col), pts in sorted(by_col.items()):
        total = len(patients_per_src[(code, source)])
        dict_rows.append({
            "project": code,
            "source": source,
            "original_column": col,
            "canonical_variable": canon_of[(code, source, col)],
            "domain": "pending",
            "signal_type": "pending",
            "unit": "pending",
            "sampling_rate": "pending",
            "filter_required": "pending",
            "filter_method": "pending",
            "normalization": "pending",
            "statistics": "pending",
            "graph": "pending",
            "available_patients": len(pts),
            "missing_patients": total - len(pts),
        })

    schema_rows = []
    for (code, source) in sorted(patients_per_src):
        union_cols = [k for k in by_col if k[0] == code and k[1] == source]
        n_patients = len(patients_per_src[(code, source)])
        stable = sum(1 for k in union_cols
                     if len(by_col[k]) == n_patients)
        schema_rows.append({
            "project": code,
            "source": source,
            "n_files_scanned": len(files_per_src[(code, source)]),
            "n_patients": n_patients,
            "n_union_columns": len(union_cols),
            "n_stable_columns": stable,
            "schema_stability": ("stable" if stable == len(union_cols)
                                 else "variable"),
        })

    variant_groups: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    variant_pts: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    for (code, source, col), pts in by_col.items():
        canon = canon_of[(code, source, col)] or "(unmapped)"
        variant_groups[(code, source, canon)].add(col)
        variant_pts[(code, source, canon)] |= pts
    variant_rows = [{
        "project": code,
        "source": source,
        "canonical_variable": canon,
        "variant_original_column": col,
        "n_patients": len(variant_pts[(code, source, canon)]),
    } for (code, source, canon), cols in sorted(variant_groups.items())
        for col in sorted(cols)]

    dupe_rows = [{
        "project": code,
        "source": source,
        "original_column": col,
        "n_files_with_duplicates": len(d["files"]),
        "max_duplicates_in_one_file": d["max_in_file"],
    } for (code, source, col), d in sorted(dupes.items())]

    # ---- write + upload ---------------------------------------------------
    def to_csv(rows: list[dict], fieldnames: list[str]) -> bytes:
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
        return buf.getvalue().encode("utf-8")

    outputs = {
        "all_columns.csv": (
            ["project", "source", "patient_token", "file_token",
             "position", "original_column", "occurrence",
             "canonical_variable"], all_columns_rows),
        "column_dictionary.csv": (
            ["project", "source", "original_column", "canonical_variable",
             "domain", "signal_type", "unit", "sampling_rate",
             "filter_required", "filter_method", "normalization",
             "statistics", "graph", "available_patients",
             "missing_patients"], dict_rows),
        "project_source_schema.csv": (
            ["project", "source", "n_files_scanned", "n_patients",
             "n_union_columns", "n_stable_columns",
             "schema_stability"], schema_rows),
        "column_variants.csv": (
            ["project", "source", "canonical_variable",
             "variant_original_column", "n_patients"], variant_rows),
        "duplicate_columns.csv": (
            ["project", "source", "original_column",
             "n_files_with_duplicates",
             "max_duplicates_in_one_file"], dupe_rows),
    }

    svc = azure_auth.get_blob_service_client(ACCOUNT)
    azure_auth.ensure_container(svc, REPORTS)
    for fname, (fields, rows) in outputs.items():
        payload = to_csv(rows, fields)
        blob = f"recon/schemas_{ts}/{fname}"
        svc.get_blob_client(container=REPORTS,
                            blob=blob).upload_blob(payload, overwrite=True)
        print(f"[schemas] wrote reports/{blob} ({len(rows)} rows)",
              flush=True)

    n_cols = len(by_col)
    n_dup_files = sum(len(d["files"]) for d in dupes.values())
    reasons = ", ".join(f"{k}={v}" for k, v in fail_reasons.items())
    exts = ", ".join(f"{e}={c}"
                     for e, c in sorted(fail_extensions.items(),
                                        key=lambda kv: -kv[1])[:10])
    print(f"[schemas] census complete: {len(all_columns_rows)} occurrences, "
          f"{n_cols} unique (project, source, column), "
          f"{n_dup_files} files with duplicate column names, "
          f"{failed} header reads failed ({reasons}; top extensions: "
          f"{exts or 'none'}), "
          f"{skipped_binary} non-csv files skipped (binaries need parsers)",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
