"""Daily pipeline: Dropbox -> Azure (ENCRYPTED) -> feed.

Stages:
  A. Mirror ETT deliveries: Dropbox -> rawdata/ett/incoming/*.parquet.zip.enc
     (encrypted; Dropbox remains the source until direct ETT->Azure lands).
  B. Ingest new ETT deliveries: decrypt -> parse -> processed/<EXAM>/...
  C. Legacy ingest (ALL data projects): Dropbox signal files ->
     encrypted parquet in rawdata/<CODE>/ (tools/legacy_pipeline.py).
  D. Build the de-identified website feed -> reports/feed/ (plaintext).

Everything in rawdata/ and processed/ is Fernet-encrypted (PIPELINE_DATA_KEY).
Only reports/ (de-identified aggregates) stays plaintext for the website.

State lives at processed/_pipeline/state.json.enc (encrypted).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import dropbox

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools import azure_auth, sync_dropbox_cloud  # noqa: E402
from tools.crypto import decrypt_bytes, encrypt_bytes  # noqa: E402

ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT", "labdataplatform")
RAW = "rawdata"
PROCESSED = "processed"
REPORTS = "reports"
ETT_PREFIX = "ett/incoming/"
STATE_BLOB = "processed/_pipeline/state.json.enc"
STATE_BLOB_LEGACY_PLAINTEXT = "processed/_pipeline/state.json"  # one-time migration


# ---------------------------------------------------------------------------
# State (encrypted)
# ---------------------------------------------------------------------------

def _blob(account: str, container: str, blob: str):
    return azure_auth.get_blob_service_client(account).get_blob_client(
        container=container, blob=blob)


def load_state(account: str) -> dict:
    for name in (STATE_BLOB, STATE_BLOB_LEGACY_PLAINTEXT):
        try:
            data = _blob(account, PROCESSED, name).download_blob().readall()
            if name.endswith(".enc"):
                data = decrypt_bytes(data)
            return json.loads(data.decode("utf-8"))
        except Exception:
            continue
    return {"ett_raw": {}, "ett_processed": {}, "ett_exams": {}, "legacy": {}}


def save_state(account: str, state: dict) -> None:
    token = encrypt_bytes(json.dumps(state, indent=1).encode("utf-8"))
    _blob(account, PROCESSED, STATE_BLOB).upload_blob(
        token, overwrite=True, metadata={"enc": "fernet"})


# ---------------------------------------------------------------------------
# A. Mirror ETT deliveries (encrypted)
# ---------------------------------------------------------------------------

def _sha256_stream(resp) -> tuple[str, bytes]:
    import hashlib
    h = hashlib.sha256()
    buf = io.BytesIO()
    for chunk in resp.iter_content(1 << 20):
        h.update(chunk)
        buf.write(chunk)
    return h.hexdigest(), buf.getvalue()


def mirror_ett(dbx, account: str, state: dict) -> dict:
    root = "/Liam/Projets actifs/parquet_zip"
    print(f"[ett] listing {root}", flush=True)
    try:
        res = dbx.files_list_folder(root)
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            print("[ett] folder not found yet, skipping", flush=True)
            return state
        raise
    files = [m for m in res.entries
             if isinstance(m, dropbox.files.FileMetadata)
             and m.name.endswith(".parquet.zip")]
    print(f"[ett] {len(files)} deliveries in Dropbox", flush=True)

    raw = state.setdefault("ett_raw", {})
    for meta in files:
        prev = raw.get(meta.name)
        if prev and prev.get("rev") == meta.rev:
            continue
        print(f"[ett] downloading {meta.name} ({meta.size / 1e9:.2f} GB)", flush=True)
        _, resp = dbx.files_download(meta.path_display)
        digest, data = _sha256_stream(resp)
        blob = ETT_PREFIX + meta.name + ".enc"
        _blob(account, RAW, blob).upload_blob(
            encrypt_bytes(data), overwrite=True,
            metadata={"enc": "fernet", "sha256": digest})
        raw[meta.name] = {"sha256": digest, "size": meta.size, "rev": meta.rev,
                          "blob": blob,
                          "mirrored_at": datetime.now(timezone.utc).isoformat()}
        print(f"[ett] mirrored+encrypted {meta.name}", flush=True)
    return state


# ---------------------------------------------------------------------------
# B. Ingest new ETT deliveries (decrypt -> parse)
# ---------------------------------------------------------------------------

def process_ett_batch(account: str, state: dict) -> dict:
    from tools.ingest_ett import ingest_one_delivery

    raw = state.get("ett_raw", {})
    done = state.setdefault("ett_processed", {})
    exams = state.setdefault("ett_exams", {})

    new = [name for name in raw if name not in done]
    print(f"[ett] {len(new)} new deliveries to ingest", flush=True)
    for name in new:
        info = raw[name]
        print(f"[ett] ingesting {name}", flush=True)
        with tempfile.TemporaryDirectory(prefix="ett_") as tmp:
            zpath = Path(tmp) / name
            token = _blob(account, RAW, info["blob"]).download_blob().readall()
            zpath.write_bytes(decrypt_bytes(token))
            try:
                result = ingest_one_delivery(zpath, account, PROCESSED)
            except Exception as exc:
                print(f"[ett] FAILED {name}: {exc}", flush=True)
                done[name] = {"status": "failed", "error": str(exc)[:500]}
                continue
        for exam in result.get("exams", []):
            exams[exam["exam_id"]] = exam
        done[name] = {"status": "ok", "exams": [e["exam_id"] for e in result.get("exams", [])]}
        print(f"[ett] done {name}: {len(result.get('exams', []))} exams", flush=True)
    return state


# ---------------------------------------------------------------------------
# D. Feed (de-identified; plaintext for the website)
# ---------------------------------------------------------------------------

def build_ett_feed(state: dict) -> dict:
    from tools.build_feed import summarize_exam
    exams = state.get("ett_exams", {})
    cards = [summarize_exam(e) for e in exams.values()]
    by_project: dict[str, list] = {}
    for c in cards:
        by_project.setdefault(c["project"], []).append(c)
    projects = []
    for proj, items in sorted(by_project.items()):
        subjects = {i["exam_id"] for i in items}
        projects.append({
            "code": proj, "kind": "ett",
            "n_exams": len(items), "n_subjects": len(subjects),
            "signals": sorted({s for i in items for s in i.get("signals", [])}),
            "exams": items,
        })
    return {"generated_at": datetime.now(timezone.utc).isoformat(),
            "projects": projects}


def publish_feed(account: str, feed: dict) -> None:
    payload = json.dumps(feed, indent=1).encode("utf-8")
    _blob(account, REPORTS, "feed/feed.json").upload_blob(payload, overwrite=True)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> int:
    print("[pipeline] starting", flush=True)
    dbx = sync_dropbox_cloud.get_dropbox_client()
    sync_dropbox_cloud.ensure_container(ACCOUNT, RAW)
    sync_dropbox_cloud.ensure_container(ACCOUNT, PROCESSED)
    sync_dropbox_cloud.ensure_container(ACCOUNT, REPORTS)

    state = load_state(ACCOUNT)
    state = mirror_ett(dbx, ACCOUNT, state)
    state = process_ett_batch(ACCOUNT, state)

    from tools import legacy_pipeline
    state = legacy_pipeline.run_ingest(dbx, ACCOUNT, state)

    feed = build_ett_feed(state)
    publish_feed(ACCOUNT, feed)
    print(f"[pipeline] feed published: {len(feed['projects'])} ETT projects", flush=True)

    save_state(ACCOUNT, state)
    print("[pipeline] done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
