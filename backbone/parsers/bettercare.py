"""
BetterCare parser.

Ingestion preserves the original source data. Recognised variables are
standardised using the project variable ontology, while unknown columns remain
under their original names.

BetterCare files may contain high-frequency waveform data (typically 200 Hz)
and slower derived values. Ingestion therefore preserves the source temporal
resolution. Downsampling belongs to preprocessing.

Files ending in -1, -2, -3, ... are consecutive slices of one recording.
Their elapsed-millisecond clocks are already continuous, so no artificial
offset is added between files.
"""

from __future__ import annotations

import pandas as pd

from ..config import normalise_key
from ._common import (
    apply_missing_rules,
    describe,
    file_sequence_number,
    resolve_columns,
    sniff_delimiter,
    start_time_from_filename,
)

_ENCODINGS = ["utf-8", "utf-8-sig", "cp1252", "latin-1"]
_DELIMITERS = [";", ",", "\t", "|"]


def sniff_header(path, profile, cfg) -> dict:
    """Inspect a BetterCare file without loading the full dataset."""
    try:
        delimiter = sniff_delimiter(path, default=";")

        with open(
            path,
            "r",
            encoding=cfg.get("encoding", "utf-8"),
            errors="replace",
        ) as fh:
            first = fh.readline()
            second = fh.readline()

    except OSError as exc:
        return {
            "readable": False,
            "error": str(exc),
            "header": [],
            "recognised": set(),
            "time_column": None,
            "data_rows": 0,
        }

    if not first.strip():
        return {
            "readable": True,
            "error": None,
            "delimiter": delimiter,
            "header": [],
            "recognised": set(),
            "time_column": None,
            "data_rows": 0,
        }

    header = [
        h.strip().strip('"')
        for h in first.split(delimiter)
    ]

    mapping = resolve_columns(header, profile)

    time_col = cfg.get("time", {}).get(
        "column",
        "Time (msecs)",
    )

    time_found = any(
        normalise_key(h) == normalise_key(time_col)
        for h in header
    )

    return {
        "readable": True,
        "error": None,
        "delimiter": delimiter,
        "header": header,
        "recognised": set(mapping.values()),
        "time_column": time_col if time_found else None,
        "data_rows": 1 if second.strip() else 0,
    }


def _read_header(path):
    """Read column names while tolerating delimiters and encodings."""
    sniffed = sniff_delimiter(path)

    separators = [sniffed] + [
        sep for sep in _DELIMITERS
        if sep != sniffed
    ]

    for sep in separators:
        for enc in _ENCODINGS:
            try:
                return pd.read_csv(
                    path,
                    sep=sep,
                    nrows=0,
                    encoding=enc,
                    engine="python",
                ).columns
            except Exception:
                continue

    raise ValueError(f"cannot read header of {path}")


def _read_chunk(path, time_col):
    """Read one BetterCare slice while preserving all source columns."""
    try:
        return _read_csv(
            path,
            sniff_delimiter(path),
            time_col,
        )
    except (UnicodeDecodeError, pd.errors.ParserError):
        pass

    sniffed = sniff_delimiter(path)

    for sep in _DELIMITERS:
        if sep == sniffed:
            continue

        try:
            return _read_csv(
                path,
                sep,
                time_col,
            )
        except (UnicodeDecodeError, pd.errors.ParserError):
            continue

    return None


def _read_csv(path, sep, time_col):
    """
    Read the complete source table.

    No usecols restriction is applied because ingestion must retain all
    original BetterCare columns.
    """
    last = None

    for enc in _ENCODINGS:
        try:
            return pd.read_csv(
                path,
                sep=sep,
                encoding=enc,
                low_memory=False,
                decimal=",",
            )
        except Exception as exc:
            last = exc

    if last is not None:
        raise last

    raise ValueError(f"cannot read BetterCare file: {path}")


def _standardise_columns(frame, profile):
    """
    Standardise recognised columns while preserving unknown columns.

    If multiple source columns map to the same standard variable, only the
    first gets the standard name. The other source columns retain their
    original names so no data is silently overwritten.
    """
    mapping = resolve_columns(
        list(frame.columns),
        profile,
    )

    rename_map = {}
    used_standard_names = set()

    for source, standard in mapping.items():
        if source not in frame.columns:
            continue

        if standard in used_standard_names:
            continue

        rename_map[source] = standard
        used_standard_names.add(standard)

    return frame.rename(columns=rename_map), mapping


def parse(path, profile, cfg):
    """Parse one BetterCare file."""
    return parse_group(
        [path],
        profile,
        cfg,
    )


def parse_group(paths, profile, cfg):
    """
    Parse a complete BetterCare recording.

    The -1, -2, -3 sequence is treated as one continuous recording.

    Ingestion:
      - preserves all source columns
      - preserves source temporal resolution
      - does not downsample
      - does not add artificial time offsets
      - standardises recognised variables
      - preserves unknown variables
    """
    paths = sorted(
        (str(p) for p in paths),
        key=file_sequence_number,
    )

    if not paths:
        raise ValueError(
            "BetterCare parse_group received no files"
        )

    time_cfg = cfg.get("time", {})

    time_col = time_cfg.get(
        "column",
        "Time (msecs)",
    )

    tz = time_cfg.get(
        "timezone",
        "America/Montreal",
    )

    # Inspect first file.
    header = _read_header(paths[0])
    header = [str(col) for col in header]

    actual_time_col = next(
        (
            col
            for col in header
            if normalise_key(col) == normalise_key(time_col)
        ),
        None,
    )

    if actual_time_col is None:
        raise ValueError(
            f"time column {time_col!r} absent from "
            f"BetterCare file {paths[0]!r}"
        )

    # Recover wall-clock start from filename when configured.
    start = None

    if time_cfg.get("start_from_filename"):
        start = start_time_from_filename(
            paths[0],
            time_cfg.get(
                "start_regex",
                r"\.(1\d{15})\.",
            ),
            tz,
        )

    # Read every slice at source resolution.
    chunks = []

    for file_path in paths:
        piece = _read_chunk(
            file_path,
            actual_time_col,
        )

        if piece is None:
            continue

        piece.columns = [
            str(col)
            for col in piece.columns
        ]

        slice_time_col = next(
            (
                col
                for col in piece.columns
                if normalise_key(col) == normalise_key(time_col)
            ),
            None,
        )

        if slice_time_col is None:
            raise ValueError(
                f"time column {time_col!r} absent from "
                f"BetterCare slice {file_path!r}"
            )

        if slice_time_col != time_col:
            piece = piece.rename(
                columns={
                    slice_time_col: time_col,
                }
            )

        piece[time_col] = pd.to_numeric(
            piece[time_col],
            errors="coerce",
        )

        piece = piece.dropna(
            subset=[time_col],
        )

        if piece.empty:
            continue

        chunks.append(piece)

    if not chunks:
        return pd.DataFrame(), {
            "rows": 0,
            "source_columns": {},
            "files": paths,
            "wall_clock_recovered": start is not None,
            "start_source": (
                "filename"
                if start is not None
                else "none, elapsed only"
            ),
            "note": "no readable data rows",
        }

    # Concatenate slices without adding artificial offsets.
    frame = pd.concat(
        chunks,
        ignore_index=True,
        sort=False,
    )

    # Sort by the original elapsed clock.
    frame = frame.sort_values(
        by=time_col,
        kind="stable",
    )

    # Remove duplicate timestamps.
    frame = frame.drop_duplicates(
        subset=[time_col],
        keep="first",
    )

    elapsed_ms = frame[time_col].copy()

    # Build timestamp index at original source resolution.
    if start is not None:
        index = (
            start
            + pd.to_timedelta(
                elapsed_ms,
                unit="ms",
            )
        )
    else:
        index = pd.to_datetime(
            elapsed_ms,
            unit="ms",
            utc=True,
        )

    frame = frame.drop(
        columns=[time_col],
    )

    # Standardise known variables while retaining unknown columns.
    frame, mapping = _standardise_columns(
        frame,
        profile,
    )

    frame.index = index
    frame.index.name = "timestamp"

    frame = frame.sort_index()

    frame = frame[
        ~frame.index.duplicated(
            keep="first",
        )
    ]

    # Apply only explicit missing-value rules.
    frame = apply_missing_rules(
        frame,
        profile,
    )

    frame = frame.dropna(
        how="all",
    )

    recognised_mapping = {
        source: standard
        for source, standard in mapping.items()
        if source in header
    }

    known_standard_columns = set(
        recognised_mapping.values()
    )

    unknown_columns = [
        str(col)
        for col in frame.columns
        if str(col) not in known_standard_columns
    ]

    meta = describe(
        frame,
        recognised_mapping,
    )

    meta["files"] = paths
    meta["file_count"] = len(paths)

    meta["wall_clock_recovered"] = (
        start is not None
    )

    meta["start_source"] = (
        "filename"
        if start is not None
        else "none, elapsed only"
    )

    meta["source_resolution_preserved"] = True
    meta["downsampled"] = False
    meta["source_time_column"] = time_col

    meta["recognised_columns"] = sorted(
        known_standard_columns
    )

    meta["unknown_columns"] = unknown_columns

    meta["source_column_mapping"] = recognised_mapping

    if len(elapsed_ms) > 1:
        diffs = elapsed_ms.diff().dropna()

        if not diffs.empty:
            meta["median_interval_ms"] = float(
                diffs.median()
            )

            meta["min_interval_ms"] = float(
                diffs.min()
            )

            meta["max_interval_ms"] = float(
                diffs.max()
            )

    median_interval_ms = meta.get(
        "median_interval_ms"
    )

    if (
        median_interval_ms is not None
        and median_interval_ms > 0
    ):
        meta["estimated_hz"] = round(
            1000.0 / median_interval_ms,
            6,
        )

    return frame, meta