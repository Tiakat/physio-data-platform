"""Stage 1 — ingest legacy Dropbox projects into Azure as ENCRYPTED PARQUET.

Corrected design (2026-09-30, K's call):

* Azure NEVER holds plaintext patient data.  Every blob in ``rawdata`` is
  Fernet-encrypted; only the pipeline scripts (which hold PIPELINE_DATA_KEY)
  can read it back.
* Signal files are parsed to standardized parquet, THEN encrypted.
  (``<CODE>/parquet/<device>_<sha8>.parquet.enc``).  Blob names carry no
  patient codes — patient linkage lives in the encrypted catalog/state.
* Files that cannot be parsed yet (BIS, device files, projects without a
  parser profile) are stored as ENCRYPTED SOURCE BYTES
  (``<CODE>/raw/<relpath>.enc``) so nothing is lost; they are re-ingested
  as parquet once a parser exists.
* Documents (ethics PDFs, spreadsheets, ...) and junk files are NEVER sent
  to Azure.  They stay in Dropbox.
* Dropbox is the read-only raw source and is never modified.

State (encrypted ``processed/_pipeline/state.json.enc``) records, per source
file: source sha256, stored blob, plaintext sha256, parse outcome.  Re-runs
are incremental; changed source files are re-ingested (Dropbox remains the
system of record).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import dropbox
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

# Documents that must never go to Azure (ethics paperwork, spreadsheets, ...).
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt", ".rtf",
    ".xls", ".xlsx", ".ods", ".csv",  # NOTE: .csv re-added below for data
    ".ppt", ".pptx", ".odp",
}
# .csv files ARE data for our parsers, so they are not documents.
DOCUMENT_EXTENSIONS.discard(".csv")

JUNK_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}

RAW_PREFIX = "rawdata"          # container
PARQUET_DIR = "parquet"         # rawdata/<CODE>/parquet/...
BYTES_DIR = "raw"               # rawdata/<CODE>/raw/... (encrypted source bytes)

STATE_BLOB = "processed/_pipeline/state.json.enc"


# ---------------------------------------------------------------------------
# Project selection — ALL data projects, never hard-coded
# ---------------------------------------------------------------------------

def load_projects_config() -> List[dict]:
    with open(REPO_ROOT / "config" / "projects.yaml", encoding="utf-8") as fh:
        return yaml.safe_load(fh)["projects"]


def load_profile(code: str) -> Optional[dict]:
    path = REPO_ROOT / "config" / "profiles" / f"{code.lower().replace(' ', '-')}.yaml"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def profile_tier(profile: Optional[dict], filename: str) -> str:
    """Return the tier ('a', 'b', 'c') of a file per the project profile.

    Projects without a profile return 'u' (unprofiled).
    """
    if not profile:
        return "u"
    tiers = (profile.get("acquisition") or {}).get("tiers", {})
    for tier_name, tier in tiers.items():
        for pat in tier.get("include", []) or []:
            if _glob_match(filename, pat):
                return tier_name
    return "b"


def _glob_match(filename: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(filename.lower(), pattern.lower())


def is_document(relpath: str) -> bool:
    return Path(relpath).suffix.lower() in DOCUMENT_EXTENSIONS


def is_junk(name: str) -> bool:
    return name.lower() in JUNK_NAMES


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

    projects: List[dict] = []
    for entry in load_projects_config():
        code = entry["code"]
        if entry.get("status") != "data":
            continue
        if wanted is not None and code.upper() not in wanted:
            continue
        profile = load_profile(code)
        roots = entry.get("data_roots") or ["."]
        dropbox_root = "/Liam/Projets actifs/" + entry["dropbox"] + "/" + roots[0]
        projects.append({
            "code": code,
            "dropbox_root": dropbox_root,
            "profile": profile,
            "tier_a_devices": _tier_a_devices(profile),
        })

    # Auto-discovery: warn about Dropbox folders with no config entry.
    unconfigured: List[str] = []
    try:
        res = dbx.files_list_folder("/Liam/Projets actifs")
        known = {e["dropbox"] for e in load_projects_config()}
        unconfigured = [
            e.name for e in res.entries
            if isinstance(e, dropbox.files.FolderMetadata) and e.name not in known
        ]
    except Exception:
        pass  # listing the root is best-effort; never block ingestion
    return projects, unconfigured


def _tier_a_devices(profile: Optional[dict]) -> List[str]:
    if not profile:
        return []
    tiers = (profile.get("acquisition") or {}).get("tiers", {})
    devices = []
    for dev in (tiers.get("a") or {}).get("devices", []) or []:
        devices.append(dev)
    return devices


# ---------------------------------------------------------------------------
# Dropbox listing (paginated, recursive)
# ---------------------------------------------------------------------------

def list_dropbox_tree(dbx, root: str) -> List[dict]:
    entries: List[dict] = []
    try:
        res = dbx.files_list_folder(root, recursive=True)
    except dropbox.exceptions.ApiError as e:
        if e.error.is_path() and e.error.get_path().is_not_found():
            print(f"[ingest] folder not found, skipping: {root}", flush=True)
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
# Batching — group-aware (BetterCare multi-file recordings stay atomic)
# ---------------------------------------------------------------------------

def group_key(code: str, relpath: str) -> str:
    parts = relpath.replace("\\", "/").split("/")
    if code.upper() == "V-RAPS" and len(parts) >= 2 and parts[0].isdigit():
        return parts[0]
    if len(parts) >= 2 and parts[0].lower().startswith("p-"):
        return parts[0]
    return relpath


def classify_entries(project: dict, entries: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Split entries into (parseable tier-A, storable-as-bytes).

    Documents and junk are dropped in both cases.
    """
    profile = project["profile"]
    tier_a, as_bytes = [], []
    for e in entries:
        if is_junk(e["name"]) or is_document(e["relpath"]):
            continue
        tier = profile_tier(profile, e["name"])
        if tier == "a":
            tier_a.append(e)
        elif tier in ("b", "u"):
            as_bytes.append(e)
        # tier 'c' -> dropped
    return tier_a, as_bytes


def select_batch(project: dict, entries: List[dict],
                 done: Dict[str, dict], budget_bytes: int) -> Tuple[List[dict], List[dict]]:
    """Select new/changed entries within budget.

    Tier-A entries are selected in whole recording groups (never split a
    BetterCare multi-file recording across runs).  Tier-B bytes are selected
    individually.
    """
    tier_a, as_bytes = classify_entries(project, entries)

    # --- tier A: group-atomic selection ------------------------------------
    groups: Dict[str, List[dict]] = {}
    for e in tier_a:
        groups.setdefault(group_key(project["code"], e["relpath"]), []).append(e)

    def group_dirty(g: List[dict]) -> bool:
        for e in g:
            prev = done.get(e["relpath"])
            if prev is None:
                return True
            if prev.get("kind") == "failed":
                return True
            if prev.get("rev") != e["rev"]:
                return True  # source changed in Dropbox -> re-ingest
        return False

    chosen_a: List[dict] = []
    used = 0
    for _gkey in sorted(groups):
        g = groups[_gkey]
        if not group_dirty(g):
            continue
        gsize = sum(e["size"] for e in g)
        if used + gsize > budget_bytes and chosen_a:
            break
        chosen_a.extend(g)
        used += gsize

    # --- tier B bytes: individual selection ---------------------------------
    remaining = budget_bytes - used
    chosen_b: List[dict] = []
    for e in sorted(as_bytes, key=lambda x: x["relpath"]):
        prev = done.get(e["relpath"])
        if prev and prev.get("kind") != "failed" and prev.get("rev") == e["rev"]:
            continue  # already stored and unchanged
        if e["size"] > remaining and chosen_b:
            break
        chosen_b.append(e)
        remaining -= e["size"]

    return chosen_a, chosen_b


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
    from azure.storage.blob import BlobClient
    from tools.crypto import encrypt_bytes  # noqa: F401 (imported for clarity)
    svc = azure_auth.get_blob_service_client(account)
    return svc.get_blob_client(container=container, blob=blob)


def azure_upload_encrypted(account: str, container: str, blob: str,
                           plaintext: bytes, plaintext_sha256: str) -> None:
    from tools.crypto import encrypt_bytes
    from azure.core.exceptions import ResourceExistsError
    token = encrypt_bytes(plaintext)
    client = _blob_client(account, container, blob)
    try:
        client.upload_blob(
            token, overwrite=True,
            metadata={"enc": "fernet", "sha256": plaintext_sha256},
        )
    except ResourceExistsError:
        pass


def azure_blob_exists(account: str, container: str, blob: str) -> bool:
    try:
        return _blob_client(account, container, blob).exists()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------

def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ingest_project(dbx, project: dict, account: str,
                   done: Dict[str, dict], budget_bytes: int) -> Dict[str, dict]:
    code = project["code"]
    print(f"[ingest] {code}: listing {project['dropbox_root']}", flush=True)
    entries = list_dropbox_tree(dbx, project["dropbox_root"])
    print(f"[ingest] {code}: {len(entries)} files in Dropbox", flush=True)

    batch_a, batch_b = select_batch(project, entries, done, budget_bytes)
    print(f"[ingest] {code}: {len(batch_a)} tier-A files, "
          f"{len(batch_b)} as-bytes files selected", flush=True)
    if not batch_a and not batch_b:
        return done

    with tempfile.TemporaryDirectory(prefix=f"ingest_{code}_") as tmp:
        tmpdir = Path(tmp)

        # --- tier A: download, parse to parquet, encrypt, upload -------------
        if batch_a:
            srcdir = tmpdir / "src"
            for e in batch_a:
                dest = srcdir / e["relpath"]
                dest.parent.mkdir(parents=True, exist_ok=True)
                _download_file(dbx, project["dropbox_root"] + "/" + e["relpath"], str(dest))
            _parse_and_store_parquet(code, srcdir, batch_a, account, done, tmpdir)

        # --- tier B / unprofiled: download, encrypt bytes, upload ------------
        for e in batch_b:
            dest = tmpdir / "b" / e["relpath"]
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                _download_file(dbx, project["dropbox_root"] + "/" + e["relpath"], str(dest))
                data = dest.read_bytes()
                blob = f"{code}/{BYTES_DIR}/{e['relpath']}.enc"
                azure_upload_encrypted(account, RAW_PREFIX, blob, data, _sha256_bytes(data))
                done[e["relpath"]] = {
                    "rev": e["rev"], "sha256": _sha256_bytes(data), "size": e["size"],
                    "kind": "bytes", "stored": blob,
                }
                print(f"[ingest] {code}: stored bytes {e['relpath']}", flush=True)
            except Exception as exc:
                done[e["relpath"]] = {"kind": "failed", "error": str(exc)[:300]}
                print(f"[ingest] {code}: FAILED bytes {e['relpath']}: {exc}", flush=True)

    return done


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_and_store_parquet(code: str, srcdir: Path, batch: List[dict],
                             account: str, done: Dict[str, dict], tmpdir: Path) -> None:
    """Run the existing parser (tools/run_local.py), then encrypt+upload each
    resulting parquet.  Blob names carry NO patient codes.
    """
    from tools.run_local import load_profile as _lp  # noqa
    outdir = tmpdir / "parsed"
    cmd = [sys.executable, "-m", "tools.run_local", "--profile", code.lower().replace(" ", "-"),
           "--root", str(srcdir), "--out", str(outdir)]
    print(f"[ingest] {code}: parsing with {' '.join(cmd[3:])}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=5400)
    if proc.returncode != 0:
        print(f"[ingest] {code}: parser failed:\n{proc.stderr[-4000:]}", flush=True)
        for e in batch:
            done[e["relpath"]] = {"kind": "failed", "error": "run_local failed"}
        return

    catalog_path = None
    for cand in sorted(outdir.rglob("catalog.json")):
        catalog_path = cand
        break
    if catalog_path is None:
        print(f"[ingest] {code}: no catalog.json produced by parser", flush=True)
        for e in batch:
            done[e["relpath"]] = {"kind": "failed", "error": "no catalog from run_local"}
        return
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    parquet_dir = catalog_path.parent / "parquet"

    parquet_by_source: Dict[str, str] = {}
    for entry in catalog:
        if entry.get("parquet"):
            parquet_by_source.setdefault(entry.get("file", ""), entry["parquet"])

    n_ok = n_fail = 0
    for e in batch:
        rel = e["relpath"]
        pkey = parquet_by_source.get(rel)
        if not pkey:
            done[rel] = {"kind": "failed", "error": "no parquet produced"}
            n_fail += 1
            continue
        pfile = parquet_dir / pkey
        if not pfile.exists():
            done[rel] = {"kind": "failed", "error": "parquet missing"}
            n_fail += 1
            continue
        data = pfile.read_bytes()
        digest = _sha256_bytes(data)
        # Blob name: device + content hash — no patient codes.
        device = pkey.split("_")[1] if "_" in pkey else "sig"
        blob = f"{code}/{PARQUET_DIR}/{device}_{digest[:8]}.parquet.enc"
        try:
            azure_upload_encrypted(account, RAW_PREFIX, blob, data, digest)
            done[rel] = {"rev": e["rev"], "sha256": digest, "size": e["size"],
                         "kind": "parquet", "stored": blob}
            n_ok += 1
        except Exception as exc:
            done[rel] = {"kind": "failed", "error": str(exc)[:300]}
            n_fail += 1
    print(f"[ingest] {code}: parquet stored={n_ok} failed={n_fail}", flush=True)

    # Encrypted catalog (patient linkage stays encrypted, never in blob names).
    if catalog_path.exists():
        from tools.crypto import encrypt_bytes
        raw = catalog_path.read_bytes()
        azure_upload_encrypted(account, RAW_PREFIX,
                               f"{code}/catalog.json.enc", raw, _sha256_bytes(raw))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_ingest(dbx, account: str, state: dict) -> dict:
    from tools import azure_auth

    budget_gb = float(os.getenv("LEGACY_BUDGET_GB", "3"))
    budget_bytes = int(budget_gb * (1024 ** 3))

    projects, unconfigured = select_projects(dbx)
    if unconfigured:
        print(f"[ingest] unconfigured Dropbox folders (add to config/projects.yaml): "
              f"{', '.join(unconfigured)}", flush=True)
    if not projects:
        print("[ingest] no data projects selected", flush=True)
        return state

    azure_auth.ensure_container(azure_auth.get_blob_service_client(account), RAW_PREFIX)

    legacy = state.setdefault("legacy", {})
    for proj in projects:
        code = proj["code"]
        entry = legacy.setdefault(code, {})
        done = entry.setdefault("files", {})
        entry["files"] = ingest_project(dbx, proj, account, done, budget_bytes)
    return state
