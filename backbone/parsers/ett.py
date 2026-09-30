"""
ETT / BioDASh (iKinesia) parser.

Delivery unit: one ``.parquet.zip`` per exam. Inside the zip a
Hive-partitioned dataset::

    time_bin=<n>/sensor=<s>/signal=<g>/*.parquet

Every partitioned file carries the same key-value metadata in its Parquet
footer -- there is no sidecar file. Identity is keyed on
``(session_guid, exam_guid)``, read from BOTH the zip filename and the
footer; a mismatch quarantines the exam.

Contract: ``parse(path, profile, device_cfg) -> (frame, meta)`` like every
other backbone parser, plus ``sniff_header``.

frame : long format, indexed by timezone aware UTC timestamp built from
        ``local_time``. Columns: sensor, signal, dimension, value,
        local_time, remote_time, received_time. Native sampling rates are
        preserved -- signals are never forced onto one common rate.
meta  : dict with at least {rows, duration_s, interval_s, source_columns}
        plus ``ett`` block: exam_guid, session_guid, ett_version,
        sampling_frequency, signal_type, sentinel_null_counts, manifest.

Privacy: footer metadata and filenames may carry patient IDs, staff free
text (annotation, form, exam_annotation) and local Windows paths. The
parser keeps them in ``meta`` for the internal catalog only. Use
``strip_identifying(meta)`` before anything leaves the controlled
environment; the website feed builder does this.
"""

from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd

# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------

# <PatientID>_<YYYY-MM-DD>_<HH-MM-SS>_<sessionGUID>_<examGUID>.parquet.zip
# PatientID may itself contain underscores; the date/time/guid tail is fixed.
FILENAME_RE = re.compile(
    r"^(?P<patient>.+)_(?P<date>\d{4}-\d{2}-\d{2})_(?P<time>\d{2}-\d{2}-\d{2})"
    r"_(?P<session_guid>[0-9a-fA-F]{8})_(?P<exam_guid>[0-9a-fA-F]{8})"
    r"\.parquet\.zip$"
)

# Draeger "lead not connected" sentinel. Must become null, with counts logged.
DRAEGER_SENTINEL = -32768

# Footer keys / frame columns that may identify a patient or a workstation.
# Never let these reach the website feed.
IDENTIFYING_KEYS = {
    "patient_id",
    "annotation",
    "form",
    "exam_annotation",
    "local_path",
    "exam_files",
    "exam_files_all",
    "ignored_files",
    "session_files",
}

# Nullable superset schema across projects. Columns vary by project; never
# assume one exists. __index_level_0__ is a pandas write artifact: ignore it.
EXPECTED_COLUMNS = {
    "local_time": "float64",
    "remote_time": "float64",
    "received_time": "float64",
    "value": "float64",
    "dimension": "string",
    "annotation": "string",
    "form": "string",
}


class ETTQuarantine(Exception):
    """Raised when an exam must be quarantined, not parsed.

    Attributes: reason (short code), detail (human explanation).
    The orchestrator catches this and moves the zip to quarantine/.
    """


def parse_filename(name: str) -> dict:
    """Split the delivery filename. Never derive timestamps from it."""
    match = FILENAME_RE.match(Path(name).name)
    if not match:
        raise ETTQuarantine(
            "bad_filename",
            f"filename does not match "
            f"<PatientID>_<YYYY-MM-DD>_<HH-MM-SS>_<sessionGUID>_<examGUID>.parquet.zip: {name!r}",
        )
    return match.groupdict()


def verify_zip(path: Path) -> list[str]:
    """CRC32-check every entry. Returns the member names."""
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad is not None:
                raise ETTQuarantine(
                    "crc32_failure",
                    f"CRC32 mismatch in zip member {bad!r}",
                )
            members = [n for n in zf.namelist() if n.endswith(".parquet")]
    except zipfile.BadZipFile as exc:
        raise ETTQuarantine("unreadable_zip", f"not a readable zip: {exc}") from exc
    if not members:
        raise ETTQuarantine("empty_zip", "zip contains no .parquet files")
    return members


def _read_footer_kv(path: Path, member: str) -> dict:
    """Read Parquet footer key-value metadata from one member."""
    import pyarrow.parquet as pq

    with zipfile.ZipFile(path) as zf:
        with zf.open(member) as fh:
            buf = io.BytesIO(fh.read())
        pf = pq.ParquetFile(buf)
        raw = pf.metadata.metadata or {}
    out = {}
    for k, v in raw.items():
        key = k.decode("utf-8", errors="replace") if isinstance(k, bytes) else str(k)
        val = v.decode("utf-8", errors="replace") if isinstance(v, bytes) else str(v)
        out[key] = val
    return out


def _partition_cols(member: str) -> dict:
    """time_bin=<n>/sensor=<s>/signal=<g>/file.parquet -> dict."""
    out = {}
    for part in Path(member).parts:
        if "=" in part:
            k, _, v = part.partition("=")
            if k in ("time_bin", "sensor", "signal"):
                out[k] = v
    return out


def _read_dataset(path: Path, members: list[str]) -> pd.DataFrame:
    """Read every leaf parquet, attach partition columns, concat.

    Uses a nullable superset schema: missing columns become null, extra
    columns are kept and reported as schema drift.
    """
    import pyarrow.parquet as pq

    frames = []
    seen_columns: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        for member in members:
            with zf.open(member) as fh:
                table = pq.read_table(io.BytesIO(fh.read()))
            df = table.to_pandas()
            seen_columns.update(df.columns)
            parts = _partition_cols(member)
            for k, v in parts.items():
                df[k] = v
            frames.append(df)

    frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    if "__index_level_0__" in frame.columns:
        frame = frame.drop(columns=["__index_level_0__"])

    for col, dtype in EXPECTED_COLUMNS.items():
        if col not in frame.columns:
            frame[col] = pd.NA
        try:
            if dtype == "float64":
                frame[col] = pd.to_numeric(frame[col], errors="coerce")
            else:
                frame[col] = frame[col].astype("string")
        except (ValueError, TypeError):
            pass

    drift = {
        "unexpected_columns": sorted(
            c
            for c in seen_columns
            if c not in EXPECTED_COLUMNS and c != "__index_level_0__"
        ),
        "missing_columns": sorted(c for c in EXPECTED_COLUMNS if c not in seen_columns),
    }
    frame.attrs["schema_drift"] = drift
    return frame


def _apply_sentinels(frame: pd.DataFrame) -> dict:
    """Map Draeger invalid sentinel -32768 to null. Returns per-signal counts."""
    counts: dict[str, int] = {}
    if frame.empty or "value" not in frame.columns:
        return counts
    mask = (frame["sensor"] == "draeger") & (frame["value"] == DRAEGER_SENTINEL)
    if mask.any():
        for signal, group in frame[mask].groupby("signal", observed=True):
            counts[str(signal)] = int(len(group))
        frame.loc[mask, "value"] = np.nan
    return counts


def _parse_kv_list(raw: str) -> dict:
    """Parse ['draeger_ecg=200', ...] style footer values into a dict."""
    out: dict[str, str] = {}
    text = (raw or "").strip().strip("[]")
    for item in text.split(","):
        item = item.strip().strip("'\"")
        if "=" in item:
            k, _, v = item.partition("=")
            out[k.strip()] = v.strip()
    return out


def parse(path, profile, device_cfg) -> tuple[pd.DataFrame, dict]:
    """Parse one ETT .parquet.zip delivery into (frame, meta)."""
    path = Path(path)
    if path.suffix == ".zip" and "".join(path.suffixes[-2:]) != ".parquet.zip":
        # tolerate being handed the zip directly; suffix check is advisory
        pass

    # --- integrity ---------------------------------------------------------
    members = verify_zip(path)

    # --- identity ----------------------------------------------------------
    fn = parse_filename(path.name)
    footer = _read_footer_kv(path, members[0])

    for key in ("exam_guid", "session_guid"):
        f_val = (footer.get(key) or "").strip().lower()
        n_val = (fn.get(key) or "").strip().lower()
        if f_val and n_val and f_val != n_val:
            raise ETTQuarantine(
                "guid_mismatch",
                f"{key}: filename has {fn[key]!r}, footer has {footer.get(key)!r}",
            )

    exam_guid = (footer.get("exam_guid") or fn["exam_guid"]).strip()
    session_guid = (footer.get("session_guid") or fn["session_guid"]).strip()
    patient_id = footer.get("patient_id", "").strip()
    ett_version = footer.get("ett_version", "").strip()

    # --- payload -----------------------------------------------------------
    frame = _read_dataset(path, members)

    # Sort by local_time: NOT monotonic at waveform batch boundaries
    # (reconstructed backwards, ~0.14 s steps back). Stable sort keeps the
    # vendor's within-batch order for exact ties.
    if not frame.empty:
        frame = frame.sort_values("local_time", kind="stable").reset_index(drop=True)

    sentinel_counts = _apply_sentinels(frame)

    # Canonical UTC index from local_time (float64 Unix seconds, UTC).
    if not frame.empty:
        ts = pd.to_datetime(frame["local_time"], unit="s", utc=True, errors="coerce")
        frame = frame.set_index(ts).rename_axis("timestamp")
        frame = frame.sort_index(kind="stable")

    sampling = _parse_kv_list(footer.get("sampling_frequency", ""))
    signal_type = _parse_kv_list(footer.get("signal_type", ""))

    def _f(key: str):
        try:
            return float(footer[key]) if footer.get(key) not in (None, "") else None
        except (ValueError, TypeError):
            return None

    def _iso(key: str):
        v = _f(key)
        if v is None:
            return None
        return pd.to_datetime(v, unit="s", utc=True).isoformat()

    n_signals = (
        int(frame["signal"].nunique()) if not frame.empty and "signal" in frame else 0
    )
    duration_s = None
    if not frame.empty and frame.index.notna().any():
        idx = frame.index.dropna()
        duration_s = float((idx.max() - idx.min()).total_seconds())

    source_columns = {c: c for c in frame.columns if c != "timestamp"}

    meta: dict = {
        "rows": int(len(frame)),
        "duration_s": duration_s,
        "interval_s": None,  # mixed native rates; per-signal rates in ett.sampling_frequency
        "source_columns": source_columns,
        "variables": sorted(str(c) for c in frame.columns),
        "n_partitions": len(members),
        "n_signals": n_signals,
        "ett": {
            "exam_guid": exam_guid,
            "session_guid": session_guid,
            "patient_id": patient_id,  # internal only; strip before export
            "ett_version": ett_version,
            "exam_start_time": _iso("exam_start_time"),
            "exam_stop_time": _iso("exam_stop_time"),
            "exam_duration": _f("exam_duration"),
            "recording_start_time": _iso("recording_start_time"),
            "recording_stop_time": _iso("recording_stop_time"),
            "recording_duration": _f("recording_duration"),
            "sampling_frequency": sampling,
            "signal_type": signal_type,
            "sensors_list": footer.get("sensors_list", ""),
            "sentinel_null_counts": sentinel_counts,
            "schema_drift": frame.attrs.get("schema_drift", {}),
            "filename_patient": fn["patient"],
        },
    }
    return frame, meta


def sniff_header(path, profile, cfg) -> dict:
    """Cheap structural check: zip of hive-partitioned parquet with footer."""
    path = Path(path)
    try:
        members = verify_zip(path)
    except ETTQuarantine as exc:
        return {
            "readable": False,
            "error": f"{exc.args[0]}: {exc.args[1] if len(exc.args) > 1 else ''}",
            "header": [],
            "recognised": set(),
            "time_column": "local_time",
            "data_rows": 0,
        }
    try:
        fn = parse_filename(path.name)
        footer = _read_footer_kv(path, members[0])
        keys = set(footer)
    except Exception as exc:  # noqa: BLE001
        return {
            "readable": False,
            "error": str(exc),
            "header": [],
            "recognised": set(),
            "time_column": "local_time",
            "data_rows": 0,
        }
    return {
        "readable": True,
        "error": None,
        "header": sorted(keys),
        "recognised": {"local_time", "value", "sensor", "signal"},
        "time_column": "local_time",
        "data_rows": len(members),
        "exam_guid": footer.get("exam_guid") or fn.get("exam_guid"),
        "session_guid": footer.get("session_guid") or fn.get("session_guid"),
    }


def strip_identifying(meta: dict) -> dict:
    """Return a copy of parser meta safe to leave the controlled environment."""
    import copy

    clean = copy.deepcopy(meta)
    ett = clean.get("ett", {})
    for key in IDENTIFYING_KEYS:
        ett.pop(key, None)
        clean.pop(key, None)
    # filename_patient can re-identify: drop it too
    ett.pop("filename_patient", None)
    clean["ett"] = ett
    return clean
