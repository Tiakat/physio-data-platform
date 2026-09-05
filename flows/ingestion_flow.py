"""
Research data platform - Azure ingestion orchestration.

Current ingestion stages:

    Azure Blob Storage
        rawdata/incoming/
              |
              v
        Azure discovery
              |
              v
        SHA-256 checksum
              |
              v
        duplicate check
           +--+--+
           |     |
       NEW       DUPLICATE
        |           |
        v           v
   register      stop
      hash
        |
        v
   next stage

Scientific logic remains in backbone/.
Prefect handles orchestration, retries, logging and scheduling.
"""

from __future__ import annotations

import os
from typing import Any

from dotenv import load_dotenv
from prefect import flow, task, get_run_logger
from azure.storage.blob import BlobServiceClient

from azure.checksum import sha256_blob
from azure.hash_index import HashIndex


load_dotenv()


AZURE_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT")
AZURE_KEY = os.getenv("AZURE_STORAGE_KEY")
AZURE_CONTAINER = os.getenv(
    "AZURE_CONTAINER_RAW",
    "rawdata",
)

INCOMING_PREFIX = "incoming/"
QUARANTINE_PREFIX = "quarantine/"


def get_blob_service() -> BlobServiceClient:
    """Create an Azure Blob Storage client."""

    if not AZURE_ACCOUNT:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT is not configured."
        )

    if not AZURE_KEY:
        raise RuntimeError(
            "AZURE_STORAGE_KEY is not configured."
        )

    return BlobServiceClient(
        account_url=(
            f"https://{AZURE_ACCOUNT}.blob.core.windows.net"
        ),
        credential=AZURE_KEY,
    )


@task(
    retries=3,
    retry_delay_seconds=30,
)
def discover_incoming_files() -> list[dict[str, Any]]:
    """
    Discover files waiting in Azure rawdata/incoming/.

    Existing files directly under rawdata/ are NOT touched.
    """

    logger = get_run_logger()

    service = get_blob_service()
    container = service.get_container_client(
        AZURE_CONTAINER
    )

    files: list[dict[str, Any]] = []

    for blob in container.list_blobs(
        name_starts_with=INCOMING_PREFIX
    ):
        # Ignore directory-like entries.
        if blob.name.endswith("/"):
            continue

        files.append(
            {
                "blob_key": blob.name,
                "size_bytes": blob.size,
                "last_modified": (
                    blob.last_modified.isoformat()
                    if blob.last_modified
                    else None
                ),
                "status": "DISCOVERED",
            }
        )

    logger.info(
        f"Discovered {len(files)} incoming file(s)."
    )

    for file_info in files:
        logger.info(
            f"  {file_info['blob_key']} "
            f"({file_info['size_bytes']} bytes)"
        )

    return files


@task(
    retries=2,
    retry_delay_seconds=30,
)
def checksum_file(
    file_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Calculate the full SHA-256 checksum of an Azure blob.
    """

    logger = get_run_logger()

    blob_key = file_info["blob_key"]

    logger.info(
        f"Calculating SHA-256: {blob_key}"
    )

    sha256 = sha256_blob(
        AZURE_CONTAINER,
        blob_key,
    )

    file_info["sha256"] = sha256
    file_info["status"] = "CHECKSUMMED"

    logger.info(
        f"SHA-256: {sha256}"
    )

    return file_info


@task
def check_duplicate(
    file_info: dict[str, Any],
) -> dict[str, Any]:
    """
    Check whether the SHA-256 has already been processed.

    If the hash is new, register it in the development
    hash index.

    If the hash already exists, mark the file as DUPLICATE.
    """

    logger = get_run_logger()

    index = HashIndex()

    sha256 = file_info["sha256"]

    existing = index.find(sha256)

    if existing:
        file_info["status"] = "DUPLICATE"
        file_info["duplicate_of"] = existing["blob_key"]

        logger.warning(
            f"Duplicate detected: "
            f"{file_info['blob_key']} "
            f"matches {existing['blob_key']}"
        )

        return file_info

    index.register(
        sha256=sha256,
        blob_key=file_info["blob_key"],
        size_bytes=file_info["size_bytes"],
    )

    file_info["status"] = "DEDUPLICATED"

    logger.info(
        f"New file registered: "
        f"{file_info['blob_key']}"
    )

    return file_info


@flow(
    name="research-data-ingestion",
)
def research_data_ingestion() -> list[dict[str, Any]]:
    """
    Discover incoming files, calculate SHA-256 checksums,
    and detect duplicates.
    """

    files = discover_incoming_files()

    results: list[dict[str, Any]] = []

    for file_info in files:

        # Stage 1: checksum
        result = checksum_file(file_info)

        # Stage 2: duplicate detection
        result = check_duplicate(result)

        results.append(result)

    return results


if __name__ == "__main__":
    research_data_ingestion()