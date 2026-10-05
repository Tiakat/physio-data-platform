"""Rebuild PROMISES ground-truth labels from K's Dropbox ZIP.

The ZIP contains 50 recordings with doctor-manually-cleaned versions.
Compares raw vs cleaned to generate artifact labels for all 50 files.

Article reference: 50 recordings, 648,143 observations, 16,039 artifacts, 238 episodes.

Usage:
  DROPBOX_APP_KEY=... DROPBOX_APP_SECRET=... DROPBOX_REFRESH_TOKEN=... \
    python -m tools.build_promises_labels --out labels/promises_labels.csv
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import zipfile
from pathlib import Path

import requests


def get_dropbox_token():
    """Get Dropbox access token via refresh."""
    r = requests.post("https://api.dropboxapi.com/oauth2/token", data={
        "grant_type": "refresh_token",
        "refresh_token": os.environ["DROPBOX_REFRESH_TOKEN"],
        "client_id": os.environ["DROPBOX_APP_KEY"],
        "client_secret": os.environ["DROPBOX_APP_SECRET"],
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def download_dropbox_url(shared_url: str, token: str) -> bytes:
    """Download a shared Dropbox link."""
    # Convert shared link to direct download
    dl_url = shared_url.replace("?dl=0", "?dl=1").replace("&dl=0", "&dl=1")
    if "dl=1" not in dl_url:
        dl_url += "&dl=1" if "?" in dl_url else "?dl=1"
    r = requests.get(dl_url, headers={"Authorization": f"Bearer {token}"},
                     timeout=300)
    r.raise_for_status()
    return r.content


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--dropbox-url", default="https://www.dropbox.com/scl/fi/u2h5sy8jf1ia67j5s9oo1/new-version.zip?rlkey=4glk5e9zcgzpl0su448ormhr1&st=ceo58pgj&dl=0",
                    help="Dropbox shared link to PROMISES ZIP")
    ap.add_argument("--out", required=True, help="Output labels CSV path")
    ap.add_argument("--zip-path", default=None, help="Local ZIP path (skip download)")
    args = ap.parse_args(argv)

    # Get ZIP data
    if args.zip_path:
        print(f"[build-labels] using local ZIP: {args.zip_path}", flush=True)
        zip_data = Path(args.zip_path).read_bytes()
    else:
        print("[build-labels] getting Dropbox token...", flush=True)
        token = get_dropbox_token()
        print("[build-labels] downloading ZIP from Dropbox...", flush=True)
        zip_data = download_dropbox_url(args.dropbox_url, token)
        print(f"[build-labels] downloaded {len(zip_data)} bytes", flush=True)

    zf = zipfile.ZipFile(io.BytesIO(zip_data))
    names = zf.namelist()
    print(f"[build-labels] ZIP contains {len(names)} files", flush=True)

    # Identify file pairs: raw vs doctor-cleaned
    # Expected structure: each recording has raw and cleaned versions
    # Look for patterns indicating cleaned files
    import re
    
    # Group by base name
    pairs = {}  # base_id -> {"raw": name, "cleaned": name}
    for n in names:
        nl = n.lower()
        # Skip directories and non-data files
        if n.endswith("/") or not any(nl.endswith(ext) for ext in [".csv", ".txt", ".dat", ".mat"]):
            continue
        
        # Try to extract recording ID
        # Look for patterns like "patient_01", "rec_01", etc.
        m = re.search(r'(\d+)', os.path.basename(n))
        if not m:
            continue
        rec_id = m.group(1)
        
        # Determine if raw or cleaned
        is_cleaned = any(kw in nl for kw in ["clean", "manual", "doctor", "filtered", "corrected", "ground_truth", "gt"])
        
        if rec_id not in pairs:
            pairs[rec_id] = {}
        key = "cleaned" if is_cleaned else "raw"
        # Prefer more specific matches
        if key not in pairs[rec_id] or is_cleaned:
            pairs[rec_id][key] = n

    print(f"[build-labels] found {len(pairs)} recording groups", flush=True)
    complete = {k: v for k, v in pairs.items() if "raw" in v and "cleaned" in v}
    print(f"[build-labels] {len(complete)} complete pairs (raw + cleaned)", flush=True)

    # For each pair, compare raw vs cleaned to find artifact segments
    import pandas as pd
    import numpy as np
    
    all_labels = []
    for rec_id, pair in sorted(complete.items()):
        try:
            # Load raw and cleaned
            raw_data = zf.read(pair["raw"])
            clean_data = zf.read(pair["cleaned"])
            
            # Try to parse as CSV
            try:
                df_raw = pd.read_csv(io.BytesIO(raw_data))
                df_clean = pd.read_csv(io.BytesIO(clean_data))
            except:
                print(f"[build-labels] {rec_id}: cannot parse as CSV, skipping", flush=True)
                continue
            
            # Find ART column (arterial pressure)
            art_col = None
            for col in df_raw.columns:
                if "art" in col.lower():
                    art_col = col
                    break
            if not art_col or art_col not in df_clean.columns:
                print(f"[build-labels] {rec_id}: no ART column, skipping", flush=True)
                continue
            
            # Compare: where cleaned is NaN but raw has value = artifact removed
            # Where values differ significantly = artifact corrected
            raw_vals = pd.to_numeric(df_raw[art_col], errors="coerce").values
            clean_vals = pd.to_numeric(df_clean[art_col], errors="coerce").values
            
            # Align lengths
            n = min(len(raw_vals), len(clean_vals))
            raw_vals = raw_vals[:n]
            clean_vals = clean_vals[:n]
            
            # Find artifact segments: cleaned is NaN where raw wasn't
            raw_valid = ~np.isnan(raw_vals)
            clean_nan = np.isnan(clean_vals)
            artifact_mask = raw_valid & clean_nan
            
            # Also: large differences (>20% or >20 mmHg)
            both_valid = raw_valid & ~clean_nan
            diff = np.abs(raw_vals[both_valid] - clean_vals[both_valid])
            # Mark as artifact where diff is large
            large_diff_idx = np.where(both_valid)[0][diff > 20]
            artifact_mask[large_diff_idx] = True
            
            # Convert mask to segments (start_s, end_s)
            # Assume 1 Hz sampling (1 sample per second) - adjust if needed
            segments = []
            in_seg = False
            seg_start = 0
            for i, is_art in enumerate(artifact_mask):
                if is_art and not in_seg:
                    seg_start = i
                    in_seg = True
                elif not is_art and in_seg:
                    segments.append((seg_start, i, "artifact"))
                    in_seg = False
            if in_seg:
                segments.append((seg_start, len(artifact_mask), "artifact"))
            
            # Also add clean segments (gaps between artifacts)
            clean_segments = []
            prev_end = 0
            for s, e, _ in segments:
                if s > prev_end:
                    clean_segments.append((prev_end, s, "clean"))
                prev_end = e
            if prev_end < n:
                clean_segments.append((prev_end, n, "clean"))
            
            for s, e, label in segments + clean_segments:
                all_labels.append({
                    "project": "PROMISES",
                    "patient": f"patient {rec_id}",
                    "signal": art_col,
                    "start_s": s,
                    "end_s": e,
                    "label": label,
                })
            
            print(f"[build-labels] {rec_id}: {len(segments)} artifact segments, "
                  f"{len(clean_segments)} clean segments", flush=True)
            
        except Exception as e:
            print(f"[build-labels] {rec_id}: error - {e}", flush=True)
            continue

    # Write CSV
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    df_out = pd.DataFrame(all_labels)
    df_out.to_csv(out_path, index=False)
    
    n_art = len([l for l in all_labels if l["label"] == "artifact"])
    n_clean = len([l for l in all_labels if l["label"] == "clean"])
    print(f"\n[build-labels] DONE: {len(all_labels)} segments "
          f"({n_art} artifact, {n_clean} clean) "
          f"from {len(complete)} recordings", flush=True)
    print(f"[build-labels] written to {out_path}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
