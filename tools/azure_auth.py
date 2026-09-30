"""Azure authentication for the pipeline tools.

Priority order (first one found wins):

1. ``AZURE_STORAGE_KEY`` — storage account key (workstation use).
2. ``AZURE_SAS_TOKEN`` — SAS token (workstation use, no ``?`` prefix needed).
3. :class:`~azure.identity.DefaultAzureCredential` — managed identity when the
   code runs *inside* Azure (Container Apps Job / Function App with a
   system-assigned identity that has "Storage Blob Data Contributor" on the
   storage account), or ``az login`` on a workstation.

Managed identity is the production path: no secrets to rotate, nothing to
paste. Keys and SAS tokens are fallbacks for local runs only.
"""
from __future__ import annotations

import os

from azure.storage.blob import BlobServiceClient

DEFAULT_ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT", "labdataplatform")


def get_credential():
    """Return a usable Azure credential following the priority order above."""
    key = os.getenv("AZURE_STORAGE_KEY")
    if key:
        return key
    sas = os.getenv("AZURE_SAS_TOKEN")
    if sas:
        return sas.lstrip("?")
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential()


def get_blob_service_client(account: str | None = None) -> BlobServiceClient:
    """BlobServiceClient authenticated via :func:`get_credential`."""
    account = account or DEFAULT_ACCOUNT
    return BlobServiceClient(
        account_url=f"https://{account}.blob.core.windows.net",
        credential=get_credential(),
    )


def ensure_container(client: BlobServiceClient, name: str):
    """Create the container if it does not exist (idempotent)."""
    container = client.get_container_client(name)
    try:
        container.create_container()
        print(f"created container: {name}")
    except Exception as exc:  # already exists
        if "ContainerAlreadyExists" not in type(exc).__name__ and "already exists" not in str(exc).lower():
            raise
    return container
