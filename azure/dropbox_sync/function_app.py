import os
from datetime import datetime, timezone

import azure.functions as func
import dropbox
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

app = func.FunctionApp()

DROPBOX_ROOT = os.getenv(
    "DROPBOX_ROOT",
    "/Liam/Projects actifs",
)

PROJECT = "V-RAPS"
RAW_PREFIX = f"{PROJECT}/"


def get_dropbox_client():
    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")

    if not app_key or not app_secret or not refresh_token:
        raise RuntimeError(
            "Dropbox OAuth settings are missing."
        )

    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret,
    )


def get_container():
    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    container_name = os.getenv(
        "AZURE_CONTAINER_RAW",
        "rawdata",
    )

    if not account:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT is missing."
        )

    credential = DefaultAzureCredential()

    service = BlobServiceClient(
        account_url=(
            f"https://{account}.blob.core.windows.net"
        ),
        credential=credential,
    )

    return service.get_container_client(
        container_name
    )


def list_dropbox_files(dbx):
    path = (
        f"{DROPBOX_ROOT}/{PROJECT}/Database/RawData"
    )

    result = dbx.files_list_folder(
        path,
        recursive=True,
    )

    entries = list(result.entries)

    while result.has_more:
        result = dbx.files_list_folder_continue(
            result.cursor
        )
        entries.extend(result.entries)

    return [
        entry
        for entry in entries
        if isinstance(
            entry,
            dropbox.files.FileMetadata,
        )
    ]


def sync_file(dbx, container, entry):
    relative = entry.path_display.split(
        f"/{PROJECT}/Database/RawData/",
        1,
    )[1]

    blob_name = f"{RAW_PREFIX}{relative}"

    blob = container.get_blob_client(
        blob_name
    )

    dropbox_hash = entry.content_hash
    existing = None

    try:
        existing = blob.get_blob_properties()
    except Exception:
        existing = None

    if existing:
        metadata = existing.metadata or {}

        if (
            metadata.get(
                "dropbox_content_hash"
            )
            == dropbox_hash
            and existing.size == entry.size
        ):
            return "ALREADY_VERIFIED"

    metadata, response = dbx.files_download(
        entry.path_display
    )

    blob.upload_blob(
        response.content,
        overwrite=False,
        metadata={
            "dropbox_content_hash": dropbox_hash,
            "source": "dropbox",
            "project": PROJECT,
            "relative_path": relative,
            "source_size": str(entry.size),
            "source_modified": (
                entry.server_modified.isoformat()
            ),
            "ingested_at": (
                datetime.now(
                    timezone.utc
                ).isoformat()
            ),
        },
        max_concurrency=1,
    )

    return "UPLOADED"


@app.timer_trigger(
    schedule="0 0 */6 * * *",
    arg_name="timer",
    run_on_startup=True,
    use_monitor=True,
)
def dropbox_sync(timer: func.TimerRequest) -> None:
    print(
        "Dropbox -> Azure sync started."
    )

    dbx = get_dropbox_client()

    account = dbx.users_get_current_account()

    print(
        f"Dropbox account: "
        f"{account.name.display_name}"
    )

    container = get_container()

    files = list_dropbox_files(dbx)

    print(
        f"Dropbox files discovered: "
        f"{len(files)}"
    )

    uploaded = 0
    verified = 0
    failed = 0

    for entry in files:
        try:
            result = sync_file(
                dbx,
                container,
                entry,
            )

            if result == "UPLOADED":
                uploaded += 1
            else:
                verified += 1

        except Exception as exc:
            failed += 1

            print(
                f"FAILED: "
                f"{entry.path_display}: "
                f"{exc}"
            )

    print(
        "Dropbox -> Azure sync finished. "
        f"uploaded={uploaded}, "
        f"already_verified={verified}, "
        f"failed={failed}"
    )
