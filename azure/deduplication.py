"""
Duplicate detection for Azure Blob Storage.

Uses full SHA-256 hashes to identify identical files.
This module is read-only: it does not move, delete, or overwrite blobs.
"""

from __future__ import annotations

from typing import Any

from azure.checksum import sha256_blob
from azure.storage.blob import BlobServiceClient

from dotenv import load_dotenv
import os


load_dotenv()


def get_blob_service() -> BlobServiceClient:
    """Create an Azure Blob Storage client."""

    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    key = os.getenv("AZURE_STORAGE_KEY")

    if not account:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT is not configured."
        )

    if not key:
        raise RuntimeError(
            "AZURE_STORAGE_KEY is not configured."
        )

    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=key,
    )


def find_duplicates(
    container_name: str,
    blob_key: str,
) -> list[dict[str, Any]]:
    """
    Find Azure blobs with the same full SHA-256 hash.

    The target blob itself is excluded from the returned results.
    """

    service = get_blob_service()

    container = service.get_container_client(
        container_name
    )

    target_blob = container.get_blob_client(blob_key)

    if not target_blob.exists():
        raise FileNotFoundError(
            f"Blob does not exist: {blob_key}"
        )

    target_hash = sha256_blob(
        container_name,
        blob_key,
    )

    duplicates: list[dict[str, Any]] = []

    for blob in container.list_blobs():

        if blob.name == blob_key:
            continue

        # A different-sized file cannot have identical contents.
        if blob.size != target_blob.get_blob_properties().size:
            continue

        candidate_hash = sha256_blob(
            container_name,
            blob.name,
        )

        if candidate_hash == target_hash:
            duplicates.append(
                {
                    "blob_key": blob.name,
                    "size_bytes": blob.size,
                    "sha256": candidate_hash,
                }
            )

    return duplicates


def check_duplicate(
    container_name: str,
    blob_key: str,
) -> dict[str, Any]:
    """
    Check one blob and return a structured duplicate result.
    """

    sha256 = sha256_blob(
        container_name,
        blob_key,
    )

    duplicates = find_duplicates(
        container_name,
        blob_key,
    )

    return {
        "blob_key": blob_key,
        "sha256": sha256,
        "is_duplicate": len(duplicates) > 0,
        "duplicates": duplicates,
    }