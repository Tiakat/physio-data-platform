"""Ingest: copy raw files from Dropbox to local staging, with a manifest.

The manifest (``staging/<project>/ingest_manifest.json``) records every file's
Dropbox path, size, content hash and download time. Re-runs skip files whose
remote content hash is unchanged, so ingest is idempotent and resumable.

Needs a Dropbox access token in the ``DROPBOX_TOKEN`` environment variable.
``pip install dropbox``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

from .config import ProjectConfig

log = logging.getLogger(__name__)

MANIFEST_NAME = "ingest_manifest.json"
CHUNK = 4 * 1024 * 1024


@dataclass
class StagedFile:
    dropbox_path: str
    local_path: str
    size_bytes: int
    content_hash: str
    downloaded_at: str


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(CHUNK), b""):
            h.update(blk)
    return h.hexdigest()


def _manifest_path(cfg: ProjectConfig) -> Path:
    return Path(cfg.staging_dir) / cfg.name / MANIFEST_NAME


def load_manifest(cfg: ProjectConfig) -> dict[str, dict]:
    p = _manifest_path(cfg)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_manifest(cfg: ProjectConfig, manifest: dict[str, dict]) -> None:
    p = _manifest_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(manifest, indent=2))


def _dropbox_client():
    try:
        import dropbox
    except ImportError as e:
        raise RuntimeError("the 'dropbox' package is required for ingest "
                           "(pip install dropbox)") from e
    token = os.environ.get("DROPBOX_TOKEN")
    if not token:
        raise RuntimeError("set the DROPBOX_TOKEN environment variable")
    return dropbox.Dropbox(token)


def ingest_project(cfg: ProjectConfig, dry_run: bool = False) -> list[StagedFile]:
    """Download new/changed files for one project. Returns the staged files
    (including ones already staged from a previous run)."""
    dbx = None if dry_run else _dropbox_client()
    manifest = load_manifest(cfg)
    staged: list[StagedFile] = []
    seen: set[str] = set()

    def walk(folder: str):
        if dry_run:
            return
        res = dbx.files_list_folder(folder, recursive=cfg.dropbox.recursive)
        while True:
            for entry in res.entries:
                yield entry
            if not res.has_more:
                break
            res = dbx.files_list_folder_continue(res.cursor)

    if dry_run:
        log.info("[dry-run] would list %s (%s)", cfg.dropbox.source_dir,
                 cfg.dropbox.file_pattern)
        return [StagedFile(**v) for v in manifest.values()]

    import fnmatch
    for entry in walk(cfg.dropbox.source_dir):
        meta = getattr(entry, "name", None)
        if meta is None:  # folders etc.
            continue
        if not fnmatch.fnmatch(entry.name, cfg.dropbox.file_pattern):
            continue
        seen.add(entry.path_lower)
        remote_hash = getattr(entry, "content_hash", "") or ""
        prev = manifest.get(entry.path_lower)
        local = Path(cfg.staging_dir) / cfg.name / "raw" / entry.name

        if prev and prev.get("content_hash") == remote_hash and local.exists():
            staged.append(StagedFile(**prev))
            continue  # unchanged: skip download

        log.info("downloading %s", entry.path_display)
        local.parent.mkdir(parents=True, exist_ok=True)
        dbx.files_download_to_file(str(local), entry.path_lower)
        rec = StagedFile(
            dropbox_path=entry.path_display,
            local_path=str(local),
            size_bytes=local.stat().st_size,
            content_hash=remote_hash or _sha256(local),
            downloaded_at=datetime.now(timezone.utc).isoformat(),
        )
        manifest[entry.path_lower] = asdict(rec)
        staged.append(rec)

    # drop manifest entries for files deleted upstream
    for key in [k for k in manifest if k != "__meta__" and k not in seen]:
        log.info("upstream deleted: %s", manifest[key].get("dropbox_path"))
        del manifest[key]

    manifest["__meta__"] = {
        "project": cfg.name,
        "config_fingerprint": cfg.fingerprint,
        "ingested_at": datetime.now(timezone.utc).isoformat(),
    }
    save_manifest(cfg, manifest)
    log.info("ingest done: %d files staged for project %s", len(staged), cfg.name)
    return staged
