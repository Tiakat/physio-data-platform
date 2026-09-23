"""
Phase 3 - resumable, auditable Dropbox -> Azure upload driven by the reconciliation.

    python -m physio.sync --dry-run                      # what would happen
    python -m physio.sync                                # upload every MISSING file (action=upload)
    python -m physio.sync --project PVB-ABDO --limit 20  # a small batch first
    python -m physio.sync --retry-failed                 # only files that FAILED before
    python -m physio.sync --verify-legacy                # hash-check MATCHED_SIZE blobs, stamp metadata

Guarantees
  * RawData is never overwritten: uploads use overwrite=False; an existing blob
    with other content is logged as CONFLICT and left alone.
  * Every download is verified against Dropbox's content_hash before upload,
    and the uploaded size is checked. SHA-256 is computed and stored as metadata.
  * Every attempt is appended to data/_ingestion/manifests/ingestion_manifest.jsonl.
    A re-run skips files already VERIFIED, so a crash / timeout just means "run again".
  * Identical content found twice in Dropbox is uploaded once; the copy is LINKED.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from physio.common import ROOT, DropboxHasher, azure_container, dropbox_client

MANIFEST = ROOT / "data/_ingestion/manifests/ingestion_manifest.jsonl"
CHUNK = 8 * 1024 * 1024
PIPELINE_VERSION = "physio-sync/1.0"
_lock = threading.Lock()


def now():
    return datetime.now(timezone.utc).isoformat()


def log(entry: dict):
    entry.setdefault("timestamp", now())
    with _lock:
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def manifest_state() -> dict:
    """(dropbox_path, content_hash) -> last entry."""
    state = {}
    if MANIFEST.exists():
        for line in open(MANIFEST, encoding="utf-8"):
            try:
                e = json.loads(line)
            except json.JSONDecodeError:
                continue
            state[(e.get("dropbox_path"), e.get("content_hash"))] = e
    return state


def meta(row: dict, sha256: str) -> dict:
    """Blob metadata. Values must be ASCII -> URL-quote paths (accents, spaces)."""
    m = {"sha256": sha256, "dropbox_content_hash": row["content_hash"],
         "dropbox_path": quote(row["dropbox_path"]), "project": row["project"],
         "subject": row.get("subject") or "", "source": row.get("source") or "",
         "stage": row.get("stage") or "", "ingested_at": now(), "pipeline": PIPELINE_VERSION}
    return {k: str(v) for k, v in m.items()}


def download_verified(dbx, row: dict, tmpdir: str) -> tuple[str, str]:
    """Stream Dropbox file to a temp file; return (path, sha256). Raises on hash mismatch."""
    _, resp = dbx.files_download(row["dropbox_path"])
    sha, dbh = hashlib.sha256(), DropboxHasher()
    fd, tmp = tempfile.mkstemp(dir=tmpdir, suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            for chunk in resp.iter_content(CHUNK):
                sha.update(chunk)
                dbh.update(chunk)
                f.write(chunk)
    finally:
        resp.close()
    got = dbh.hexdigest()
    if got != row["content_hash"]:
        os.remove(tmp)
        raise IOError(f"download corrupted: dropbox hash {got[:12]} != {row['content_hash'][:12]}")
    return tmp, sha.hexdigest()


def upload_one(dbx, cc, row: dict, tmpdir: str, attempts: int = 3) -> dict:
    from azure.core.exceptions import ResourceExistsError
    target = row["target_path"]
    base = {k: row.get(k) for k in ("project", "dropbox_path", "content_hash", "size_bytes",
                                     "subject", "source", "stage")}
    base["azure_path"] = target
    blob = cc.get_blob_client(target)
    last = None
    for i in range(attempts):
        tmp = None
        try:
            tmp, sha = download_verified(dbx, row, tmpdir)
            try:
                with open(tmp, "rb") as f:
                    blob.upload_blob(f, overwrite=False, metadata=meta(row, sha),
                                     max_concurrency=4, timeout=3600)
            except ResourceExistsError:
                md = blob.get_blob_properties().metadata or {}
                if md.get("dropbox_content_hash") == row["content_hash"]:
                    return {**base, "status": "VERIFIED", "sha256": sha, "note": "already present"}
                return {**base, "status": "CONFLICT", "sha256": sha,
                        "error": "blob exists with different content; not overwritten"}
            size = blob.get_blob_properties().size
            if size != int(row["size_bytes"]):
                return {**base, "status": "FAILED", "error": f"size after upload {size}"}
            return {**base, "status": "VERIFIED", "sha256": sha}
        except Exception as exc:                        # network / throttling -> retry
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(min(60, 5 * 2 ** i))
        finally:
            if tmp and os.path.exists(tmp):
                os.remove(tmp)
    return {**base, "status": "FAILED", "error": last}


def verify_legacy(cc, row: dict) -> dict:
    """Hash an existing blob that only matched by size; stamp metadata if identical."""
    blob = cc.get_blob_client(row["azure_path"])
    sha, dbh = hashlib.sha256(), DropboxHasher()
    for chunk in blob.download_blob(max_concurrency=4).chunks():
        sha.update(chunk)
        dbh.update(chunk)
    base = {k: row.get(k) for k in ("project", "dropbox_path", "content_hash", "size_bytes")}
    base["azure_path"] = row["azure_path"]
    if dbh.hexdigest() != row["content_hash"]:
        return {**base, "status": "CONFLICT", "error": "legacy blob differs from Dropbox"}
    props = blob.get_blob_properties()
    blob.set_blob_metadata({**(props.metadata or {}), **meta(row, sha.hexdigest())})
    return {**base, "status": "VERIFIED", "sha256": sha.hexdigest(), "note": "legacy verified"}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--plan", default="reports/phase2/reconciliation.csv")
    ap.add_argument("--project")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--include-review", action="store_true", help="also upload action=review rows")
    ap.add_argument("--retry-failed", action="store_true", help="only rows whose last attempt FAILED")
    ap.add_argument("--verify-legacy", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    rows = list(csv.DictReader(open(a.plan, encoding="utf-8")))
    if a.project:
        rows = [r for r in rows if r["project"].lower() == a.project.lower()]
    state = manifest_state()
    done = lambda r: state.get((r["dropbox_path"], r["content_hash"]), {}).get("status") == "VERIFIED"

    if a.verify_legacy:
        todo = [r for r in rows if r["status"] == "MATCHED_SIZE" and not done(r)]
        work = lambda r: verify_legacy(cc, r)
    else:
        acts = {"upload"} | ({"review"} if a.include_review else set())
        todo = [r for r in rows if r["action"] in acts and not done(r)]
        if a.retry_failed:
            todo = [r for r in todo
                    if state.get((r["dropbox_path"], r["content_hash"]), {}).get("status") == "FAILED"]
        work = lambda r: upload_one(dbx, cc, r, tmpdir)
    todo = todo[:a.limit] if a.limit else todo
    gb = sum(int(r["size_bytes"]) for r in todo) / 1e9
    print(f"{len(todo)} files, {gb:.2f} GB to process "
          f"({sum(done(r) for r in rows)} already VERIFIED in manifest)")
    if a.dry_run or not todo:
        for r in todo[:50]:
            print(f"  {r['action']:7} {r['dropbox_path']}  ->  {r['target_path'] or r['azure_path']}")
        if len(todo) > 50:
            print(f"  ... {len(todo) - 50} more")
        return

    dbx = dropbox_client()
    cc = azure_container()
    tmpdir = tempfile.mkdtemp(prefix="physio_sync_")
    counts = {}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(work, r): r for r in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            res = fut.result()
            log(res)
            counts[res["status"]] = counts.get(res["status"], 0) + 1
            print(f"[{i}/{len(todo)}] {res['status']:9} {res['dropbox_path']}"
                  + (f"  ({res.get('error')})" if res.get("error") else ""))

    # duplicates: point at the blob holding the same content
    if not a.verify_legacy:
        state = manifest_state()
        by_hash = {e["content_hash"]: e["azure_path"] for e in state.values() if e.get("status") == "VERIFIED"}
        for r in rows:
            if r["action"] == "link" and not done(r) and r["content_hash"] in by_hash:
                log({"project": r["project"], "dropbox_path": r["dropbox_path"],
                     "content_hash": r["content_hash"], "size_bytes": r["size_bytes"],
                     "azure_path": by_hash[r["content_hash"]], "status": "VERIFIED",
                     "note": f"duplicate of {r['duplicate_of']}"})
    print("\nSUMMARY", counts, "\nmanifest:", MANIFEST)
    if counts.get("FAILED"):
        print("Some files failed -> run again with --retry-failed")


if __name__ == "__main__":
    main()
