"""
Automatic quality control.

Produces explicit flags, not a single 0 to 100 score. A weighted score needs a
scientific justification that does not yet exist, and flags are easier to
defend, easier to debug and easier for a reviewer to check.

Severity drives what happens next:
    info     recorded, no action
    warning  recorded, recording continues, appears in the review queue
    error    recording is held for human review before approval
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .config import variable_spec


def run(frame: pd.DataFrame, meta: dict, profile: dict, device: str) -> list[dict]:
    """Return a list of flag dictionaries. An empty list means clean."""
    rules = profile.get("quality", {})
    device_cfg = profile.get("devices", {}).get(device, {})
    flags: list[dict] = []

    if frame.empty:
        return [_flag("NO_DATA", "error", None, None, None,
                      "file parsed but contains no usable rows")]

    duration_min = meta.get("duration_s", 0) / 60.0
    minimum = rules.get("min_duration_minutes", 0)
    if duration_min < minimum:
        flags.append(_flag("RECORDING_TOO_SHORT", "error", duration_min, minimum, None,
                           f"{duration_min:.1f} min recorded, {minimum} min required"))

    flags += _check_interval(frame, meta, rules, device_cfg)
    flags += _check_gaps(frame, rules)

    for column in frame.columns:
        series = frame[column]
        flags += _check_missing(series, column, rules)
        flags += _check_flat(series, column, rules, meta)
        flags += _check_held(series, column, rules, meta)
        flags += _check_range(series, column, profile)

    return flags


# ------------------------------------------------------------------ checks

def _check_interval(frame, meta, rules, device_cfg):
    expected = device_cfg.get("expected_interval_s")
    observed = meta.get("interval_s")
    if not expected or not observed:
        return []
    tolerance = rules.get("rate_tolerance", 0.25)
    if abs(observed - expected) / expected > tolerance:
        return [_flag("SAMPLING_IRREGULAR", "warning", observed, expected, None,
                      f"median interval {observed:.2f} s, expected {expected} s")]
    return []


def _check_gaps(frame, rules):
    limit = rules.get("max_gap_seconds", 60)
    if len(frame) < 2:
        return []
    deltas = ((frame.index[1:] - frame.index[:-1]) / np.timedelta64(1, "s")).astype(float)
    worst = float(deltas.max()) if deltas.size else 0.0
    if worst > limit:
        count = int((deltas > limit).sum())
        return [_flag("TIMESTAMP_GAP", "warning", worst, limit, None,
                      f"{count} gaps, longest {worst / 60:.1f} min")]
    return []


def _check_missing(series, column, rules):
    limit = rules.get("max_missing_fraction", 0.4)
    fraction = float(series.isna().mean())
    if fraction > limit:
        return [_flag("MISSING_DATA_HIGH", "warning", fraction, limit, column,
                      f"{fraction:.0%} of {column} missing")]
    return []


def _check_flat(series, column, rules, meta):
    """A signal that never changes for a long stretch is a stuck sensor."""
    seconds = rules.get("flat_signal_seconds", 300)
    interval = meta.get("interval_s") or 1
    window = max(int(seconds / interval), 2)
    values = series.dropna()
    if len(values) < window:
        return []
    changed = values.diff().ne(0)
    run_length = (~changed).astype(int).groupby(changed.cumsum()).cumsum().max()
    if run_length >= window:
        return [_flag("SIGNAL_FLAT", "warning", float(run_length * interval), seconds,
                      column, f"{column} unchanged for {run_length * interval / 60:.1f} min")]
    return []


def _check_held(series, column, rules, meta):
    """
    The check that caught the BetterCare brachial channels refreshing once every
    149 seconds while every other channel refreshed every 3 seconds. A held
    channel looks identical to a measured one in a CSV, and a model that
    consumes it is modelling the monitor's refresh policy, not the patient.
    """
    limit = rules.get("held_channel_seconds", 10)
    interval = meta.get("interval_s") or 1
    values = series.dropna()
    if len(values) < 10:
        return []
    changes = np.flatnonzero(values.diff().fillna(0).to_numpy() != 0)
    if len(changes) < 3:
        return [_flag("CHANNEL_HELD", "error", None, limit, column,
                      f"{column} changes fewer than 3 times in the whole recording")]
    median_gap = float(np.median(np.diff(changes))) * interval
    if median_gap > limit:
        return [_flag("CHANNEL_HELD", "error", median_gap, limit, column,
                      f"{column} refreshes every {median_gap:.0f} s, "
                      f"not usable for second by second analysis")]
    return []


def _check_range(series, column, profile):
    spec = variable_spec(profile, column)
    if not spec:
        return []
    low, high = spec.get("min"), spec.get("max")
    if low is None or high is None:
        return []
    values = series.dropna()
    if values.empty:
        return []
    outside = float(((values < low) | (values > high)).mean())
    if outside > 0.01:
        return [_flag("RANGE_WARNING", "warning", outside, 0.01, column,
                      f"{outside:.1%} of {column} outside {low} to {high} {spec.get('unit','')}")]
    return []


def _flag(name, severity, measured, threshold, variable, detail):
    return {"flag": name, "severity": severity, "measured": measured,
            "threshold": threshold, "variable": variable, "detail": detail}


# ------------------------------------------------------------------ verdict

def verdict(flags: list[dict]) -> str:
    """PASS, REVIEW or FAIL. Only errors force a human to look."""
    if any(f["severity"] == "error" for f in flags):
        return "REVIEW"
    if any(f["severity"] == "warning" for f in flags):
        return "REVIEW"
    return "PASS"
