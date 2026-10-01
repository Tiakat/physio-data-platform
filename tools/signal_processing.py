"""Signal-specific processing engine (Phases 9-11).

One engine, driven by ``configs/signals/*.yaml``. For every column:

1. **Validity** -- physiological range from the dictionary, zero handling from
   ``zero_is_valid``, global missing codes. Violations become NaN in the
   filtered output plus a QC flag. Raw is never modified.
2. **Artifact detection** -- per-signal detectors declared in the YAML
   (flatline, spike, saturation, dropout...), never a generic filter.
3. **Filtering** -- per-signal method (lowpass/bandpass for waveforms,
   robust smoothing for ~1 Hz parameters, none for exposure data).

Columns with no dictionary entry are NOT processed: they go to the review
queue so K can define their handling. Unknown columns are never silently
dropped and never force-fit through another signal's filter.

QC vocabulary: VALID, INVALID_RANGE, SPIKE, FLATLINE, MISSING, GAP,
SATURATION, DEVICE_ARTIFACT, LOW_QUALITY.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# Highest severity first. One flag per sample: the most severe wins.
FLAG_PRIORITY = [
    "MISSING",
    "INVALID_RANGE",
    "SATURATION",
    "FLATLINE",
    "DEVICE_ARTIFACT",
    "SPIKE",
    "GAP",
    "LOW_QUALITY",
    "VALID",
]


def load_signal_configs(config_dir: str | Path) -> dict:
    """Load every configs/signals/*.yaml into {signal_name: config}."""
    configs = {}
    for path in sorted(Path(config_dir).glob("*.yaml")):
        with open(path, encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh)
        if cfg and "signal" in cfg:
            configs[cfg["signal"]] = cfg
    return configs


def _normalise(name: str) -> str:
    import re

    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def find_config(column: str, configs: dict) -> dict | None:
    """Match a column to its signal config via channels or aliases.

    Duplicate columns (HR, HR.1, HR.2 ...) all resolve to the same signal
    config; disambiguation of the duplicates happens in the per-source
    scripts, never here.
    """
    norm = _normalise(column)
    # Strip pandas duplicate suffixes: HR.1 -> HR, SpO2.2 -> SpO2.
    import re

    base = re.sub(r"\.\d+$", "", column)
    base_norm = _normalise(base)
    for signal, cfg in configs.items():
        channels = [_normalise(c) for c in cfg.get("channels", [])]
        if norm in channels or base_norm in channels:
            return cfg
        if norm == _normalise(signal) or base_norm == _normalise(signal):
            return cfg
    return None


def _worse(current: str, candidate: str) -> str:
    if FLAG_PRIORITY.index(candidate) < FLAG_PRIORITY.index(current):
        return candidate
    return current


def apply_validity(series: pd.Series, cfg: dict,
                   missing_codes: list) -> tuple[pd.Series, np.ndarray]:
    """Range / zero / missing-code checks. Returns (cleaned, flags)."""
    flags = np.full(len(series), "VALID", dtype=object)
    out = pd.to_numeric(series, errors="coerce").astype(float)

    if missing_codes:
        is_code = out.isin(missing_codes)
        out = out.mask(is_code)
        flags[is_code.to_numpy()] = "MISSING"

    validity = cfg.get("validity", {}) or {}
    zero_is_valid = validity.get("zero_is_valid", False)
    if not zero_is_valid:
        is_zero = out.eq(0) & out.notna()
        out = out.mask(is_zero)
        flags[is_zero.to_numpy()] = np.array(
            [_worse(f, "INVALID_RANGE") for f in flags[is_zero.to_numpy()]])

    vrange = validity.get("range")
    if vrange:
        lo, hi = vrange
        bad = (out.lt(lo) | out.gt(hi)) & out.notna()
        out = out.mask(bad)
        bad_idx = bad.to_numpy()
        flags[bad_idx] = np.array(
            [_worse(f, "INVALID_RANGE") for f in flags[bad_idx]])
    return out, flags


def detect_flatline(series: pd.Series, fs_hz: float,
                    window_s: float = 2.0,
                    epsilon: float | None = None) -> np.ndarray:
    """Sliding variance below epsilon -> transducer disconnect / lead off."""
    n = max(int(window_s * fs_hz), 2)
    roll_std = series.rolling(n, center=True, min_periods=n).std()
    if epsilon is None:
        # Scale-free fallback: variance < 1e-9 of the series' own range.
        span = series.max() - series.min()
        epsilon = (span * 1e-6) if pd.notna(span) and span > 0 else 1e-9
    hit = (roll_std < epsilon) & series.notna()
    return hit.to_numpy()


def detect_spike(series: pd.Series, fs_hz: float, window_s: float = 10.0,
                 n_sigma: float = 5.0) -> np.ndarray:
    """Robust deviation vs local median (MAD scale). Context, not thresholds."""
    n = max(int(window_s * fs_hz), 3)
    med = series.rolling(n, center=True, min_periods=n).median()
    mad = (series - med).abs().rolling(n, center=True,
                                      min_periods=n).median()
    scale = mad * 1.4826
    with np.errstate(divide="ignore", invalid="ignore"):
        z = (series - med).abs() / scale
    hit = (z > n_sigma) & series.notna() & (scale > 0)
    return hit.fillna(False).to_numpy()


def detect_saturation(series: pd.Series,
                      min_consecutive: int = 5) -> np.ndarray:
    """Samples stuck at the ADC rail -> clipping."""
    hit = np.zeros(len(series), dtype=bool)
    vals = series.to_numpy()
    if len(vals) == 0:
        return hit
    rail_lo, rail_hi = np.nanmin(vals), np.nanmax(vals)
    if not np.isfinite(rail_lo):
        return hit
    at_rail = (vals == rail_lo) | (vals == rail_hi)
    run = 0
    for i, v in enumerate(at_rail):
        if v and np.isfinite(vals[i]):
            run += 1
            if run >= min_consecutive:
                hit[i - run + 1: i + 1] = True
        else:
            run = 0
    return hit


def detect_artifacts(series: pd.Series, fs_hz: float,
                     cfg: dict) -> np.ndarray:
    """Run the artifact detectors declared in the signal config."""
    flags = np.full(len(series), "VALID", dtype=object)
    for det in cfg.get("artifact_detection", []) or []:
        name = det.get("name", "")
        params = det.get("params", {}) or {}
        flag = det.get("flag", "DEVICE_ARTIFACT")
        hit = None
        if name == "flatline":
            hit = detect_flatline(series, fs_hz, **{
                k: v for k, v in params.items()
                if k in ("window_s", "epsilon")})
        elif name == "spike":
            hit = detect_spike(series, fs_hz, **{
                k: v for k, v in params.items()
                if k in ("window_s", "n_sigma")})
        elif name == "saturation_clipping":
            hit = detect_saturation(
                series,
                min_consecutive=int(params.get("min_consecutive", 5)))
        elif name == "out_of_range":
            continue  # handled by apply_validity
        if hit is not None:
            flags[hit] = np.array(
                [_worse(f, flag) for f in flags[hit]])
    return flags


def apply_filter(series: pd.Series, fs_hz: float,
                 cfg: dict) -> pd.Series:
    """Filter declared in the signal config. Waveforms and parameters differ."""
    spec = cfg.get("filter", {}) or {}
    method = spec.get("method", "none")
    params = spec.get("params", {}) or {}
    out = series.copy()

    if method in ("lowpass", "bandpass", "highpass"):
        from scipy.signal import butter, filtfilt

        nyq = fs_hz / 2.0
        if method == "lowpass":
            wn = float(params.get("high_hz", nyq * 0.9)) / nyq
            btype, wn_arg = "low", min(wn, 0.99)
        elif method == "highpass":
            wn = float(params.get("low_hz", 0.1)) / nyq
            btype, wn_arg = "high", max(wn, 0.01)
        else:
            wn_arg = [max(float(params.get("low_hz", 0.1)) / nyq, 0.01),
                      min(float(params.get("high_hz", nyq * 0.9)) / nyq, 0.99)]
            btype = "band"
        order = int(params.get("order", 4))
        b, a = butter(order, wn_arg, btype=btype)
        padlen = 3 * max(len(a), len(b))
        valid = out.notna().to_numpy()
        if valid.sum() > padlen + 1:
            # Filter contiguous valid segments so NaN gaps never smear.
            vals = out.to_numpy(dtype=float)
            idx = np.where(valid)[0]
            breaks = np.where(np.diff(idx) > 1)[0]
            segments = np.split(idx, breaks + 1)
            for seg in segments:
                if len(seg) > padlen + 1:
                    vals[seg] = filtfilt(b, a, vals[seg])
            out = pd.Series(vals, index=out.index)
    elif method == "robust_smoothing":
        # Hampel-style: rolling median clip, parameters only.
        window_s = float(params.get("window_s", 10.0))
        n_sigma = float(params.get("n_sigma", 3.0))
        n = max(int(window_s * fs_hz), 3)
        med = out.rolling(n, center=True, min_periods=1).median()
        mad = (out - med).abs().rolling(n, center=True,
                                        min_periods=1).median() * 1.4826
        # MAD collapses to 0 on constant stretches (e.g. a parameter stuck at
        # exactly 70.0). Fall back to a tiny relative scale so a genuine
        # spike is still clipped instead of dividing by zero.
        scale = mad.mask(mad.eq(0),
                         (med.abs() + 1.0) * 1e-9)
        with np.errstate(divide="ignore", invalid="ignore"):
            z = (out - med).abs() / scale
        out = out.mask(z > n_sigma, med)
    # method "none": exposure data and unfiltered signals pass through.
    return out


def flag_gaps(flags: np.ndarray, series: pd.Series, fs_hz: float,
              max_gap_s: float = 10.0) -> np.ndarray:
    """Missing runs longer than max_gap_s become GAP (not silent)."""
    is_nan = series.isna().to_numpy()
    if not is_nan.any():
        return flags
    max_n = max(int(max_gap_s * fs_hz), 1)
    run = 0
    for i, v in enumerate(is_nan):
        if v:
            run += 1
        else:
            if run >= max_n:
                for j in range(i - run, i):
                    flags[j] = _worse(flags[j], "GAP")
            run = 0
    if run >= max_n:
        for j in range(len(flags) - run, len(flags)):
            flags[j] = _worse(flags[j], "GAP")
    return flags


def process_series(column: str, series: pd.Series, fs_hz: float,
                   cfg: dict, missing_codes: list,
                   max_gap_s: float = 10.0) -> tuple[pd.Series, pd.Series]:
    """Full chain for one column. Returns (filtered, qc_flags)."""
    cleaned, flags = apply_validity(series, cfg, missing_codes)
    art = detect_artifacts(cleaned, fs_hz, cfg)
    for i in range(len(flags)):
        flags[i] = _worse(flags[i], art[i])
    filtered = apply_filter(cleaned, fs_hz, cfg)
    # Filtering never resurrects invalid samples: re-apply the NaN mask.
    filtered = filtered.mask(cleaned.isna())
    flags = flag_gaps(flags, cleaned, fs_hz, max_gap_s)
    qc = pd.Series(flags, index=series.index, name=f"{column}__qc")
    filtered.name = column
    return filtered, qc


def measure_fs_hz(time_index: pd.DatetimeIndex,
                  series: pd.Series) -> float | None:
    """Observed sampling rate of one column from its non-null timestamps."""
    ts = time_index[series.notna().to_numpy()]
    if len(ts) < 3:
        return None
    deltas = pd.Series(ts).diff().dt.total_seconds().dropna()
    deltas = deltas[deltas > 0]
    if deltas.empty:
        return None
    return float(1.0 / deltas.median())


def process_frame(df: pd.DataFrame, time_col: str | None,
                  configs: dict, missing_codes: list,
                  fs_overrides: dict | None = None,
                  source: str = "") -> tuple[pd.DataFrame, pd.DataFrame,
                                             list[dict]]:
    """Process every column. Returns (filtered, qc, review_queue).

    review_queue holds one entry per column with no dictionary entry:
    {source, column, n_rows, n_non_null}. Nothing is dropped, nothing is
    force-fit through another signal's filter.
    """
    fs_overrides = fs_overrides or {}
    filtered_cols: dict[str, pd.Series] = {}
    qc_cols: dict[str, pd.Series] = {}
    review_queue: list[dict] = []

    if time_col and time_col in df.columns:
        time_index = pd.to_datetime(df[time_col], errors="coerce")
    else:
        time_index = pd.DatetimeIndex(
            pd.to_datetime(df.index, errors="coerce"))

    for column in df.columns:
        if column == time_col:
            filtered_cols[column] = df[column]
            continue
        cfg = find_config(column, configs)
        if cfg is None:
            review_queue.append({
                "source": source,
                "column": column,
                "n_rows": int(len(df)),
                "n_non_null": int(df[column].notna().sum()),
                "reason": "no_dictionary_entry",
            })
            # Preserve the column untouched in the filtered output.
            filtered_cols[column] = df[column]
            qc_cols[f"{column}__qc"] = pd.Series(
                ["LOW_QUALITY"] * len(df), index=df.index,
                name=f"{column}__qc")
            continue
        series = pd.to_numeric(df[column], errors="coerce")
        fs = fs_overrides.get(column)
        if fs is None:
            fs = measure_fs_hz(time_index, series)
        if fs is None or fs <= 0:
            review_queue.append({
                "source": source,
                "column": column,
                "n_rows": int(len(df)),
                "n_non_null": int(series.notna().sum()),
                "reason": "sampling_rate_unknown",
            })
            filtered_cols[column] = df[column]
            qc_cols[f"{column}__qc"] = pd.Series(
                ["LOW_QUALITY"] * len(df), index=df.index,
                name=f"{column}__qc")
            continue
        if (cfg.get("status") or "").lower() == "draft":
            # Configs stay draft until K reviews the intervals.
            pass
        filt, qc = process_series(column, series, fs, cfg, missing_codes)
        filtered_cols[column] = filt
        qc_cols[qc.name] = qc

    filtered = pd.DataFrame(filtered_cols, index=df.index)
    qc_frame = pd.DataFrame(qc_cols, index=df.index)
    return filtered, qc_frame, review_queue


def write_review_queue(entries: list[dict], path: str | Path) -> None:
    """Append review entries as JSONL (privacy-safe: names and counts only)."""
    if not entries:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e) + "\n")


# ---------------------------------------------------------------------------
# Shared per-source driver (used by process_bettercare.py / process_infinity.py)
# ---------------------------------------------------------------------------

def _build_time_index(df: pd.DataFrame, time_col: str | None,
                      start_time: str | None) -> pd.DatetimeIndex:
    if time_col and time_col in df.columns:
        col = df[time_col]
        parsed = pd.to_datetime(col, errors="coerce")
        if parsed.notna().sum() >= len(col) // 2:
            return pd.DatetimeIndex(parsed)
        # Numeric timebase: elapsed units from the recording start.
        num = pd.to_numeric(col, errors="coerce")
        unit = "ms" if num.max() > 1e6 else "s"
        base = (pd.Timestamp(start_time, tz="UTC") if start_time
                else pd.Timestamp("2000-01-01", tz="UTC"))
        return pd.DatetimeIndex(
            base + pd.to_timedelta(num - num.min(), unit=unit))
    return pd.DatetimeIndex(pd.to_datetime(df.index, errors="coerce"))


def run_source_script(source: str, argv: list[str] | None = None) -> int:
    """CLI driver shared by the per-source processing scripts."""
    import argparse

    ap = argparse.ArgumentParser(
        description=f"Process {source} files with the signal dictionary "
                    "(raw is never modified; filtered + QC are written).")
    ap.add_argument("--input", required=True,
                    help="Standardized parquet or CSV to process")
    ap.add_argument("--output", required=True,
                    help="Filtered parquet output path")
    ap.add_argument("--qc-output", default="",
                    help="QC flag parquet output path (default: <output>.qc.parquet)")
    ap.add_argument("--review-queue", default="review_queue.jsonl",
                    help="JSONL file for columns with no dictionary entry")
    ap.add_argument("--config-dir", default="configs/signals",
                    help="Signal dictionary configs")
    ap.add_argument("--variables", default="profiles/_variables.yaml",
                    help="Ontology with units, ranges, missing codes")
    ap.add_argument("--time-col", default="",
                    help="Timestamp column (auto-detected if empty)")
    ap.add_argument("--start-time", default="",
                    help="ISO start time for numeric elapsed timebases")
    args = ap.parse_args(argv)

    configs = load_signal_configs(args.config_dir)
    drafts = [s for s, c in configs.items()
              if (c.get("status") or "").lower() == "draft"]
    if drafts:
        print(f"[warn] {len(drafts)} signal configs are still draft "
              f"({', '.join(sorted(drafts)[:8])}...); intervals await review.")

    with open(args.variables, encoding="utf-8") as fh:
        variables = yaml.safe_load(fh)
    missing_codes = (variables.get("global_missing_codes", []) or [])

    if args.input.lower().endswith(".parquet"):
        df = pd.read_parquet(args.input)
    else:
        df = pd.read_csv(args.input, low_memory=False)
    # Duplicate columns (HR, HR.1, ...) are preserved as-is; the engine
    # matches each to its signal config without collapsing them.
    print(f"[{source}] loaded {args.input}: {len(df)} rows, "
          f"{len(df.columns)} columns")

    time_col = args.time_col or None
    if time_col is None:
        for cand in ("timestamp", "Time (msecs)", "OBSERVATION_DATETIME",
                     "time"):
            if cand in df.columns:
                time_col = cand
                break
    df = df.copy()
    df["_time_index"] = _build_time_index(df, time_col, args.start_time or None)

    filtered, qc, review = process_frame(
        df.drop(columns=["_time_index"]),
        time_col="_time_index",
        configs=configs,
        missing_codes=missing_codes,
        source=source,
    )
    # Restore a readable timestamp column in the outputs.
    filtered.insert(0, "timestamp", df["_time_index"].to_numpy())
    qc.insert(0, "timestamp", df["_time_index"].to_numpy())

    filtered.to_parquet(args.output, index=False)
    qc_path = args.qc_output or (args.output + ".qc.parquet")
    qc.to_parquet(qc_path, index=False)
    write_review_queue(review, args.review_queue)

    n_flagged = int((qc.drop(columns=["timestamp"], errors="ignore")
                     != "VALID").any(axis=1).sum()) if len(qc.columns) > 1 else 0
    print(f"[{source}] wrote {args.output} ({len(filtered)} rows)")
    print(f"[{source}] wrote {qc_path}")
    print(f"[{source}] rows with any QC flag: {n_flagged}; "
          f"review queue entries: {len(review)}")
    for e in review:
        print(f"[{source}] REVIEW: {e['column']} ({e['reason']}, "
              f"n_non_null={e['n_non_null']})")
    return 0
