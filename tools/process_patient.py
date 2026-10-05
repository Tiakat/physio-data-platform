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
    """Return (t_seconds, method, fs_hz).

    t_seconds: seconds from recording start (float).
    method is 'file' (real timestamps from the file) or 'reconstructed'
    (row index scaled by sampling rate, explicitly labeled).
    """
    for cand in TIME_CANDIDATES:
        for col in df.columns:
            if col.lower() == cand:
                t = pd.to_datetime(df[col], errors="coerce")
                if t.notna().sum() > len(df) * 0.5:
                    t0 = t.min()
                    tsec = (t - t0).dt.total_seconds()
                    dt = tsec.diff()
                    dt = dt[(dt > 0) & (dt < 3600)]
                    fs = 1.0 / dt.median() if len(dt) else fs_hint
                    return tsec, "file", fs
    # BetterCare-style relative milliseconds: a column with values like
    # 0, 5, 10... (ms from recording start, no absolute date).
    for col in df.columns:
        cl = col.lower()
        if "ms" in cl or "millis" in cl or cl in ("t", "elapsed"):
            v = pd.to_numeric(df[col], errors="coerce")
            if v.notna().sum() > len(df) * 0.5 and v.min() >= 0:
                # Heuristic: monotonic-ish increasing, ms-scale values.
                if v.max() > 1000:  # at least 1 second of data
                    tsec = (v - v.min()) / 1000.0
                    dt = tsec.diff()
                    dt = dt[(dt > 0) & (dt < 3600)]
                    fs = 1.0 / dt.median() if len(dt) else fs_hint
                    return tsec, "bettercare_ms", fs
    # No usable time column: reconstruct from sampling rate.
    fs = fs_hint or 1.0
    tsec = pd.Series(np.arange(len(df), dtype=float) / fs)
    return tsec, "reconstructed", fs


def compress_gaps(tsec: pd.Series, y: pd.Series, max_gap_s: float = 300.0):
    """Split a series into segments, breaking at gaps longer than max_gap_s.

    tsec: seconds from start. Returns list of (t_seg, y_seg).
    """
    valid = y.notna()
    if not valid.any():
        return []
    tarr = tsec.to_numpy()
    idx = np.where(valid.to_numpy())[0]
    breaks = np.where(np.diff(tarr[idx]) > max_gap_s)[0]
    segments = []
    start = 0
    for b in breaks:
        seg_idx = idx[start:b + 1]
        segments.append((tsec.iloc[seg_idx], y.iloc[seg_idx]))
        start = b + 1
    seg_idx = idx[start:]
    segments.append((tsec.iloc[seg_idx], y.iloc[seg_idx]))
    return segments


def _decimate_plot(tsec, *frames, max_pts=4000, min_n=100_000):
    """Stride-decimate aligned Series/DataFrames for display.

    Memory guard (OOM fix): a 14"-wide figure at 110 dpi is ~1540 px, so
    plotting millions of points is invisible overplotting that explodes
    Agg renderer memory. Decimation is display-only; data is untouched.
    Files below min_n rows are plotted in full: stride decimation can drop
    every valid sample of an ultra-sparse column (e.g. NIBP with a handful
    of readings), which then crashes the overview's pd.concat on an empty
    segment list.
    """
    n = len(tsec)
    if n <= max(min_n, max_pts):
        return (tsec,) + frames
    step = int(np.ceil(n / max_pts))
    idx = np.arange(0, n, step)
    def _take(f):
        return f.iloc[idx] if isinstance(f, (pd.Series, pd.DataFrame)) else f
    return (_take(tsec),) + tuple(_take(f) for f in frames)


def graph_signal(tsec, raw, filtered, qc, col, patient_label,
                 source_label, path, duration_s):
    """Per-signal graph: seconds x-axis, source-labeled, tight scales."""
    tsec, raw, filtered, qc = _decimate_plot(tsec, raw, filtered, qc)
    segments_filt = compress_gaps(tsec, filtered)
    if not segments_filt:
        return False
    fig, ax = plt.subplots(figsize=(14, 4))
    # Raw in light gray behind, filtered in red on top -- judge the
    # filtering yourself.
    raw_segs = compress_gaps(tsec, raw)
    for i, (ts_seg, y_seg) in enumerate(raw_segs):
        ax.plot(ts_seg, y_seg, color="0.7", lw=0.6, alpha=0.7,
                label="raw" if i == 0 else "")
    for i, (ts_seg, y_seg) in enumerate(segments_filt):
        ax.plot(ts_seg, y_seg, color="tab:red", lw=1.2,
                label="filtered" if i == 0 else "")
    bad = (qc.to_numpy() != "VALID") & raw.notna().to_numpy()
    if bad.any():
        ax.scatter(tsec.to_numpy()[bad], raw.to_numpy()[bad],
                   color="black", s=12, zorder=5, label="flagged")
    n_gaps = len(segments_filt) - 1
    title = f"{patient_label} [{source_label}] — {col}"
    if n_gaps:
        title += f" ({n_gaps} gap{'s' if n_gaps > 1 else ''} compressed)"
    ax.set_title(title)
    ax.set_ylabel(col)
    ax.set_xlabel(f"time (s) — full recording: {duration_s:.0f}s")
    # Tight scales: no wasted axis space.
    all_y = pd.concat([s[1] for s in segments_filt])
    pad = (all_y.max() - all_y.min()) * 0.08 or 1.0
    ax.set_ylim(all_y.min() - pad, all_y.max() + pad)
    ax.set_xlim(0, duration_s)
    ax.grid(alpha=0.3)
    ax.legend(loc="upper right", fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)
    return True


def graph_overview(tsec, filt, qc, cols, title, path, duration_s):
    """One graph with many columns (K's regroup request)."""
    cols = [c for c in cols if c in filt.columns
            and filt[c].notna().sum() > 0]
    if not cols:
        return False
    tsec, filt = _decimate_plot(tsec, filt)
    n = len(cols)
    fig, axes = plt.subplots(n, 1, figsize=(14, 2.6 * n), sharex=True,
                             squeeze=False)
    axes = axes[:, 0]
    for ax, col in zip(axes, cols):
        segs = compress_gaps(tsec, filt[col])
        for ts_seg, y_seg in segs:
            ax.plot(ts_seg, y_seg, lw=0.9)
        if segs:
            all_y = pd.concat([s[1] for s in segs])
            pad = (all_y.max() - all_y.min()) * 0.08 or 1.0
            ax.set_ylim(all_y.min() - pad, all_y.max() + pad)
        # Empty segs: decimation dropped every valid sample of this sparse
        # column in a giant file. Subplot stays blank; data is intact in
        # the processed parquet (display-only limitation).
        ax.set_ylabel(col, fontsize=9)
        ax.grid(alpha=0.3)
    axes[0].set_title(title)
    axes[-1].set_xlabel(f"time (s) — full recording: {duration_s:.0f}s")
    fig.tight_layout()
    fig.savefig(path, dpi=100)
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
    # Source device from blob name (e.g. bettercare_xxx -> BetterCare).
    blob_lower = (args.blob or "").lower()
    if "bettercare" in blob_lower:
        source_label = "BetterCare"
    elif "infinity" in blob_lower:
        source_label = "Infinity"
    elif "bis" in blob_lower:
        source_label = "BIS"
    elif "nol" in blob_lower:
        source_label = "NOL"
    elif "pump" in blob_lower or "perf" in blob_lower:
        source_label = "Pump"
    else:
        source_label = "unknown source"

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
    # Memory guard (OOM fix): giant BetterCare files (5M rows x 53 cols)
    # are ~2.1 GB as float64 and SIGTERM-killed 3 sweep jobs (Oct 2026).
    # float32 halves RAM; precision is plenty for filtering/QC/graphs.
    # Raw source data stays untouched in Azure.
    for c in df.columns:
        if df[c].dtype == np.float64:
            df[c] = df[c].astype(np.float32)
    print(f"[process-patient] loaded {len(df)} rows x {len(df.columns)} cols",
          flush=True)

    # Real timestamps (or explicitly reconstructed).
    t, time_method, fs = resolve_time(df)
    print(f"[process-patient] time: {time_method}, fs~{fs:.2f} Hz"
          if fs else "[process-patient] time: reconstructed",
          flush=True)

    # Process ALL signals (no column restriction).
    # USE_SMART_FILTER=1 -> knowledge-driven filtering (dictionary hard
    # bounds + trained ML models + safety rails that can never wipe a
    # signal). Default keeps the legacy process_frame path.
    smart_mode = os.environ.get("USE_SMART_FILTER", "0") == "1"
    filter_log = None
    if smart_mode:
        from tools.smart_filter import smart_filter_frame
        filt, qc, filter_log = smart_filter_frame(df, t, svc)
        review = {"smart_filter": True}
        print("[process-patient] filtering via smart_filter "
              "(knowledge-driven)", flush=True)
    else:
        filt, qc, review = process_frame(
            df, None, configs, index, missing_codes,
            source="process_patient", time_index=t)

    duration_s = float(t.max() - t.min()) if len(t) else 0.0
    # Lineage: hashes + counts only, no patient data.
    lineage = {
        "tool": "process_patient",
        "ts": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "patient_label": patient_label,
        "source_device": source_label,
        "source_blob": blob,
        "source_blob_sha256": blob_sha,
        "source_dropbox_sha256": args.source_sha256 or None,
        "rows": len(df),
        "columns": len(df.columns),
        "duration_s": duration_s,
        "time_method": time_method,
        "smart_filter": smart_mode,
        "filter_log_summary": (
            {k: v for k, v in filter_log.items() if k != "columns"}
            if filter_log else None),
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

    # Graphs: per-signal + regrouped overviews (K's request).
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
                        filt[col], q, col, patient_label, source_label,
                        gdir / f"{safe_col}.png", duration_s):
            n_graphs += 1
    # Overviews: all columns regrouped by family.
    def _fam(prefixes):
        return [c for c in filt.columns
                if any(c.upper().startswith(p) for p in prefixes)]
    overviews = {
        "overview_hr": _fam(["HR"]),
        "overview_ecg": _fam(["ECG"]),
        "overview_pressures": _fam(["PA_", "PRES_", "NBP", "CVP", "PAP",
                                    "PAD", "PAI"]),
        "overview_resp": _fam(["RESP", "RR", "CO2", "ETCO2", "AIR_",
                               "PAW", "TV", "VT"]),
        "overview_other": _fam(["SPO2", "PLETH", "PVC", "PNI", "LA_",
                                "RA_"]),
    }
    for name, cols in overviews.items():
        if graph_overview(t, filt, qc, cols,
                          f"{patient_label} [{source_label}] — {name}",
                          gdir / f"{name}.png", duration_s):
            n_graphs += 1
    print(f"[process-patient] {n_graphs} graphs", flush=True)
    # Upload graphs to the dedicated 'graphs' container (K's request:
    # separate container, browseable in Azure portal before website).
    # Path: graphs/{PROJECT}/{safe_patient}/{column}.png
    try:
        for png in sorted(gdir.glob("*.png")):
            svc.get_blob_client(
                container="graphs",
                blob=f"{args.project}/{safe_patient}/{png.name}").upload_blob(
                    png.read_bytes(), overwrite=True,
                    content_settings={"content_type": "image/png"})
        print(f"[process-patient] uploaded {n_graphs} graphs to graphs/{args.project}/{safe_patient}/", flush=True)
    except Exception as e:
        print(f"[process-patient] WARNING: graph upload failed: {e}", flush=True)
    print(f"[process-patient] done: {patient_label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
