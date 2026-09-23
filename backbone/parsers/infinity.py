"""
Draeger Infinity parser.

Preserves every source column. Recognised variables are standardised according
to the project variable ontology; unrecognised/device-specific columns remain
under their original names.

No downsampling, interpolation, or artificial transformation is performed.
"""

from __future__ import annotations

import pandas as pd

from ..config import normalise_key
from ._common import (
    apply_missing_rules,
    describe,
    resolve_columns,
    sniff_delimiter,
    to_utc,
)


def sniff_header(path, profile, cfg) -> dict:
    """Cheap structural check of a Draeger Infinity export."""
    try:
        with open(
            path,
            "r",
            encoding="utf-8-sig",
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
            "header": [],
            "recognised": set(),
            "time_column": None,
            "data_rows": 0,
        }

    delimiter = cfg.get("delimiter") or sniff_delimiter(
        path,
        default=";",
    )

    header = [
        h.strip().strip('"')
        for h in first.split(delimiter)
    ]

    mapping = resolve_columns(header, profile)

    time_col = cfg.get(
        "time",
        {},
    ).get(
        "column",
        "OBSERVATION_DATETIME",
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


def parse(path, profile, cfg):
    """
    Parse one Infinity export while preserving all source columns.

    Recognised columns are renamed to standard variables. Unknown columns are
    retained unchanged so they remain available for future research analyses.
    """

    delimiter = cfg.get("delimiter") or sniff_delimiter(
        path,
        default=";",
    )

    encoding = cfg.get(
        "encoding",
        "utf-8",
    )

    time_cfg = cfg.get(
        "time",
        {},
    )

    time_col = time_cfg.get(
        "column",
        "OBSERVATION_DATETIME",
    )

    # Infinity exports may use different delimiters.
    with open(path, "r", encoding=encoding, errors="replace") as f:
        header = f.readline()

    candidates = [";", ",", "\t", "|"]
    actual_delimiter = max(candidates, key=header.count)

    frame = pd.read_csv(
        path,
        sep=actual_delimiter,
        encoding=encoding,
        low_memory=False,
    )

    frame.columns = [
        str(col).strip().strip('"')
        for col in frame.columns
    ]

    # Find timestamp column robustly.
    actual_time_col = next(
        (
            col
            for col in frame.columns
            if normalise_key(col) == normalise_key(time_col)
        ),
        None,
    )

    if actual_time_col is None:
        raise ValueError(
            f"time column {time_col!r} absent; "
            f"header has {list(frame.columns)[:10]}"
        )

    # Resolve recognised variables.
    mapping = resolve_columns(
        frame.columns,
        profile,
    )

    # Convert source wall-clock timestamps to UTC.
    raw_time = frame[actual_time_col].astype("string")
    raw_time = raw_time.str.split(".", n=1).str[0]

    ts = to_utc(
        raw_time,
        time_cfg.get(
            "format",
            "%Y%m%d%H%M%S",
        ),
        time_cfg.get(
            "timezone",
            "America/Montreal",
        ),
    )

    good = ts.notna().to_numpy()

    frame = frame.loc[good].copy()
    ts = ts.loc[good]

    if frame.empty:
        return pd.DataFrame(), {
            "rows": 0,
            "source_columns": mapping,
            "recognised_columns": sorted(set(mapping.values())),
            "unknown_columns": [],
            "note": "no rows with a usable timestamp",
        }

    # Timestamp becomes the index, so it is no longer a signal column.
    frame = frame.drop(
        columns=[actual_time_col]
    )

    # Rename recognised columns.
    #
    # resolve_columns() already prevents two source columns from mapping to
    # the same standard name. This protects columns such as HR, HR.1, HR.2,
    # etc. from accidental data loss.
    rename_map = {
        source: standard
        for source, standard in mapping.items()
        if source in frame.columns
    }

    frame = frame.rename(
        columns=rename_map
    )

    frame.index = ts.to_numpy()
    frame.index.name = "timestamp"

    frame = frame.sort_index()

    # Do not aggregate duplicate timestamps. Keep the first source row.
    frame = frame[
        ~frame.index.duplicated(
            keep="first"
        )
    ]

    # Apply only explicitly defined missing-value rules.
    frame = apply_missing_rules(
        frame,
        profile,
    )

    # Remove rows where every retained source variable is missing.
    frame = frame.dropna(
        how="all"
    )

    if frame.empty:
        return pd.DataFrame(), {
            "rows": 0,
            "source_columns": rename_map,
            "recognised_columns": sorted(
                set(rename_map.values())
            ),
            "unknown_columns": [],
            "note": "no usable signal values after missing-value rules",
        }

    # Standardised variables.
    recognised_columns = sorted(
        set(rename_map.values())
    )

    # Everything else remains under its original source name.
    unknown_columns = [
        str(col)
        for col in frame.columns
        if str(col) not in recognised_columns
    ]

    meta = describe(
        frame,
        rename_map,
    )

    meta["location"] = _location_from_name(
        str(path)
    )

    meta["source_resolution_preserved"] = True
    meta["downsampled"] = False
    meta["source_time_column"] = actual_time_col

    meta["recognised_columns"] = recognised_columns
    meta["unknown_columns"] = unknown_columns

    meta["source_column_mapping"] = rename_map

    meta["source_column_count"] = int(
        len(frame.columns)
    )

    meta["recognised_column_count"] = int(
        len(recognised_columns)
    )

    meta["unknown_column_count"] = int(
        len(unknown_columns)
    )

    # Estimate temporal resolution.
    if len(frame.index) > 1:
        deltas = (
            frame.index[1:]
            - frame.index[:-1]
        )

        seconds = (
            deltas / pd.Timedelta(seconds=1)
        )

        seconds = pd.Series(
            seconds,
            dtype="float64",
        )

        seconds = seconds[
            seconds > 0
        ]

        if not seconds.empty:
            median_interval = float(
                seconds.median()
            )

            meta["median_interval_s"] = median_interval
            meta["min_interval_s"] = float(
                seconds.min()
            )
            meta["max_interval_s"] = float(
                seconds.max()
            )

            if median_interval > 0:
                meta["estimated_hz"] = round(
                    1.0 / median_interval,
                    6,
                )
            else:
                meta["estimated_hz"] = None
        else:
            meta["estimated_hz"] = None
    else:
        meta["estimated_hz"] = None

    # Event-only / unrecognised exports are explicitly identified.
    if not recognised_columns:
        meta["export_type"] = (
            "non_physiological_or_unrecognised"
        )
    else:
        meta["export_type"] = "physiological"

    return frame, meta


def _location_from_name(filename: str):
    """
    Infinity filename encodes the recording location.

    Example:
        20260812142219.infinity.data.OR^^BLOC03.csv
        -> OR^^BLOC03
    """
    import re

    match = re.search(
        r"\.data\.([^.]+)\.csv$",
        filename,
        re.IGNORECASE,
    )

    return (
        match.group(1)
        if match
        else None
    )
