"""
Medasense NOL ExcelData parser.

This file is not a rectangular CSV and will break pandas.read_csv. Measured
structure of V-RAPS patient 37:

    line 0   Age,Weight,Height,Gender,Surgery,Notes,
    line 1   N/A,N/A,N/A,N/A,N/A,N/A,
    line 2   <padding>Abs Time Vector,Relative Time,NOL,HR,Events
    line 3   12-Aug-2026 14:06:11,0,NaN,NaN," Type 5 Starting Time @..."
    line 4   <blank>
    line 5   12-Aug-2026 14:06:16,5,NaN,NaN,

Two stacked header blocks in one file. A demographics block of six columns,
then a time series block of five, separated and padded with whitespace, with
blank lines interspersed. Comma delimited where the monitors use semicolons.
Dates as "12-Aug-2026 14:06:11", a third format again.

In that file 636 of 1231 NOL values were NaN, so 52 percent of the primary
variable is missing. That is normal for this device and is reported as coverage
rather than treated as a fault.
"""

from __future__ import annotations

import io
import re

import pandas as pd

from ..config import normalise_key
from ._common import apply_missing_rules, describe, resolve_columns, to_utc

TIME_ROW = re.compile(r"^\s*\d{1,2}-[A-Za-z]{3}-\d{4}\s+\d{2}:\d{2}:\d{2}")


def sniff_header(path, profile, cfg) -> dict:
    """
    Cheap structural check for validation. The Medasense export has TWO stacked
    header blocks: demographics on lines 0-1, then the real time series header
    ('Abs Time Vector,Relative Time,NOL,HR,Events') on a later line, padded.
    A validator that only looks at line 0 would call a perfect file corrupt,
    which is exactly what happened in V-RAPS.
    """
    try:
        with open(path, "r", encoding=cfg.get("encoding", "utf-8"),
                  errors="replace") as fh:
            head = fh.read(256 * 1024)
    except OSError as exc:
        return {"readable": False, "error": str(exc), "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}

    lines = head.splitlines()
    header_idx, columns = _find_series_header(lines)
    if header_idx is None:
        return {"readable": True, "error": None, "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}

    mapping = resolve_columns(columns, profile)
    time_cfg = cfg.get("time", {})
    time_col = time_cfg.get("column", "Abs Time Vector")
    time_found = any(normalise_key(c) == normalise_key(time_col) for c in columns)
    data_rows = sum(1 for ln in lines[header_idx + 1:] if TIME_ROW.match(ln))
    return {
        "readable": True, "error": None, "delimiter": ",",
        "header": columns, "recognised": set(mapping.values()),
        "time_column": time_col if time_found else (columns[0] if columns else None),
        "data_rows": data_rows,
    }


def parse(path, profile, cfg):
    with open(path, "r", encoding=cfg.get("encoding", "utf-8"), errors="replace") as fh:
        lines = fh.read().splitlines()

    demographics = _read_demographics(lines)
    header_idx, columns = _find_series_header(lines)
    if header_idx is None:
        raise ValueError("no time series header found in NOL export")

    body = [ln for ln in lines[header_idx + 1:] if TIME_ROW.match(ln)]
    if not body:
        return pd.DataFrame(), {"rows": 0, "source_columns": {},
                                "demographics": demographics,
                                "note": "header present, no data rows"}

    frame = pd.read_csv(io.StringIO("\n".join([",".join(columns)] + body)),
                        sep=",", engine="python", skipinitialspace=True,
                        names=columns, header=0, on_bad_lines="skip")
    frame.columns = [str(c).strip() for c in frame.columns]

    time_col = cfg.get("time", {}).get("column", "Abs Time Vector")
    if time_col not in frame.columns:
        time_col = frame.columns[0]

    events = _extract_events(frame)

    mapping = resolve_columns(frame.columns, profile)
    wanted = set(cfg.get("keep_variables") or mapping.values())
    keep = {src: std for src, std in mapping.items() if std in wanted}

    ts = to_utc(frame[time_col].astype(str).str.strip(),
                cfg.get("time", {}).get("format", "%d-%b-%Y %H:%M:%S"),
                cfg.get("time", {}).get("timezone", "America/Montreal"))

    values = frame[list(keep)].rename(columns=keep)
    good = ts.notna().to_numpy()
    values = values.loc[good].copy()
    values.index = ts[good]
    values = values.sort_index()
    values = values[~values.index.duplicated(keep="first")]
    values = apply_missing_rules(values, profile)

    meta = describe(values, keep)
    meta["demographics"] = demographics
    meta["events"] = events
    meta["missing_fraction"] = _missing_fraction(values)
    return values, meta


def _read_demographics(lines) -> dict:
    """The six column block at the top. Often all N/A, which is fine."""
    if len(lines) < 2:
        return {}
    keys = [k.strip() for k in lines[0].split(",") if k.strip()]
    vals = [v.strip() for v in lines[1].split(",")]
    out = {}
    for i, key in enumerate(keys):
        value = vals[i] if i < len(vals) else ""
        out[key] = None if value.upper() in ("", "N/A", "NA", "NAN") else value
    return out


def _find_series_header(lines):
    """
    Locate the second header. It is padded with a large run of spaces, so the
    search is for a line containing the known column names rather than for a
    fixed line number.
    """
    for i, line in enumerate(lines[:20]):
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if "time" in lowered and ("nol" in lowered or "relative" in lowered):
            columns = [c.strip() for c in stripped.split(",")]
            if len(columns) >= 3:
                return i, columns
    return None, None


def _extract_events(frame) -> list:
    """
    The Events column carries free text annotations in mixed French and English,
    for example "Type 1 fin bloc @12-08-2026 14:55:37". Kept as a list rather
    than discarded, because they mark the surgical timeline.
    """
    col = next((c for c in frame.columns if c.strip().lower() == "events"), None)
    if col is None:
        return []
    out = []
    for stamp, text in zip(frame.iloc[:, 0], frame[col]):
        text = str(text).strip().strip('"').strip()
        if text and text.lower() not in ("nan", "none"):
            out.append({"at": str(stamp).strip(), "text": text})
    return out


def _missing_fraction(frame) -> dict:
    if frame.empty:
        return {}
    return {c: round(float(frame[c].isna().mean()), 4) for c in frame.columns}
