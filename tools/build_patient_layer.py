"""Patient integration layer: ONE integrated record per patient.

Reads the per-device level2 outputs (filtered + QC + lineage) produced by
tools/process_patient.py and builds K's three analysis datasets:

  A. timeseries.parquet  - one row per analysis window (default 10 s):
     window_start, window_end, then one column per signal holding the
     median of VALID samples in the window. No upsampling: a 200 Hz
     waveform and a 1 Hz trend both become one value per window.
  B. events.parquet       - one row per detected/annotated event:
     drug-rate step changes, recording gaps, and optional clinical
     annotations (--annotations CSV with event,time_s columns), each with
     baseline/peak/delta responses for HR/MAP/NOL/BIS when present.
  C. features.parquet     - one row per patient: recording summary,
     per-signal mean/SD/validity, NOL/BIS/MAP exposure metrics, total
     drug doses (rate x time), and event-response summaries.

Plus a plaintext manifest.json (inputs, hashes, window size, column map).

Time model (v1): every device contributes seconds-from-its-own-recording-
start (the `timestamp` column process_patient writes). Devices are aligned
at t=0; per-device time_method values are recorded in the manifest.
True cross-device sync via absolute timestamps is a v2 refinement.

Column naming: the config channel name when the dictionary resolves the
raw column (via tools.signal_processing.find_config), else the raw name
with the vendor unit segment stripped. On cross-device collision the
device name is suffixed: HR__bettercare vs HR__infinity.

Usage:
  python -m tools.build_patient_layer --project DEXREM --patient "patient 1" \\
      --in layer_in/ --out layer_out/ --window-s 10
  # layer_in/<device>/{filtered.parquet,qc.parquet,lineage.json}

  python -m tools.build_patient_layer --project DEXREM --patient "patient 1" \\
      --in layer_in/ --out layer_out/ --encrypt --annotations events.csv
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    from tools.signal_processing import (
        find_config, load_signal_configs, _strip_unit_segment)
    _HAS_SP = True
except Exception:  # pragma: no cover - degraded mode without the engine
    _HAS_SP = False

# Preferred column order for the timeseries frame (K's spec). Matching is
# case-insensitive; anything else present is appended after, so no signal
# is ever dropped.
CORE_ORDER = ["HR", "SBP", "DBP", "MAP", "ART_SYS", "ART_DIA", "ART_MEAN",
              "SPO2", "RR", "ETCO2", "NOL", "BIS", "SQI", "EMG",
              "FIO2", "PEEP", "PIP", "VT", "MV",
              "PROPOFOL", "PROPOFOL_RATE", "REMIFENTANIL", "REMIFENTANIL_RATE",
              "REMI", "SEVOFLURANE", "DESFLURANE", "MAC"]

# Column-name fragments marking drug/exposure variables (not waveforms).
DRUG_HINTS = ("rate", "infusion", "dose", "propofol", "remifentanil", "remi",
              "sevo", "desflurane", "fio2", "norepinephrine", "noradrenaline",
              "phenylephrine", "vasopressor", "atracurium", "rocuronium",
              "sufentanil", "fentanyl", "ketamine", "dexmedetomidine",
              "mac", "pump", "syringe")

# Signals for which event responses (baseline/peak/delta) are computed.
RESPONSE_SIGNALS = ("HR", "MAP", "ART_MEAN", "NOL", "BIS")


def _is_valid(qc_col: pd.Series) -> pd.Series:
    """Boolean mask of VALID samples; robust to categorical/object dtype."""
    return qc_col.astype(str) == "VALID"


def _label_column(col: str, configs=None, index=None) -> str:
    """Map a raw column to its config channel name (fallback: cleaned raw)."""
    if index is not None and _HAS_SP:
        try:
            _cfg, channel = find_config(col, configs or {}, index)
            if channel:
                return channel
        except Exception:
            pass
    if _HAS_SP:
        try:
            return _strip_unit_segment(col).strip().upper().replace(" ", "_")
        except Exception:
            pass
    return re.sub(r"\s+", "_", str(col).strip())


def _load_device(ddir: Path, configs=None, index=None):
    """Load one device dir -> dict or None (skipped with reason)."""
    filt_p = ddir / "filtered.parquet"
    qc_p = ddir / "qc.parquet"
    lin_p = ddir / "lineage.json"
    if not filt_p.exists():
        return None, f"missing {filt_p}"
    filt = pd.read_parquet(filt_p)
    qc = pd.read_parquet(qc_p) if qc_p.exists() else None
    lineage = json.loads(lin_p.read_text()) if lin_p.exists() else {}
    device = lineage.get("source_device") or ddir.name
    device = re.sub(r"\W+", "_", str(device)).strip("_").lower() or "unknown"

    if "timestamp" not in filt.columns:
        # Fall back to any time-like column, else row index as seconds.
        tcol = next((c for c in filt.columns
                     if c.lower() in ("t_seconds", "time_s", "time")),
                    None)
        if tcol is not None:
            filt = filt.rename(columns={tcol: "timestamp"})
        else:
            filt = filt.copy()
            filt.insert(0, "timestamp", np.arange(len(filt), dtype=float))
    t = pd.to_numeric(filt["timestamp"], errors="coerce")

    signals = {}
    for col in filt.columns:
        if col == "timestamp":
            continue
        s = pd.to_numeric(filt[col], errors="coerce")
        if s.notna().sum() == 0:
            continue
        qcol = col + "__qc"
        if qc is not None and qcol in qc.columns:
            valid = _is_valid(qc[qcol])
        else:
            valid = s.notna()
        label = _label_column(col, configs, index)
        # Duplicate labels within a device: keep the fuller one.
        if label in signals:
            if valid.sum() <= signals[label]["valid"].sum():
                continue
        signals[label] = {"values": s, "valid": valid, "raw": col}

    meta = {
        "device": device,
        "dir": str(ddir),
        "time_method": lineage.get("time_method"),
        "fs_hz": lineage.get("fs_hz"),
        "source_blob": lineage.get("source_blob"),
        "rows": len(filt),
    }
    return {"t": t, "signals": signals, "meta": meta}, None


def _windowize(dev, window_s: float):
    """Bin one device's signals into windows -> (win_index, frame, counts).

    frame: window_start -> {label: median of VALID samples}.
    counts: window_start -> {label: n_valid}.
    """
    t = dev["t"].to_numpy(dtype=float)
    if len(t) == 0:
        return None, None
    t0 = np.nanmin(t)
    win = np.floor((t - t0) / window_s).astype(int)
    starts = np.unique(win[~np.isnan(win)])
    frame = {}
    counts = {}
    for w in starts:
        m = win == w
        row, crow = {}, {}
        for label, sd in dev["signals"].items():
            v = sd["values"].to_numpy(dtype=float)[m]
            ok = sd["valid"].to_numpy()[m] & ~np.isnan(v)
            n = int(ok.sum())
            crow[label] = n
            row[label] = float(np.median(v[ok])) if n else np.nan
        frame[float(w * window_s + t0)] = row
        counts[float(w * window_s + t0)] = crow
    fdf = pd.DataFrame.from_dict(frame, orient="index")
    fdf.index.name = "window_start"
    cdf = pd.DataFrame.from_dict(counts, orient="index")
    cdf.index.name = "window_start"
    return fdf, cdf


def build_timeseries(devices, window_s: float = 10.0):
    """Merge device window frames into one timeseries + column map.

    Returns (timeseries_df, column_map). Colliding labels across devices
    become LABEL__device.
    """
    per_dev = []
    for dev in devices:
        fdf, _cdf = _windowize(dev, window_s)
        if fdf is None or fdf.empty:
            continue
        per_dev.append((dev["meta"]["device"], fdf))
    if not per_dev:
        return pd.DataFrame(), {}

    # Detect cross-device label collisions.
    seen, dup = set(), set()
    for _d, fdf in per_dev:
        for c in fdf.columns:
            if c in seen:
                dup.add(c)
            seen.add(c)

    column_map = {}
    parts = []
    for device, fdf in per_dev:
        rename = {}
        for c in fdf.columns:
            new = f"{c}__{device}" if c in dup else c
            rename[c] = new
            column_map[new] = {"label": c, "device": device}
        parts.append(fdf.rename(columns=rename))
    ts = pd.concat(parts, axis=1).sort_index()
    ts.index.name = "window_start"
    # Explicit full window grid (missing = NaN, never zero): windows with
    # no samples on any device become explicit empty rows, so recording
    # gaps are visible in the data itself, not just inferred.
    if not ts.empty:
        lo, hi = float(ts.index.min()), float(ts.index.max())
        n = int(round((hi - lo) / window_s))
        full = np.round(lo + np.arange(n + 1) * window_s, 6)
        ts = ts.reindex(pd.Index(full, dtype="float64"))
        ts.index.name = "window_start"
    ts["window_end"] = ts.index + window_s

    # Column order: core signals first (case-insensitive), then the rest.
    core_rank = {c: i for i, c in enumerate(CORE_ORDER)}
    def _rank(col):
        base = col.split("__")[0].upper()
        return (core_rank.get(base, len(core_rank)), col)
    ordered = sorted([c for c in ts.columns if c != "window_end"],
                     key=_rank)
    ts = ts[["window_end"] + ordered] if False else ts[ordered + ["window_end"]]
    # Put window_end right after the index for readability.
    cols = ["window_end"] + ordered
    ts = ts[cols]
    return ts, column_map


def _is_drug_column(col: str) -> bool:
    cl = col.lower()
    return any(h in cl for h in DRUG_HINTS)


def detect_events(ts: pd.DataFrame, window_s: float,
                  annotations: pd.DataFrame | None = None,
                  gap_s: float = 300.0):
    """Detect drug step-changes and recording gaps; merge annotations.

    Returns events DataFrame with columns:
    event, t_start, t_end, detail + response columns (baseline/peak/delta)
    for HR/MAP/NOL/BIS when present.
    """
    events = []
    if ts.empty:
        return pd.DataFrame()

    sig_cols = [c for c in ts.columns if c not in ("window_end",)]
    starts = ts.index.to_numpy(dtype=float)

    # --- drug-rate step changes -------------------------------------------
    for col in sig_cols:
        if not _is_drug_column(col):
            continue
        v = ts[col].to_numpy(dtype=float)
        ok = ~np.isnan(v)
        if ok.sum() < 4:
            continue
        vv = v[ok]
        med = np.median(np.abs(vv[np.nonzero(vv)])) if np.any(vv) else 0.0
        scale = med if med > 0 else 1.0
        d = np.abs(np.diff(np.where(ok, v, np.nan)))
        # step = jump > 25% of typical non-zero level, sustained 3 windows
        for i in range(1, len(v) - 3):
            if not (ok[i - 1] and ok[i]):
                continue
            jump = abs(v[i] - v[i - 1])
            if jump > 0.25 * scale and np.all(ok[i:i + 3]):
                if abs(np.median(v[i:i + 3]) - v[i - 1]) > 0.25 * scale:
                    events.append({
                        "event": "drug_admin",
                        "t_start": float(starts[i]),
                        "t_end": float(starts[i] + window_s),
                        "detail": (f"{col}: {v[i-1]:.3g} -> "
                                   f"{np.median(v[i:i+3]):.3g}"),
                    })
                    break  # one event per drug column (v1)

    # --- recording gaps ----------------------------------------------------
    any_valid = np.zeros(len(ts), dtype=bool)
    for col in sig_cols:
        any_valid |= ~np.isnan(ts[col].to_numpy(dtype=float))
    if len(starts):
        # gap = consecutive windows with nothing valid, >= gap_s long
        run_start = None
        for i, ok in enumerate(any_valid):
            if not ok and run_start is None:
                run_start = starts[i]
            if ok and run_start is not None:
                if starts[i] - run_start >= gap_s:
                    events.append({"event": "recording_gap",
                                   "t_start": float(run_start),
                                   "t_end": float(starts[i]),
                                   "detail": ""})
                run_start = None
        if run_start is not None and starts[-1] - run_start >= gap_s:
            events.append({"event": "recording_gap", "t_start": float(run_start),
                           "t_end": float(starts[-1] + window_s), "detail": ""})

    # --- clinical annotations ----------------------------------------------
    if annotations is not None and not annotations.empty:
        for _, r in annotations.iterrows():
            try:
                t = float(r["time_s"])
            except (KeyError, ValueError, TypeError):
                continue
            events.append({"event": str(r.get("event", "annotation")),
                           "t_start": t, "t_end": t + window_s,
                           "detail": str(r.get("detail", ""))})

    ev = pd.DataFrame(events)
    if ev.empty:
        return ev
    ev = ev.sort_values("t_start").reset_index(drop=True)

    # --- event responses ----------------------------------------------------
    resp_map = {}
    for col in sig_cols:
        base = col.split("__")[0].upper()
        for want in RESPONSE_SIGNALS:
            if base == want and want not in resp_map:
                resp_map[want] = col
    for want, col in resp_map.items():
        v = ts[col].to_numpy(dtype=float)
        base_l, peak_l, delta_l = [], [], []
        for _, r in ev.iterrows():
            t0 = r["t_start"]
            pre = (starts >= t0 - 300) & (starts < t0)
            post = (starts > t0) & (starts <= t0 + 300)
            b = np.nanmedian(v[pre]) if pre.any() else np.nan
            pv = v[post][~np.isnan(v[post])] if post.any() else np.array([])
            if len(pv):
                p = (np.min(pv) if want in ("MAP", "ART_MEAN", "BIS")
                     else np.max(pv))
            else:
                p = np.nan
            base_l.append(b)
            peak_l.append(p)
            delta_l.append(p - b if not (np.isnan(p) or np.isnan(b))
                           else np.nan)
        ev[f"{want}_baseline"] = base_l
        ev[f"{want}_peak"] = peak_l
        ev[f"{want}_delta"] = delta_l
    return ev


def build_features(ts: pd.DataFrame, events: pd.DataFrame, devices,
                   project: str, patient: str, window_s: float):
    """One-row patient summary (K's spec C)."""
    feat = {"patient_id": patient, "project": project,
            "window_s": window_s,
            "n_devices": len(devices),
            "devices": ",".join(d["meta"]["device"] for d in devices)}
    if ts.empty:
        feat["recording_duration_s"] = 0.0
        return pd.DataFrame([feat])
    starts = ts.index.to_numpy(dtype=float)
    feat["recording_duration_s"] = float(starts.max() - starts.min() +
                                        window_s)
    feat["n_windows"] = int(len(ts))
    feat["time_methods"] = ",".join(sorted(
        {str(d["meta"].get("time_method")) for d in devices}))
    sig_cols = [c for c in ts.columns if c != "window_end"]

    def _col(want):
        for c in sig_cols:
            if c.split("__")[0].upper() == want:
                return c
        return None

    for want in ("HR", "MAP", "ART_MEAN", "SPO2", "NOL", "BIS", "RR",
                 "ETCO2"):
        c = _col(want)
        if c is None:
            continue
        v = ts[c].to_numpy(dtype=float)
        ok = ~np.isnan(v)
        feat[f"mean_{want}"] = float(np.nanmean(v)) if ok.any() else np.nan
        feat[f"sd_{want}"] = float(np.nanstd(v)) if ok.sum() > 1 else np.nan
        feat[f"pct_valid_{want}"] = float(100 * ok.mean())

    # Exposure metrics (K's spec).
    nol_c = _col("NOL")
    if nol_c is not None:
        v = ts[nol_c].to_numpy(dtype=float)
        ok = ~np.isnan(v)
        if ok.any():
            feat["NOL_AUC"] = float(np.trapz(np.where(ok, v, 0.0),
                                             dx=window_s))
            feat["pct_NOL_gt_25"] = float(100 * np.nanmean(v[ok] > 25))
    bis_c = _col("BIS")
    if bis_c is not None:
        v = ts[bis_c].to_numpy(dtype=float)
        ok = ~np.isnan(v)
        if ok.any():
            feat["pct_BIS_40_60"] = float(
                100 * np.nanmean((v[ok] >= 40) & (v[ok] <= 60)))
    map_c = _col("MAP") or _col("ART_MEAN")
    if map_c is not None:
        v = ts[map_c].to_numpy(dtype=float)
        ok = ~np.isnan(v)
        if ok.any():
            feat["pct_MAP_lt_65"] = float(100 * np.nanmean(v[ok] < 65))

    # Total drug doses: integrate rate columns (rate x window_s).
    for c in sig_cols:
        if _is_drug_column(c):
            v = ts[c].to_numpy(dtype=float)
            ok = ~np.isnan(v)
            if ok.any():
                feat[f"total_{c}"] = float(
                    np.trapz(np.where(ok, v, 0.0), dx=window_s))

    # Overall data quality.
    allv = np.concatenate([ts[c].to_numpy(dtype=float) for c in sig_cols])
    feat["pct_valid_all"] = float(100 * np.mean(~np.isnan(allv)))
    feat["n_events"] = int(len(events))
    if not events.empty:
        for want in RESPONSE_SIGNALS:
            dcol = f"{want}_delta"
            if dcol in events.columns:
                dd = pd.to_numeric(events[dcol], errors="coerce")
                if dd.notna().any():
                    feat[f"max_abs_delta_{want}"] = float(dd.abs().max())
    return pd.DataFrame([feat])


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _maybe_encrypt(df: pd.DataFrame, path: Path, encrypt: bool):
    if encrypt:
        from tools.crypto import encrypt_bytes  # deferred: needs the key
        with tempfile.NamedTemporaryFile(suffix=".parquet") as tmp:
            df.to_parquet(tmp.name, index=False)
            with open(tmp.name, "rb") as fh:
                data = encrypt_bytes(fh.read())
        path = path.with_suffix(".parquet.enc")
        path.write_bytes(data)
    else:
        df.to_parquet(path, index=False)
    return path


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build the per-patient integration layer "
                    "(timeseries/events/features).")
    ap.add_argument("--project", required=True)
    ap.add_argument("--patient", required=True)
    ap.add_argument("--in", dest="indir", required=True,
                    help="dir with per-device subdirs "
                         "{filtered,qc}.parquet + lineage.json")
    ap.add_argument("--out", dest="outdir", required=True)
    ap.add_argument("--window-s", type=float, default=10.0)
    ap.add_argument("--annotations", default=None,
                    help="CSV with event,time_s[,detail] columns")
    ap.add_argument("--encrypt", action="store_true",
                    help="encrypt outputs with PIPELINE_DATA_KEY")
    args = ap.parse_args(argv)

    indir, outdir = Path(args.indir), Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Dictionary for column labeling (optional; falls back to raw names).
    configs, index = {}, None
    if _HAS_SP:
        for cd, vp in [(Path("configs/signals"),
                        Path("profiles/_variables.yaml"))]:
            if cd.exists() and vp.exists():
                try:
                    configs, index = load_signal_configs(cd, vp)
                    break
                except Exception as exc:
                    print(f"[patient-layer] dictionary load failed: {exc}")

    devices, skipped = [], []
    for ddir in sorted(p for p in indir.iterdir() if p.is_dir()):
        dev, reason = _load_device(ddir, configs, index)
        if dev is None:
            skipped.append(f"{ddir.name}: {reason}")
        else:
            devices.append(dev)
    for s in skipped:
        print(f"[patient-layer] skipped {s}")
    if not devices:
        print("[patient-layer] no usable device inputs", flush=True)
        return 1
    print(f"[patient-layer] {args.patient}: {len(devices)} devices "
          f"({', '.join(d['meta']['device'] for d in devices)})", flush=True)

    ts, column_map = build_timeseries(devices, args.window_s)
    print(f"[patient-layer] timeseries: {ts.shape[0]} windows x "
          f"{ts.shape[1]} cols", flush=True)

    annotations = None
    if args.annotations:
        annotations = pd.read_csv(args.annotations)
    events = detect_events(ts, args.window_s, annotations)
    print(f"[patient-layer] events: {len(events)}", flush=True)

    feats = build_features(ts, events, devices, args.project, args.patient,
                           args.window_s)

    manifest = {
        "tool": "build_patient_layer",
        "ts": datetime.now(timezone.utc).isoformat(),
        "project": args.project,
        "patient": args.patient,
        "window_s": args.window_s,
        "n_windows": int(ts.shape[0]),
        "n_events": int(len(events)),
        "column_map": column_map,
        "inputs": [
            {"device": d["meta"]["device"],
             "time_method": d["meta"].get("time_method"),
             "fs_hz": d["meta"].get("fs_hz"),
             "rows": d["meta"]["rows"],
             "n_signals": len(d["signals"]),
             "source_blob": d["meta"].get("source_blob"),
             "sha256": {
                 "filtered": _sha256_file(Path(d["meta"]["dir"]) /
                                          "filtered.parquet"),
                 "qc": _sha256_file(Path(d["meta"]["dir"]) / "qc.parquet")
                 if (Path(d["meta"]["dir"]) / "qc.parquet").exists()
                 else None,
             }} for d in devices],
        "skipped": skipped,
    }

    # window_start is the time base: keep it as a column, not an index,
    # so the parquet round-trips without pandas index metadata.
    _maybe_encrypt(ts.reset_index(), outdir / "timeseries.parquet",
                   args.encrypt)
    _maybe_encrypt(events, outdir / "events.parquet", args.encrypt)
    _maybe_encrypt(feats, outdir / "features.parquet", args.encrypt)
    (outdir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"[patient-layer] wrote timeseries/events/features + manifest.json "
          f"to {outdir}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
