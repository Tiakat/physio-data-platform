"""Shared helpers: configuration, Dropbox / Azure clients, content hashes."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG = ROOT / "config"
DROPBOX_ROOT = os.getenv("DROPBOX_ROOT") or "/Liam/Projets actifs"
DBX_BLOCK = 4 * 1024 * 1024          # Dropbox content_hash block size


def load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except ImportError:
        pass


def load_projects() -> dict:
    return yaml.safe_load((CONFIG / "projects.yaml").read_text(encoding="utf-8"))


def dropbox_client():
    load_env()
    import dropbox
    for k in ("DROPBOX_APP_KEY", "DROPBOX_APP_SECRET", "DROPBOX_REFRESH_TOKEN"):
        if not os.getenv(k):
            raise RuntimeError(f"{k} is missing from .env")
    return dropbox.Dropbox(oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
                           app_key=os.environ["DROPBOX_APP_KEY"],
                           app_secret=os.environ["DROPBOX_APP_SECRET"],
                           timeout=900)


def azure_container(name: str | None = None):
    load_env()
    from azure.core.pipeline.transport import RequestsTransport
    from azure.storage.blob import BlobServiceClient
    account = os.getenv("AZURE_STORAGE_ACCOUNT")
    if not account:
        raise RuntimeError("AZURE_STORAGE_ACCOUNT is missing from .env")
    cred = os.getenv("AZURE_STORAGE_KEY")
    if not cred:                                   # fall back to az login / managed identity
        from azure.identity import DefaultAzureCredential
        cred = DefaultAzureCredential()
    svc = BlobServiceClient(f"https://{account}.blob.core.windows.net", credential=cred,
                            transport=RequestsTransport(connection_timeout=300, read_timeout=600))
    return svc.get_container_client(name or os.getenv("AZURE_CONTAINER_RAW", "rawdata"))


class DropboxHasher:
    """Incremental Dropbox content_hash (SHA-256 of per-4MB-block SHA-256 digests).

    Lets us verify a download, and compare an Azure blob with a Dropbox file
    by content, without trusting names or sizes.
    """

    def __init__(self):
        self._overall = hashlib.sha256()
        self._block = hashlib.sha256()
        self._pos = 0

    def update(self, data: bytes):
        mv = memoryview(data)
        while len(mv):
            take = min(DBX_BLOCK - self._pos, len(mv))
            self._block.update(mv[:take])
            self._pos += take
            mv = mv[take:]
            if self._pos == DBX_BLOCK:
                self._overall.update(self._block.digest())
                self._block, self._pos = hashlib.sha256(), 0

    def hexdigest(self) -> str:
        if self._pos:
            self._overall.update(self._block.digest())
            self._block, self._pos = hashlib.sha256(), 0
        return self._overall.hexdigest()


def norm_key(path: str) -> str:
    """Case/Unicode-insensitive path key (Dropbox paths are case-insensitive)."""
    import unicodedata
    return unicodedata.normalize("NFC", path.replace("\\", "/")).strip("/").lower()
