"""Full per-patient processing: whole recording, real timestamps, all signals.

Runs inside GitHub Actions (PIPELINE_DATA_KEY never leaves the runner).
For one patient's encrypted parquets:
  - decrypts and loads the FULL file (no windowing)
  - resolves real timestamps (file time column, else reconstructed from
    sampling rate and explicitly labeled as such)
  - processes ALL signals through the engine (not just HR/SpO2)
  - writes encrypted filtered + QC parquets to processed/level2/
  - writes a plaintext lineage record (hashes + counts, no patient data)
  - draws per-signal graphs with gap compression and "Patient N" labels

Usage:
  python -m tools.process_patient --project DEXREM --patient "patient 1" \\
      --blob rawdata/DEXREM/parquet/bettercare_abc123.parquet.enc \\
      --out processed_out/
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes, encrypt_bytes  # noqa: E402
from tools.signal_processing import (  # noqa: E402
    find_config, get_var_spec, load_signal_configs, process_frame)

TIME_CANDIDATES = ["timestamp", "time", "datetime", "date_time",
                   "time_ms", "time_s", "epoch_ms", "epoch_s"]


def resolve_time(df: pd.DataFrame, fs_hint: float | None = None):
    """Return (time_index, method, fs_hz).

    method is 'file' (real timestamps from the file) or 'reconstructed'
    (row index scaled by sampling rate, explicitly labeled).
    """
    for cand in TIME_CANDIDATES:
        for col in df.columns:
            if col.lower() == cand:
                t = pd.to_datetime(df[col], errors="coerce")
                if t.notna().sum() > len(df) * 0.5:
                    # Measure fs from the real timestamps.
                    dt = t.dropna().diff().dt.total_seconds()
                    dt = dt[(dt > 0) & (dt < 3600)]
                    fs = 1.0 / dt.median() if len(dt) else fs_hint
                    return t, "file", fs
    # No usable time column: reconstruct from sampling rate.
    fs = fs_hint or 1.0
    t = pd.to_datetime(pd.Series(np.arange(len(df)) / fs, dtype=float),
                                 unit="s", origin="1970-01-01")
    return t, "reconstructed", fs


def compress_gaps(t: pd.Series, y: pd.Series, max_gap_s: float = 300.0):
    """Split a series into segments, breaking at gaps longer than max_gap_s.

    Returns a list of (t_seg, y_seg). Long missing stretches become
    axis breaks instead of plotted emptiness.
    """
    valid = y.notna()
    if not valid.any():
        return []
    # Time in seconds for gap measurement.
    ts = pd.to_datetime(t, errors="coerce")
    tsec = (ts - ts.min()).dt.total_seconds().to_numpy()
    idx = np.where(valid.to_numpy())[0]
    # Split where the time gap between consecutive valid samples is large.
    breaks = np.where(np.diff(tsec[idx]) > max_gap_s)[0]
    segments = []
    start = 0
    for b in breaks:
        seg_idx = idx[start:b + 1]
        segments.append((ts.iloc[seg_idx], y.iloc[seg_idx]))
        start = b + 1
    seg_idx = idx[start:]
    segments.append((ts.iloc[seg_idx], y.iloc[seg_idx]))
    return segments


def graph_signal(t, raw, filtered, qc, col, patient_label, path):
    """Per-signal graph with gap compression and honest labeling."""
    segments_raw = compress_gaps(t, raw)
    segments_filt = compress_gaps(t, filtered)
    if not segments_filt:
        return False
    fig, ax = plt.subplots(figsize=(14, 4))
    for ts_seg, y_seg in segments_raw:
        ax.plot(ts_seg, y_seg, color="0.75", lw=0.7, alpha=0.8)
    for ts_seg, y_seg in segments_filt:
        ax.plot(ts_seg, y_seg, color="tab:red", lw=1.1)
    # Flagged samples as markers (only where data exists).
    bad = (qc.to_numpy() != "VALID") & raw.notna().to_numpy()
    if bad.any():
        ax.scatter(pd.to_datetime(t).to_numpy()[bad],
                   raw.to_numpy()[bad], color="black", s=10, zorder=5)
    n_gaps = len(segments_filt) - 1
    title = f"{patient_label} — {col}"
    if n_gaps:
        title += f" ({n_gaps} gap{'s' if n_gaps > 1 else ''} compressed)"
    ax.set_title(title)
    ax.set_ylabel(col)
    ax.grid(alpha=0.3)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Full per-patient processing.")
    ap.add_argument("--project", required=True)
    ap.add_argument("--patient", required=True,
                    help="Patient label, e.g. 'patient 1' (as in folders).")
    ap.add_argument("--blob", default="",
                    help="Encrypted parquet blob path in rawdata container "
                         "(auto-discovered from --project if omitted).")
    ap.add_argument("--out", required=True)
    ap.add_argument("--source-sha256", default="",
                    help="SHA256 of the source Dropbox file (from state).")
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    patient_label = f"{args.project} {args.patient}"

    configs, index = load_signal_configs(
        Path("configs/signals"), Path("profiles/_variables.yaml"))
    missing_codes = list(index.get("global_missing_codes", []))
    drafts = [s for s, c in configs.items() if c.get("status") == "draft"]
    print(f"[process-patient] {patient_label}; draft configs: {drafts}",
          flush=True)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)

    blob = args.blob
    if not blob:
        # Auto-discover: first parquet for the project.
        container = svc.get_container_client("rawdata")
        prefix = f"{args.project}/parquet/"
        names = []
        for b in container.list_blobs(name_starts_with=prefix):
            name = b["name"] if isinstance(b, dict) else b.name
            if name.endswith(".parquet.enc"):
                names.append(name)
        if not names:
            print(f"[process-patient] no blobs under {prefix}", flush=True)
            return 1
        blob = sorted(names)[0]
        print(f"[process-patient] auto-selected {blob}", flush=True)

    # Download + decrypt the full file.
    raw = svc.get_blob_client(
        container="rawdata", blob=blob).download_blob().readall()
    blob_sha = hashlib.sha256(raw).hexdigest()
    df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
    print(f"[process-patient] loaded {len(df)} rows x {len(df.columns)} cols",
          flush=True)

    # Real timestamps (or explicitly reconstructed).
    t, time_method, fs = resolve_time(df)
    print(f"[process-patient] time: {time_method}, fs~{fs:.2f} Hz"
          if fs else "[process-patient] time: reconstructed",
          flush=True)

    # Process ALL signals (no column restriction).
    filt, qc, review = process_frame(
        df, None, configs, index, missing_codes, source="process_patient",
        time_index=t)

    # Lineage: hashes + counts only, no patient data.
    lineage = {
        "tool": "process_patient",
        "ts": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "patient_label": patient_label,
        "source_blob": blob,
        "source_blob_sha256": blob_sha,
        "source_dropbox_sha256": args.source_sha256 or None,
        "rows": len(df),
        "columns": len(df.columns),
        "time_method": time_method,
        "fs_hz": fs,
        "draft_configs": drafts,
        "qc_summary": {
            c: qc[c].value_counts().to_dict()
            for c in qc.columns if c.endswith("__qc")
        },
        "review": review,
    }
    (out / "lineage.json").write_text(json.dumps(lineage, indent=1))

    # Encrypted outputs back to Azure.
    def _enc_parquet(frame: pd.DataFrame) -> bytes:
        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            frame.to_parquet(tmp.name, index=False)
            with open(tmp.name, "rb") as fh:
                return encrypt_bytes(fh.read())

    safe_patient = "".join(
        ch if ch.isalnum() else "_" for ch in args.patient).strip("_")
    base = f"processed/level2/{args.project}/{safe_patient}"
    svc.get_blob_client(
        container="processed",
        blob=f"{base}/filtered.parquet.enc").upload_blob(
            _enc_parquet(filt), overwrite=True)
    svc.get_blob_client(
        container="processed",
        blob=f"{base}/qc.parquet.enc").upload_blob(
            _enc_parquet(qc), overwrite=True)
    svc.get_blob_client(
        container="processed",
        blob=f"{base}/lineage.json").upload_blob(
            json.dumps(lineage, indent=1), overwrite=True)
    print(f"[process-patient] wrote encrypted outputs to {base}", flush=True)

    # Graphs: one per signal with data, gap-compressed.
    gdir = out / "graphs"
    gdir.mkdir(exist_ok=True)
    n_graphs = 0
    for col in filt.columns:
        if col == "timestamp":
            continue
        if filt[col].notna().sum() == 0:
            continue
        qcol = col + "__qc"
        q = qc[qcol] if qcol in qc.columns else pd.Series(
            "VALID", index=filt.index)
        safe_col = "".join(
            ch if ch.isalnum() else "_" for ch in col).strip("_")
        if graph_signal(t, df[col] if col in df.columns else filt[col],
                        filt[col], q, col, patient_label,
                        gdir / f"{safe_col}.png"):
            n_graphs += 1
    print(f"[process-patient] {n_graphs} graphs", flush=True)
    print(f"[process-patient] done: {patient_label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
