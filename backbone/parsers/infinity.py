"""
Draeger Infinity parser.

One row per second, approximately 130 columns, semicolon delimited, timestamp
as a 14 digit local wall clock, for example 20260812142219.

Only the columns declared in keep_variables are retained, read with a usecols
callable so a 130 column file is not fully materialised.
"""

from __future__ import annotations

import pandas as pd

from ..config import normalise_key
from ._common import (apply_missing_rules, describe, resolve_columns,
                      sniff_delimiter, to_utc)


def sniff_header(path, profile, cfg) -> dict:
    """
    Cheap structural check for validation. The Draeger export is a flat,
    semicolon delimited header on line 0; OBSERVATION_DATETIME carries the
    local wall clock. Some files are compressed header-only exports that carry
    no recognised signal columns; that is a different (valid) export type,
    reported as WARNING_EMPTY by the validator, not as a failure.
    """
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            first = fh.readline()
            second = fh.readline()
    except OSError as exc:
        return {"readable": False, "error": str(exc), "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}

    if not first.strip():
        return {"readable": True, "error": None, "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}

    delimiter = cfg.get("delimiter") or sniff_delimiter(path, default=";")
    header = [h.strip().strip('"') for h in first.split(delimiter)]
    mapping = resolve_columns(header, profile)
    time_col = cfg.get("time", {}).get("column", "OBSERVATION_DATETIME")
    time_found = any(normalise_key(h) == normalise_key(time_col) for h in header)
    return {
        "readable": True, "error": None, "delimiter": delimiter,
        "header": header, "recognised": set(mapping.values()),
        "time_column": time_col if time_found else None,
        "data_rows": 1 if second.strip() else 0,
    }


def parse(path, profile, cfg):
    delimiter = cfg.get("delimiter") or sniff_delimiter(path)
    encoding = cfg.get("encoding", "utf-8")
    time_cfg = cfg.get("time", {})
    time_col = time_cfg.get("column", "OBSERVATION_DATETIME")

    header = pd.read_csv(path, sep=delimiter, nrows=0, encoding=encoding,
                         engine="python").columns
    mapping = resolve_columns(header, profile)
    wanted = set(cfg.get("keep_variables") or mapping.values())
    keep_source = {src for src, std in mapping.items() if std in wanted}

    if not keep_source:
        return pd.DataFrame(), {"rows": 0, "source_columns": mapping,
                                "note": "no recognised signal columns in this export"}

    if time_col not in header:
        raise ValueError(f"time column {time_col!r} absent; header has {list(header)[:6]}")

    frame = pd.read_csv(
        path, sep=delimiter, encoding=encoding, low_memory=False,
        usecols=lambda c: c == time_col or c in keep_source,
    )

    ts = to_utc(frame[time_col].astype(str).str.split(".").str[0],
                time_cfg.get("format", "%Y%m%d%H%M%S"),
                time_cfg.get("timezone", "America/Montreal"))
    frame = frame.drop(columns=[time_col]).rename(columns=mapping)
    good = ts.notna().to_numpy()
    frame = frame.loc[good].copy()
    if frame.shape[1] == 0:
        return pd.DataFrame(), {"rows": 0, "source_columns": mapping,
                                "note": "no recognised signal columns in this export"}
    frame.index = ts[good]
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="first")]
    frame = apply_missing_rules(frame, profile)
    frame = frame.dropna(how="all")
    if frame.empty:
        return pd.DataFrame(), {"rows": 0, "source_columns": mapping,
                                "note": "no rows with a usable timestamp"}

    meta = describe(frame, {k: v for k, v in mapping.items() if k in keep_source})
    meta["location"] = _location_from_name(str(path))
    return frame, meta


def _location_from_name(filename: str):
    """
    The Infinity filename encodes where the recording was made:
        20260812142219.infinity.data.OR^^BLOC03.csv   operating room
        20240913080208.infinity.data.CU1^^Bed13.csv   intensive care
    ESMONOL records in intensive care, the rest in theatre, so this is kept.
    """
    import re
    match = re.search(r"\.data\.([^.]+)\.csv$", filename, re.IGNORECASE)
    return match.group(1) if match else None
