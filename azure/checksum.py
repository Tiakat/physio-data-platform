"""
Azure Blob SHA-256 utilities.

Streams Azure blobs without loading the entire file into memory.
"""

from __future__ import annotations

import hashlib
import os

from azure.storage.blob import BlobServiceClient
from dotenv import load_dotenv


load_dotenv()


def get_blob_service() -> BlobServiceClient:
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


def sha256_blob(
    container_name: str,
    blob_key: str,
    chunk_size: int = 8 * 1024 * 1024,
) -> str:
    """
    Calculate the full SHA-256 hash of an Azure blob.

    The blob is streamed in chunks so large physiological
    data files do not need to fit in RAM.
    """

    service = get_blob_service()

    container = service.get_container_client(
        container_name
    )

    blob = container.get_blob_client(blob_key)

    hasher = hashlib.sha256()

    stream = blob.download_blob()

    for chunk in stream.chunks():
        hasher.update(chunk)

    return hasher.hexdigest()