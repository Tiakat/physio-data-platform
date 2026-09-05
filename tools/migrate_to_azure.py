"""
One time migration of the existing Dropbox archive into Azure Blob Storage.

Reads from the locally synced Dropbox folder and writes into the rawdata container,
under a normalised key that encodes project, patient, device and original file name.
The original file content is never altered.

    rawdata/<project>/<participant>/<device>/<original file name>

What it does
    - walks the archive
    - classifies each file into tier A parsed, B kept, C ignored
    - computes a full SHA-256
    - skips a file already present in Azure with the same checksum, so the script
      is safely restartable and can be run again after new patients are added
    - writes a manifest CSV recording every decision, which becomes the input to
      the database ingestion step

What it deliberately does not do
    - it does not rename, reformat or clean anything
    - it does not skip unknown file types, they are uploaded and marked tier B

Dry run first, always:

    python -m tools.migrate_to_azure --root "C:/Dropbox/Liam/Projects actifs" --dry-run
    python -m tools.migrate_to_azure --root "C:/Dropbox/Liam/Projects actifs" --project IPAMS
    python -m tools.migrate_to_azure --root "C:/Dropbox/Liam/Projects actifs"
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

from backbone.discover import classify          # same tier rules as discovery

try:
    from azure.storage.blob import BlobServiceClient, StandardBlobTier
except ImportError:
    BlobServiceClient = None

CHUNK = 8 * 1024 * 1024

# Folder names that identify a device, matched case and space insensitively.
# This is the one place where the naming chaos documented in the survey is absorbed.
DEVICE_PATTERNS = [
    ("bettercare", re.compile(r"^better\s*care$", re.I)),
    ("infinity",   re.compile(r"^(the\s+)?infinity(_brute)?$", re.I)),
    ("nol",        re.compile(r"^(nol|medasense(_data)?)$", re.I)),
    ("bis",        re.compile(r"^bis$", re.I)),
    ("pump",       re.compile(r"^(remi|propofol|perfusion|pump|dexmedetomidine)$", re.I)),
    ("photo",      re.compile(r"^photos?$", re.I)),
]

# Folders that are containers, not devices, and should be stepped through.
TRANSPARENT = re.compile(
    r"^(database|rawdata|raw\s*data|extracted\s*data|extracteddata|"
    r"included\s+patients|superposition)$", re.I)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def normalise_participant(name: str) -> str:
    """
    Turn any of the observed patient folder conventions into a bare number.

        "12"                          -> 12
        "Patient 13 20240905 PLL"     -> 13
        "PROMISES 61 6 aout 2025"     -> 61
        "Patient 37 23-04-2025"       -> 37

    The initials present in some PROMISES folder names are dropped here, which is
    deliberate: they are directly identifying and must not travel to the cloud.
    """
    m = re.search(r"(?:patient|promises)?\s*0*(\d{1,4})", name, re.I)
    return m.group(1) if m else None


def normalise_project(name: str) -> str:
    """
    "ESMONOL Patients recrutes" -> ESMONOL,  "V-RAPS" -> V-RAPS.
    Accents removed, first token kept, so blob keys stay ASCII and predictable.
    """
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    token = re.split(r"[\s_]+", plain.strip())[0]
    return re.sub(r"[^A-Za-z0-9-]", "", token).upper() or "UNKNOWN"


def classify_device(parts: list[str]) -> tuple[str, str | None]:
    """
    Walk the path from the deepest folder upward looking for a device name.
    Returns (device, qualifier). The qualifier preserves information that would
    otherwise be lost, for example which drug a pump file belongs to.
    """
    for i in range(len(parts) - 1, -1, -1):
        segment = parts[i].strip()
        for code, pattern in DEVICE_PATTERNS:
            if pattern.match(segment):
                qualifier = None
                if code == "pump":
                    # the matched folder IS the drug name, keep it
                    qualifier = segment.lower()
                elif i + 1 < len(parts):
                    # a meaningful subfolder below the device, for example Radiale
                    nxt = parts[i + 1].strip()
                    if not TRANSPARENT.match(nxt) and not re.match(r"^[\d\W_]+$", nxt):
                        qualifier = re.sub(r"[^A-Za-z0-9-]+", "_", nxt).strip("_").lower()[:40]
                return code, qualifier
    return "other", None


def plan_key(root: Path, path: Path) -> dict:
    """Work out project, participant, device and destination key for one file."""
    rel = path.relative_to(root)
    parts = list(rel.parts[:-1])
    project = normalise_project(parts[0]) if parts else "UNKNOWN"

    participant = None
    for seg in parts[1:]:
        if TRANSPARENT.match(seg.strip()):
            continue
        cand = normalise_participant(seg)
        if cand:
            participant = cand
            break

    device, qualifier = classify_device(parts)
    if participant is None:
        participant = "unassigned"

    leaf = f"{device}/{qualifier}" if qualifier else device
    key = f"{project}/{participant}/{leaf}/{path.name}"
    return {"project": project, "participant": participant, "device": device,
            "qualifier": qualifier or "", "key": key, "source": str(rel)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True, help="Local Dropbox archive folder")
    ap.add_argument("--container", default="rawdata")
    ap.add_argument("--project", help="Migrate one project only")
    ap.add_argument("--manifest", default="reports/migration_manifest.csv")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--conn", default=os.environ.get("AZURE_STORAGE_CONNECTION_STRING"))
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    client = None
    existing = {}
    if not args.dry_run:
        if BlobServiceClient is None:
            sys.exit("pip install azure-storage-blob")
        if not args.conn:
            sys.exit("Set AZURE_STORAGE_CONNECTION_STRING or pass --conn")
        client = BlobServiceClient.from_connection_string(args.conn)
        container = client.get_container_client(args.container)
        try:
            container.create_container()
        except Exception:
            pass
        print("reading what is already uploaded ...", flush=True)
        for blob in container.list_blobs(include=["metadata"]):
            if blob.metadata and "sha256" in blob.metadata:
                existing[blob.name] = blob.metadata["sha256"]
        print(f"  {len(existing)} blobs already present")

    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    fh = open(args.manifest, "w", newline="", encoding="utf-8")
    writer = csv.DictWriter(fh, fieldnames=[
        "source", "project", "participant", "device", "qualifier", "tier",
        "key", "size_bytes", "sha256", "action"])
    writer.writeheader()

    counts = {"uploaded": 0, "skipped_same": 0, "skipped_tierC": 0, "failed": 0}
    bytes_up = 0
    started = time.time()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            path = Path(dirpath) / fn
            try:
                rel_parts = path.relative_to(root).parts
            except ValueError:
                continue
            if args.project and rel_parts and rel_parts[0] != args.project:
                continue

            tier = classify(path)
            info = plan_key(root, path)
            info["tier"] = tier
            try:
                info["size_bytes"] = path.stat().st_size
            except OSError as exc:
                info.update(action=f"failed: {exc}", sha256="")
                writer.writerow(info); counts["failed"] += 1
                continue

            if tier == "C":
                info.update(action="skipped_tierC", sha256="")
                writer.writerow(info); counts["skipped_tierC"] += 1
                continue

            info["sha256"] = "" if args.dry_run else sha256_file(path)

            if args.dry_run:
                info["action"] = "would_upload"
                writer.writerow(info)
                counts["uploaded"] += 1
                bytes_up += info["size_bytes"]
                continue

            if existing.get(info["key"]) == info["sha256"]:
                info["action"] = "skipped_same"
                writer.writerow(info); counts["skipped_same"] += 1
                continue

            try:
                blob = client.get_blob_client(args.container, info["key"])
                with path.open("rb") as data:
                    blob.upload_blob(
                        data, overwrite=True, max_concurrency=4,
                        metadata={"sha256": info["sha256"],
                                  "tier": tier,
                                  "source": info["source"][:8000]})
                info["action"] = "uploaded"
                counts["uploaded"] += 1
                bytes_up += info["size_bytes"]
            except Exception as exc:                 # one bad file never stops the rest
                info["action"] = f"failed: {exc}"
                counts["failed"] += 1
            writer.writerow(info)
            fh.flush()

            done = sum(counts.values())
            if done % 25 == 0:
                rate = bytes_up / max(time.time() - started, 1) / 1e6
                print(f"  {done} files, {bytes_up/1e9:.2f} GB, {rate:.1f} MB/s", flush=True)

    fh.close()
    print()
    for k, v in counts.items():
        print(f"{k:<16}{v:>8}")
    print(f"{'GB moved':<16}{bytes_up/1e9:>8.2f}")
    print(f"\nManifest: {args.manifest}")
    if args.dry_run:
        print("Dry run. Nothing was uploaded. Remove --dry-run to proceed.")


if __name__ == "__main__":
    main()
