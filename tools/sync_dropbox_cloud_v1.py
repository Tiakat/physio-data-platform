import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import dropbox
from dotenv import load_dotenv
from azure.storage.blob import BlobServiceClient


load_dotenv()

DROPBOX_TOKEN = os.getenv("DROPBOX_ACCESS_TOKEN")
AZURE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
AZURE_KEY = os.getenv("AZURE_STORAGE_KEY")
AZURE_CONTAINER = os.getenv("AZURE_CONTAINER_RAW", "rawdata")

if not DROPBOX_TOKEN:
    raise RuntimeError("DROPBOX_ACCESS_TOKEN is missing from .env")

if not AZURE_ACCOUNT:
    raise RuntimeError("AZURE_STORAGE_ACCOUNT is missing from .env")

if not AZURE_KEY:
    raise RuntimeError("AZURE_STORAGE_KEY is missing from .env")


DROPBOX_ROOT = "/Liam/Projects actifs"
PROJECT_ROOT = "V-RAPS"

STATE_DIR = Path("sync_state")
REPORT_DIR = Path("sync_reports")

STATE_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)


dbx = dropbox.Dropbox(DROPBOX_TOKEN)

account = dbx.users_get_current_account()

print("Dropbox authentication successful.")
print(f"Connected account: {account.name.display_name}")


blob_service = BlobServiceClient(
    account_url=f"https://{AZURE_ACCOUNT}.blob.core.windows.net",
    credential=AZURE_KEY,
)

container = blob_service.get_container_client(AZURE_CONTAINER)


def list_dropbox_files(project):
    """List all files in the project's RawData folder.

    The nested RawData/RawData mirror is excluded.
    """

    root = f"{DROPBOX_ROOT}/{project}/Database/RawData"

    print(f"\nScanning Dropbox:")
    print(root)

    files = []

    result = dbx.files_list_folder(root, recursive=True)

    while True:

        for entry in result.entries:

            if not isinstance(entry, dropbox.files.FileMetadata):
                continue

            relative = entry.path_display[len(root):].lstrip("/")

            # Exclude duplicate mirror.
            if relative.startswith("RawData/"):
                continue

            files.append(
                {
                    "dropbox_path": entry.path_display,
                    "relative_path": relative,
                    "name": entry.name,
                    "size": entry.size,
                    "content_hash": entry.content_hash,
                    "modified": entry.server_modified.isoformat(),
                }
            )

        if not result.has_more:
            break

        result = dbx.files_list_folder_continue(result.cursor)

    return files


def sha256_dropbox(dropbox_path):
    """Calculate SHA-256 while streaming from Dropbox."""

    _, response = dbx.files_download(dropbox_path)

    sha256 = hashlib.sha256()

    while True:

        chunk = response.raw.read(8 * 1024 * 1024)

        if not chunk:
            break

        sha256.update(chunk)

    return sha256.hexdigest()


def sha256_azure(blob):
    """Calculate SHA-256 while streaming from Azure."""

    response = blob.download_blob()

    sha256 = hashlib.sha256()

    for chunk in response.chunks():
        sha256.update(chunk)

    return sha256.hexdigest()


def upload_file(project, item):
    """Upload one Dropbox file to Azure."""

    relative = item["relative_path"]

    azure_path = f"{project}/{relative}"

    blob = container.get_blob_client(azure_path)

    print("\n" + "-" * 70)
    print(f"FILE: {relative}")
    print(f"SIZE: {item['size']} bytes")

    if blob.exists():

        properties = blob.get_blob_properties()

        if properties.size != item["size"]:

            print("STATUS: SIZE_MISMATCH")
            return "SIZE_MISMATCH"

        print("Existing Azure blob found.")
        print("Verifying SHA-256...")

        dropbox_hash = sha256_dropbox(item["dropbox_path"])
        azure_hash = sha256_azure(blob)

        if dropbox_hash == azure_hash:

            print("STATUS: ALREADY_VERIFIED")
            return "ALREADY_VERIFIED"

        print("STATUS: HASH_MISMATCH")

        timestamp = datetime.now(timezone.utc).strftime(
            "%Y%m%dT%H%M%SZ"
        )

        version_path = (
            f"{project}/{relative}.v2-{timestamp}"
        )

        print(f"Creating version: {version_path}")

        version_blob = container.get_blob_client(version_path)

        _, response = dbx.files_download(item["dropbox_path"])

        version_blob.upload_blob(
            response.raw,
            overwrite=False,
            max_concurrency=1,
        )

        print("STATUS: VERSION_UPLOADED")
        return "VERSION_UPLOADED"

    print("STATUS: UPLOADING")

    _, response = dbx.files_download(item["dropbox_path"])

    blob.upload_blob(
        response.raw,
        overwrite=False,
        max_concurrency=1,
    )

    print("Upload complete.")

    print("Verifying SHA-256...")

    dropbox_hash = sha256_dropbox(item["dropbox_path"])
    azure_hash = sha256_azure(blob)

    if dropbox_hash != azure_hash:

        print("STATUS: HASH_MISMATCH_AFTER_UPLOAD")
        return "HASH_MISMATCH_AFTER_UPLOAD"

    print("STATUS: UPLOADED_AND_VERIFIED")

    return "UPLOADED_AND_VERIFIED"


def save_state(project, files):

    state_file = STATE_DIR / f"{project}.json"

    state = {
        "project": project,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    state_file.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8",
    )


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--project",
        default="V-RAPS",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Process only the first N files.",
    )

    parser.add_argument(
        "--only",
        default=None,
        help="Process only paths containing this text.",
    )

    args = parser.parse_args()

    project = args.project

    files = list_dropbox_files(project)

    print("\n" + "=" * 70)
    print("DISCOVERY")
    print("=" * 70)

    print(f"Discovered files: {len(files)}")

    if args.only:

        files = [
            item
            for item in files
            if args.only in item["relative_path"]
        ]

        print(f"After --only filter: {len(files)}")

    if args.limit:

        files = files[:args.limit]

        print(f"After --limit: {len(files)}")

    print("\nStarting ingestion...")

    results = []

    for item in files:

        try:

            status = upload_file(
                project,
                item,
            )

            results.append(
                {
                    "relative_path": item["relative_path"],
                    "status": status,
                    "size": item["size"],
                    "dropbox_hash": item["content_hash"],
                }
            )

        except Exception as exc:

            print(f"STATUS: FAILED")
            print(f"ERROR: {exc}")

            results.append(
                {
                    "relative_path": item["relative_path"],
                    "status": "FAILED",
                    "size": item["size"],
                    "dropbox_hash": item["content_hash"],
                    "error": str(exc),
                }
            )

    save_state(
        project,
        results,
    )

    report_file = (
        REPORT_DIR
        / f"{project}_cloud_sync.json"
    )

    report_file.write_text(
        json.dumps(results, indent=2),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)
    print("INGESTION SUMMARY")
    print("=" * 70)

    counts = {}

    for result in results:

        status = result["status"]

        counts[status] = counts.get(status, 0) + 1

    for status, count in sorted(counts.items()):

        print(f"{status}: {count}")

    print(f"\nReport: {report_file}")


if __name__ == "__main__":
    main()