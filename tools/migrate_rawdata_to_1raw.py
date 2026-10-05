"""Migrate K's 1-raw container from Azure rawdata (no Dropbox re-download).

K's idea: the 8,026 files are ALREADY in Azure as encrypted parquets:
    rawdata/{PROJECT}/parquet/{device}_{sha8}.parquet.enc

Instead of Dropbox -> 1-raw (slow, keeps getting killed), do:
    rawdata -> decrypt -> CSV -> 1-raw   (Azure-to-Azure, fast)

Identity recovery (the ingest's own mapping, no guessing):
  The v2 ingest wrote, per project, encrypted parse catalogs:
      rawdata/{PROJECT}/catalog.json.enc   (or catalog_{tag}.json.enc)
  Each catalog entry carries:
      file            -> Dropbox relative path (contains the patient folder)
      source_label    -> the REAL Dropbox patient folder name
      device          -> device code
      parquet_sha256  -> SHA256 of the parsed parquet bytes
      parse_status    -> "parsed" | "duplicate" | "not_supported" | "failed"
  The rawdata blob name is deterministic:
      {PROJECT}/parquet/{device}_{parquet_sha256[:8]}.parquet.enc
  So catalog entries resolve to blobs EXACTLY, with zero Dropbox downloads.

K's rule (enforced): patient folders in 1-raw must match Dropbox EXACTLY.
The script lists the project's Dropbox patient folders (header-only, no
downloads) and only uploads files whose catalog patient name EXACTLY matches
one of them (case-sensitive). Non-matching entries are SKIPPED and logged;
no provisional or guessed patient folders are ever created.

Output:
    1-raw/{PROJECT}/{patient}/{device}/{file}.csv   (content_type=text/csv)

Blob metadata on every 1-raw CSV (provenance chain):
    dropbox_relpath, parquet_sha256, source_blob, project, patient, device,
    migrated_at

Resumable: destinations that already exist in 1-raw are skipped.
Nothing is deleted by this tool.

Usage:
    python -m tools.migrate_rawdata_to_1raw --project COLECTOMIE
    python -m tools.migrate_rawdata_to_1raw --project COLECTOMIE --dry-run
    python -m tools.migrate_rawdata_to_1raw --project all
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes  # noqa: E402

# Reuse the proven helpers from the direct Dropbox builder: identical
# patient extraction, device mapping, filename sanitization and Dropbox
# listing, so both builders obey K's rules the same way.
from tools.build_1raw import (  # noqa: E402
    DEVICE_FOLDERS,
    DROPBOX_ROOT,
    dest_exists,
    extract_patient_folder,
    get_dropbox_client,
    is_patient_folder_name,
    list_dropbox_tree,
    load_patient_regex,
    load_projects_config,
    normalize_device,
    sanitize_filename,
)

import pandas as pd  # noqa: E402

RAW_CONTAINER = "rawdata"
DST_CONTAINER = "1-raw"


def list_catalog_blobs(raw, code: str) -> list[str]:
    """Names of the encrypted parse catalogs for a project in rawdata."""
    out = []
    for b in raw.list_blobs(name_starts_with=f"{code}/catalog"):
        if b.name.endswith(".json.enc"):
            out.append(b.name)
    return sorted(out)


def load_catalogs(raw, code: str) -> tuple[list[dict], list[str]]:
    """Download + decrypt all catalogs for a project. Later catalogs win
    on duplicate relpaths (re-ingested changed files)."""
    names = list_catalog_blobs(raw, code)
    merged: dict[str, dict] = {}
    for name in names:
        try:
            token = raw.get_blob_client(name).download_blob().readall()
            entries = json.loads(decrypt_bytes(token).decode("utf-8"))
        except Exception as e:
            print(f"[migrate] catalog unreadable {name}: {e}", flush=True)
            continue
        if not isinstance(entries, list):
            print(f"[migrate] catalog {name} is not a list, skipped",
                  flush=True)
            continue
        for e in entries:
            if isinstance(e, dict) and e.get("file"):
                merged[e["file"]] = e  # later catalog overrides
        print(f"[migrate] catalog {name}: {len(entries)} entries",
              flush=True)
    return list(merged.values()), names


def resolve_blob_name(code: str, device: str, parquet_sha256: str) -> str:
    """Mirror tools.legacy_pipeline._parquet_blob_name exactly."""
    dev = (str(device) or "other").lower()
    return f"{code}/parquet/{dev}_{parquet_sha256[:8]}.parquet.enc"


def build_dropbox_patient_set(dbx, project: str) -> set[str]:
    """Authoritative patient folder names from Dropbox (header-only listing,
    no downloads). Exact strings, case-sensitive."""
    cfg = load_projects_config()
    proj_cfg = cfg.get("projects", {}).get(project, {})
    dropbox_folder = proj_cfg.get("dropbox", project)
    data_roots = proj_cfg.get("data_roots", ["Database/RawData"])
    regex = load_patient_regex(project)
    valid: set[str] = set()
    for data_root in data_roots:
        root = f"{DROPBOX_ROOT}/{dropbox_folder}/{data_root}"
        try:
            _, folders = list_dropbox_tree(dbx, root)
        except Exception as e:
            print(f"[migrate] Dropbox folder listing failed {root}: {e}",
                  flush=True)
            continue
        for fname in folders:
            if is_patient_folder_name(fname, regex):
                valid.add(fname)
    return valid


def patient_from_entry(entry: dict, regex) -> str | None:
    """Patient folder from the catalog's Dropbox relpath (preferred), with
    the catalog's own source_label as cross-check (never invented)."""
    rel = str(entry.get("file", "") or "")
    patient = extract_patient_folder(rel, regex)
    label = entry.get("source_label")
    if patient and label and patient != label:
        print(f"[migrate] NOTE patient mismatch in catalog: relpath gives "
              f"'{patient}' but source_label is '{label}' ({rel})",
              flush=True)
    return patient or (str(label) if label else None)


def migrate_project(svc, dbx, project: str,
                    max_patients: int | None = None,
                    dry_run: bool = False) -> dict:
    raw = svc.get_container_client(RAW_CONTAINER)
    dst = svc.get_container_client(DST_CONTAINER)

    # Resolve the exact config key (blob prefixes use the verbatim key).
    cfg = load_projects_config()
    code = None
    for k in cfg.get("projects", {}):
        if k.upper() == project.strip().upper():
            code = k
            break
    if code is None:
        print(f"[migrate] {project}: not in config/projects.yaml, skipped",
              flush=True)
        return {"error": "unknown_project"}

    print(f"[migrate] === {code} ===", flush=True)
    entries, catalog_names = load_catalogs(raw, code)
    if not catalog_names:
        print(f"[migrate] {code}: NO catalog blobs in rawdata — cannot "
              f"recover patient identity, skipping project (nothing guessed)",
              flush=True)
        return {"error": "no_catalog", "catalog_blobs": []}

    regex = load_patient_regex(code)
    valid_patients = build_dropbox_patient_set(dbx, code)
    print(f"[migrate] {code}: {len(entries)} catalog entries, "
          f"{len(valid_patients)} Dropbox patient folders", flush=True)

    # Test mode: keep only the first N patient folders (sorted). Names stay
    # exact Dropbox strings.
    if max_patients:
        kept = sorted(valid_patients)[:max_patients]
        print(f"[migrate] TEST MODE: limiting to patients {kept}", flush=True)
        valid_patients = set(kept)

    # Usable entries: parsed files (+ duplicates resolved to their original).
    by_file = {e["file"]: e for e in entries}
    usable: list[dict] = []
    n_dup = n_dead = 0
    for e in entries:
        if e.get("parquet_sha256") and e.get("parse_status") == "parsed":
            usable.append(e)
        elif e.get("parse_status") == "duplicate" and e.get("duplicate_of"):
            orig = by_file.get(e["duplicate_of"])
            if orig and orig.get("parquet_sha256"):
                e = dict(e)
                e["parquet_sha256"] = orig["parquet_sha256"]
                e["device"] = orig.get("device", e.get("device"))
                usable.append(e)
                n_dup += 1
            else:
                n_dead += 1
        else:
            n_dead += 1
    print(f"[migrate] {code}: {len(usable)} usable entries "
          f"({n_dup} duplicates resolved, {n_dead} unusable)", flush=True)

    # Group entries by their rawdata blob: download/decrypt each blob once.
    blob_groups: dict[str, list[dict]] = {}
    unresolved: list[str] = []
    for e in usable:
        blob = resolve_blob_name(code, e.get("device"), e["parquet_sha256"])
        if not blob:
            unresolved.append(str(e.get("file")))
            continue
        blob_groups.setdefault(blob, []).append(e)

    stats = {
        "project": code,
        "catalog_blobs": catalog_names,
        "catalog_entries": len(entries),
        "usable_entries": len(usable),
        "unique_blobs": len(blob_groups),
        "uploaded": 0,
        "skipped_exists": 0,
        "name_mismatch": 0,
        "mismatched_names": [],
        "missing_blobs": [],
        "integrity_fail": 0,
        "convert_fail": 0,
        "dry_run": dry_run,
        "example_paths": [],
    }
    mismatched: set[str] = set()

    for bi, (blob, group) in enumerate(sorted(blob_groups.items())):
        if bi % 25 == 0:
            print(f"[migrate] {code}: blob {bi}/{len(blob_groups)} ...",
                  flush=True)
        blob_client = raw.get_blob_client(blob)
        try:
            props = blob_client.get_blob_properties()
        except Exception:
            stats["missing_blobs"].append(blob)
            continue
        meta = props.metadata or {}
        try:
            token = blob_client.download_blob().readall()
            plaintext = decrypt_bytes(token)
        except Exception as e:
            print(f"[migrate] decrypt failed {blob}: {e}", flush=True)
            stats["integrity_fail"] += 1
            continue
        # Integrity: plaintext hash must equal the catalog/metadata hash.
        got = hashlib.sha256(plaintext).hexdigest()
        want = (meta.get("sha256") or "").strip()
        entry_sha = str(group[0].get("parquet_sha256") or "").strip()
        if (want and got != want) or (entry_sha and got != entry_sha):
            print(f"[migrate] INTEGRITY FAIL {blob}: hash mismatch",
                  flush=True)
            stats["integrity_fail"] += 1
            continue
        try:
            df = pd.read_parquet(io.BytesIO(plaintext))
        except Exception as e:
            print(f"[migrate] parquet read failed {blob}: {e}", flush=True)
            stats["convert_fail"] += 1
            continue
        if df is None or df.empty:
            stats["convert_fail"] += 1
            continue
        try:
            csv_data = df.to_csv(index=False).encode("utf-8")
        except Exception as e:
            print(f"[migrate] csv convert failed {blob}: {e}", flush=True)
            stats["convert_fail"] += 1
            continue
        finally:
            del df

        # One CSV per catalog entry (every Dropbox file counts, even
        # duplicates sharing one blob).
        for e in group:
            patient = patient_from_entry(e, regex)
            if not patient or patient not in valid_patients:
                stats["name_mismatch"] += 1
                if patient:
                    mismatched.add(patient)
                continue
            device = normalize_device(e.get("device"))
            dest_name = sanitize_filename(
                str(e.get("name") or e.get("file", "file")))
            dest = f"{code}/{patient}/{device}/{dest_name}"
            if len(stats["example_paths"]) < 5:
                stats["example_paths"].append(dest)
            if dest_exists(dst, dest):
                stats["skipped_exists"] += 1
                continue
            if dry_run:
                stats["uploaded"] += 1
                continue
            blob_meta = {
                "dropbox_relpath": str(e.get("file", ""))[:1000],
                "parquet_sha256": entry_sha,
                "source_blob": blob,
                "project": code,
                "patient": patient,
                "device": device,
                "migrated_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                dst.get_blob_client(dest).upload_blob(
                    csv_data, overwrite=True,
                    content_settings={"content_type": "text/csv"},
                    metadata=blob_meta,
                )
                stats["uploaded"] += 1
            except Exception as ex:
                print(f"[migrate] upload failed {dest}: {ex}", flush=True)
                stats["convert_fail"] += 1
        del csv_data

        if (bi + 1) % 50 == 0:
            print(f"[migrate] {code}: {stats['uploaded']} uploaded, "
                  f"{stats['skipped_exists']} existed...", flush=True)

    stats["mismatched_names"] = sorted(mismatched)
    print(f"[migrate] {code}: {stats['uploaded']} uploaded, "
          f"{stats['skipped_exists']} existed, "
          f"{stats['name_mismatch']} name-mismatch, "
          f"{len(stats['missing_blobs'])} missing blobs, "
          f"{stats['integrity_fail']} integrity-fail, "
          f"{stats['convert_fail']} convert-fail", flush=True)
    if mismatched:
        print(f"[migrate] {code}: mismatched names skipped (never created): "
              f"{sorted(mismatched)}", flush=True)
    return stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="COLECTOMIE",
                    help="Project code (e.g. COLECTOMIE) or 'all'")
    ap.add_argument("--out", default="migrate_out",
                    help="Local dir for the migration manifest")
    ap.add_argument("--max-patients", type=int, default=None,
                    help="Test mode: only the first N patient folders "
                         "(sorted). Omit for all patients.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Resolve everything but upload nothing.")
    args = ap.parse_args()

    if args.max_patients:
        print(f"[migrate] TEST MODE: max {args.max_patients} patients",
              flush=True)
    if args.dry_run:
        print("[migrate] DRY RUN: no uploads", flush=True)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    azure_auth.ensure_container(svc, DST_CONTAINER)
    print(f"[migrate] container ready: {DST_CONTAINER}", flush=True)

    dbx = get_dropbox_client()
    print("[migrate] Dropbox connected (folder validation only, "
          "no file downloads)", flush=True)

    if args.project.strip().lower() == "all":
        cfg = load_projects_config()
        projects = sorted([k for k, v in cfg.get("projects", {}).items()
                           if v.get("status") == "data"])
    else:
        projects = [args.project.strip()]
    print(f"[migrate] projects: {projects}", flush=True)

    manifest: dict = {"container": DST_CONTAINER,
                      "source": RAW_CONTAINER, "projects": {}}
    for project in projects:
        manifest["projects"][project] = migrate_project(
            svc, dbx, project,
            max_patients=args.max_patients, dry_run=args.dry_run)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / f"migrate_manifest_{args.project}.json"
    mpath.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"[migrate] manifest: {mpath}", flush=True)
    print("[migrate] DONE. Identity came from the ingest's own catalogs; "
          "patient names are REAL Dropbox folder names.", flush=True)


if __name__ == "__main__":
    main()
