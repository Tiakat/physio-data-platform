"""Restructure Azure into K's 4-container layout.

K's order: exactly 4 containers - raw, processed, graphes, analysis.
Same architecture everywhere: {PROJECT}/{patient}/files.

- raw/{PROJECT}/{patient}/{file}.csv
    Source: decrypt rawdata/{PROJECT}/**/*.parquet.enc -> CSV.
    Patient folder from lineage.json source_blob mapping (ground truth);
    raw files with no processed counterpart go to raw/{PROJECT}/unprocessed/.
- processed/{PROJECT}/{patient}/{file}.csv
    Source: decrypt processed/level2/{PROJECT}/{patient}/*.parquet.enc -> CSV.
    Because a 'processed' container already exists with the old level2 layout,
    CSVs are staged in 'processed-new'; the erase-old phase swaps it into place.
- graphes/{PROJECT}/{patient}/{file}.png
    Source: processed/level2/{PROJECT}/{patient}/graphs/*.png AND the existing
    graphs/{PROJECT}/{patient}/*.png (dedup by destination name).
    content_type=image/png so PNGs preview in the browser.
- analysis/{PROJECT}/{patient}/
    Created empty; populated later by the stats phase (PNG + Excel).

Containers are private (default). Nothing is deleted here.

Usage:
    python -m tools.restructure_azure --project COLECTOMIE   # one project (pilot)
    python -m tools.restructure_azure --project all          # everything

Resumable: blobs that already exist at the destination are skipped.
"""

from __future__ import annotations

import argparse
import io
import json
import os
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


def safe_patient(label: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in label).strip("_")


def ensure_containers(svc):
    for name in NEW_CONTAINERS:
        try:
            svc.create_container(name)
            print(f"[restructure] created container: {name}", flush=True)
        except Exception as exc:  # already exists
            if "ContainerAlreadyExists" not in type(exc).__name__:
                raise
            print(f"[restructure] container exists: {name}", flush=True)


def blob_names(container, prefix=""):
    for b in container.list_blobs(name_starts_with=prefix):
        yield b["name"] if isinstance(b, dict) else b.name


def build_lineage_map(svc, project: str) -> dict[str, str]:
    """Map raw source_blob path -> safe patient folder, from lineage.json files."""
    proc = svc.get_container_client("processed")
    mapping: dict[str, str] = {}
    prefix = f"processed/level2/{project}/"
    for name in blob_names(proc, prefix):
        if not name.endswith("/lineage.json"):
            continue
        parts = name.split("/")
        # processed/level2/{PROJECT}/{patient}/lineage.json
        if len(parts) != 5:
            continue
        patient = parts[3]
        try:
            raw = proc.get_blob_client(name).download_blob().readall()
            lin = json.loads(raw)
            src = lin.get("source_blob")
            if src:
                mapping[src] = patient
        except Exception as exc:
            print(f"[restructure] WARN could not read {name}: {exc}", flush=True)
    print(f"[restructure] lineage map for {project}: {len(mapping)} entries", flush=True)
    return mapping


def dest_exists(container, blob: str) -> bool:
    try:
        container.get_blob_client(blob).get_blob_properties()
        return True
    except Exception:
        return False


def migrate_raw(svc, project: str, lineage_map: dict[str, str]) -> tuple[int, int]:
    """rawdata/{PROJECT}/**/*.parquet.enc -> raw/{PROJECT}/{patient}/{file}.csv"""
    src = svc.get_container_client("rawdata")
    dst = svc.get_container_client("raw")
    done, skipped = 0, 0
    for blob in sorted(blob_names(src, f"{project}/")):
        if not blob.endswith(".parquet.enc"):
            continue
        patient = lineage_map.get(blob, "unprocessed")
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
    print(f"[restructure] raw {project}: {done} new, {skipped} already existed",
          flush=True)
    return done, skipped


def migrate_processed(svc, project: str) -> tuple[int, int]:
    """processed/level2/{PROJECT}/{patient}/*.parquet.enc -> processed-new/.../*.csv"""
    src = svc.get_container_client("processed")
    dst = svc.get_container_client(STAGING_PROCESSED)
    done, skipped = 0, 0
    prefix = f"processed/level2/{project}/"
    for blob in sorted(blob_names(src, prefix)):
        if not blob.endswith(".parquet.enc"):
            continue
        parts = blob.split("/")
        # processed/level2/{PROJECT}/{patient}/{file}.parquet.enc
        if len(parts) != 5:
            continue
        patient, fname = parts[3], parts[4].removesuffix(".parquet.enc")
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
    print(f"[restructure] processed {project}: {done} new, {skipped} already "
          f"existed", flush=True)
    return done, skipped


def migrate_graphs(svc, project: str) -> tuple[int, int]:
    """processed/level2 graphs + graphs container -> graphes/{PROJECT}/{patient}/"""
    dst = svc.get_container_client("graphes")
    done, skipped = 0, 0

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

    # Source 1: processed/level2/{PROJECT}/{patient}/graphs/*.png
    proc = svc.get_container_client("processed")
    prefix = f"processed/level2/{project}/"
    for blob in sorted(blob_names(proc, prefix)):
        parts = blob.split("/")
        # processed/level2/{PROJECT}/{patient}/graphs/{file}.png
        if len(parts) != 6 or parts[4] != "graphs" or not blob.endswith(".png"):
            continue
        patient, fname = parts[3], parts[5]
        try:
            _copy_from(proc, blob, f"{project}/{patient}/{fname}")
        except Exception as exc:
            print(f"[restructure] ERROR graph {blob}: {exc}", flush=True)

    # Source 2: existing graphs/{PROJECT}/{patient}/*.png (dedup by dest name)
    try:
        old = svc.get_container_client("graphs")
        for blob in sorted(blob_names(old, f"{project}/")):
            if not blob.endswith(".png"):
                continue
            parts = blob.split("/")
            if len(parts) < 3:
                continue
            patient, fname = parts[1], parts[-1]
            try:
                _copy_from(old, blob, f"{project}/{patient}/{fname}")
            except Exception as exc:
                print(f"[restructure] ERROR graph {blob}: {exc}", flush=True)
    except Exception as exc:
        print(f"[restructure] WARN old graphs container: {exc}", flush=True)

    print(f"[restructure] graphes {project}: {done} new, {skipped} already "
          f"existed", flush=True)
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
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)

    ensure_containers(svc)

    projects = list_projects(svc) if args.project.strip().lower() == "all" \
        else [args.project.strip().upper()]
    print(f"[restructure] projects: {projects}", flush=True)

    manifest: dict = {"projects": {}}
    for project in projects:
        print(f"[restructure] === {project} ===", flush=True)
        lineage_map = build_lineage_map(svc, project)
        r_new, r_skip = migrate_raw(svc, project, lineage_map)
        p_new, p_skip = migrate_processed(svc, project)
        g_new, g_skip = migrate_graphs(svc, project)
        manifest["projects"][project] = {
            "raw": {"new": r_new, "skipped": r_skip},
            "processed": {"new": p_new, "skipped": p_skip},
            "graphes": {"new": g_new, "skipped": g_skip},
        }

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / f"restructure_manifest_{args.project}.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    print(f"[restructure] manifest: {mpath}", flush=True)
    print("[restructure] DONE. analysis container created empty; old containers "
          "untouched (erase-old runs only after K confirms).", flush=True)


if __name__ == "__main__":
    main()
