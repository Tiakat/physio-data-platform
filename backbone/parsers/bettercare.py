"""
BetterCare parser.

Three properties of this format, all measured in the archive, drive the code:

1. Rows arrive every 5 ms, that is 200 Hz, but derived values are refreshed only
   once per second. 199 rows in every 200 are empty for those columns, so the
   file is downsampled to 1 Hz by keeping the row that carries values.

2. There is no clock inside the file. Time is elapsed milliseconds starting at
   zero. The wall clock start is recovered from the filename and was verified
   against Infinity in all 23 dual recorded patients, median residual lag
   4 seconds.

3. The -1, -2, -3 files are consecutive slices of ONE recording and already
   share a single continuous millisecond clock. They are concatenated as they
   stand. Adding an offset between them, which an earlier version of the IPAM
   pipeline did, inflated every duration several fold.
"""

from __future__ import annotations

import pandas as pd

from ..config import normalise_key
from ._common import (apply_missing_rules, describe, file_sequence_number,
                      resolve_columns, sniff_delimiter, start_time_from_filename)

# Encodings seen across the archive, tried in order. latin-1 never fails.
_ENCODINGS = ["utf-8", "utf-8-sig", "cp1252", "latin-1"]
_DELIMITERS = [";", ",", "\t", "|"]


def sniff_header(path, profile, cfg) -> dict:
    """
    Cheap structural check for validation: is this a BetterCare export, which
    delimiter does THIS file use, is the time column there, are there rows.

    The delimiter must be detected per file. The archive contains semicolon and
    comma delimited exports of the same layout, and the profile's `delimiter`
    belongs to one patient, not all of them.
    """
    try:
        with open(path, "r", encoding=cfg.get("encoding", "utf-8"),
                  errors="replace") as fh:
            first = fh.readline()
            second = fh.readline()
    except OSError as exc:
        return {"readable": False, "error": str(exc), "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}

    if not first.strip():
        return {"readable": True, "error": None, "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}

    delimiter = sniff_delimiter(path, default=";")
    header = [h.strip().strip('"') for h in first.split(delimiter)]
    mapping = resolve_columns(header, profile)
    time_col = cfg.get("time", {}).get("column", "Time (msecs)")
    time_found = any(normalise_key(h) == normalise_key(time_col) for h in header)
    return {
        "readable": True, "error": None, "delimiter": delimiter,
        "header": header, "recognised": set(mapping.values()),
        "time_column": time_col if time_found else None,
        "data_rows": 1 if second.strip() else 0,
    }


def _read_header(path):
    """Column names only, tolerating the two delimiters and BOM encodings."""
    for sep in {sniff_delimiter(path), *_DELIMITERS}:
        for enc in _ENCODINGS:
            try:
                return pd.read_csv(path, sep=sep, nrows=0, encoding=enc,
                                   engine="python").columns
            except Exception:                                    # noqa: BLE001
                continue
    raise ValueError(f"cannot read header of {path}")


def _read_chunk(path, time_col, keep_source):
    """Read one slice. Returns None if no delimiter/encoding combination works."""
    try:
        return _read_csv(path, sniff_delimiter(path), time_col, keep_source)
    except (UnicodeDecodeError, pd.errors.ParserError):
        pass
    sniffed = sniff_delimiter(path)
    for sep in _DELIMITERS:
        if sep == sniffed:
            continue
        try:
            return _read_csv(path, sep, time_col, keep_source)
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue
    return None


def _read_csv(path, sep, time_col, keep_source):
    last = None
    for enc in _ENCODINGS:
        try:
            return pd.read_csv(path, sep=sep, encoding=enc, low_memory=False,
                               usecols=lambda c: c == time_col or c in keep_source)
        except Exception as exc:                                 # noqa: BLE001
            last = exc
    raise last


def parse(path, profile, cfg):
    """Parse a single BetterCare file. Use parse_group for a whole recording."""
    return parse_group([path], profile, cfg)


def parse_group(paths, profile, cfg):
    """
    Parse the -1, -2, -3 sequence as one recording.

    paths may be a single file or the whole chain. Order is taken from the
    trailing -N in the name, not from the filesystem. The delimiter is detected
    per file, never taken from the profile: the archive mixes semicolon and
    comma delimited exports of the SAME layout.
    """
    paths = sorted((str(p) for p in paths), key=file_sequence_number)
    time_cfg = cfg.get("time", {})
    time_col = time_cfg.get("column", "Time (msecs)")
    tz = time_cfg.get("timezone", "America/Montreal")

    header = _read_header(paths[0])
    mapping = resolve_columns(header, profile)
    wanted = set(cfg.get("keep_variables") or mapping.values())
    keep_source = {src for src, std in mapping.items() if std in wanted}
    if not keep_source:
        return pd.DataFrame(), {"rows": 0, "source_columns": mapping,
                                "note": "waveform only export, no derived signal columns"}
    if time_col not in header:
        raise ValueError(f"time column {time_col!r} absent")

    start = None
    if time_cfg.get("start_from_filename"):
        start = start_time_from_filename(
            paths[0], time_cfg.get("start_regex", r"\.(1\d{15})\."), tz)

    chunks = []
    for file_path in paths:
        piece = _read_chunk(file_path, time_col, keep_source)
        if piece is None:
            continue
        value_cols = [c for c in piece.columns if c != time_col]
        # Keep only rows that actually carry a derived value. This is the step
        # that turns 720,000 rows per hour into 3,600.
        piece = piece.dropna(subset=value_cols, how="all") if value_cols else piece
        piece = piece.dropna(subset=[time_col])
        if piece.empty:
            continue
        chunks.append(piece)

    if not chunks:
        return pd.DataFrame(), {"rows": 0, "source_columns": {},
                                "note": "no rows carried a derived value"}

    frame = pd.concat(chunks, ignore_index=True)

    # Downsample to the requested rate by taking the first complete row per bin.
    target_hz = cfg.get("downsample_to_hz", 1)
    bin_ms = int(1000 / target_hz)
    frame["_bin"] = (pd.to_numeric(frame[time_col], errors="coerce") // bin_ms).astype("Int64")
    frame = frame.dropna(subset=["_bin"])
    frame = frame.groupby("_bin", as_index=False).first()

    elapsed_ms = frame["_bin"].astype("int64") * bin_ms
    frame = frame.drop(columns=[time_col, "_bin"]).rename(columns=mapping)

    if start is not None:
        index = start + pd.to_timedelta(elapsed_ms, unit="ms")
    else:
        # No wall clock available. Fall back to epoch zero and record the fact,
        # so downstream code can refuse to align this recording with another.
        index = pd.to_datetime(elapsed_ms, unit="ms", utc=True)

    frame.index = index
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="first")]
    frame = apply_missing_rules(frame, profile)
    frame = frame.dropna(how="all")

    meta = describe(frame, {k: v for k, v in mapping.items() if k in keep_source})
    meta["files"] = [str(p).split("/")[-1] for p in paths]
    meta["wall_clock_recovered"] = start is not None
    meta["start_source"] = "filename" if start is not None else "none, elapsed only"
    return frame, meta
