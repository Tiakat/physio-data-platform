import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import dropbox
from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv


load_dotenv()


DROPBOX_ROOT = "/Liam/Projects actifs"

STATE_DIR = Path("sync_state")
REPORT_DIR = Path("sync_reports")

STATE_DIR.mkdir(exist_ok=True)
REPORT_DIR.mkdir(exist_ok=True)


def get_dropbox_client():
    token = os.getenv("DROPBOX_ACCESS_TOKEN")

    if not token:
        raise RuntimeError(
            "DROPBOX_ACCESS_TOKEN is missing from .env"
        )

    client = dropbox.Dropbox(token)

    account = client.users_get_current_account()

    print("Dropbox authentication successful.")
    print(f"Connected account: {account.name.display_name}")

    return client


def get_azure_container():
    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    key = os.getenv("AZURE_STORAGE_KEY")
    container_name = os.getenv(
        "AZURE_CONTAINER_RAW",
        "rawdata",
    )

    if not account:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT is missing from .env"
        )

    if not key:
        raise RuntimeError(
            "AZURE_STORAGE_KEY is missing from .env"
        )

    blob_service = BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )

    return blob_service.get_container_client(
        container_name
    )


def list_dropbox_files(dbx, project):
    """List files from the project's RawData folder.

    The nested RawData/RawData mirror is excluded.
    """

    root = f"{DROPBOX_ROOT}/{project}/Database/RawData"

    print("\nScanning Dropbox:")
    print(root)

    files = []

    result = dbx.files_list_folder(
        root,
        recursive=True,
    )

    while True:

        for entry in result.entries:

            if not isinstance(
                entry,
                dropbox.files.FileMetadata,
            ):
                continue

            relative = (
                entry.path_display[len(root):]
                .lstrip("/")
            )

            if relative.startswith("RawData/"):
                continue

            files.append(
                {
                    "dropbox_path": entry.path_display,
                    "relative_path": relative,
                    "name": entry.name,
                    "size": entry.size,
                    "dropbox_content_hash": (
                        entry.content_hash
                    ),
                    "modified": (
                        entry.server_modified.isoformat()
                    ),
                }
            )

        if not result.has_more:
            break

        result = dbx.files_list_folder_continue(
            result.cursor
        )

    return files


def stream_dropbox_file(dbx, dropbox_path):
    """Download a Dropbox file as a streaming response."""

    _, response = dbx.files_download(dropbox_path)

    return response


def upload_stream_and_hash(blob, response):
    """Upload a stream to Azure while calculating SHA-256.

    The Dropbox response is consumed only once.
    """

    sha256 = hashlib.sha256()

    def hashed_chunks():

        while True:

            chunk = response.raw.read(
                8 * 1024 * 1024
            )

            if not chunk:
                break

            sha256.update(chunk)

            yield chunk

    blob.upload_blob(
        hashed_chunks(),
        overwrite=False,
        max_concurrency=1,
    )

    return sha256.hexdigest()


def sha256_dropbox(dbx, dropbox_path):
    """Calculate SHA-256 from Dropbox."""

    response = stream_dropbox_file(
        dbx,
        dropbox_path,
    )

    sha256 = hashlib.sha256()

    while True:

        chunk = response.raw.read(
            8 * 1024 * 1024
        )

        if not chunk:
            break

        sha256.update(chunk)

    return sha256.hexdigest()


def get_blob_sha256(blob):
    """Read SHA-256 from Azure blob metadata."""

    properties = blob.get_blob_properties()

    metadata = properties.metadata or {}

    return metadata.get("sha256")


def build_metadata(item, sha256, project):
    """Build immutable provenance metadata."""

    return {
        "sha256": sha256,
        "dropbox_content_hash": (
            item["dropbox_content_hash"]
        ),
        "source": "dropbox",
        "project": project,
        "relative_path": item["relative_path"],
        "source_modified": item["modified"],
        "source_size": str(item["size"]),
        "ingested_at": (
            datetime.now(timezone.utc).isoformat()
        ),
    }


def set_blob_metadata(blob, metadata):
    blob.set_blob_metadata(metadata)


def make_version_path(project, relative_path):
    timestamp = datetime.now(
        timezone.utc
    ).strftime("%Y%m%dT%H%M%SZ")

    return (
        f"{project}/{relative_path}"
        f".v2-{timestamp}"
    )


def upload_version(
    dbx,
    container,
    project,
    item,
    original_sha256,
):
    """Upload changed content as an immutable version."""

    version_path = make_version_path(
        project,
        item["relative_path"],
    )

    print(
        f"Creating immutable version: "
        f"{version_path}"
    )

    version_blob = container.get_blob_client(
        version_path
    )

    response = stream_dropbox_file(
        dbx,
        item["dropbox_path"],
    )

    version_sha256 = upload_stream_and_hash(
        version_blob,
        response,
    )

    metadata = build_metadata(
        item,
        version_sha256,
        project,
    )

    set_blob_metadata(
        version_blob,
        metadata,
    )

    if version_sha256 != original_sha256:

        print(
            "WARNING: source hash changed "
            "between checks."
        )

        return {
            "status": "VERSION_HASH_CHANGED",
            "azure_path": version_path,
            "sha256": version_sha256,
        }

    print(
        "STATUS: VERSION_UPLOADED_AND_VERIFIED"
    )

    return {
        "status": "VERSION_UPLOADED_AND_VERIFIED",
        "azure_path": version_path,
        "sha256": version_sha256,
    }


def upload_file(
    dbx,
    container,
    project,
    item,
):
    """Synchronize one Dropbox file to Azure.

    Existing blobs are never overwritten.
    """

    relative = item["relative_path"]

    azure_path = f"{project}/{relative}"

    blob = container.get_blob_client(
        azure_path
    )

    print("\n" + "-" * 70)
    print(f"FILE: {relative}")
    print(f"SIZE: {item['size']} bytes")
    print(
        "DROPBOX CONTENT HASH: "
        f"{item['dropbox_content_hash']}"
    )

    if blob.exists():

        properties = blob.get_blob_properties()

        azure_size = properties.size

        print(
            f"Existing Azure blob: "
            f"{azure_size} bytes"
        )

        azure_sha256 = get_blob_sha256(
            blob
        )

        if azure_sha256:

            print(
                "Azure SHA-256 metadata found."
            )

            dropbox_sha256 = sha256_dropbox(
                dbx,
                item["dropbox_path"],
            )

            if dropbox_sha256 == azure_sha256:

                print(
                    "STATUS: ALREADY_VERIFIED"
                )

                return {
                    "status": "ALREADY_VERIFIED",
                    "azure_path": azure_path,
                    "sha256": dropbox_sha256,
                }

            print(
                "STATUS: HASH_MISMATCH"
            )

            version_result = upload_version(
                dbx,
                container,
                project,
                item,
                dropbox_sha256,
            )

            version_result[
                "original_azure_path"
            ] = azure_path

            return version_result

        print(
            "Azure SHA-256 metadata missing."
        )

        if azure_size == item["size"]:

            print(
                "Legacy blob has matching size."
            )

            dropbox_sha256 = sha256_dropbox(
                dbx,
                item["dropbox_path"],
            )

            azure_response = blob.download_blob()

            azure_hash = hashlib.sha256()

            for chunk in azure_response.chunks():
                azure_hash.update(chunk)

            azure_sha256 = azure_hash.hexdigest()

            if dropbox_sha256 == azure_sha256:

                print(
                    "STATUS: LEGACY_BLOB_VERIFIED"
                )

                set_blob_metadata(
                    blob,
                    build_metadata(
                        item,
                        dropbox_sha256,
                        project,
                    ),
                )

                return {
                    "status": "LEGACY_BLOB_VERIFIED",
                    "azure_path": azure_path,
                    "sha256": dropbox_sha256,
                }

            print(
                "STATUS: LEGACY_HASH_MISMATCH"
            )

            version_result = upload_version(
                dbx,
                container,
                project,
                item,
                dropbox_sha256,
            )

            version_result[
                "original_azure_path"
            ] = azure_path

            return version_result

        print(
            "STATUS: SIZE_MISMATCH"
        )

        return {
            "status": "SIZE_MISMATCH",
            "azure_path": azure_path,
            "azure_size": azure_size,
            "dropbox_size": item["size"],
        }

    print("STATUS: UPLOADING")

    response = stream_dropbox_file(
        dbx,
        item["dropbox_path"],
    )

    sha256 = upload_stream_and_hash(
        blob,
        response,
    )

    metadata = build_metadata(
        item,
        sha256,
        project,
    )

    set_blob_metadata(
        blob,
        metadata,
    )

    properties = blob.get_blob_properties()

    if properties.size != item["size"]:

        print(
            "STATUS: SIZE_MISMATCH_AFTER_UPLOAD"
        )

        return {
            "status": "SIZE_MISMATCH_AFTER_UPLOAD",
            "azure_path": azure_path,
            "azure_size": properties.size,
            "dropbox_size": item["size"],
            "sha256": sha256,
        }

    print(
        "STATUS: UPLOADED_AND_VERIFIED"
    )

    return {
        "status": "UPLOADED_AND_VERIFIED",
        "azure_path": azure_path,
        "sha256": sha256,
    }


def save_state(project, results):

    state_file = STATE_DIR / f"{project}.json"

    state = {
        "project": project,
        "updated_at": (
            datetime.now(timezone.utc).isoformat()
        ),
        "files": results,
    }

    state_file.write_text(
        json.dumps(
            state,
            indent=2,
        ),
        encoding="utf-8",
    )


def main():

    parser = argparse.ArgumentParser(
        description=(
            "Synchronize Dropbox RawData to "
            "Azure immutable raw storage."
        )
    )

    parser.add_argument(
        "--project",
        default="V-RAPS",
        help="Project name.",
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
        help=(
            "Process only paths containing "
            "this text."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Discover files and report planned "
            "actions without downloading or uploading."
        ),
    )

    args = parser.parse_args()

    project = args.project

    if args.dry_run:

        print(
            "\nDRY RUN: no files will be "
            "downloaded or uploaded."
        )

        dbx = get_dropbox_client()

        files = list_dropbox_files(
            dbx,
            project,
        )

    else:

        dbx = get_dropbox_client()

        container = get_azure_container()

        files = list_dropbox_files(
            dbx,
            project,
        )

    print("\n" + "=" * 70)
    print("DISCOVERY")
    print("=" * 70)

    print(
        f"Discovered files: {len(files)}"
    )

    if args.only:

        files = [
            item
            for item in files
            if args.only
            in item["relative_path"]
        ]

        print(
            f"After --only filter: "
            f"{len(files)}"
        )

    if args.limit is not None:

        if args.limit < 0:
            parser.error(
                "--limit must be >= 0"
            )

        files = files[:args.limit]

        print(
            f"After --limit: "
            f"{len(files)}"
        )

    if args.dry_run:

        results = []

        for item in files:

            results.append(
                {
                    "relative_path": (
                        item["relative_path"]
                    ),
                    "status": "WOULD_SYNC",
                    "size": item["size"],
                    "dropbox_content_hash": (
                        item[
                            "dropbox_content_hash"
                        ]
                    ),
                    "modified": item["modified"],
                }
            )

            print(
                f"WOULD_SYNC: "
                f"{item['relative_path']} "
                f"({item['size']} bytes)"
            )

    else:

        print("\nStarting ingestion...")

        results = []

        for item in files:

            try:

                result = upload_file(
                    dbx,
                    container,
                    project,
                    item,
                )

                results.append(
                    {
                        "relative_path": (
                            item["relative_path"]
                        ),
                        "status": result[
                            "status"
                        ],
                        "size": item["size"],
                        "dropbox_content_hash": (
                            item[
                                "dropbox_content_hash"
                            ]
                        ),
                        "modified": (
                            item["modified"]
                        ),
                        **{
                            key: value
                            for key, value
                            in result.items()
                            if key != "status"
                        },
                    }
                )

            except Exception as exc:

                print("STATUS: FAILED")
                print(f"ERROR: {exc}")

                results.append(
                    {
                        "relative_path": (
                            item["relative_path"]
                        ),
                        "status": "FAILED",
                        "size": item["size"],
                        "dropbox_content_hash": (
                            item[
                                "dropbox_content_hash"
                            ]
                        ),
                        "modified": (
                            item["modified"]
                        ),
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
        json.dumps(
            results,
            indent=2,
        ),
        encoding="utf-8",
    )

    print("\n" + "=" * 70)

    if args.dry_run:
        print("DRY RUN SUMMARY")
    else:
        print("INGESTION SUMMARY")

    print("=" * 70)

    counts = {}

    for result in results:

        status = result["status"]

        counts[status] = (
            counts.get(status, 0) + 1
        )

    for status, count in sorted(
        counts.items()
    ):

        print(
            f"{status}: {count}"
        )

    print(
        f"\nReport: {report_file}"
    )


if __name__ == "__main__":
    main()
