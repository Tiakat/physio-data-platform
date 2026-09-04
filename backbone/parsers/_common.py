"""Shared helpers for parsers."""

from __future__ import annotations

import csv
import re

import numpy as np
import pandas as pd

from ..config import alias_index, missing_codes, normalise_key, variable_spec


def sniff_delimiter(path, default=";", sample_bytes=8192) -> str:
    """
    Patient 34 in IPAMS uses commas where every other patient uses semicolons,
    so the delimiter is detected rather than assumed.
    """
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        sample = fh.read(sample_bytes)
    if not sample:
        return default
    try:
        return csv.Sniffer().sniff(sample, delimiters=";,\t").delimiter
    except csv.Error:
        first = sample.splitlines()[0] if sample.splitlines() else ""
        return ";" if first.count(";") >= first.count(",") else ","


def resolve_columns(columns, profile) -> dict[str, str]:
    """Source column name -> standard variable name, for the ones we recognise."""
    index = alias_index(profile)
    out = {}
    for col in columns:
        standard = index.get(normalise_key(col))
        if standard and standard not in out.values():
            out[col] = standard
    return out


def apply_missing_rules(frame: pd.DataFrame, profile) -> pd.DataFrame:
    """
    Turn 'not a measurement' into NaN, per variable.

    This is the step that has to be right. In the IPAM analysis, treating every
    zero as missing and treating no zero as missing both produced wrong answers.
    The rule comes from zero_is_valid in the data dictionary, per variable.
    """
    codes = missing_codes(profile)
    out = frame.copy()
    for column in out.columns:
        series = pd.to_numeric(out[column], errors="coerce")
        if codes:
            series = series.mask(series.isin(codes))
        spec = variable_spec(profile, column)
        if spec and not spec.get("zero_is_valid", False):
            series = series.mask(series == 0)
        out[column] = series
    return out


def to_utc(series: pd.Series, fmt: str | None, tz: str) -> pd.Series:
    """Parse local wall clock times and return timezone aware UTC."""
    parsed = pd.to_datetime(series, format=fmt, errors="coerce")
    if parsed.dt.tz is None:
        parsed = parsed.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
    return parsed.dt.tz_convert("UTC")


def start_time_from_filename(filename: str, pattern: str, tz: str):
    """
    BetterCare stores no clock inside the file. The filename carries it:

        1.2.826.0.1.3680043.2.403.36.1251001082435600.13.<...>-1.csv
                                     ^^^^^^^^^^^^^^^^
        1 251001 082435 600  ->  2025-10-01 08:24:35

    Verified against the Infinity recording of the same operation in all 23
    dual recorded patients: median absolute residual lag 4 seconds.
    """
    match = re.search(pattern, filename)
    if not match:
        return None
    digits = match.group(1)[1:]          # drop the leading 1
    try:
        stamp = pd.Timestamp(
            year=2000 + int(digits[0:2]), month=int(digits[2:4]), day=int(digits[4:6]),
            hour=int(digits[6:8]), minute=int(digits[8:10]), second=int(digits[10:12]),
            tz=tz,
        )
    except (ValueError, TypeError):
        return None
    return stamp.tz_convert("UTC")


def file_sequence_number(filename: str) -> int:
    """BetterCare files are chained as ...-1.csv, -2.csv, -3.csv."""
    match = re.search(r"-(\d+)\.csv$", filename, re.IGNORECASE)
    return int(match.group(1)) if match else 0


def describe(frame: pd.DataFrame, source_columns: dict) -> dict:
    """Standard metadata block returned by every parser."""
    if frame.empty:
        return {"rows": 0, "duration_s": 0.0, "interval_s": None,
                "source_columns": source_columns, "variables": []}
    index = frame.index
    # Resolution independent: pandas may report ns or us depending on how the
    # timestamps were built, so never divide raw int64 by a hard coded 1e9.
    if len(index) > 1:
        deltas = ((index[1:] - index[:-1]) / np.timedelta64(1, "s")).astype(float)
    else:
        deltas = np.array([])
    interval = float(np.median(deltas)) if deltas.size else None
    return {
        "rows": int(len(frame)),
        "duration_s": float((index[-1] - index[0]).total_seconds()),
        "interval_s": interval,
        "source_columns": source_columns,
        "variables": sorted(frame.columns),
        "start": index[0].isoformat(),
        "end": index[-1].isoformat(),
    }
