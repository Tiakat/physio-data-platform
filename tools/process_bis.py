"""Parse BIS Vista exports (.r2a raw EEG + .spa trend parameters).

Format reference (Connor 2022, "Emulation of the BIS Engine", PMC8449791,
Table 3; corroborated by two clinical papers and the MIT-licensed
ettiennecoetzee/bis-vista-eeg-scope):

.r2a : channel-interleaved signed 16-bit little-endian, 2 channels, 128 Hz,
       NO header. uV = raw * 1675.42688 / 32767  (512 bytes/sec).
.spa : pipe-delimited plaintext, 2 header lines, one row/sec, 54 columns.
       0-based Connor indices: timestamp 0, BIS triplet 11/25/39, SEF 36,
       MF 37, BSR 35, POW 42, EMG 43, suppression time 48.
       Negative values = missing -> NaN.
       Column mapping is header-name-driven; Connor indices are the fallback
       (layouts drift between firmware versions).

The seven other bis_lfile extensions (.ara/.e_a/.f_a/.h_a/.m_a/.o_a/.t_a)
have zero public documentation and are NOT parsed here -- they stay
quarantined in the review queue until reverse-engineered from real files.

Raw is never modified. Outputs per recording pair:
  <stem>.eeg.parquet        t_s, EEG1_uV, EEG2_uV  (128 Hz)
  <stem>.bis_params.parquet timestamp + trend parameters (1 Hz)
  <stem>.bis_qc.json        pairing/QC notes (duration reconciliation, etc.)

Usage:
  python tools/process_bis.py --input <file-or-dir> --output-dir <dir>
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

R2A_FS_HZ = 128.0
R2A_UV_PER_STEP = 1675.42688 / 32767.0
R2A_DEFAULT_CHANNELS = 2

# Connor (2022) 0-based column indices in .spa files (fallback when the
# header names don't match a known layout).
SPA_FALLBACK_INDEX = {
    "timestamp": 0,
    "BIS_1": 11,
    "BIS_2": 25,
    "BIS_3": 39,
    "BSR": 35,
    "SEF": 36,
    "MF": 37,
    "POW": 42,
    "EMG": 43,
    "SUPP_TIME": 48,
}

# Header-name fragments we accept for each canonical parameter (lowercased).
SPA_NAME_HINTS = {
    "timestamp": ["time", "date", "timestamp"],
    "BSR": ["bsr", "burst suppress"],
    "SEF": ["sef", "spectral edge"],
    "MF": ["median freq", " mf"],
    "POW": ["pow", "power"],
    "EMG": ["emg"],
    "SUPP_TIME": ["supp", "suppress"],
}


def read_r2a(path, n_channels=None, fs_hz=R2A_FS_HZ,
             byteorder="<"):
    """Read a .r2a raw-EEG file -> DataFrame(t_s, EEG1_uV, ...).

    n_channels defaults to 2 (documented layout); it is validated against
    the file size, never silently truncated. Raises on size mismatch.
    """
    raw = Path(path).read_bytes()
    if len(raw) % 2:
        raise ValueError(f"{path}: odd byte count {len(raw)} -- not int16")
    n_int16 = len(raw) // 2
    nch = n_channels or R2A_DEFAULT_CHANNELS
    if n_int16 % nch:
        raise ValueError(
            f"{path}: {n_int16} int16 samples not divisible by "
            f"{nch} channels -- pass --channels explicitly")
    data = np.frombuffer(raw, dtype=np.dtype(byteorder + "i2"))
    eeg = data.reshape(-1, nch).astype(np.float64) * R2A_UV_PER_STEP

    # Endianness sanity: a smooth 128 Hz EEG signal has small
    # sample-to-sample steps. Byte-swapped data looks like violent
    # full-scale noise -- median successive difference explodes.
    step_med = float(np.median(np.abs(np.diff(data.astype(np.float64)))))
    if step_med > 5000:  # raw int16 steps (~255 uV between adjacent samples)
        raise ValueError(
            f"{path}: median sample-to-sample step is {step_med:.0f} raw "
            f"units -- wrong byte order? (tried {byteorder}i2)")

    n = eeg.shape[0]
    df = pd.DataFrame({"t_s": np.arange(n) / fs_hz})
    for i in range(nch):
        df[f"EEG{i + 1}_uV"] = eeg[:, i]
    df.attrs["fs_hz"] = fs_hz
    df.attrs["n_channels"] = nch
    return df


def _map_spa_columns(columns):
    """Map raw .spa header names -> canonical parameters.

    Header-name-driven; falls back to Connor indices for names we don't
    recognise. Returns {canonical: raw_name_or_index}.
    """
    mapping = {}
    lowered = [str(c).lower() for c in columns]
    for canon, hints in SPA_NAME_HINTS.items():
        if canon.startswith("BIS_"):
            continue  # the BIS triplet is collected separately below
        for i, name in enumerate(lowered):
            if any(h in name for h in hints):
                mapping[canon] = columns[i]
                break
    # BIS triplet: collect up to 3 distinct BIS-like columns.
    bis_cols = [c for c in columns
                if "bis" in str(c).lower()][:3]
    for i, c in enumerate(bis_cols, start=1):
        mapping[f"BIS_{i}"] = c
    # Fallback to Connor indices for anything still unmapped.
    for canon, idx in SPA_FALLBACK_INDEX.items():
        if canon not in mapping and idx < len(columns):
            mapping[canon] = columns[idx]
    return mapping


def read_spa(path):
    """Read a .spa trend file -> DataFrame(timestamp, BIS_1, ..., EMG).

    Negative parameter values mean missing -> NaN. Timestamps parsed
    flexibly (ISO strings or epoch seconds).
    """
    lines = Path(path).read_text(errors="replace").splitlines()
    if len(lines) < 3:
        raise ValueError(f"{path}: fewer than 3 lines -- not a .spa file")
    header = [h.strip() for h in lines[1].split("|")]
    body = "\n".join(lines[2:])
    import io
    df = pd.read_csv(io.StringIO(body), sep="|", header=None,
                     names=[f"c{i}" for i in range(len(header))])
    # Re-attach the real header names positionally.
    df.columns = [header[i] if i < len(header) else f"c{i}"
                  for i in range(len(df.columns))]

    mapping = _map_spa_columns(list(df.columns))
    out = pd.DataFrame()
    for canon, raw_col in mapping.items():
        if canon == "timestamp":
            out[canon] = df[raw_col]  # parsed below, not numeric
        else:
            out[canon] = pd.to_numeric(df[raw_col], errors="coerce")

    # timestamp: try datetime, then epoch seconds.
    ts = out.get("timestamp")
    if ts is not None:
        parsed = pd.to_datetime(ts, errors="coerce", utc=True)
        if parsed.isna().all():
            parsed = pd.to_datetime(pd.to_numeric(ts, errors="coerce"),
                                    unit="s", errors="coerce", utc=True)
        out["timestamp"] = parsed

    # Negative values = missing (device convention).
    for col in out.columns:
        if col == "timestamp":
            continue
        out.loc[out[col] < 0, col] = np.nan

    # Canonical column order: timestamp first, then the known parameters.
    ordered = ["timestamp"] + [c for c in
                               ["BIS_1", "BIS_2", "BIS_3", "BSR", "SEF",
                                "MF", "POW", "EMG", "SUPP_TIME"]
                               if c in out.columns]
    rest = [c for c in out.columns if c not in ordered]
    return out[ordered + rest]


def pair_bis_files(directory):
    """Group .r2a/.spa files by filename stem -> {stem: {r2a, spa}}."""
    pairs = {}
    for p in sorted(Path(directory).iterdir()):
        if p.suffix.lower() in (".r2a", ".spa"):
            stem = p.name[: -len(p.suffix)]
            pairs.setdefault(stem, {})[p.suffix.lower().lstrip(".")] = str(p)
    return pairs


def process_pair(stem, r2a_path, spa_path, out_dir, n_channels=None):
    """Parse one recording pair; write eeg + params parquets and a QC note."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    qc = {"stem": stem, "r2a": r2a_path, "spa": spa_path, "notes": []}

    eeg = None
    if r2a_path:
        eeg = read_r2a(r2a_path, n_channels=n_channels)
        eeg.to_parquet(out_dir / f"{stem}.eeg.parquet", index=False)
        qc["eeg_seconds"] = float(eeg["t_s"].iloc[-1]) if len(eeg) else 0.0
        qc["eeg_channels"] = int(eeg.attrs.get("n_channels", 0))

    params = None
    if spa_path:
        params = read_spa(spa_path)
        params.to_parquet(out_dir / f"{stem}.bis_params.parquet",
                          index=False)
        qc["spa_rows"] = int(len(params))

    # Duration reconciliation: .r2a seconds vs .spa rows (1 Hz).
    if eeg is not None and params is not None:
        diff = abs(qc["eeg_seconds"] - qc["spa_rows"])
        if diff > 5:
            qc["notes"].append(
                f"duration mismatch: eeg {qc['eeg_seconds']:.0f}s vs "
                f"spa {qc['spa_rows']} rows (>{5}s)")
        # Flatline check: constant EEG is device artifact (impedance check),
        # not cortical suppression -- cross-check BSR before interpreting.
        for ch in [c for c in eeg.columns if c.startswith("EEG")]:
            if eeg[ch].std() == 0 and len(eeg) > 128:
                qc["notes"].append(
                    f"{ch} flat over the whole recording: likely impedance "
                    f"check / sensor off -- verify against BSR, do not read "
                    f"as suppression")

    with open(out_dir / f"{stem}.bis_qc.json", "w") as fh:
        json.dump(qc, fh, indent=2, default=str)
    return qc


def main(argv=None):
    ap = argparse.ArgumentParser(description="Parse BIS Vista exports.")
    ap.add_argument("--input", required=True,
                    help=".r2a/.spa file or a directory of them")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--channels", type=int, default=None,
                    help="EEG channel count override (default: 2)")
    args = ap.parse_args(argv)

    src = Path(args.input)
    pairs = {}
    if src.is_dir():
        pairs = pair_bis_files(src)
    elif src.suffix.lower() in (".r2a", ".spa"):
        stem = src.name[: -len(src.suffix)]
        key = src.suffix.lower().lstrip(".")
        pairs = {stem: {key: str(src)}}
        # look for the sibling with the other extension
        other = src.with_suffix(".spa" if key == "r2a" else ".r2a")
        if other.exists():
            pairs[stem]["spa" if key == "r2a" else "r2a"] = str(other)
    else:
        print(f"skipping {src}: not a .r2a/.spa file or directory",
              file=sys.stderr)
        return 1

    if not pairs:
        print("no BIS files found", file=sys.stderr)
        return 1

    n_qc_notes = 0
    for stem, files in sorted(pairs.items()):
        qc = process_pair(stem, files.get("r2a"), files.get("spa"),
                          args.output_dir, n_channels=args.channels)
        n_qc_notes += len(qc["notes"])
        for note in qc["notes"]:
            print(f"[{stem}] QC: {note}")
    print(f"processed {len(pairs)} recording(s); {n_qc_notes} QC note(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
