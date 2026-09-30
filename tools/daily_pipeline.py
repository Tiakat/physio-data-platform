"""Daily pipeline: Dropbox ETT deliveries -> Azure raw -> ingest/QC -> processed -> website feed.

One command runs the whole chain end to end, on a schedule, with no human
in the loop:

    python -m tools.daily_pipeline

Designed to run INSIDE Azure as a Container Apps Job (cron trigger) with a
system-assigned managed identity holding "Storage Blob Data Contributor" on
the storage account. No SAS, no keys. It also runs on a workstation after
``az login``.

Stages:
  A. Mirror new ``*.parquet.zip`` deliveries from Dropbox
     ``<ETt_DROPBOX_ROOT>/<ETT_PREFIX>/`` into the immutable ``rawdata``
     container: ``ett/incoming/<filename>`` (byte-identical, sha256 metadata).
  B. Ingest every mirrored delivery not yet processed via tools/ingest_ett.py
     into a local store, then upload the processed tree
     (``processed/<project>/<exam_guid>/...``) and quarantine records.
  C. Rebuild the de-identified website feed via tools/build_feed.py and
     publish ``reports/feed/feed.json`` (+ per-project) to the ``reports``
     container, which is what ``PHYSIO_FEED_URL`` on the website points at.

Idempotency: pipeline state lives in ``processed/_pipeline/state.json`` and
ingest_ett.py keeps its own catalog; re-running only processes new deliveries.

Env:
  DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN
  AZURE_STORAGE_ACCOUNT            (default: labdataplatform)
  ETT_DROPBOX_ROOT                 (default: /Liam/Projets actifs)
  ETT_PREFIX                       (default: parquet_zip)
  ETT_PROJECT                      (optional: force project code)
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from azure.storage.blob import ContentSettings
import dropbox

from tools.azure_auth import ensure_container, get_blob_service_client  # noqa: E402
from tools.sync_dropbox_cloud import get_dropbox_client  # noqa: E402

RAW_CONTAINER = "rawdata"
PROCESSED_CONTAINER = "processed"
REPORTS_CONTAINER = "reports"
STATE_BLOB = "_pipeline/state.json"
INCOMING_PREFIX = "ett/incoming/"


def log(msg: str) -> None:
    print(f"[daily-pipeline] {msg}", flush=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(4 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_dir(container, local_dir: Path, prefix: str) -> int:
    """Upload every file under local_dir to <prefix>/<relative path>."""
    n = 0
    for path in sorted(local_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(local_dir).as_posix()
        blob = container.get_blob_client(f"{prefix}{rel}")
        ctype = "application/json" if path.suffix == ".json" else "application/octet-stream"
        with open(path, "rb") as fh:
            blob.upload_blob(fh, overwrite=True,
                             content_settings=ContentSettings(content_type=ctype))
        n += 1
    return n


def load_state(container) -> dict:
    try:
        data = container.get_blob_client(STATE_BLOB).download_blob().readall()
        return json.loads(data)
    except Exception:
        return {"raw": {}, "processed": [], "quarantined": []}


def save_state(container, state: dict) -> None:
    container.get_blob_client(STATE_BLOB).upload_blob(
        json.dumps(state, indent=2).encode(), overwrite=True)


def run(cmd: list[str]) -> None:
    log("+ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=REPO_ROOT)
    if r.returncode != 0:
        raise RuntimeError(f"command failed ({r.returncode}): {' '.join(cmd)}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Daily ETT pipeline: Dropbox -> Azure -> feed.")
    ap.add_argument("--workdir", default=None, help="local scratch dir (default: temp)")
    ap.add_argument("--limit", type=int, default=0, help="process at most N new deliveries")
    args = ap.parse_args()

    dropbox_root = os.getenv("ETT_DROPBOX_ROOT", "/Liam/Projets actifs")
    prefix = os.getenv("ETT_PREFIX", "parquet_zip")
    forced_project = os.getenv("ETT_PROJECT")

    workdir = Path(args.workdir) if args.workdir else Path(tempfile.mkdtemp(prefix="ett_pipe_"))
    incoming_dir = workdir / "incoming"
    store_dir = workdir / "store"
    feed_dir = workdir / "feed"
    incoming_dir.mkdir(parents=True, exist_ok=True)

    log("authenticating (Dropbox + Azure)...")
    dbx = get_dropbox_client()
    svc = get_blob_service_client()
    raw_c = ensure_container(svc, RAW_CONTAINER)
    proc_c = ensure_container(svc, PROCESSED_CONTAINER)
    rep_c = ensure_container(svc, REPORTS_CONTAINER)

    state = load_state(proc_c)

    # ---- Stage A: mirror new deliveries into immutable raw ----
    log(f"listing Dropbox {dropbox_root}/{prefix}/ ...")
    try:
        entries = dbx.files_list_folder(f"{dropbox_root}/{prefix}").entries
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            log("Dropbox folder not found — nothing to mirror yet")
            entries = []
        else:
            raise

    zips = sorted(e.name for e in entries if e.name.endswith(".parquet.zip"))
    log(f"{len(zips)} deliveries in Dropbox")
    new = [z for z in zips if z not in state["raw"]]
    if args.limit:
        new = new[: args.limit]
    log(f"{len(new)} new deliveries to mirror")

    for name in new:
        local = incoming_dir / name
        log(f"downloading {name} ...")
        dbx.files_download_to_file(str(local), f"{dropbox_root}/{prefix}/{name}")
        digest = sha256_file(local)
        blob = raw_c.get_blob_client(INCOMING_PREFIX + name)
        with open(local, "rb") as fh:
            blob.upload_blob(fh, overwrite=True, metadata={"sha256": digest})
        state["raw"][name] = {"sha256": digest, "blob": INCOMING_PREFIX + name}
        log(f"mirrored -> rawdata/{INCOMING_PREFIX}{name}")
    save_state(proc_c, state)

    # ---- Stage B: ingest new deliveries ----
    processed_names = set(state.get("processed", []))
    to_ingest = sorted(n for n in state["raw"] if n not in processed_names)
    log(f"{len(to_ingest)} deliveries to ingest")
    if to_ingest:
        for name in to_ingest:
            local = incoming_dir / name
            if not local.exists():
                with open(local, "wb") as fh:
                    raw_c.get_blob_client(INCOMING_PREFIX + name).download_blob().readinto(fh)
        cmd = [sys.executable, "-m", "tools.ingest_ett",
               "--source", str(incoming_dir), "--out", str(store_dir)]
        if forced_project:
            cmd += ["--project", forced_project]
        run(cmd)

        catalog = json.loads((store_dir / "catalog.json").read_text())
        uploaded = 0
        for exam_guid, entry in catalog.items():
            exam_dir = store_dir / "processed" / entry["project"] / exam_guid
            if exam_dir.exists():
                uploaded += upload_dir(proc_c, exam_dir, f"{entry['project']}/{exam_guid}/")
        # quarantine records, if any
        qdir = store_dir / "quarantine"
        if qdir.exists():
            upload_dir(proc_c, qdir, "quarantine/")
        state["processed"] = sorted(set(state.get("processed", [])) | set(to_ingest))
        save_state(proc_c, state)
        log(f"uploaded {uploaded} processed files")

        # ---- Stage C: rebuild + publish the de-identified feed ----
        run([sys.executable, "-m", "tools.build_feed",
             "--store", str(store_dir), "--out", str(feed_dir)])
        n_feed = upload_dir(rep_c, feed_dir, "feed/")
        log(f"published {n_feed} feed files to reports/feed/ (PHYSIO_FEED_URL target)")

    log("done.")


if __name__ == "__main__":
    main()
