"""Build K's clean 1-raw container directly from Dropbox.

K's spec: 1-raw with only CSV files. Template:
    1-raw/{PROJECT}/{patient}/{device}/{file}.csv

- PROJECT: COLECTOMIE, DEXREM, etc.
- patient: REAL Dropbox folder name (e.g. "103"), NEVER provisional numbers
- device: infinity, bettercare, bis, nol, pumps, etc. (from Dropbox path)
- file.csv: parsed data -> CSV, content_type=text/csv (clickable preview)

Source: Dropbox /Liam/Projets actifs/{project_dropbox_folder}/{data_root}/...
Direct ingest: download -> parse -> CSV -> upload. No intermediate blobs,
no hash matching needed. Patient names come straight from Dropbox paths.

Usage:
    python -m tools.build_1raw --project COLECTOMIE
    python -m tools.build_1raw --project all

Resumable: files that already exist at the destination are skipped.
Nothing is deleted by this tool.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402

import pandas as pd  # noqa: E402

CONTAINER = "1-raw"
DROPBOX_ROOT = "/Liam/Projets actifs"

# Normalize device codes -> K's device folder names.
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

# Skip these file types (not data)
SKIP_EXTENSIONS = {
    ".pdf", ".jpeg", ".jpg", ".png", ".tmp", ".ds_store",
    ".enc", ".med", ".ara", ".o_a", ".m_a", ".h_a", ".e_a", ".t_a", ".f_a",
}
SKIP_NAMES = {"thumbs.db", ".ds_store"}


def get_dropbox_client():
    """Dropbox client using app key/secret + refresh token."""
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


def load_projects_config() -> dict:
    import yaml

    p = Path(__file__).resolve().parent.parent / "config" / "projects.yaml"
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text()) or {}


def load_profile(code: str) -> dict:
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
    """Extract the REAL patient folder name from a Dropbox relative path."""
    parts = relpath.replace("\\", "/").split("/")
    if regex:
        for part in parts:
            if regex.match(part):
                return part  # EXACT folder name as in Dropbox
    for part in parts:
        if part.isdigit():
            return part
    return None


def device_from_path(relpath: str, code: str) -> str | None:
    """Determine device from Dropbox path via profile device_dir_map + tiers."""
    doc = load_profile(code)
    dir_map = doc.get("layout", {}).get("device_dir_map", {})
    norm_map = {k.lower(): v for k, v in dir_map.items()}
    parts = relpath.replace("\\", "/").lower().split("/")
    for part in parts:
        if part in norm_map:
            return norm_map[part]
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


def normalize_device(device: str | None) -> str:
    if not device:
        return "other"
    d = device.lower().strip()
    return DEVICE_FOLDERS.get(d, d)


def should_skip(filename: str) -> bool:
    low = filename.lower()
    if low in SKIP_NAMES:
        return True
    ext = "." + low.rsplit(".", 1)[-1] if "." in low else ""
    if ext in SKIP_EXTENSIONS:
        return True
    if low.startswith("~$") or low.startswith("."):
        return True
    return False


def list_dropbox_files(dbx, root: str) -> list[dict]:
    """List all data files under a Dropbox root (recursive)."""
    import dropbox

    files: list[dict] = []
    try:
        result = dbx.files_list_folder(root, recursive=True)
    except Exception as e:
        print(f"[1raw] Dropbox list failed for {root}: {e}", flush=True)
        return files

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                if should_skip(entry.name):
                    continue
                rel = entry.path_display[len(root):].lstrip("/")
                files.append({
                    "path": entry.path_display,
                    "relpath": rel,
                    "name": entry.name,
                    "size": entry.size,
                })
        if not result.has_more:
            break
        result = dbx.files_list_folder_continue(result.cursor)
    return files


def parse_file_to_df(local_path: str, filename: str, device: str) -> pd.DataFrame | None:
    """Parse a downloaded file into a DataFrame using the appropriate parser."""
    low = filename.lower()
    try:
        if low.endswith(".csv"):
            # Try generic CSV read first
            return pd.read_csv(local_path, low_memory=False)
        elif low.endswith(".spa"):
            # BIS spa: pipe-delimited trends
            return pd.read_csv(local_path, sep="|", low_memory=False)
        elif low.endswith(".r2a"):
            # BIS r2a: binary EEG - use bis parser
            from backbone.parsers.bis import parse

            return parse(local_path)
        elif low.endswith((".xls", ".xlsx")):
            # Metadata Excel - read first sheet
            return pd.read_excel(local_path, sheet_name=0)
        else:
            # Try CSV as fallback
            try:
                return pd.read_csv(local_path, low_memory=False)
            except Exception:
                return None
    except Exception as e:
        print(f"[1raw] parse failed {filename}: {e}", flush=True)
        return None


def dest_exists(container, blob: str) -> bool:
    try:
        container.get_blob_client(blob).get_blob_properties()
        return True
    except Exception:
        return False


def sanitize_filename(name: str) -> str:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return (stem[:80] or "file") + ".csv"


def build_1raw_project(svc, dbx, project: str) -> dict:
    """Direct Dropbox -> 1-raw for one project."""
    dst = svc.get_container_client(CONTAINER)

    cfg = load_projects_config()
    proj_cfg = cfg.get("projects", {}).get(project, {})
    dropbox_folder = proj_cfg.get("dropbox", project)
    data_roots = proj_cfg.get("data_roots", ["Database/RawData"])
    exclude = proj_cfg.get("exclude_folders", [])

    regex = load_patient_regex(project)

    # Collect files from all data roots
    all_files: list[dict] = []
    for data_root in data_roots:
        root = f"{DROPBOX_ROOT}/{dropbox_folder}/{data_root}"
        print(f"[1raw] listing Dropbox: {root}", flush=True)
        files = list_dropbox_files(dbx, root)
        print(f"[1raw]   {len(files)} files", flush=True)
        all_files.extend(files)

    print(f"[1raw] {project}: {len(all_files)} Dropbox files total", flush=True)

    done, skipped, no_patient, parse_fail = 0, 0, 0, 0
    examples: list[str] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        for i, f in enumerate(all_files):
            relpath = f["relpath"]
            # Check exclude folders
            if any(excl.replace("*", "") in relpath for excl in exclude):
                continue

            patient = extract_patient_folder(relpath, regex)
            if not patient:
                no_patient += 1
                continue

            device = normalize_device(device_from_path(relpath, project))
            dest_name = sanitize_filename(f["name"])
            dest = f"{project}/{patient}/{device}/{dest_name}"

            if len(examples) < 5:
                examples.append(dest)
            if dest_exists(dst, dest):
                skipped += 1
                continue

            # Download from Dropbox
            local = os.path.join(tmpdir, f"f_{i}")
            try:
                dbx.files_download_to_file(local, f["path"])
            except Exception as e:
                print(f"[1raw] download failed {f['path']}: {e}", flush=True)
                parse_fail += 1
                continue

            # Parse to DataFrame
            df = parse_file_to_df(local, f["name"], device)
            try:
                os.remove(local)
            except Exception:
                pass
            if df is None or df.empty:
                parse_fail += 1
                continue

            # Upload as CSV
            try:
                csv_data = df.to_csv(index=False).encode("utf-8")
                dst.get_blob_client(dest).upload_blob(
                    csv_data, overwrite=True,
                    content_settings={"content_type": "text/csv"},
                )
                done += 1
                del df, csv_data
            except Exception as e:
                print(f"[1raw] upload failed {dest}: {e}", flush=True)
                parse_fail += 1

            if (done + skipped) % 50 == 0 and (done + skipped) > 0:
                print(f"[1raw] {project}: {done} new, {skipped} skipped...",
                      flush=True)

    print(f"[1raw] {project}: {done} new, {skipped} existed, "
          f"{no_patient} no-patient, {parse_fail} failed", flush=True)
    return {
        "new": done, "skipped": skipped,
        "no_patient": no_patient, "failed": parse_fail,
        "example_paths": examples,
    }


def list_projects_from_config() -> list[str]:
    cfg = load_projects_config()
    projects = cfg.get("projects", {})
    return sorted([k for k, v in projects.items()
                   if v.get("status") == "data"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="all",
                    help="Project code (e.g. COLECTOMIE) or 'all'")
    ap.add_argument("--out", default="1raw_out",
                    help="Local dir for the migration manifest")
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    azure_auth.ensure_container(svc, CONTAINER)
    print(f"[1raw] container ready: {CONTAINER}", flush=True)

    dbx = get_dropbox_client()
    print("[1raw] Dropbox connected", flush=True)

    if args.project.strip().lower() == "all":
        projects = list_projects_from_config()
    else:
        projects = [args.project.strip().upper()]
    print(f"[1raw] projects: {projects}", flush=True)

    manifest: dict = {"container": CONTAINER, "projects": {}}
    for project in projects:
        print(f"[1raw] === {project} ===", flush=True)
        manifest["projects"][project] = build_1raw_project(svc, dbx, project)

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / f"1raw_manifest_{args.project}.json"
    mpath.write_text(json.dumps(manifest, indent=2))
    print(f"[1raw] manifest: {mpath}", flush=True)
    print("[1raw] DONE. Patient names are REAL Dropbox folder names.",
          flush=True)


if __name__ == "__main__":
    main()
