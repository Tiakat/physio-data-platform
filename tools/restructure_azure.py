"""Restructure Azure into K's 4-container layout.

K's order: exactly 4 containers - raw, processed, graphes, analysis.
Same architecture everywhere: {PROJECT}/{patient}/files.

Patient names are REAL Dropbox folder names (from ingest state),
NOT provisional numbers. K: "keep the patient name exactly like you
found them in dropbox".

- raw/{PROJECT}/{patient}/{file}.csv
    Source: decrypt rawdata/{PROJECT}/**/*.parquet.enc -> CSV.
    Patient folder from ingest state (blob -> Dropbox path mapping).
- processed-new/{PROJECT}/{patient}/{file}.csv (staging)
    Source: decrypt processed/level2/{PROJECT}/*/*.parquet.enc -> CSV.
    Patient from lineage.json source_blob -> ingest state -> Dropbox.
- graphes/{PROJECT}/{patient}/{file}.png
    Source: processed/level2 graphs + existing graphs container.
    Patient names resolved via same mapping.
- analysis/{PROJECT}/{patient}/
    Created empty; populated later by stats phase.

Containers are private (default). Nothing is deleted here.

Usage:
    python -m tools.restructure_azure --project COLECTOMIE
    python -m tools.restructure_azure --project all
    python -m tools.restructure_azure --project all --erase-wrong
        Also delete blobs with provisional patient names (patient_1, etc.)

Resumable: blobs that already exist at the destination are skipped.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402

import pandas as pd  # noqa: E402

STAGING_PROCESSED = "processed-new"
NEW_CONTAINERS = ["raw", "graphes", "analysis", STAGING_PROCESSED]


def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet

    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)


def ensure_containers(svc):
    for name in NEW_CONTAINERS:
        azure_auth.ensure_container(svc, name)


def blob_names(container, prefix=""):
    for b in container.list_blobs(name_starts_with=prefix):
        yield b["name"] if isinstance(b, dict) else b.name


def dest_exists(container, blob: str) -> bool:
    try:
        container.get_blob_client(blob).get_blob_properties()
        return True
    except Exception:
        return False


def load_state(account: str) -> dict:
    svc = azure_auth.get_blob_service_client(account)
    blob = svc.get_blob_client("processed", "processed/_pipeline/state.json.enc")
    raw = blob.download_blob().readall()
    return json.loads(decrypt_bytes(raw).decode("utf-8"))


def load_patient_regex(code: str) -> re.Pattern | None:
    for fname in [f"{code.lower()}.yaml", f"{code.lower()}.yml"]:
        p = Path(__file__).resolve().parent.parent / "profiles" / fname
        if p.exists():
            try:
                import yaml
                doc = yaml.safe_load(p.read_text())
                rs = doc.get("layout", {}).get("patient_dir_regex")
                if rs:
                    return re.compile(rs)
            except Exception:
                pass
    return None


def extract_patient_folder(relpath: str, regex: re.Pattern | None) -> str | None:
    parts = relpath.replace("\\", "/").split("/")
    if regex:
        for part in parts:
            if regex.match(part):
                return part
    for part in parts:
        if part.isdigit():
            return part
    return None


def build_blob_to_patient_map(account: str) -> dict[str, str]:
    """Map Azure rawdata blob name -> REAL Dropbox patient folder name."""
    print("[restructure] loading ingest state for real Dropbox patient names...",
          flush=True)
    state = load_state(account)
    mapping: dict[str, str] = {}
    regex_cache: dict[str, re.Pattern | None] = {}
    legacy = state.get("legacy", {})
    for code, proj_data in legacy.items():
        if code not in regex_cache:
            regex_cache[code] = load_patient_regex(code)
        regex = regex_cache[code]
        files = proj_data.get("files", {})
        for relpath, entry in files.items():
            if not isinstance(entry, dict) or entry.get("status") != "ok":
                continue
            blob = entry.get("stored")
            if not blob:
                continue
            patient = extract_patient_folder(relpath, regex)
            if patient:
                mapping[blob] = patient
    print(f"[restructure] mapped {len(mapping)} blobs to real Dropbox patient folders",
          flush=True)
    return mapping


def is_provisional(name: str) -> bool:
    """Check if a patient folder name looks like a provisional number."""
    # Provisional: "patient_1", "patient 1", "patient1"
    # Real: "103", "1", "P-0103" (but "1" alone is ambiguous - check context)
    # We treat "patient_N" patterns as provisional
    return bool(re.match(r"^patient[_\s]*\d+$", name, re.IGNORECASE))


def erase_wrong_blobs(svc, project: str):
    """Delete blobs in new containers that use provisional patient names."""
    print(f"[restructure] erasing blobs with provisional names for {project}...",
          flush=True)
    total = 0
    for container_name in ["raw", STAGING_PROCESSED, "graphes"]:
        cont = svc.get_container_client(container_name)
        for blob in list(blob_names(cont, f"{project}/")):
            parts = blob.split("/")
            if len(parts) >= 2 and is_provisional(parts[1]):
                cont.get_blob_client(blob).delete_blob()
                total += 1
    print(f"[restructure] erased {total} blobs with provisional names", flush=True)


def build_lineage_patient_map(svc, project: str,
                              blob_to_patient: dict[str, str]) -> dict[str, str]:
    """Map processed/level2 blob path -> REAL Dropbox patient folder.
    
    Reads lineage.json to get source_blob, then looks up real patient name.
    """
    proc = svc.get_container_client("processed")
    mapping: dict[str, str] = {}
    prefix = f"processed/level2/{project}/"
    for name in blob_names(proc, prefix):
        if not name.endswith("/lineage.json"):
            continue
        try:
            raw = proc.get_blob_client(name).download_blob().readall()
            lin = json.loads(raw)
            src = lin.get("source_blob")
            if src:
                real_patient = blob_to_patient.get(src)
                if real_patient:
                    # Map the level2 directory to real patient name
                    # name = processed/level2/{PROJECT}/{prov}/lineage.json
                    prov_patient = name.split("/")[3]
                    mapping[prov_patient] = real_patient
        except Exception as exc:
            print(f"[restructure] WARN could not read {name}: {exc}", flush=True)
    print(f"[restructure] resolved {len(mapping)} provisional -> real patient names "
          f"for {project}", flush=True)
    return mapping


def migrate_raw(svc, project: str, blob_to_patient: dict[str, str]) -> tuple[int, int]:
    """rawdata/{PROJECT}/**/*.parquet.enc -> raw/{PROJECT}/{real_patient}/{file}.csv"""
    src = svc.get_container_client("rawdata")
    dst = svc.get_container_client("raw")
    done, skipped, unmapped = 0, 0, 0
    for blob in sorted(blob_names(src, f"{project}/")):
        if not blob.endswith(".parquet.enc"):
            continue
        patient = blob_to_patient.get(blob)
        if not patient:
            unmapped += 1
            continue  # Skip rather than use wrong name
        base = blob.rsplit("/", 1)[-1].removesuffix(".parquet.enc")
        dest = f"{project}/{patient}/{base}.csv"
        if dest_exists(dst, dest):
            skipped += 1
            continue
        try:
            raw = src.get_blob_client(blob).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
            csv_data = df.to_csv(index=False).encode("utf-8")
            dst.get_blob_client(dest).upload_blob(
                csv_data, overwrite=True,
                content_settings={"content_type": "text/csv"},
            )
            done += 1
            del df, csv_data, raw
        except Exception as exc:
            print(f"[restructure] ERROR raw {blob}: {exc}", flush=True)
        if (done + skipped) % 50 == 0:
            print(f"[restructure] raw {project}: {done} new, {skipped} skipped...",
                  flush=True)
    print(f"[restructure] raw {project}: {done} new, {skipped} existed, "
          f"{unmapped} unmapped (skipped)", flush=True)
    return done, skipped


def migrate_processed(svc, project: str,
                      prov_to_real: dict[str, str]) -> tuple[int, int]:
    """processed/level2 -> processed-new/{PROJECT}/{real_patient}/*.csv"""
    src = svc.get_container_client("processed")
    dst = svc.get_container_client(STAGING_PROCESSED)
    done, skipped, unmapped = 0, 0, 0
    prefix = f"processed/level2/{project}/"
    for blob in sorted(blob_names(src, prefix)):
        if not blob.endswith(".parquet.enc"):
            continue
        parts = blob.split("/")
        if len(parts) != 5:
            continue
        prov_patient, fname = parts[3], parts[4].removesuffix(".parquet.enc")
        patient = prov_to_real.get(prov_patient)
        if not patient:
            unmapped += 1
            continue
        dest = f"{project}/{patient}/{fname}.csv"
        if dest_exists(dst, dest):
            skipped += 1
            continue
        try:
            raw = src.get_blob_client(blob).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
            csv_data = df.to_csv(index=False).encode("utf-8")
            dst.get_blob_client(dest).upload_blob(
                csv_data, overwrite=True,
                content_settings={"content_type": "text/csv"},
            )
            done += 1
            del df, csv_data, raw
        except Exception as exc:
            print(f"[restructure] ERROR processed {blob}: {exc}", flush=True)
        if (done + skipped) % 50 == 0:
            print(f"[restructure] processed {project}: {done} new, "
                  f"{skipped} skipped...", flush=True)
    print(f"[restructure] processed {project}: {done} new, {skipped} existed, "
          f"{unmapped} unmapped", flush=True)
    return done, skipped


def migrate_graphs(svc, project: str,
                   prov_to_real: dict[str, str]) -> tuple[int, int]:
    """Graphs -> graphes/{PROJECT}/{real_patient}/"""
    dst = svc.get_container_client("graphes")
    done, skipped, unmapped = 0, 0, 0

    def _copy_from(container, blob: str, dest: str):
        nonlocal done, skipped
        if dest_exists(dst, dest):
            skipped += 1
            return
        data = container.get_blob_client(blob).download_blob().readall()
        dst.get_blob_client(dest).upload_blob(
            data, overwrite=True,
            content_settings={"content_type": "image/png"},
        )
        done += 1

    # Source 1: processed/level2/{PROJECT}/{prov}/graphs/*.png
    proc = svc.get_container_client("processed")
    prefix = f"processed/level2/{project}/"
    for blob in sorted(blob_names(proc, prefix)):
        parts = blob.split("/")
        if len(parts) != 6 or parts[4] != "graphs" or not blob.endswith(".png"):
            continue
        prov_patient, fname = parts[3], parts[5]
        patient = prov_to_real.get(prov_patient)
        if not patient:
            unmapped += 1
            continue
        try:
            _copy_from(proc, blob, f"{project}/{patient}/{fname}")
        except Exception as exc:
            print(f"[restructure] ERROR graph {blob}: {exc}", flush=True)

    # Source 2: existing graphs/{PROJECT}/{prov}/*.png
    try:
        old = svc.get_container_client("graphs")
        for blob in sorted(blob_names(old, f"{project}/")):
            if not blob.endswith(".png"):
                continue
            parts = blob.split("/")
            if len(parts) < 3:
                continue
            prov_patient, fname = parts[1], parts[-1]
            # Old graphs container may already have real names or provisional
            patient = prov_to_real.get(prov_patient, prov_patient)
            if is_provisional(patient):
                unmapped += 1
                continue
            try:
                _copy_from(old, blob, f"{project}/{patient}/{fname}")
            except Exception as exc:
                print(f"[restructure] ERROR graph {blob}: {exc}", flush=True)
    except Exception as exc:
        print(f"[restructure] WARN old graphs container: {exc}", flush=True)

    print(f"[restructure] graphes {project}: {done} new, {skipped} existed, "
          f"{unmapped} unmapped", flush=True)
    return done, skipped


def list_projects(svc) -> list[str]:
    src = svc.get_container_client("rawdata")
    codes = set()
    for b in src.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        parts = name.split("/")
        if parts:
            codes.add(parts[0].upper())
    return sorted(codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="all",
                    help="Project code (e.g. COLECTOMIE) or 'all'")
    ap.add_argument("--out", default="restructure_out",
                    help="Local dir for the migration manifest")
    ap.add_argument("--erase-wrong", action="store_true",
                    help="Delete blobs with provisional patient names first")
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)

    ensure_containers(svc)

    # Build the global blob -> real Dropbox patient map ONCE
    blob_to_patient = build_blob_to_patient_map(account)

    projects = list_projects(svc) if args.project.strip().lower() == "all" \
        else [args.project.strip().upper()]
    print(f"[restructure] projects: {projects}", flush=True)

    manifest: dict = {"projects": {}}
    for project in projects:
        print(f"[restructure] === {project} ===", flush=True)
        if args.erase_wrong:
            erase_wrong_blobs(svc, project)
        prov_to_real = build_lineage_patient_map(svc, project, blob_to_patient)
        r_new, r_skip = migrate_raw(svc, project, blob_to_patient)
        p_new, p_skip = migrate_processed(svc, project, prov_to_real)
        g_new, g_skip = migrate_graphs(svc, project, prov_to_real)
        manifest["projects"][project] = {
            "raw": {"new": r_new, "skipped": r_skip},
            "processed": {"new": p_new, "skipped": p_skip},
            "graphes": {"new": g_new, "skipped": g_skip},
            "patient_map_sample": dict(list(prov_to_real.items())[:5]),
        }

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / f"restructure_manifest_{args.project}.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    print(f"[restructure] manifest: {mpath}", flush=True)
    print("[restructure] DONE. Patient names are REAL Dropbox folder names.",
          flush=True)


if __name__ == "__main__":
    main()
