"""Build K's clean 1-Raw container.

K's spec: "rawdata rename it 1-Raw in it only csv files in each project and
all the patients must be within so its the template 1-Raw in it the projects
in each of them the patients in each of them folders for infinity, bis ect
based on what they have in them csv or bis format (if we can convert it and
it will still work as csv it will be better for me to read them) nothing
more in raw. all the other containers erase them we will start from new"

Structure:
    1-Raw/{PROJECT}/{patient}/{device}/{file}.csv

- PROJECT: COLECTOMIE, DEXREM, etc. (10 projects)
- patient: REAL Dropbox folder name (e.g. "103"), NEVER provisional numbers
- device: infinity, bettercare, bis, nol, pumps, etc. (from source)
- file.csv: decrypted parquet -> CSV, content_type=text/csv (clickable preview)

Source: rawdata/{PROJECT}/parquet/*.parquet.enc
Patient: ingest state (blob -> Dropbox path -> patient folder via profile regex)
Device: blob name <device>_<sha8> cross-checked with Dropbox path device folders

BIS: .r2a (binary EEG) and .spa (pipe-delimited trends) were parsed at ingest
into standardized parquets (tools/process_bis.py). Converting those parquets
to CSV gives K the readable CSV format she asked for.

Usage:
    python -m tools.build_1raw --project COLECTOMIE
    python -m tools.build_1raw --project all

Resumable: blobs that already exist at the destination are skipped.
Nothing is deleted by this tool (see tools/erase_all_except_1raw.py).
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


def blob_names(container, prefix=""):
    for b in container.list_blobs(name_starts_with=prefix):
        yield b["name"] if isinstance(b, dict) else b.name


def dest_exists(container, blob: str) -> bool:
    try:
        container.get_blob_client(blob).get_blob_properties()
        return True
    except Exception:
        return False


def load_state(account: str) -> dict:
    svc = azure_auth.get_blob_service_client(account)
    blob = svc.get_blob_client("processed", "processed/_pipeline/state.json.enc")
    raw = blob.download_blob().readall()
    return json.loads(decrypt_bytes(raw).decode("utf-8"))


def load_profile(code: str) -> dict:
    """Load the project profile YAML (device_dir_map, patient_dir_regex)."""
    for fname in [f"{code.lower()}.yaml", f"{code.lower()}.yml"]:
        p = Path(__file__).resolve().parent.parent / "profiles" / fname
        if p.exists():
            try:
                import yaml
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
    parts = relpath.replace("\\", "/").split("/")
    if regex:
        for part in parts:
            if regex.match(part):
                return part
    for part in parts:
        if part.isdigit():
            return part
    return None


def build_blob_info_map(account: str) -> dict[str, dict]:
    """Map Azure rawdata blob name -> {patient, relpath}.

    Patient is the REAL Dropbox folder name, never a provisional number.
    """
    print("[1raw] loading ingest state for real Dropbox patient names...",
          flush=True)
    state = load_state(account)
    mapping: dict[str, dict] = {}
    regex_cache: dict[str, re.Pattern | None] = {}
    legacy = state.get("legacy", {})
    for code, proj_data in legacy.items():
        if code not in regex_cache:
            regex_cache[code] = load_patient_regex(code)
        regex = regex_cache[code]
        files = proj_data.get("files", {})
        for relpath, entry in files.items():
            if not isinstance(entry, dict) or entry.get("status") != "ok":
                continue
            blob = entry.get("stored")
            if not blob:
                continue
            patient = extract_patient_folder(relpath, regex)
            if patient:
                mapping[blob] = {"patient": patient, "relpath": relpath}
    print(f"[1raw] mapped {len(mapping)} blobs to real Dropbox patient folders",
          flush=True)
    return mapping


def device_from_blob_name(blob: str) -> str | None:
    """Extract device from rawdata blob name: {PROJECT}/parquet/{device}_{sha8}.parquet.enc"""
    base = blob.rsplit("/", 1)[-1]
    m = re.match(r"^(.+)_([0-9a-fA-F]{8})\.parquet\.enc$", base)
    if m:
        return m.group(1).lower()
    return None


def device_from_dropbox_path(relpath: str, code: str) -> str | None:
    """Determine device from Dropbox path folders via profile device_dir_map."""
    doc = load_profile(code)
    dir_map = doc.get("layout", {}).get("device_dir_map", {})
    # Normalize map keys to lowercase for case-insensitive matching
    norm_map = {k.lower(): v for k, v in dir_map.items()}
    parts = relpath.replace("\\", "/").lower().split("/")
    for part in parts:
        if part in norm_map:
            return norm_map[part]
    # Also check tiers.A_parsed patterns by file extension
    fname = parts[-1] if parts else ""
    tiers = doc.get("tiers", {}).get("A_parsed", [])
    for tier in tiers:
        if not isinstance(tier, dict):
            continue
        pattern = tier.get("pattern", "")
        device = tier.get("device", "")
        # Simple glob matching on the filename
        import fnmatch
        if pattern and fnmatch.fnmatch(fname, pattern.lower()):
            return device
    return None


def normalize_device(device: str | None) -> str:
    """Map device code to K's folder name."""
    if not device:
        return "other"
    d = device.lower().strip()
    return DEVICE_FOLDERS.get(d, d)


def original_stem(relpath: str) -> str:
    """Original Dropbox filename without extension, sanitized for blob names."""
    fname = relpath.replace("\\", "/").rsplit("/", 1)[-1]
    stem = fname.rsplit(".", 1)[0] if "." in fname else fname
    # Sanitize: keep alphanumerics, dash, underscore, dot
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._")
    return stem[:80] or "file"


def sha8_from_blob(blob: str) -> str:
    base = blob.rsplit("/", 1)[-1]
    m = re.search(r"_([0-9a-fA-F]{8})\.parquet\.enc$", base)
    return m.group(1).lower() if m else "00000000"


def build_1raw(svc, project: str,
               blob_info: dict[str, dict]) -> tuple[int, int, int, list]:
    """rawdata/{PROJECT}/**/*.parquet.enc -> 1-Raw/{PROJECT}/{patient}/{device}/{file}.csv"""
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
        patient = info["patient"]
        relpath = info["relpath"]

        # Device: blob name first, Dropbox path as cross-check
        device = device_from_blob_name(blob)
        path_device = device_from_dropbox_path(relpath, project)
        if path_device and device:
            # Prefer the Dropbox path device when they disagree, but log it
            norm_blob = normalize_device(device)
            norm_path = normalize_device(path_device)
            if norm_blob != norm_path:
                print(f"[1raw] device mismatch {blob}: "
                      f"blob={norm_blob} path={norm_path} -> using path",
                      flush=True)
            device_folder = norm_path
        else:
            device_folder = normalize_device(device or path_device)

        stem = original_stem(relpath)
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

    # Create the 1-Raw container (private by default)
    azure_auth.ensure_container(svc, CONTAINER)
    print(f"[1raw] container ready: {CONTAINER}", flush=True)

    # Build the global blob -> {patient, relpath} map ONCE
    blob_info = build_blob_info_map(account)

    projects = list_projects(svc) if args.project.strip().lower() == "all" \
        else [args.project.strip().upper()]
    print(f"[1raw] projects: {projects}", flush=True)

    manifest: dict = {"container": CONTAINER, "projects": {}}
    for project in projects:
        print(f"[1raw] === {project} ===", flush=True)
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
