"""Build K's clean 1-raw container.

K's spec: "1-raw" with only CSV files. Template:
    1-raw/{PROJECT}/{patient}/{device}/{file}.csv

- PROJECT: COLECTOMIE, DEXREM, etc. (10 projects)
- patient: REAL Dropbox folder name (e.g. "103"), NEVER provisional numbers
- device: infinity, bettercare, bis, nol, pumps, etc. (from source)
- file.csv: decrypted parquet -> CSV, content_type=text/csv (clickable preview)

Source: rawdata/{PROJECT}/**/*.parquet.enc
Patient mapping: Dropbox content_hash matching (no state file needed).
  1. List Dropbox files for project -> {content_hash: (path, patient, device)}
  2. List rawdata blobs with metadata -> {dropbox_content_hash: blob_name}
  3. Match by content_hash -> blob -> (patient, device, relpath)

BIS: .r2a (binary EEG) and .spa (pipe-delimited trends) were parsed at ingest
into standardized parquets. Converting those parquets to CSV gives readable CSVs.

Usage:
    python -m tools.build_1raw --project COLECTOMIE
    python -m tools.build_1raw --project all

Resumable: blobs that already exist at the destination are skipped.
Nothing is deleted by this tool.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402

import pandas as pd  # noqa: E402

CONTAINER = "1-raw"
DROPBOX_ROOT = "/Liam/Projets actifs"

# Normalize ingest device codes -> K's device folder names.
DEVICE_FOLDERS = {
    "infinity": "infinity",
    "bettercare": "bettercare",
    "better care": "bettercare",
    "bis": "bis",
    "nol": "nol",
    "nol_medasense": "nol",
    "pump": "pumps",
    "pumps": "pumps",
    "remi": "pumps",
    "propofol": "pumps",
}


def decrypt_bytes(data: bytes) -> bytes:
    from cryptography.fernet import Fernet

    key = os.environ["PIPELINE_DATA_KEY"].encode()
    return Fernet(key).decrypt(data)


def get_dropbox_client():
    """Dropbox client using app key/secret + refresh token (same as ingest)."""
    import dropbox

    app_key = os.getenv("DROPBOX_APP_KEY")
    app_secret = os.getenv("DROPBOX_APP_SECRET")
    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN")
    if not app_key or not app_secret or not refresh_token:
        raise RuntimeError(
            "DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN "
            "must be set"
        )
    return dropbox.Dropbox(
        oauth2_refresh_token=refresh_token,
        app_key=app_key,
        app_secret=app_secret,
    )


def blob_names(container, prefix=""):
    for b in container.list_blobs(name_starts_with=prefix):
        yield b["name"] if isinstance(b, dict) else b.name


def dest_exists(container, blob: str) -> bool:
    try:
        container.get_blob_client(blob).get_blob_properties()
        return True
    except Exception:
        return False


def load_projects_config() -> dict:
    """Load config/projects.yaml for dropbox folder names and data_roots."""
    import yaml

    p = Path(__file__).resolve().parent.parent / "config" / "projects.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def load_profile(code: str) -> dict:
    """Load the project profile YAML (device_dir_map, patient_dir_regex)."""
    import yaml

    for fname in [f"{code.lower()}.yaml", f"{code.lower()}.yml"]:
        p = Path(__file__).resolve().parent.parent / "profiles" / fname
        if p.exists():
            try:
                return yaml.safe_load(p.read_text()) or {}
            except Exception:
                pass
    return {}


def load_patient_regex(code: str) -> re.Pattern | None:
    doc = load_profile(code)
    rs = doc.get("layout", {}).get("patient_dir_regex")
    if rs:
        try:
            return re.compile(rs)
        except Exception:
            pass
    return None


def extract_patient_folder(relpath: str, regex: re.Pattern | None) -> str | None:
    """Extract the REAL patient folder name from a Dropbox relative path.

    Returns the exact folder name string (e.g. "103"), never a made-up number.
    """
    parts = relpath.replace("\\", "/").split("/")
    if regex:
        for part in parts:
            m = regex.match(part)
            if m:
                # Return the exact folder name as it appears in Dropbox
                return part
    # Fallback: pure-digit folder
    for part in parts:
        if part.isdigit():
            return part
    return None


def device_from_dropbox_path(relpath: str, code: str) -> str | None:
    """Determine device from Dropbox path folders via profile device_dir_map."""
    doc = load_profile(code)
    dir_map = doc.get("layout", {}).get("device_dir_map", {})
    norm_map = {k.lower(): v for k, v in dir_map.items()}
    parts = relpath.replace("\\", "/").lower().split("/")
    for part in parts:
        if part in norm_map:
            return norm_map[part]
    # Also check tiers.A_parsed patterns by file extension
    fname = parts[-1] if parts else ""
    tiers = doc.get("tiers", {}).get("A_parsed", [])
    import fnmatch

    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        pattern = tier.get("pattern", "")
        device = tier.get("device", "")
        if pattern and fnmatch.fnmatch(fname, pattern.lower()):
            return device
    return None


def list_dropbox_files(dbx, root: str) -> dict[str, dict]:
    """List all files under a Dropbox root.

    Returns: {content_hash: {"path": full_path, "relpath": rel, "size": size}}
    If multiple files share a content_hash (duplicates), keeps the first.
    """
    import dropbox

    files: dict[str, dict] = {}
    try:
        result = dbx.files_list_folder(root, recursive=True)
    except Exception as e:
        print(f"[1raw] Dropbox list failed for {root}: {e}", flush=True)
        return files

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                ch = entry.content_hash
                if ch not in files:  # keep first on duplicates
                    rel = entry.path_display[len(root):].lstrip("/")
                    files[ch] = {
                        "path": entry.path_display,
                        "relpath": rel,
                        "size": entry.size,
                        "name": entry.name,
                    }
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    return files


def build_blob_info_map(account: str, project: str) -> dict[str, dict]:
    """Map Azure rawdata blob name -> {patient, device, relpath, filename}.

    Uses Dropbox content_hash matching (no state file needed):
    1. List Dropbox files for project -> content_hash -> path info
    2. List rawdata blobs with metadata -> dropbox_content_hash -> blob
    3. Match and extract patient/device from Dropbox path.

    Patient is the REAL Dropbox folder name, never a provisional number.
    """
    print(f"[1raw] building Dropbox -> blob mapping for {project}...",
          flush=True)

    # --- 1. Dropbox listing ---
    cfg = load_projects_config()
    projects_cfg = cfg.get("projects", {})
    proj_cfg = projects_cfg.get(project, {})
    dropbox_folder = proj_cfg.get("dropbox", project)
    data_roots = proj_cfg.get("data_roots", ["Database/RawData"])

    dbx = get_dropbox_client()
    dbx_files: dict[str, dict] = {}  # content_hash -> info
    for data_root in data_roots:
        root = f"{DROPBOX_ROOT}/{dropbox_folder}/{data_root}"
        print(f"[1raw] listing Dropbox: {root}", flush=True)
        files = list_dropbox_files(dbx, root)
        print(f"[1raw]   found {len(files)} files", flush=True)
        for ch, info in files.items():
            if ch not in dbx_files:
                dbx_files[ch] = info
    print(f"[1raw] Dropbox total: {len(dbx_files)} unique files", flush=True)

    # --- 2. rawdata blobs with metadata ---
    svc = azure_auth.get_blob_service_client(account)
    src = svc.get_container_client("rawdata")
    blob_by_hash: dict[str, str] = {}  # dropbox_content_hash -> blob name
    n_blobs = 0
    for b in src.list_blobs(name_starts_with=f"{project}/"):
        n_blobs += 1
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith(".parquet.enc"):
            continue
        try:
            props = src.get_blob_client(name).get_blob_properties()
            meta = props.metadata or {}
            ch = meta.get("dropbox_content_hash")
            if ch:
                blob_by_hash[ch] = name
        except Exception:
            pass
    print(f"[1raw] rawdata blobs: {n_blobs}, with content_hash: "
          f"{len(blob_by_hash)}", flush=True)

    # --- 3. Match ---
    regex = load_patient_regex(project)
    mapping: dict[str, dict] = {}
    matched, no_patient = 0, 0
    for ch, blob in blob_by_hash.items():
        info = dbx_files.get(ch)
        if not info:
            continue
        relpath = info["relpath"]
        patient = extract_patient_folder(relpath, regex)
        if not patient:
            no_patient += 1
            continue
        device = device_from_dropbox_path(relpath, project)
        mapping[blob] = {
            "patient": patient,  # EXACT Dropbox folder name
            "device": device,
            "relpath": relpath,
            "filename": info["name"],
        }
        matched += 1

    print(f"[1raw] matched {matched} blobs to Dropbox files "
          f"({no_patient} without patient folder)", flush=True)
    return mapping


def device_from_blob_name(blob: str) -> str | None:
    """Extract device from rawdata blob name: {PROJECT}/.../{device}_{sha8}.parquet.enc"""
    base = blob.rsplit("/", 1)[-1]
    m = re.match(r"^(.+)_([0-9a-fA-F]{8})\.parquet\.enc$", base)
    if m:
        return m.group(1).lower()
    return None


def normalize_device(device: str | None) -> str:
    """Map device code to K's folder name."""
    if not device:
        return "other"
    d = device.lower().strip()
    return DEVICE_FOLDERS.get(d, d)


def original_stem(filename: str) -> str:
    """Original Dropbox filename without extension, sanitized for blob names."""
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return stem[:80] or "file"


def sha8_from_blob(blob: str) -> str:
    base = blob.rsplit("/", 1)[-1]
    m = re.search(r"_([0-9a-fA-F]{8})\.parquet\.enc$", base)
    return m.group(1).lower() if m else "00000000"


def build_1raw(svc, project: str,
               blob_info: dict[str, dict]) -> tuple[int, int, int, list]:
    """rawdata/{PROJECT}/**/*.parquet.enc -> 1-raw/{PROJECT}/{patient}/{device}/{file}.csv"""
    src = svc.get_container_client("rawdata")
    dst = svc.get_container_client(CONTAINER)
    done, skipped, unmapped = 0, 0, 0
    examples: list[str] = []
    for blob in sorted(blob_names(src, f"{project}/")):
        if not blob.endswith(".parquet.enc"):
            continue
        info = blob_info.get(blob)
        if not info or not info.get("patient"):
            unmapped += 1
            continue  # Skip rather than use a wrong/provisional name
        patient = info["patient"]  # EXACT Dropbox folder name
        relpath = info["relpath"]
        filename = info["filename"]

        # Device: Dropbox path first (more reliable), blob name as fallback
        device = info.get("device") or device_from_blob_name(blob)
        device_folder = normalize_device(device)

        stem = original_stem(filename)
        sha8 = sha8_from_blob(blob)
        dest = f"{project}/{patient}/{device_folder}/{stem}_{sha8}.csv"
        if len(examples) < 5:
            examples.append(dest)
        if dest_exists(dst, dest):
            skipped += 1
            continue
        try:
            raw = src.get_blob_client(blob).download_blob().readall()
            df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
            csv_data = df.to_csv(index=False).encode("utf-8")
            dst.get_blob_client(dest).upload_blob(
                csv_data, overwrite=True,
                content_settings={"content_type": "text/csv"},
            )
            done += 1
            del df, csv_data, raw
        except Exception as exc:
            print(f"[1raw] ERROR {blob}: {exc}", flush=True)
        if (done + skipped) % 50 == 0 and (done + skipped) > 0:
            print(f"[1raw] {project}: {done} new, {skipped} skipped...",
                  flush=True)
    print(f"[1raw] {project}: {done} new, {skipped} existed, "
          f"{unmapped} unmapped (skipped)", flush=True)
    return done, skipped, unmapped, examples


def list_projects(svc) -> list[str]:
    src = svc.get_container_client("rawdata")
    codes = set()
    for b in src.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        parts = name.split("/")
        if parts:
            codes.add(parts[0].upper())
    return sorted(codes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="all",
                    help="Project code (e.g. COLECTOMIE) or 'all'")
    ap.add_argument("--out", default="1raw_out",
                    help="Local dir for the migration manifest")
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)

    # Create the 1-raw container (private by default)
    azure_auth.ensure_container(svc, CONTAINER)
    print(f"[1raw] container ready: {CONTAINER}", flush=True)

    projects = list_projects(svc) if args.project.strip().lower() == "all" \
        else [args.project.strip().upper()]
    print(f"[1raw] projects: {projects}", flush=True)

    manifest: dict = {"container": CONTAINER, "projects": {}}
    for project in projects:
        print(f"[1raw] === {project} ===", flush=True)
        # Build the blob -> {patient, device, relpath} map per project
        # (Dropbox listing is per-project, more efficient than global)
        blob_info = build_blob_info_map(account, project)
        new, skipped, unmapped, examples = build_1raw(svc, project, blob_info)
        manifest["projects"][project] = {
            "new": new, "skipped": skipped, "unmapped": unmapped,
            "example_paths": examples,
        }

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / f"1raw_manifest_{args.project}.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    print(f"[1raw] manifest: {mpath}", flush=True)
    print("[1raw] DONE. Patient names are REAL Dropbox folder names.",
          flush=True)


if __name__ == "__main__":
    main()
