"""BIS Vista export parser (.r2a raw EEG, .spa 1 Hz parameter trends).

Format documentation: docs/bis_format_research.md (Connor 2022).
  .r2a: channel-interleaved signed int16 little-endian, 2 channels,
        128 Hz, no header, scale x1675.42688/32767 uV per step.
  .spa: pipe-delimited plaintext, 2 header lines, 54 columns, 1 Hz;
        BIS at 11/25/39, BSR 35, SEF 36, MF 37, POW 42, EMG 43,
        suppression time 48 (0-based). Negative values mean missing.

The seven other BIS extensions (.ara/.e_a/.f_a/.h_a/.m_a/.o_a/.t_a) are
undocumented and stay quarantined -- this parser refuses them loudly
rather than guessing.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

R2A_FS_HZ = 128.0
R2A_UV_PER_STEP = 1675.42688 / 32767.0
R2A_DEFAULT_CHANNELS = 2

# 0-based Connor indices, used only when header names don't match.
SPA_FALLBACK = {
    "BIS_1": 11, "BIS_2": 25, "BIS_3": 39,
    "BSR": 35, "SEF": 36, "MF": 37, "POW": 42, "EMG": 43,
    "SUPP_TIME": 48, "timestamp": 0,
}
SPA_NAME_HINTS = {
    "timestamp": ["time", "date", "timestamp"],
    "BSR": ["bsr", "burst suppress"],
    "SEF": ["sef", "spectral edge"],
    "MF": ["median freq", " mf"],
    "POW": ["pow", "power"],
    "EMG": ["emg"],
    "SUPP_TIME": ["supp", "suppress"],
}

QUARANTINED = {".ara", ".e_a", ".f_a", ".h_a", ".m_a", ".o_a", ".t_a"}


def _tz(cfg):
    return (cfg.get("time", {}) or {}).get("timezone", "America/Montreal")


def sniff_header(path, profile, cfg) -> dict:
    from pathlib import Path
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in QUARANTINED:
        return {"readable": True, "error": None, "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0,
                "note": f"{suffix} quarantined: undocumented BIS extension"}
    try:
        if suffix == ".r2a":
            size = p.stat().st_size
            ok = size > 0 and size % 2 == 0
            return {"readable": ok, "error": None if ok else "bad size",
                    "header": [], "recognised": {"EEG1_uV", "EEG2_uV"},
                    "time_column": None, "data_rows": size // 4 if ok else 0,
                    "kind": "r2a_raw_eeg"}
        if suffix == ".spa":
            lines = p.read_text(errors="replace").splitlines()
            ok = len(lines) >= 3 and "|" in lines[1]
            header = [h.strip() for h in lines[1].split("|")] if ok else []
            return {"readable": ok,
                    "error": None if ok else "not a .spa file",
                    "header": header, "recognised": set(header),
                    "time_column": header[0] if header else None,
                    "data_rows": max(len(lines) - 2, 0),
                    "kind": "spa_trends"}
    except OSError as exc:
        return {"readable": False, "error": str(exc), "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}
    return {"readable": True, "error": None, "header": [],
            "recognised": set(), "time_column": None, "data_rows": 0,
            "note": f"unexpected suffix {suffix} for BIS parser"}


def _read_r2a(path, n_channels, fs_hz):
    from pathlib import Path
    raw = Path(path).read_bytes()
    if len(raw) % 2:
        raise ValueError(f"{path}: odd byte count -- not int16")
    n_int16 = len(raw) // 2
    if n_int16 % n_channels:
        raise ValueError(
            f"{path}: {n_int16} int16 samples not divisible by "
            f"{n_channels} channels")
    data = np.frombuffer(raw, dtype="<i2")
    step_med = float(np.median(np.abs(np.diff(data.astype(np.float64)))))
    if step_med > 5000:
        raise ValueError(f"{path}: median sample step {step_med:.0f} -- "
                         f"wrong byte order?")
    eeg = data.reshape(-1, n_channels).astype(np.float64) * R2A_UV_PER_STEP
    df = pd.DataFrame({"t_s": np.arange(eeg.shape[0]) / fs_hz})
    for i in range(n_channels):
        df[f"EEG{i + 1}_uV"] = eeg[:, i]
    return df


def _map_spa_columns(columns):
    mapping = {}
    lowered = [str(c).lower() for c in columns]
    for canon, hints in SPA_NAME_HINTS.items():
        for i, name in enumerate(lowered):
            if any(h in name for h in hints):
                mapping[canon] = columns[i]
                break
    bis_cols = [c for c in columns if "bis" in str(c).lower()][:3]
    for i, c in enumerate(bis_cols, start=1):
        mapping[f"BIS_{i}"] = c
    for canon, idx in SPA_FALLBACK.items():
        if canon not in mapping and idx < len(columns):
            mapping[canon] = columns[idx]
    return mapping


def _read_spa(path):
    from pathlib import Path
    import io
    lines = Path(path).read_text(errors="replace").splitlines()
    if len(lines) < 3:
        raise ValueError(f"{path}: fewer than 3 lines -- not a .spa file")
    header = [h.strip() for h in lines[1].split("|")]
    df = pd.read_csv(io.StringIO("\n".join(lines[2:])), sep="|",
                     header=None, names=[f"c{i}" for i in range(len(header))])
    df.columns = [header[i] if i < len(header) else f"c{i}"
                  for i in range(len(df.columns))]
    mapping = _map_spa_columns(list(df.columns))
    out = pd.DataFrame()
    for canon, raw_col in mapping.items():
        out[canon] = (df[raw_col] if canon == "timestamp"
                      else pd.to_numeric(df[raw_col], errors="coerce"))
    ts = out.get("timestamp")
    if ts is not None:
        parsed = pd.to_datetime(ts, errors="coerce", utc=True)
        if parsed.isna().all():
            parsed = pd.to_datetime(pd.to_numeric(ts, errors="coerce"),
                                    unit="s", errors="coerce", utc=True)
        out["timestamp"] = parsed
    for col in out.columns:
        if col != "timestamp":
            out.loc[out[col] < 0, col] = np.nan
    return out


def _spa_mate(path):
    from pathlib import Path
    mate = Path(path).with_suffix(".spa")
    return mate if mate.exists() else None


def parse(path, profile, cfg):
    from pathlib import Path
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in QUARANTINED:
        raise ValueError(f"{path}: {suffix} is quarantined (undocumented)")
    tz = _tz(cfg)
    source_columns = {}
    meta = {"device": "bis"}

    if suffix == ".spa":
        df = _read_spa(p)
        source_columns = {c: c for c in df.columns if c != "timestamp"}
        ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert(tz)
        frame = df.drop(columns=["timestamp"]).copy()
        frame.index = ts
        frame.index.name = "timestamp"
        meta.update({"rows": len(frame),
                     "duration_s": ((ts.iloc[-1] - ts.iloc[0]).total_seconds()
                                    if len(ts) > 1 else 0.0),
                     "interval_s": 1.0,
                     "source_columns": source_columns,
                     "kind": "spa_trends"})
        return frame.sort_index(), meta

    if suffix == ".r2a":
        n_channels = (cfg.get("bis", {}) or {}).get("channels",
                                                    R2A_DEFAULT_CHANNELS)
        df = _read_r2a(p, n_channels, R2A_FS_HZ)
        eeg_cols = [c for c in df.columns if c != "t_s"]
        source_columns = {c: c for c in eeg_cols}
        mate = _spa_mate(p)
        if mate is not None:
            try:
                spa = _read_spa(mate)
                t0 = pd.to_datetime(spa["timestamp"].iloc[0], utc=True)
                index = t0 + pd.to_timedelta(df["t_s"], unit="s")
                time_note = f"absolute, t0 from {mate.name}"
            except Exception as exc:  # noqa: BLE001
                index = pd.to_timedelta(df["t_s"], unit="s")
                time_note = f"relative (mate unreadable: {exc})"
        else:
            index = pd.to_timedelta(df["t_s"], unit="s")
            time_note = "relative (no .spa mate found)"
        frame = df[eeg_cols].copy()
        frame.index = index
        frame.index.name = "timestamp"
        meta.update({"rows": len(frame),
                     "duration_s": float(df["t_s"].iloc[-1]) if len(df) else 0.0,
                     "interval_s": 1.0 / R2A_FS_HZ,
                     "source_columns": source_columns,
                     "kind": "r2a_raw_eeg",
                     "time_base": time_note,
                     "fs_hz": R2A_FS_HZ})
        return frame, meta

    raise ValueError(f"{path}: suffix {suffix!r} not handled by BIS parser")
