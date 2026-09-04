"""
Infusion pump history export, for example Perf_724723_History(Device)_260415-114542.csv

Event based rather than a regular time series, so it is parsed into events and
not into the signals table. Used by DEXREM, where the folder name carries the
drug (remi, propofol) and is preserved as recording metadata.
"""

from __future__ import annotations

import pandas as pd

from ._common import describe, sniff_delimiter


def sniff_header(path, profile, cfg) -> dict:
    """Cheap structural check for the pump export handler (event based)."""
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
    time_col = next((c for c in header
                     if any(k in c.lower() for k in ("time", "date", "heure"))), None)
    return {
        "readable": True, "error": None, "delimiter": delimiter,
        "header": header, "recognised": set(), "time_column": time_col,
        "data_rows": 1 if second.strip() else 0,
    }


def parse(path, profile, cfg):
    delimiter = cfg.get("delimiter") or sniff_delimiter(path)
    frame = pd.read_csv(path, sep=delimiter, encoding=cfg.get("encoding", "utf-8"),
                        low_memory=False, engine="python")
    frame.columns = [str(c).strip() for c in frame.columns]

    time_col = next((c for c in frame.columns
                     if any(k in c.lower() for k in ("time", "date", "heure"))), None)
    if time_col is None:
        return pd.DataFrame(), {"rows": 0, "kind": "event", "source_columns": {},
                                "note": "no recognisable time column"}

    ts = pd.to_datetime(frame[time_col], errors="coerce", dayfirst=True)
    frame = frame[ts.notna()].copy()
    frame.index = (ts[ts.notna()].dt.tz_localize(cfg.get("time", {}).get("timezone",
                                                                        "America/Montreal"),
                                                 ambiguous="NaT", nonexistent="NaT")
                   .dt.tz_convert("UTC"))
    frame = frame.sort_index()

    meta = describe(frame, {})
    meta["kind"] = "event"
    meta["event_columns"] = list(frame.columns)
    return frame, meta
