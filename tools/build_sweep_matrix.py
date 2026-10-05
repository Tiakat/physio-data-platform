"""Build the sweep matrix: every encrypted parquet, chunked for workers.

Lists rawdata/<CODE>/parquet/*.parquet.enc across all (or selected)
projects and groups them into chunks. Output is a JSON list of chunks,
each chunk a list of {project, blob, label}.

Labels use REAL Dropbox patient folder names (from ingest state),
not provisional numbers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes  # noqa: E402


def load_state(account: str) -> dict:
    """Load and decrypt the ingest state from Azure."""
    svc = azure_auth.get_blob_service_client(account)
    blob = svc.get_blob_client("processed", "processed/_pipeline/state.json.enc")
    raw = blob.download_blob().readall()
    return json.loads(decrypt_bytes(raw).decode("utf-8"))


def load_patient_regex(code: str) -> re.Pattern | None:
    """Load patient_dir_regex from project profile."""
    # Try profiles directory
    for fname in [f"{code.lower()}.yaml", f"{code.lower()}.yml"]:
        p = Path(__file__).resolve().parent.parent / "profiles" / fname
        if p.exists():
            try:
                import yaml
                doc = yaml.safe_load(p.read_text())
                regex_str = doc.get("layout", {}).get("patient_dir_regex")
                if regex_str:
                    return re.compile(regex_str)
            except Exception:
                pass
    return None


def extract_patient_folder(relpath: str, regex: re.Pattern | None) -> str | None:
    """Extract patient folder name from Dropbox relative path.
    
    Example: 'RawData/103/ExtractedData/Infinity/file.csv' -> '103'
    """
    parts = relpath.replace("\\", "/").split("/")
    if regex:
        for part in parts:
            if regex.match(part):
                return part
    # Fallback: look for numeric folder (common pattern)
    for part in parts:
        if part.isdigit():
            return part
    return None


def build_blob_to_patient_map(state: dict) -> dict[str, str]:
    """Build map: Azure blob name -> Dropbox patient folder.
    
    State structure: state["legacy"][code]["files"][relpath] = {"stored": blob, ...}
    """
    blob_to_patient = {}
    # Cache regex per project
    regex_cache = {}
    
    legacy = state.get("legacy", {})
    for code, proj_data in legacy.items():
        files = proj_data.get("files", {})
        if code not in regex_cache:
            regex_cache[code] = load_patient_regex(code)
        regex = regex_cache[code]
        
        for relpath, entry in files.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("status") != "ok":
                continue
            blob = entry.get("stored")
            if not blob:
                continue
            patient = extract_patient_folder(relpath, regex)
            if patient:
                blob_to_patient[blob] = patient
    
    return blob_to_patient


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default="all",
                    help="Comma-separated codes or 'all'.")
    ap.add_argument("--chunk-size", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="",
                    help=("Targeted retry: comma-separated 'PROJECT:label' "
                          "pairs, e.g. 'COLECTOMIE:103'. Only those "
                          "files are included in the matrix."))
    args = ap.parse_args(argv)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("rawdata")

    wanted = None
    if args.projects.strip().lower() != "all":
        wanted = {p.strip().upper()
                  for p in args.projects.split(",") if p.strip()}

    # Load state for real patient folder names
    print("[sweep-matrix] loading ingest state for Dropbox patient names...", flush=True)
    try:
        state = load_state(account)
        blob_to_patient = build_blob_to_patient_map(state)
        print(f"[sweep-matrix] mapped {len(blob_to_patient)} blobs to Dropbox patient folders", flush=True)
    except Exception as exc:
        print(f"[sweep-matrix] WARNING: could not load state: {exc}", flush=True)
        print("[sweep-matrix] falling back to blob filename (no provisional numbers)", flush=True)
        blob_to_patient = {}

    by_project: dict[str, list[str]] = {}
    for b in container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith(".parquet.enc"):
            continue
        parts = name.split("/")
        if len(parts) < 3:
            continue
        code = parts[0].upper()
        if wanted and code not in wanted:
            continue
        by_project.setdefault(code, []).append(name)

    only = None
    if args.only.strip():
        only = set()
        for piece in args.only.split(","):
            piece = piece.strip()
            if ":" in piece:
                proj, lab = piece.split(":", 1)
                only.add((proj.strip().upper(), lab.strip().lower()))

    # Flatten into (project, blob) pairs, then chunk.
    # Labels are REAL Dropbox patient folder names, never provisional numbers.
    pairs = []
    unmapped = 0
    for code in sorted(by_project):
        for blob in sorted(by_project[code]):
            patient = blob_to_patient.get(blob)
            if patient:
                label = patient
            else:
                # Fallback: use blob filename without extension, NOT a made-up number
                label = blob.rsplit("/", 1)[-1].replace(".parquet.enc", "")
                unmapped += 1
            if only is not None and (code, label.lower()) not in only:
                continue
            pairs.append({
                "project": code,
                "blob": blob,
                "label": label,
            })
    chunks = [pairs[i:i + args.chunk_size]
              for i in range(0, len(pairs), args.chunk_size)]
    Path(args.out).write_text(json.dumps(chunks))
    print(f"[sweep-matrix] {len(pairs)} files -> {len(chunks)} chunks", flush=True)
    if unmapped:
        print(f"[sweep-matrix] WARNING: {unmapped} blobs had no Dropbox mapping (used filename)", flush=True)
    for code in sorted(by_project):
        print(f"  {code}: {len(by_project[code])} files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
