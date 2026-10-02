"""Full signal-presence census from Parquet footers (no row data read).

Stage 2 discovery flags "column X entirely missing in measured sample"
from a 20-file sample (first 200k rows, first 12 signal columns per file).
This tool replaces the sample with complete counts: for EVERY encrypted
standardized parquet in rawdata, it reads only the Parquet footer
statistics (null counts per column) and reports, per
(project, device, column): in how many files the column exists, and in
how many it holds at least one valid (non-null) value.

Privacy: counts only, never values. Output is a plaintext CSV.

Usage:
  python -m tools.signal_presence_scan --out presence_out/
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes  # noqa: E402

ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT", "labdataplatform")
RAW = "rawdata"


def _device_of(blob: str) -> str:
    stem = blob[:-len(".parquet.enc")] if blob.endswith(".parquet.enc") \
        else blob
    return stem.rsplit("_", 1)[0].split("/")[-1]


def _project_of(blob: str) -> str:
    parts = blob.split("/")
    return parts[0].upper() if parts else "?"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)
    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)

    svc = azure_auth.get_blob_service_client(ACCOUNT)
    container = svc.get_container_client(RAW)

    blobs = []
    for b in container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith(".parquet.enc") and "/parquet/" in name:
            blobs.append(name)
    print(f"[signal-presence] {len(blobs)} parquets to census (footer-only)",
          flush=True)

    import pyarrow.parquet as pq

    # key -> {blob -> True/False/None}; per-file validity ORs row groups.
    per_file: dict[tuple[str, str, str], dict[str, object]] = \
        defaultdict(dict)
    n_ok = 0
    for i, blob in enumerate(sorted(blobs)):
        try:
            raw = svc.get_blob_client(
                container=RAW, blob=blob).download_blob().readall()
            pf = pq.ParquetFile(io.BytesIO(decrypt_bytes(raw)))
            project = _project_of(blob)
            device = _device_of(blob)
            md = pf.metadata
            file_valid: dict[str, object] = {}
            for rg_i in range(md.num_row_groups):
                rg = md.row_group(rg_i)
                for c_i in range(rg.num_columns):
                    col = rg.column(c_i)
                    path = col.path_in_schema
                    name = path[0] if len(path) == 1 else ".".join(path)
                    stats = col.statistics
                    try:
                        if stats and stats.has_null_count:
                            valid: object = \
                                stats.num_values - stats.null_count > 0
                        else:
                            valid = None
                    except Exception:
                        valid = None
                    prev = file_valid.get(name)
                    file_valid[name] = True if (prev is True or valid is True) \
                        else (None if prev is None and valid is None else False)
            for name, valid in file_valid.items():
                per_file[(project, device, name)][blob] = valid
            n_ok += 1
        except Exception as exc:  # noqa: BLE001
            print(f"[signal-presence] SKIP {blob}: "
                  f"{type(exc).__name__}: {str(exc)[:80]}", flush=True)
        if (i + 1) % 50 == 0:
            print(f"[signal-presence] {i + 1}/{len(blobs)}", flush=True)

    # Collapse per-file observations: n_files, n_files_with_valid (None =
    # unknown statistics -> counted as present, validity unknown).
    rows = []
    for (project, device, col), files in sorted(per_file.items()):
        n_files = len(files)
        n_valid = sum(1 for v in files.values() if v is True)
        n_unknown = sum(1 for v in files.values() if v is None)
        rows.append({
            "project": project, "device": device, "column": col,
            "n_files": n_files,
            "n_files_with_valid_data": n_valid,
            "n_files_stats_unknown": n_unknown,
            "frac_with_valid_data": round(n_valid / n_files, 4)
            if n_files else 0.0,
        })

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    csv_path = outdir / f"presence_{ts}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=[
            "project", "device", "column", "n_files",
            "n_files_with_valid_data", "n_files_stats_unknown",
            "frac_with_valid_data"])
        w.writeheader()
        w.writerows(rows)
    print(f"[signal-presence] {n_ok}/{len(blobs)} files read; "
          f"{len(rows)} (project, device, column) rows -> {csv_path}",
          flush=True)

    # Console summary: signals with zero valid data anywhere (the
    # "entirely missing" verdict, now on complete counts).
    print("== signals with NO valid data in any file (project/device) ==")
    for r in rows:
        if r["n_files_with_valid_data"] == 0 and \
                r["n_files_stats_unknown"] == 0:
            print(f"  {r['project']}/{r['device']}: {r['column']} "
                  f"({r['n_files']} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
