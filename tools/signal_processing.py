"""Signal-specific processing engine (Phases 9-11).

The dictionary (profiles/_variables.yaml) is the single source of truth for
valid ranges, zero semantics and raw-name aliases. Signal configs under
configs/signals/ declare *behaviour* only: which detectors to run, which
filter to apply, which features to extract. Any range in a config is a
fallback for channels with no dictionary entry yet, and a disagreement with
the dictionary is logged as a conflict (dictionary wins).

Philosophy: invalid observations are flagged, never deleted. Raw is never
modified; this module produces filtered + QC sidecar dataframes, leaving
the source untouched.
"""

import argparse
import json
import os
import re

import numpy as np
import pandas as pd
import yaml

# Detectors actually implemented below. A config may *declare* any detector
# name, but names outside this set are reported at load time and run nothing
# -- never silently skipped.
IMPLEMENTED_DETECTORS = {"flatline", "spike", "saturation_clipping",
                         "out_of_range"}

# Filters actually implemented below.
IMPLEMENTED_FILTERS = {"none", "lowpass", "bandpass", "robust_smoothing"}

# Column names we accept as the time base, in preference order.
TIME_CANDIDATES = ["timestamp", "time", "time_ms", "time_s", "datetime",
                   "epoch_ms", "epoch_s"]

# Matches a parenthesised unit/embedded segment, e.g. " (mm(hg)^^ISO+)",
# " (/min^^ISO+)". Used to strip vendor unit suffixes for alias matching.
_UNIT_SEGMENT_RE = re.compile(
    r"\s*\((?:[^()]*\d[^()]*|mm\s?hg|iso|bpm|mv|uv|%|hz)[^()]*\)",
    re.IGNORECASE)

# pandas duplicate-column suffix: HR.1, HR.11 ...
_DUPLICATE_SUFFIX_RE = re.compile(r"\.(\d+)$")


# --------------------------------------------------------------------------
# Alias / canonicalisation helpers
# --------------------------------------------------------------------------

def _strip_unit_segment(name: str) -> str:
    """Remove vendor unit segments from a raw column name."""
    return _UNIT_SEGMENT_RE.sub("", name).strip()


def _normalize_name(name: str) -> str:
    """Case/whitespace-insensitive key for alias lookup."""
    return re.sub(r"\s+", " ", str(name)).strip().lower()


# --------------------------------------------------------------------------
# Config + dictionary loading
# --------------------------------------------------------------------------

def load_signal_configs(config_dir, variables_path):
    """Load signal configs and build the lookup index.

    Returns (configs, index) where configs maps config filename stem ->
    config dict, and index is a dict with:
      alias -> (config, canonical_variable)
      conflicts: [{channel, config_range, dictionary_range}]
      unresolved_channels: [{config, channel}]  (config fallback in use)
      unimplemented_detectors: [{config, detector, method}]
      unimplemented_filters: [{config, method}]
      dictionary_channels: set of canonical names found in the dictionary
    """
    configs = {}
    index = {
        "alias": {},
        "conflicts": [],
        "unresolved_channels": [],
        "unimplemented_detectors": [],
        "unimplemented_filters": [],
        "dictionary_channels": set(),
    }

    # --- dictionary ------------------------------------------------------
    dictionary = {}
    global_missing = []
    if variables_path and os.path.exists(str(variables_path)):
        with open(variables_path, encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        dictionary = raw.get("variables", {}) or {}
        global_missing = raw.get("global_missing_codes", []) or []

    for canon, spec in dictionary.items():
        if not isinstance(spec, dict):
            continue
        index["dictionary_channels"].add(canon)
        # Sentinel key carries the dictionary spec for this canonical name.
        index["alias"]["\x00" + canon] = spec

    index["global_missing_codes"] = list(global_missing)

    # --- configs ---------------------------------------------------------
    for fname in sorted(os.listdir(config_dir)):
        if not fname.endswith((".yaml", ".yml")):
            continue
        stem = os.path.splitext(fname)[0]
        with open(os.path.join(config_dir, fname), encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        configs[stem] = cfg
        signal = cfg.get("signal", stem)

        # Register channels and their aliases against this config.
        for channel in cfg.get("channels", []) or []:
            spec = index["alias"].get("\x00" + channel)
            if spec is None:
                index["unresolved_channels"].append(
                    {"config": signal, "channel": channel})
            else:
                # YAML range present but dictionary wins: log the conflict.
                yaml_range = (cfg.get("validity") or {}).get("range")
                dict_range = ([spec.get("min"), spec.get("max")]
                              if spec.get("min") is not None
                              and spec.get("max") is not None else None)
                if yaml_range and dict_range and list(yaml_range) != dict_range:
                    index["conflicts"].append({
                        "config": signal,
                        "channel": channel,
                        "config_range": list(yaml_range),
                        "dictionary_range": dict_range,
                    })
            names = [channel]
            if spec:
                names += spec.get("aliases", []) or []
            for name in names:
                key = _normalize_name(name)
                stripped = _normalize_name(_strip_unit_segment(name))
                index["alias"].setdefault(key, (cfg, channel))
                index["alias"].setdefault(stripped, (cfg, channel))

        # Report declared-but-unimplemented detectors/filters at load time.
        for det in cfg.get("artifact_detection", []) or []:
            name = det.get("name") if isinstance(det, dict) else det
            if name not in IMPLEMENTED_DETECTORS:
                index["unimplemented_detectors"].append({
                    "config": signal, "detector": name,
                    "method": (det.get("method") if isinstance(det, dict)
                               else None),
                })
        filt = cfg.get("filter", {}) or {}
        method = filt.get("method", "none") if isinstance(filt, dict) else "none"
        if method not in IMPLEMENTED_FILTERS:
            index["unimplemented_filters"].append(
                {"config": signal, "method": method})

    return configs, index


def find_config(column, configs, index):
    """Resolve a raw column name to (config, canonical_variable).

    Order: exact alias -> unit-segment stripped -> duplicate suffix
    stripped (HR.11 -> HR.1 -> HR) -> alias again. Returns (None, None)
    when nothing resolves.
    """
    alias = index.get("alias", {})
    key = _normalize_name(column)
    hit = alias.get(key)
    if isinstance(hit, tuple):
        return hit
    stripped = _normalize_name(_strip_unit_segment(column))
    hit = alias.get(stripped)
    if isinstance(hit, tuple):
        return hit
    # pandas duplicate suffixes: strip repeatedly (HR.11 -> HR.1 -> HR)
    base = column
    while True:
        m = _DUPLICATE_SUFFIX_RE.search(base)
        if not m:
            break
        base = base[:m.start()]
        hit = alias.get(_normalize_name(base))
        if isinstance(hit, tuple):
            return hit
        hit = alias.get(_normalize_name(_strip_unit_segment(base)))
        if isinstance(hit, tuple):
            return hit
    return None, None


def get_var_spec(canonical, index):
    """Return the dictionary spec for a canonical variable, or None."""
    return index.get("alias", {}).get("\x00" + canonical)


# --------------------------------------------------------------------------
# Validity (Phase 9)
# --------------------------------------------------------------------------

def apply_validity(series, config, var_spec, missing_codes):
    """Flag out-of-range / invalid-zero / missing-code samples.

    Returns (cleaned, flags): cleaned has invalid samples as NaN, flags is a
    Series of QC labels. Missing codes (device sentinels like -1401) become
    MISSING. Out-of-range values become INVALID_RANGE. Raw is untouched.
    """
    s = pd.to_numeric(series, errors="coerce")
    flags = pd.Series("VALID", index=s.index, dtype=object)
    cleaned = s.copy()

    # Native missing values are MISSING, never VALID. (NaN comparisons are
    # always False, so without this they would silently stay VALID.)
    is_nan = s.isna()
    flags[is_nan] = "MISSING"

    codes = list(missing_codes or [])
    if codes:
        is_code = s.isin(codes)
        flags[is_code] = "MISSING"
        cleaned[is_code] = np.nan

    if var_spec is not None:
        lo, hi = var_spec.get("min"), var_spec.get("max")
        zero_ok = var_spec.get("zero_is_valid", False)
    else:
        validity = (config or {}).get("validity", {}) or {}
        rng = validity.get("range")
        lo, hi = (rng[0], rng[1]) if rng else (None, None)
        zero_ok = validity.get("zero_is_valid", False)

    if lo is not None and hi is not None:
        bad = (s < lo) | (s > hi)
        # invalid zeros that are *inside* range still flagged when zero invalid
        if not zero_ok:
            bad = bad | (s == 0)
        bad = bad & (flags == "VALID")  # don't overwrite MISSING
        flags[bad] = "INVALID_RANGE"
        cleaned[bad] = np.nan

    return cleaned, flags


# --------------------------------------------------------------------------
# Artifact detection (Phase 10)
# --------------------------------------------------------------------------

def detect_flatline(series, fs_hz, window_s=5.0):
    """Boolean mask: windows with ~zero variance (disconnected lead)."""
    s = pd.to_numeric(series, errors="coerce")
    n = max(int(round(window_s * fs_hz)), 3)
    if len(s) < n:
        return pd.Series(False, index=s.index)
    roll = s.rolling(n, center=True, min_periods=n)
    var = roll.var()
    eps = 1e-12
    hit = (var < eps).fillna(False)
    return hit


def detect_spike(series, fs_hz, window_s=10.0, n_sigma=5.0):
    """Boolean mask: samples deviating n_sigma (MAD scale) from local median."""
    s = pd.to_numeric(series, errors="coerce")
    n = max(int(round(window_s * fs_hz)), 3)
    if len(s) < n:
        return pd.Series(False, index=s.index)
    med = s.rolling(n, center=True, min_periods=n).median()
    mad = (s - med).abs().rolling(n, center=True,
                                  min_periods=n).median()
    scale = mad * 1.4826
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (s - med).abs() / scale.replace(0, np.nan)
    hit = (z > n_sigma).fillna(False)
    return hit


def detect_saturation(series, min_consecutive=5):
    """Boolean mask: runs at the series' own min/max (ADC clipping)."""
    s = pd.to_numeric(series, errors="coerce")
    if len(s) < min_consecutive:
        return pd.Series(False, index=s.index)
    vmax, vmin = s.max(), s.min()
    at_edge = (s == vmax) | (s == vmin)
    # find runs of at_edge with length >= min_consecutive
    hit = pd.Series(False, index=s.index)
    run_start = None
    vals = at_edge.to_numpy()
    for i, v in enumerate(vals):
        if v and run_start is None:
            run_start = i
        if not v and run_start is not None:
            if i - run_start >= min_consecutive:
                hit.iloc[run_start:i] = True
            run_start = None
    if run_start is not None and len(vals) - run_start >= min_consecutive:
        hit.iloc[run_start:] = True
    return hit


def detect_artifacts(series, fs_hz, detectors):
    """Run the *implemented* detectors declared in the config.

    detectors: list of {name, params}. Names outside IMPLEMENTED_DETECTORS
    are ignored here -- they were already reported at load time.
    Returns a dict name -> boolean mask.
    """
    out = {}
    for det in detectors or []:
        if not isinstance(det, dict):
            continue
        name = det.get("name")
        params = det.get("params", {}) or {}
        if name == "flatline":
            out[name] = detect_flatline(
                series, fs_hz, window_s=params.get("window_s", 5.0))
        elif name == "spike":
            out[name] = detect_spike(
                series, fs_hz, window_s=params.get("window_s", 10.0),
                n_sigma=params.get("n_sigma", 5.0))
        elif name == "saturation_clipping":
            out[name] = detect_saturation(
                series,
                min_consecutive=params.get("min_consecutive", 5))
        # out_of_range is handled by apply_validity (Phase 9), not here.
    return out


# --------------------------------------------------------------------------
# Filtering (Phase 11) -- signal-specific, never generic
# --------------------------------------------------------------------------

def _butter_filter(x, fs_hz, low_hz=None, high_hz=None, order=4,
                   kind="bandpass"):
    from scipy.signal import butter, sosfiltfilt
    nyq = fs_hz / 2.0
    if kind == "lowpass":
        if high_hz is None or high_hz >= nyq:
            return x  # cutoff at/above Nyquist: no-op, must not invent data
        sos = butter(order, high_hz / nyq, btype="low", output="sos")
    elif kind == "highpass":
        if low_hz is None or low_hz <= 0 or low_hz >= nyq:
            return x  # Nyquist-invalid: no-op, must not invent data
        sos = butter(order, low_hz / nyq, btype="high", output="sos")
    else:
        if low_hz is None or low_hz <= 0:
            return _butter_filter(x, fs_hz, high_hz=high_hz, order=order,
                                  kind="lowpass")
        if high_hz is None or high_hz >= nyq:
            return _butter_filter(x, fs_hz, low_hz=low_hz, order=order,
                                  kind="highpass")
        if low_hz >= nyq or high_hz <= 0 or low_hz >= high_hz:
            return x  # Nyquist-invalid: no-op, must not invent data
        sos = butter(order, [low_hz / nyq, high_hz / nyq], btype="band",
                     output="sos")
    mask = ~np.isnan(x)
    if mask.sum() < order * 3 + 1:
        return x  # too few valid samples to filter honestly
    y = np.full_like(x, np.nan)
    y[mask] = sosfiltfilt(sos, x[mask])
    return y


def apply_filter(series, fs_hz, cfg):
    """Apply the config's signal-specific filter.

    NaN stays NaN: filtering never resurrects invalid samples. Missing
    samples are not interpolated across here (that is a separate, logged
    decision, not part of filtering).
    """
    filt = (cfg or {}).get("filter", {}) or {}
    method = filt.get("method", "none")
    params = filt.get("params", {}) or {}
    x = pd.to_numeric(series, errors="coerce").to_numpy(dtype=float)

    if method == "none":
        return pd.Series(x, index=series.index)
    if method == "lowpass":
        y = _butter_filter(x, fs_hz, high_hz=params.get("high_hz"),
                           order=params.get("order", 4), kind="lowpass")
        return pd.Series(y, index=series.index)
    if method == "bandpass":
        y = _butter_filter(x, fs_hz, low_hz=params.get("low_hz"),
                           high_hz=params.get("high_hz"),
                           order=params.get("order", 4), kind="bandpass")
        return pd.Series(y, index=series.index)
    if method == "robust_smoothing":
        # Parameter-level QC: replace robust outliers with local median.
        window_s = params.get("window_s", 10.0)
        n_sigma = params.get("n_sigma", 3.0)
        n = max(int(round(window_s * fs_hz)), 3)
        s = pd.Series(x, index=series.index)
        med = s.rolling(n, center=True, min_periods=1).median()
        mad = (s - med).abs().rolling(n, center=True,
                                      min_periods=1).median()
        scale = mad * 1.4826
        # A perfectly flat neighbourhood has MAD 0: any deviation there is
        # an outlier by definition, so floor the scale instead of dividing
        # by zero (which would silently keep the spike).
        scale = scale.mask(scale == 0, 1e-9)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (s - med).abs() / scale
        outlier = (z > n_sigma).fillna(False) & s.notna()
        out = s.copy()
        out[outlier] = med[outlier]
        return out
    # Unimplemented filter names were reported at load; never invent one.
    return pd.Series(x, index=series.index)


# --------------------------------------------------------------------------
# Series / frame processing
# --------------------------------------------------------------------------

def measure_fs_hz(time_index, series=None):
    """Observed sampling rate from a datetime index (median dt)."""
    try:
        idx = pd.DatetimeIndex(pd.to_datetime(time_index, errors="coerce"))
        idx = idx[~idx.isna()]
        if len(idx) < 2:
            return None
        dt = idx.to_series().diff().dt.total_seconds().median()
        if dt and dt > 0:
            return 1.0 / dt
    except Exception:
        pass
    return None


def process_series(col, series, fs_hz, cfg, var_spec, missing_codes,
                   max_gap_s=10.0):
    """Process one column: validity -> artifact flags -> filter.

    Returns (filtered, qc) Series. Invalid samples are NaN in filtered with
    the reason in qc. Gaps longer than max_gap_s are flagged GAP, never
    bridged.
    """
    cleaned, flags = apply_validity(series, cfg, var_spec, missing_codes)
    # Samples missing *before* validity processing: structural gaps. Samples
    # invalidated by validity keep their INVALID_RANGE/MISSING reason -- a
    # column that is entirely out of range is not a "gap".
    originally_missing = pd.to_numeric(series, errors="coerce").isna()

    detectors = ((cfg or {}).get("artifact_detection") or [])
    masks = detect_artifacts(cleaned, fs_hz or 1.0,
                             [d for d in detectors
                              if isinstance(d, dict)
                              and d.get("name") in IMPLEMENTED_DETECTORS
                              and d.get("name") != "out_of_range"])
    flag_names = {"flatline": "FLATLINE", "spike": "SPIKE",
                  "saturation_clipping": "SATURATION"}
    for det_name, mask in masks.items():
        hit = mask & (flags == "VALID")
        flags[hit] = flag_names.get(det_name, "DEVICE_ARTIFACT")
        cleaned[hit] = np.nan

    # Long runs of *originally missing* samples: mark GAP (structural
    # interruption, not bridged). Never overwrite validity flags.
    if max_gap_s and fs_hz:
        min_len = int(round(max_gap_s * fs_hz))
        isnan = originally_missing.to_numpy()
        if isnan.any() and min_len > 1:
            run_id = np.cumsum(np.diff(np.concatenate([[False], isnan])) != 0)
            for r in np.unique(run_id[isnan]):
                idx = np.where((run_id == r) & isnan)[0]
                if len(idx) >= min_len:
                    sel = flags.iloc[idx]
                    flags.iloc[idx] = sel.where(sel != "VALID", "GAP")

    filt_method = ((cfg or {}).get("filter", {}) or {}).get("method", "none")
    try:
        filtered = apply_filter(cleaned, fs_hz or 1.0, cfg or {})
    except Exception as exc:  # noqa: BLE001
        # A filter that cannot run on real data must never kill the
        # pipeline: keep the cleaned signal, flag for human review.
        filtered = cleaned.copy()
        review_note = (f"filter_failed:{filt_method}:"
                       f"{type(exc).__name__}:fs_hz={fs_hz}")
        if isinstance(flags, pd.Series):
            flags = flags.copy()
        # stash the note where process_frame can collect it
        filtered.attrs["filter_failure"] = review_note
    # Safety: filtering must never resurrect an invalid sample.
    filtered[cleaned.isna()] = np.nan
    return filtered, flags


def process_frame(df, time_col, configs, index, missing_codes,
                  fs_overrides=None, source="unknown", time_index=None):
    """Process a whole source file.

    Returns (filtered, qc, review):
      filtered: dataframe with the time column + processed signals
      qc: dataframe with the time column + <col>__qc flag columns
      review: list of dicts for columns needing human review
    Unknown columns are preserved untouched and queued for review --
    never dropped, never processed with a generic rule.
    """
    fs_overrides = fs_overrides or {}
    review = []
    filtered_cols = {}
    qc_cols = {}

    # --- time base ------------------------------------------------------
    if time_index is not None:
        t_index = pd.DatetimeIndex(pd.to_datetime(time_index,
                                                  errors="coerce"))
    elif time_col and time_col in df.columns:
        t_index = pd.DatetimeIndex(pd.to_datetime(df[time_col],
                                                  errors="coerce"))
    else:
        found = next((c for c in TIME_CANDIDATES if c in df.columns), None)
        if found:
            print(f"[warn] requested time_col={time_col!r} not found in "
                  f"columns; using {found!r} instead.")
            t_index = pd.DatetimeIndex(pd.to_datetime(df[found],
                                                       errors="coerce"))
        else:
            print(f"[warn] time_col={time_col!r} not found in columns and "
                  f"no candidate {TIME_CANDIDATES} present; falling back to "
                  f"the row index (sampling rate will be unreliable).")
            t_index = pd.RangeIndex(len(df))

    # Pass the original time column through untouched when present.
    if time_col and time_col in df.columns:
        filtered_cols[time_col] = df[time_col]

    for column in df.columns:
        if column == time_col:
            continue
        s = df[column]
        cfg, canonical = find_config(column, configs, index)
        var_spec = get_var_spec(canonical, index) if canonical else None

        if cfg is None:
            # Unknown: preserve, queue for review, do not process.
            filtered_cols[column] = s
            qc_cols[column + "__qc"] = pd.Series("UNREVIEWED",
                                                 index=s.index, dtype=object)
            review.append({
                "column": column, "source": source,
                "reason": "no_dictionary_entry",
                "detail": "No config and no dictionary entry; preserved "
                          "untouched, needs human classification.",
            })
            continue

        # Sampling rate: explicit override > measured from time base.
        fs = fs_overrides.get(column) or fs_overrides.get(canonical)
        if fs is None:
            fs = measure_fs_hz(t_index, s)
        if fs is None:
            fs = 1.0
            review.append({
                "column": column, "source": source,
                "reason": "sampling_rate_unknown",
                "detail": "Could not measure fs; assumed 1 Hz. Verify.",
            })

        fallback = var_spec is None
        filt, flags = process_series(column, s, fs, cfg, var_spec,
                                     missing_codes)
        failure = getattr(filt, "attrs", {}).get("filter_failure")
        if failure:
            review.append({
                "column": column, "source": source,
                "reason": "filter_failed",
                "detail": failure + "; kept cleaned signal unfiltered.",
            })
        if fallback:
            # Config fallback ranges were used: never report clean VALID.
            flags = flags.where(flags != "VALID", "LOW_QUALITY")
        filtered_cols[column] = filt
        qc_cols[column + "__qc"] = flags

    filtered = pd.DataFrame(filtered_cols, index=df.index)
    qc = pd.DataFrame(qc_cols, index=df.index)

    # A readable timestamp column leads the outputs (unless the source
    # already has one under that name).
    try:
        ts_vals = pd.DatetimeIndex(
            pd.to_datetime(t_index, errors="coerce")).to_numpy()
    except Exception:
        ts_vals = np.asarray(t_index)
    if "timestamp" not in filtered.columns:
        filtered.insert(0, "timestamp", ts_vals)
    if "timestamp" not in qc.columns:
        qc.insert(0, "timestamp", ts_vals)

    return filtered, qc, review


# --------------------------------------------------------------------------
# CLI driver: one source file in, filtered + QC + review out
# --------------------------------------------------------------------------

def run_source_script(device, argv=None):
    """Process a single source file.

    argv: --input, --output, --review-queue, --config-dir, --variables,
          --time-col, --fs (JSON dict of column->Hz overrides),
          --missing-codes (JSON list, extra device sentinels).
    Writes <output> (filtered) and <output>.qc.<ext> (QC flags), appends
    review entries as JSON lines to --review-queue.
    """
    ap = argparse.ArgumentParser(
        description=f"Process one {device} source file (Phases 9-11).")
    ap.add_argument("--input", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--review-queue", required=True)
    ap.add_argument("--config-dir", required=True)
    ap.add_argument("--variables", required=True)
    ap.add_argument("--time-col", default=None)
    ap.add_argument("--fs", default="{}")
    ap.add_argument("--missing-codes", default="[]")
    args = ap.parse_args(argv)

    configs, index = load_signal_configs(args.config_dir, args.variables)

    for w in index["unresolved_channels"]:
        print(f"[warn] channel {w['channel']!r} of signal {w['config']} has "
              f"no dictionary entry yet (config_fallback ranges in use).")
    for w in index["unimplemented_detectors"]:
        print(f"[warn] detector {w['detector']!r} declared by signal "
              f"{w['config']} is NOT implemented -- no artifact detection "
              f"runs for it ({w['method']}).")
    for w in index["unimplemented_filters"]:
        print(f"[warn] filter method {w['method']!r} declared by signal "
              f"{w['config']} is NOT implemented -- column passes through "
              f"unfiltered.")
    drafts = [stem for stem, c in configs.items()
              if c.get("status") == "draft"]
    if drafts:
        print(f"[warn] {len(drafts)} signal configs are still draft "
              f"({', '.join(drafts[:5])}{'...' if len(drafts) > 5 else ''}); "
              f"intervals await review.")

    df = pd.read_csv(args.input)
    print(f"[{device}] loaded {args.input}: {len(df)} rows, "
          f"{len(df.columns)} columns")

    fs_overrides = json.loads(args.fs)
    extra_codes = json.loads(args.missing_codes)
    missing_codes = list(index.get("global_missing_codes", [])) + extra_codes

    filtered, qc, review = process_frame(
        df, args.time_col, configs, index, missing_codes,
        fs_overrides=fs_overrides, source=device)

    out = args.output
    if out.endswith(".parquet"):
        filtered.to_parquet(out, index=False)
        qc_path = out.replace(".parquet", ".qc.parquet")
        qc.to_parquet(qc_path, index=False)
    else:
        filtered.to_csv(out, index=False)
        qc_path = out + ".qc.csv"
        qc.to_csv(qc_path, index=False)

    with open(args.review_queue, "a", encoding="utf-8") as fh:
        for entry in review:
            entry["file"] = args.input
            fh.write(json.dumps(entry) + "\n")

    n_flagged = int((qc.filter(like="__qc")
                       .apply(lambda c: c != "VALID")).sum().sum())
    print(f"[{device}] wrote {out} + {qc_path}; "
          f"{len(review)} review entries, {n_flagged} flagged samples.")
    return 0


if __name__ == "__main__":
    import sys
    device = sys.argv[1] if len(sys.argv) > 1 else "source"
    sys.exit(run_source_script(device, sys.argv[2:]))
