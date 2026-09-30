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


def _ensure_containers(account: str) -> None:
    svc = azure_auth.get_blob_service_client(account)
    for name in (RAW, PROCESSED, REPORTS):
        azure_auth.ensure_container(svc, name)


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

def _upload_tree_encrypted(account: str, local_root: Path, container: str,
                         prefix: str = "") -> int:
    """Upload every file under local_root to container, Fernet-encrypted."""
    from tools.crypto import encrypt_bytes
    import hashlib
    n = 0
    for f in sorted(local_root.rglob("*")):
        if not f.is_file():
            continue
        rel = f.relative_to(local_root).as_posix()
        blob = f"{prefix}{rel}.enc" if prefix else f"{rel}.enc"
        data = f.read_bytes()
        _blob(account, container, blob).upload_blob(
            encrypt_bytes(data), overwrite=True,
            metadata={"enc": "fernet",
                      "sha256": hashlib.sha256(data).hexdigest()})
        n += 1
    return n


def _signal_summary(out_root: Path, entry: dict) -> list:
    """Compact per-signal stats for the feed, from the local features file."""
    import pandas as pd
    feat_rel = entry.get("features", "")
    if not feat_rel:
        return []
    fp = out_root / feat_rel
    if not fp.exists():
        return []
    df = pd.read_parquet(fp)
    return [{
        "signal": str(r["signal"]), "sensor": str(r["sensor"]),
        "n": int(r["n"]), "n_null": int(r["n_null"]),
        "duration_s": float(r["duration_s"]),
    } for _, r in df.iterrows()]


def process_ett_batch(account: str, state: dict) -> dict:
    raw = state.get("ett_raw", {})
    done = state.setdefault("ett_processed", {})
    exams = state.setdefault("ett_exams", {})

    new = [name for name in raw if name not in done]
    print(f"[ett] {len(new)} new deliveries to ingest", flush=True)
    if not new:
        return state

    from tools.ingest_ett import ingest_one, load_catalog, project_for

    with tempfile.TemporaryDirectory(prefix="ett_") as tmp:
        tmpdir = Path(tmp)
        # Recreate the Dropbox source layout so project_for maps correctly:
        # <src>/parquet_zip/<name>  (prefix "parquet_zip" -> ETT_GENERAL)
        src_root = tmpdir / "src"
        incoming = src_root / "parquet_zip"
        incoming.mkdir(parents=True)
        out_root = tmpdir / "store"
        catalog = load_catalog(out_root / "catalog.json")
        quarantine_root = out_root / "quarantine"
        source_map = {"parquet_zip": "ETT_GENERAL"}

        for name in new:
            info = raw[name]
            print(f"[ett] ingesting {name}", flush=True)
            zpath = incoming / name
            token = _blob(account, RAW, info["blob"]).download_blob().readall()
            zpath.write_bytes(decrypt_bytes(token))
            try:
                project = project_for(zpath, src_root, source_map, None)
                entry = ingest_one(zpath, project=project, out_root=out_root,
                                   catalog=catalog, quarantine_root=quarantine_root)
            except Exception as exc:  # noqa: BLE001 -- keep pipeline alive
                print(f"[ett] FAILED {name}: {exc}", flush=True)
                done[name] = {"status": "failed", "error": str(exc)[:500]}
                continue

            if entry.get("status") == "ingested":
                entry["signal_summary"] = _signal_summary(out_root, entry)
                n_up = _upload_tree_encrypted(account, out_root / "processed",
                                              PROCESSED)
                print(f"[ett] uploaded {n_up} processed blobs (encrypted)",
                      flush=True)
            elif entry.get("status") == "quarantined":
                _upload_tree_encrypted(account, quarantine_root, PROCESSED,
                                       prefix="_quarantine/")

            exams[entry.get("exam_guid", name)] = entry
            done[name] = {"status": entry.get("status"),
                          "exam_guid": entry.get("exam_guid")}
            print(f"[ett] {name}: {entry.get('status')}", flush=True)
    return state


# ---------------------------------------------------------------------------
# D. Feed (de-identified; plaintext for the website)
# ---------------------------------------------------------------------------

def build_ett_feed(state: dict) -> dict:
    from tools.build_feed import _suppress
    exams = state.get("ett_exams", {})
    by_project: dict[str, list] = {}
    for e in exams.values():
        if e.get("status") != "ingested":
            continue
        by_project.setdefault(e["project"], []).append(e)
    projects = []
    for proj, items in sorted(by_project.items()):
        sig_agg: dict[str, dict] = {}
        for e in items:
            for s in e.get("signal_summary", []):
                a = sig_agg.setdefault(s["signal"],
                                       {"n_exams": 0, "total_hours": 0.0,
                                        "n": 0, "n_null": 0})
                a["n_exams"] += 1
                a["total_hours"] += s["duration_s"] / 3600
                a["n"] += s["n"]
                a["n_null"] += s["n_null"]
        signals = {}
        for sig, a in sorted(sig_agg.items()):
            if _suppress(a["n_exams"]) is None:
                continue
            signals[sig] = {
                "n_exams": a["n_exams"],
                "total_hours": round(a["total_hours"], 1),
                "null_fraction": round(a["n_null"] / a["n"], 4) if a["n"] else 0.0,
            }
        verdicts: dict[str, int] = {}
        for e in items:
            v = e.get("validation_verdict", "UNKNOWN")
            verdicts[v] = verdicts.get(v, 0) + 1
        n = len(items)
        projects.append({
            "code": proj, "kind": "ett",
            "n_exams": _suppress(n),
            "suppressed": _suppress(n) is None,
            "total_rows": _suppress(sum(e.get("rows", 0) for e in items)),
            "validation_verdicts": {k: _suppress(v)
                                    for k, v in verdicts.items()},
            "signals": signals,
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
    _ensure_containers(ACCOUNT)

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
