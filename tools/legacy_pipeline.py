"""Stage 1 — ingest legacy Dropbox projects into Azure as ENCRYPTED PARQUET.

Corrected design (2026-09-30, K's call):

* Azure NEVER holds plaintext patient data.  Every blob in ``rawdata`` is
  Fernet-encrypted; only the pipeline scripts (which hold PIPELINE_DATA_KEY)
  can read it back.
* Signal files are parsed with the project's real parser
  (``tools/run_local.py`` + ``profiles/<code>.yaml``), THEN encrypted
  (``<CODE>/parquet/<device>_<sha8>.parquet.enc``).  Blob names carry no
  patient codes — patient linkage lives in the encrypted catalog/state.
* Files that cannot be parsed yet (BIS, device files, projects without a
  profile) are stored as ENCRYPTED SOURCE BYTES
  (``<CODE>/raw/<relpath>.enc``) so nothing is lost; they are re-ingested
  as parquet once a parser exists.
* Documents (ethics PDFs, spreadsheets, ...) and junk files are NEVER sent
  to Azure.  They stay in Dropbox.
* Dropbox is the read-only raw source and is never modified.

Projects come from ``config/projects.yaml`` (dict keyed by code, with
``status``, ``dropbox`` folder name, ``data_roots`` and ``exclude_folders``).
Only ``status: data`` projects are ingested.

State (encrypted ``processed/_pipeline/state.json.enc``) records, per source
file: source sha256, stored blob, plaintext sha256, parse outcome.  Re-runs
are incremental; changed source files are re-ingested (Dropbox remains the
system of record).
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import dropbox
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DROPBOX_ROOT = "/Liam/Projets actifs"

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Documents that must never go to Azure (ethics paperwork, spreadsheets, ...).
# NOTE: .csv files ARE data for our parsers, so they are not documents.
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods",
    ".ppt", ".pptx", ".odp",
}

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

# Patient photos are NEVER ingested to Azure — not even encrypted.
# The lab template defines Photos/ as the photo folder; image files are
# also excluded wherever they appear (defense in depth).
PHOTO_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".heic", ".heif", ".webp",
}
GLOBAL_EXCLUDE_FOLDERS = ["Photos"]

RAW_PREFIX = "rawdata"          # container
PARQUET_DIR = "parquet"         # rawdata/<CODE>/parquet/...
BYTES_DIR = "raw"               # rawdata/<CODE>/raw/... (encrypted source bytes)


def is_document(relpath: str) -> bool:
    return Path(relpath).suffix.lower() in DOCUMENT_EXTENSIONS


def is_photo(name: str) -> bool:
    return Path(name).suffix.lower() in PHOTO_EXTENSIONS


def is_junk(name: str) -> bool:
    return name.lower() in JUNK_NAMES


def _excluded(relpath: str, patterns: List[str]) -> bool:
    parts = relpath.replace("\\", "/").split("/")
    return any(fnmatch.fnmatch(p, pat) or fnmatch.fnmatch(p.lower(), pat.lower())
               for p in parts for pat in patterns)


# ---------------------------------------------------------------------------
# Project selection — ALL data projects from config/projects.yaml
# ---------------------------------------------------------------------------

def load_projects_config() -> dict:
    """Return the projects dict from config/projects.yaml (code -> entry)."""
    with open(REPO_ROOT / "config" / "projects.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["projects"]


def load_profile(code: str) -> Optional[dict]:
    path = REPO_ROOT / "profiles" / f"{code.lower()}.yaml"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def select_projects(dbx) -> Tuple[List[dict], List[str]]:
    """Return (configured data projects, unconfigured Dropbox folders).

    LEGACY_PROJECTS=all (default) selects every project with status 'data'.
    Also lists the Dropbox root and warns about folders that have no entry
    in config/projects.yaml so new projects are noticed, not silently skipped.
    """
    wanted_raw = os.getenv("LEGACY_PROJECTS", "all").strip()
    wanted = None if wanted_raw.lower() == "all" else {
        w.strip().upper() for w in wanted_raw.split(",") if w.strip()
    }

    cfg = load_projects_config()
    projects: List[dict] = []
    for code, entry in cfg.items():
        if not isinstance(entry, dict) or entry.get("status") != "data":
            continue
        if wanted is not None and code.upper() not in wanted:
            continue
        projects.append({
            "code": code,
            "dropbox_base": f"{DROPBOX_ROOT}/{entry['dropbox']}",
            "data_roots": entry.get("data_roots") or ["."],
            # Photos/ is always excluded (lab template): patient photos are
            # never ingested, not even encrypted.
            "exclude_folders": (entry.get("exclude_folders") or [])
            + GLOBAL_EXCLUDE_FOLDERS,
            "profile": load_profile(code),
        })

    # Auto-discovery: warn about Dropbox folders with no config entry,
    # and about configured projects whose Dropbox folder does not exist.
    unconfigured: List[str] = []
    try:
        res = dbx.files_list_folder(DROPBOX_ROOT)
        actual = {e.name for e in res.entries
                  if isinstance(e, dropbox.files.FolderMetadata)}
        known = {e["dropbox"] for e in cfg.values()
                 if isinstance(e, dict) and "dropbox" in e}
        unconfigured = [n for n in sorted(actual) if n not in known]
        for p in projects:
            want = p["dropbox_base"].rsplit("/", 1)[1]
            if want not in actual:
                print(f"[ingest] WARNING: configured project {p['code']} "
                      f"expects Dropbox folder '{want}' which does not exist "
                      f"under {DROPBOX_ROOT}", flush=True)
    except Exception:
        pass  # listing the root is best-effort; never block ingestion
    return projects, unconfigured


# ---------------------------------------------------------------------------
# Dropbox listing (paginated, recursive)
# ---------------------------------------------------------------------------

def _log_parent_contents(dbx, root: str) -> None:
    """Best-effort: when a data root is missing, show what the parent
    folder actually contains so a wrong folder name is diagnosable."""
    parent = root.rsplit("/", 1)[0]
    try:
        res = dbx.files_list_folder(parent)
        names = sorted(e.name for e in res.entries
                       if isinstance(e, dropbox.files.FolderMetadata))
        print(f"[ingest] contents of {parent}: {names}", flush=True)
    except Exception:
        pass  # never block ingestion on diagnostics


def list_dropbox_tree(dbx, root: str) -> List[dict]:
    entries: List[dict] = []
    try:
        res = dbx.files_list_folder(root, recursive=True)
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            print(f"[ingest] folder not found, skipping: {root}", flush=True)
            _log_parent_contents(dbx, root)
            return []
        raise
    while True:
        for meta in res.entries:
            if isinstance(meta, dropbox.files.FileMetadata):
                rel = meta.path_display[len(root):].lstrip("/")
                entries.append({
                    "relpath": rel,
                    "name": meta.name,
                    "size": meta.size,
                    "rev": meta.rev,
                })
        if not res.has_more:
            break
        res = dbx.files_list_folder_continue(res.cursor)
    return entries


# ---------------------------------------------------------------------------
# Dropbox download helper
# ---------------------------------------------------------------------------

def _download_file(dbx, dropbox_path: str, local_path: str) -> None:
    """Download a Dropbox file to a local path (streamed, constant memory)."""
    _, resp = dbx.files_download(dropbox_path)
    with open(local_path, "wb") as fh:
        for chunk in resp.iter_content(1 << 20):
            fh.write(chunk)


# ---------------------------------------------------------------------------
# Azure helpers (encrypted)
# ---------------------------------------------------------------------------

def _blob_client(account: str, container: str, blob: str):
    from tools import azure_auth
    svc = azure_auth.get_blob_service_client(account)
    return svc.get_blob_client(container=container, blob=blob)


def azure_upload_encrypted(account: str, container: str, blob: str,
                           plaintext: bytes, plaintext_sha256: str) -> None:
    from tools.crypto import encrypt_bytes
    token = encrypt_bytes(plaintext)
    client = _blob_client(account, container, blob)
    client.upload_blob(
        token, overwrite=True,
        metadata={"enc": "fernet", "sha256": plaintext_sha256},
    )


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _store_bytes(code: str, path: Path, e: dict, account: str,
                 done: Dict[str, dict]) -> None:
    """Encrypt the source file as-is and upload to <CODE>/raw/."""
    try:
        data = path.read_bytes()
        blob = f"{code}/{BYTES_DIR}/{e['relpath']}.enc"
        azure_upload_encrypted(account, RAW_PREFIX, blob, data,
                               _sha256_bytes(data))
        done[e["relpath"]] = {
            "status": "ok", "rev": e["rev"], "sha256": _sha256_bytes(data),
            "size": e["size"], "kind": "bytes", "stored": blob,
        }
    except Exception as exc:  # noqa: BLE001 -- record, keep going
        done[e["relpath"]] = {"status": "failed", "error": str(exc)[:300]}
        print(f"[ingest] {code}: FAILED bytes {e['relpath']}: {exc}", flush=True)


def _parse_and_store(code: str, srcdir: Path, batch: List[dict], account: str,
                     done: Dict[str, dict], tmpdir: Path,
                     catalog_tag: str = "") -> None:
    """Run the project's real parser (tools/run_local.py), then encrypt and
    upload each resulting parquet.  Blob names carry NO patient codes.
    Files the parser cannot handle fall back to encrypted source bytes.
    """
    store = tmpdir / "store"
    cmd = [sys.executable, "-m", "tools.run_local",
           "--project", code, "--root", str(srcdir), "--out", str(store)]
    print(f"[ingest] {code}: parsing {len(batch)} files with run_local",
          flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5400)
    if proc.returncode != 0:
        print(f"[ingest] {code}: parser failed, storing as bytes:\n"
              f"{proc.stderr[-2000:]}", flush=True)
        for e in batch:
            _store_bytes(code, srcdir / e["relpath"], e, account, done)
        return

    catalogs = sorted(store.rglob("catalog.json"))
    if not catalogs:
        print(f"[ingest] {code}: no catalog.json from parser, storing as bytes",
              flush=True)
        for e in batch:
            _store_bytes(code, srcdir / e["relpath"], e, account, done)
        return

    catalog_path = catalogs[0]
    out_root = catalog_path.parent
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_file = {c.get("file", ""): c for c in catalog}

    n_parquet = n_bytes = 0
    for e in batch:
        rel = e["relpath"]
        c = by_file.get(rel) or {}
        pkey = c.get("parquet")
        pfile = out_root / pkey if pkey else None
        if pfile is not None and pfile.exists():
            data = pfile.read_bytes()
            digest = _sha256_bytes(data)
            device = str(c.get("device") or "other").lower()
            blob = _parquet_blob_name(code, device, digest)
            try:
                azure_upload_encrypted(account, RAW_PREFIX, blob, data, digest)
                done[rel] = {
                    "status": "ok", "rev": e["rev"], "sha256": digest,
                    "size": e["size"], "kind": "parquet", "stored": blob,
                }
                n_parquet += 1
            except Exception as exc:  # noqa: BLE001
                done[rel] = {"status": "failed", "error": str(exc)[:300]}
        else:
            _store_bytes(code, srcdir / rel, e, account, done)
            n_bytes += 1
    print(f"[ingest] {code}: parquet stored={n_parquet} as-bytes={n_bytes}",
          flush=True)

    # Encrypted catalog (patient linkage stays encrypted, never in blob names).
    # catalog_tag distinguishes per-chunk catalogs when a project is
    # ingested in several chunks.
    raw = catalog_path.read_bytes()
    cname = f"catalog_{catalog_tag}.json.enc" if catalog_tag else "catalog.json.enc"
    azure_upload_encrypted(account, RAW_PREFIX, f"{code}/{cname}",
                           raw, _sha256_bytes(raw))


def _select_new(candidates: List[dict], done: Dict[str, dict],
                budget_bytes: int) -> Tuple[List[dict], int]:
    """Select new/changed/previously-failed entries within budget.

    Entries already stored with an unchanged Dropbox revision are skipped;
    changed revisions are re-ingested.
    """
    selected: List[dict] = []
    used = 0
    for e in sorted(candidates, key=lambda x: x["relpath"]):
        prev = done.get(e["relpath"])
        if prev and prev.get("status") == "ok" and prev.get("rev") == e["rev"]:
            continue  # already stored and unchanged
        if used + e["size"] > budget_bytes and selected:
            break
        selected.append(e)
        used += e["size"]
    return selected, used


def _parquet_blob_name(code: str, device: str, digest: str) -> str:
    """Blob name for a parsed parquet: device + content hash, no patient codes."""
    return f"{code}/{PARQUET_DIR}/{device}_{digest[:8]}.parquet.enc"


def _resource_snapshot() -> tuple:
    """(disk_free_gb, mem_available_gb) for the temp filesystem / host."""
    free_gb = float("nan")
    for _p in (tempfile.gettempdir(), "/"):
        try:
            free_gb = shutil.disk_usage(_p).free / 1e9
            break
        except Exception:  # noqa: BLE001
            continue
    mem_gb = float("nan")
    try:
        with open("/proc/meminfo") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    mem_gb = int(line.split()[1]) / 1e6
                    break
    except Exception:  # noqa: BLE001
        pass
    return free_gb, mem_gb


def _split_chunks(selected: List[dict], chunk_bytes: int) -> List[List[dict]]:
    """Split selected files into size-bounded chunks (peak disk control)."""
    chunks: List[List[dict]] = []
    cur: List[dict] = []
    cur_size = 0
    for e in selected:
        if cur and cur_size + e["size"] > chunk_bytes:
            chunks.append(cur)
            cur, cur_size = [], 0
        cur.append(e)
        cur_size += e["size"]
    if cur:
        chunks.append(cur)
    return chunks


def ingest_project(dbx, project: dict, account: str, done: Dict[str, dict],
                   budget_bytes: int, on_chunk=None) -> Tuple[Dict[str, dict], int, dict]:
    """Ingest one project in small disk-bounded chunks.

    Each chunk is downloaded, parsed, uploaded, then wiped before the next
    chunk starts, so peak disk stays near the chunk size even when parsed
    parquet is much larger than the source files (e.g. compressed/encrypted
    sources expanding into float columns).  ``on_chunk`` is called after
    each chunk so the caller can persist state incrementally.
    Returns (done, bytes selected, stats) where stats holds listed/eligible/
    selected/ok_new/failed_new counts for the supervisor's health checks.
    """
    code = project["code"]

    entries: List[dict] = []
    for root in project["data_roots"]:
        base = project["dropbox_base"] if root == "." else \
            f"{project['dropbox_base']}/{root}"
        print(f"[ingest] {code}: listing {base}", flush=True)
        for e in list_dropbox_tree(dbx, base):
            e["dbx_path"] = f"{base}/{e['relpath']}"
            if root != ".":
                e["relpath"] = f"{root}/{e['relpath']}"
            entries.append(e)
    print(f"[ingest] {code}: {len(entries)} files in Dropbox", flush=True)

    excl = project["exclude_folders"]
    candidates = [e for e in entries
                  if not is_junk(e["name"]) and not is_document(e["relpath"])
                  and not is_photo(e["name"])
                  and not _excluded(e["relpath"], excl)]

    selected, used = _select_new(candidates, done, budget_bytes)
    stats = {"listed": len(entries), "eligible": len(candidates),
             "selected": len(selected), "ok_new": 0, "failed_new": 0}
    print(f"[ingest] {code}: {len(selected)} files selected "
          f"({used / 1e9:.2f} GB)", flush=True)
    if not selected:
        return done, 0, stats

    chunk_bytes = int(float(os.getenv("INGEST_CHUNK_GB", "1")) * (1024 ** 3))
    chunks = _split_chunks(selected, chunk_bytes)
    for i, chunk in enumerate(chunks):
        tag = f"c{i:02d}"
        free_gb, mem_gb = _resource_snapshot()
        print(f"[ingest] {code}: chunk {tag} ({i + 1}/{len(chunks)}, "
              f"{len(chunk)} files) "
              f"[disk_free={free_gb:.1f}GB mem_avail={mem_gb:.1f}GB]",
              flush=True)
        with tempfile.TemporaryDirectory(prefix=f"ingest_{code}_{tag}_") as tmp:
            tmpdir = Path(tmp)
            srcdir = tmpdir / "src"
            for e in chunk:
                dest = srcdir / e["relpath"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    _download_file(dbx, e["dbx_path"], str(dest))
                except Exception as exc:  # noqa: BLE001
                    done[e["relpath"]] = {"status": "failed",
                                          "error": f"download: {exc}"[:300]}
            downloaded = [e for e in chunk
                          if (srcdir / e["relpath"]).exists()]

            if project["profile"] and downloaded:
                _parse_and_store(code, srcdir, downloaded, account, done,
                                 tmpdir, catalog_tag=tag)
            else:
                if not project["profile"]:
                    print(f"[ingest] {code}: no parser profile, storing as "
                          f"encrypted bytes", flush=True)
                for e in downloaded:
                    _store_bytes(code, srcdir / e["relpath"], e, account, done)
        if on_chunk is not None:
            on_chunk()
    for e in selected:
        st = (done.get(e["relpath"]) or {}).get("status")
        if st == "ok":
            stats["ok_new"] += 1
        elif st == "failed":
            stats["failed_new"] += 1
    return done, used, stats


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_ingest(dbx, account: str, state: dict, progress_cb=None) -> dict:
    """Ingest every selected data project.

    The budget (LEGACY_BUDGET_GB, default 4) applies PER PROJECT, not per
    run: one run walks all projects, and the 120-minute workflow timeout is
    the backstop.  After each project, ``progress_cb(state)`` is called so
    the caller can persist state incrementally — a timed-out run loses at
    most the in-progress project, and the next scheduled run resumes where
    it left off on its own.
    """
    from tools import azure_auth

    budget_gb = float(os.getenv("LEGACY_BUDGET_GB", "4"))
    budget_bytes = int(budget_gb * (1024 ** 3))

    projects, unconfigured = select_projects(dbx)
    if unconfigured:
        print(f"[ingest] unconfigured Dropbox folders (add to config/projects.yaml): "
              f"{', '.join(unconfigured)}", flush=True)
    if not projects:
        print("[ingest] no data projects selected", flush=True)
        return state

    azure_auth.ensure_container(azure_auth.get_blob_service_client(account),
                                RAW_PREFIX)

    legacy = state.setdefault("legacy", {})
    for proj in projects:
        code = proj["code"]
        entry = legacy.setdefault(code, {})
        done = entry.setdefault("files", {})
        entry["files"], used, stats = ingest_project(
            dbx, proj, account, done, budget_bytes,
            on_chunk=(lambda: progress_cb(state)) if progress_cb else None)
        print(f"[ingest] {code}: done ({used / 1e9:.2f} GB this run)",
              flush=True)
        # Per-run record for the supervisor's health checks (stall/failure
        # spike detection). History is capped at 10 runs.
        ok_total = sum(1 for f in entry["files"].values()
                       if isinstance(f, dict) and f.get("status") == "ok")
        run_rec = {"ts": datetime.now(timezone.utc).strftime("%Y%m%d_%H%M"),
                   **stats, "ok_total": ok_total,
                   "budget_capped": used >= budget_bytes}
        entry["last_run"] = run_rec
        hist = entry.setdefault("run_history", [])
        hist.append(run_rec)
        del hist[:-10]
        if progress_cb is not None:
            progress_cb(state)
    return state
