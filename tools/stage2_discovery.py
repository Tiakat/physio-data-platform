"""Stage 2, phases 1-7: dataset discovery, integrity, column-level data
dictionary, participant/demographic linkage assessment, sampling-rate
detection, missing-data measurement.

Reads the encrypted standardized parquets listed in the pipeline state
(rawdata/<CODE>/parquet/<device>_<sha>.parquet.enc), samples them, and
builds a column taxonomy per project:

    timestamp | identifier | demographic | metadata |
    signal | derived | qc | unknown

- ``signal`` = standard variables from profiles/_variables.yaml (unit and
  physiological range attached from the ontology).
- ``identifier`` matches are privacy-critical: counted, never printed with
  values; real column names stay encrypted.
- ``unknown`` columns go to an encrypted review queue for K — never plaintext.

Phases 6-7 MEASURE the data (per-signal time bases, missing-data
taxonomy). They never filter, smooth, impute, or modify anything.

Never touches Dropbox. Reads ``rawdata``, writes ``processed/stage2/``::

    dictionary_<ts>.json.enc    full column taxonomy (encrypted)
    digest_<ts>.json            counts + standard names only (plaintext)
    review_queue_<ts>.json.enc  unknown columns w/ real names (encrypted)

An identifier-class column inside a standardized parquet is a finding: it
is reported through the stage2 issue channel (label ``stage2``).
"""

from __future__ import annotations

import io
import json
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

STAGE2_PREFIX = "stage2"

# Token sets are matched against whole underscore-separated tokens of the
# normalised column name, so "filename" never matches "name".
_IDENTIFIER_TOKENS = {
    "patient", "subject", "mrn", "guid", "uuid", "dob", "ipp", "nhs",
    "firstname", "lastname", "fullname", "surname", "birthdate",
}
_DEMOGRAPHIC_TOKENS = {
    "age", "sex", "gender", "weight", "height", "bmi", "asa",
}
_METADATA_TOKENS = {
    "device", "file", "source", "version", "record", "recording",
    "samplerate", "sample_rate",
}
_QC_TOKENS = {
    "qc", "quality", "flag", "artifact", "artefact", "valid",
    "invalid", "excluded", "exclude",
}
_DERIVED_SUFFIXES = (
    "_filt", "_filtered", "_clean", "_cleaned", "_smooth", "_smoothed",
    "_derived", "_calc", "_calculated", "_norm", "_normalized",
)


def _normalise(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(name).lower()).strip("_")


def load_ontology(repo_root: str | Path) -> Dict[str, dict]:
    """Standard variable -> {label, unit, min, max, zero_is_valid}."""
    import yaml
    path = Path(repo_root) / "profiles" / "_variables.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return data.get("variables", {}) or {}


def classify_column(name: str, ontology: Dict[str, dict]) -> Tuple[str, dict]:
    """Return (class, detail) for one column name."""
    norm = _normalise(name)
    if norm in ontology or name in ontology:
        key = norm if norm in ontology else name
        spec = ontology[key]
        return ("signal", {"unit": spec.get("unit"),
                           "range": [spec.get("min"), spec.get("max")],
                           "label": spec.get("label")})
    tokens = set(norm.split("_"))
    if tokens & _IDENTIFIER_TOKENS:
        return ("identifier", {})
    if tokens & _DEMOGRAPHIC_TOKENS:
        return ("demographic", {})
    if tokens & _METADATA_TOKENS:
        return ("metadata", {})
    if norm.endswith(_DERIVED_SUFFIXES) or norm.startswith("derived_"):
        return ("derived", {})
    if tokens & _QC_TOKENS:
        return ("qc", {})
    if norm in ("timestamp", "__index_level_0__"):
        return ("timestamp", {})
    return ("unknown", {})


def _device_of(blob: str) -> str:
    stem = blob[: -len(".parquet.enc")] if blob.endswith(".parquet.enc") \
        else blob
    return stem.rsplit("_", 1)[0]


def _download_parquet(account: str, blob: str) -> bytes:
    from tools import azure_auth
    from tools.crypto import decrypt_bytes
    svc = azure_auth.get_blob_service_client(account)
    raw = svc.get_blob_client(
        container="rawdata", blob=blob).download_blob().readall()
    return decrypt_bytes(raw)


def discover_project(account: str, code: str, files: dict,
                     ontology: Dict[str, dict],
                     sample_n: int = 20,
                     time_range_n: int = 3) -> dict:
    """Sample a project's parquets; return taxonomy + integrity report."""
    import pyarrow.parquet as pq

    blobs = [f["stored"] for f in files.values()
             if isinstance(f, dict) and f.get("status") == "ok"
             and f.get("kind") == "parquet" and f.get("stored")]
    by_device: Dict[str, List[str]] = {}
    for b in blobs:
        by_device.setdefault(_device_of(b), []).append(b)

    # Round-robin across devices so every device's schema is seen.
    sampled: List[str] = []
    queues = {d: list(bs) for d, bs in by_device.items()}
    while len(sampled) < sample_n and any(queues.values()):
        for d in sorted(queues):
            if queues[d] and len(sampled) < sample_n:
                sampled.append(queues[d].pop(0))

    columns: Dict[str, dict] = {}
    col_files: Dict[str, set] = {}
    total_rows = 0
    empty_files = 0
    tmin: Optional[str] = None
    tmax: Optional[str] = None
    checked = 0
    errors: List[str] = []
    sampling_acc: Dict[str, list] = defaultdict(list)
    missing_acc: Dict[str, dict] = defaultdict(
        lambda: {"fracs": [], "longest": 0, "kinds": set(), "device": ""})
    measure_notes: List[str] = []

    for i, blob in enumerate(sampled):
        try:
            data = _download_parquet(account, blob)
            pf = pq.ParquetFile(io.BytesIO(data))
            # schema_arrow is a real pyarrow.Schema (.field() exists);
            # pf.schema is a ParquetSchema, which has no .field() method.
            arrow_schema = pf.schema_arrow
            names = arrow_schema.names
            total_rows += pf.metadata.num_rows
            if pf.metadata.num_rows == 0:
                empty_files += 1
            checked += 1
            for n in names:
                if n not in columns:
                    cls, detail = classify_column(n, ontology)
                    columns[n] = {
                        "class": cls, "detail": detail,
                        "arrow_type": str(arrow_schema.field(n).type),
                    }
                # Book-keeping only after the column is safely registered:
                # a failure above must never leave col_files/columns
                # inconsistent (that mismatch raised KeyError and killed
                # whole projects in the first production run).
                col_files.setdefault(n, set()).add(blob)
            if i < time_range_n:
                import pandas as pd
                idx = pd.read_parquet(io.BytesIO(data), columns=[]).index
                try:
                    lo, hi = idx.min(), idx.max()
                    lo_s, hi_s = str(lo), str(hi)
                    if tmin is None or lo_s < tmin:
                        tmin = lo_s
                    if tmax is None or hi_s > tmax:
                        tmax = hi_s
                except Exception:  # noqa: BLE001 -- non-datetime index
                    pass
            # Phases 6-7: read-only measurement on a capped slice.
            # Never breaks discovery; problems go to measure_notes.
            if i < _MEASURE_N:
                _measure_blob(pf, names, ontology, _device_of(blob),
                              sampling_acc, missing_acc, measure_notes)
        except Exception as exc:  # noqa: BLE001 -- record, keep going
            errors.append(f"{blob}: {str(exc)[:120]}")

    for n, seen in col_files.items():
        columns[n]["files_seen_in"] = len(seen)

    drift = sorted(n for n, seen in col_files.items() if len(seen) < checked)
    by_class: Dict[str, int] = {}
    for c in columns.values():
        by_class[c["class"]] = by_class.get(c["class"], 0) + 1

    sampling, missingness = _summarise_measurement(sampling_acc, missing_acc)

    return {
        "n_parquet_files": len(blobs),
        "sampled": checked,
        "devices": sorted(by_device),
        "total_rows_sampled": total_rows,
        "empty_files_in_sample": empty_files,
        "n_columns": len(columns),
        "by_class": by_class,
        "signals": sorted(n for n, c in columns.items()
                          if c["class"] == "signal"),
        "schema_drift": drift,
        "time_range": [tmin, tmax],
        "sampling": sampling,
        "missingness": missingness,
        "measure_notes": measure_notes,
        "errors": errors,
        "columns": columns,
    }


# ---- Phases 6-7: read-only measurement (sampling rate, missing data) ----
# These MEASURE the data; they never filter, smooth, impute, or modify it.
_MEASURE_N = 5          # files per project measured in depth
_MEASURE_ROWS = 200_000  # row cap per measured file (streamed batches)
_MEASURE_COLS = 12       # signal-column cap per measured file
_MEASURE_CLASSES = {"signal", "derived"}
_MISSING_ORDER = ["complete", "sparse_isolated", "gappy", "high_missing",
                  "entirely_missing"]


def _measure_timebase(df, ts_col):
    """Phase 6: sampling-rate detection from a timestamp column.

    Read-only. dt = t[i+1]-t[i] on raw order; nominal rate = 1/median(dt).
    Reports nominal vs observed, irregularity, disorder (non-positive dt)
    and gap stats. Returns None when the column is not interpretable
    as time. Never raises.
    """
    import numpy as np
    import pandas as pd
    try:
        s = df[ts_col]
        if pd.api.types.is_datetime64_any_dtype(s.dtype):
            s = s.dropna()
            if len(s) < 3:
                return None
            vals = s.astype("int64").to_numpy() / 1e9
            unit = "datetime64"
        else:
            vals = pd.to_numeric(s, errors="coerce").to_numpy(dtype=float)
            vals = vals[np.isfinite(vals)]
            if len(vals) < 3:
                return None
            med_abs = float(np.median(np.abs(vals)))
            if med_abs > 1e11:        # epoch milliseconds
                vals = vals / 1000.0
                unit = "epoch_ms(inferred)"
            elif med_abs > 1e7:      # epoch seconds
                unit = "epoch_s(inferred)"
            else:                    # elapsed seconds
                unit = "elapsed_s(inferred)"
        dt = np.diff(vals)
        if len(dt) == 0:
            return None
        pos = dt[dt > 0]
        disorder = float(np.mean(dt <= 0))
        if len(pos) < 2:
            return {"time_unit": unit,
                    "disorder_fraction": round(disorder, 4),
                    "note": "too few positive diffs"}
        med = float(np.median(pos))
        gaps = pos[pos > 10 * med]
        return {
            "time_unit": unit,
            "median_dt_s": round(med, 6),
            "min_dt_s": round(float(pos.min()), 6),
            "max_dt_s": round(float(pos.max()), 6),
            "nominal_hz": round(1.0 / med, 3) if med > 0 else None,
            "irregularity": round(float(np.mean(np.abs(pos - med)
                                                > 0.5 * med)), 4),
            "disorder_fraction": round(disorder, 4),
            "gap_fraction": round(len(gaps) / len(pos), 4),
            "total_gap_time_s": round(float(gaps.sum()), 1),
            "n_diffs": int(len(pos)),
        }
    except Exception:  # noqa: BLE001 -- measurement never breaks discovery
        return None


def _measure_missingness(df, columns):
    """Phase 7: per-column missing-data measurement. Read-only.

    Kinds follow the pipeline taxonomy: entirely_missing (type 4),
    high_missing (candidate structural, type 5), gappy (types 2-3),
    sparse_isolated (type 1), complete. Never raises.
    """
    import numpy as np
    out = {}
    for col in columns:
        try:
            m = df[col].isna().to_numpy()
        except Exception:  # noqa: BLE001
            continue
        n = len(m)
        if n == 0:
            continue
        miss = float(m.mean())
        longest = 0
        if m.any() and not m.all():
            code = np.diff(np.concatenate([[0], m.astype(np.int8), [0]]))
            starts = np.where(code == 1)[0]
            ends = np.where(code == -1)[0]
            if len(starts) and len(ends):
                longest = int((ends - starts).max())
        if miss >= 1.0:
            kind = "entirely_missing"
        elif miss > 0.5:
            kind = "high_missing"
        elif longest >= 10:
            kind = "gappy"
        elif miss > 0:
            kind = "sparse_isolated"
        else:
            kind = "complete"
        out[col] = {"missing_frac": round(miss, 4),
                    "longest_gap_run": longest, "kind": kind}
    return out


def _measure_blob(pf, names, ontology, device, sampling_acc, missing_acc,
                  notes):
    """Phases 6+7 on one parquet: streamed, row- and column-capped.

    Never raises; any problem is recorded in notes and discovery continues.
    """
    import pandas as pd
    try:
        ts_col = None
        measure_cols = []
        for n in names:
            cls, _ = classify_column(n, ontology)
            if cls == "timestamp" and ts_col is None:
                ts_col = n
            elif cls in _MEASURE_CLASSES and len(measure_cols) < _MEASURE_COLS:
                measure_cols.append(n)
        cols = ([ts_col] if ts_col else []) + measure_cols
        if not cols:
            return
        batches = []
        n_rows = 0
        for batch in pf.iter_batches(batch_size=50_000, columns=cols):
            batches.append(batch.to_pandas())
            n_rows += batch.num_rows
            if n_rows >= _MEASURE_ROWS:
                break
        if not batches:
            return
        df = pd.concat(batches, ignore_index=True)
        if ts_col is not None:
            tb = _measure_timebase(df, ts_col)
            if tb:
                sampling_acc[device].append(tb)
            else:
                notes.append(f"{device}: time column not interpretable")
        else:
            notes.append(f"{device}: no timestamp column in measured sample")
        if measure_cols:
            for col, m in _measure_missingness(df, measure_cols).items():
                acc = missing_acc[col]
                acc["fracs"].append(m["missing_frac"])
                acc["longest"] = max(acc["longest"], m["longest_gap_run"])
                acc["kinds"].add(m["kind"])
                acc["device"] = device
    except Exception as exc:  # noqa: BLE001
        notes.append(f"{device}: measurement skipped ({str(exc)[:80]})")


def _summarise_measurement(sampling_acc, missing_acc):
    """Aggregate per-blob measurements into the project report."""
    import numpy as np
    sampling = {}
    for device, tbs in sampling_acc.items():
        meds = [t["median_dt_s"] for t in tbs if t.get("median_dt_s")]
        hzs = [t["nominal_hz"] for t in tbs if t.get("nominal_hz")]
        hz_set = {round(h, 1) for h in hzs}
        sampling[device] = {
            "n_files_measured": len(tbs),
            "median_dt_s": (round(float(np.median(meds)), 6)
                            if meds else None),
            "nominal_hz_range": ([round(min(hzs), 3), round(max(hzs), 3)]
                                 if hzs else None),
            "mixed_time_bases": len(hz_set) > 1,
            "mean_irregularity": (round(float(np.mean(
                [t["irregularity"] for t in tbs
                 if "irregularity" in t])), 4) if tbs else None),
            "mean_gap_fraction": (round(float(np.mean(
                [t["gap_fraction"] for t in tbs
                 if "gap_fraction" in t])), 4) if tbs else None),
        }
    missingness = {}
    for col, acc in missing_acc.items():
        kinds = [k for k in _MISSING_ORDER if k in acc["kinds"]]
        worst = kinds[-1] if kinds else "complete"
        missingness[col] = {
            "device": acc["device"],
            "mean_missing_frac": (round(sum(acc["fracs"]) / len(acc["fracs"]), 4)
                                  if acc["fracs"] else None),
            "max_longest_gap_run": acc["longest"],
            "worst_kind": worst,
        }
    return sampling, missingness


def assess_linkage(report: dict) -> dict:
    """Phase 4: where does participant linkage live? Never the linkage."""
    has_identifiers = report["by_class"].get("identifier", 0) > 0
    return {
        # Blob names carry no patient codes by design; per-file linkage
        # lives only in the encrypted per-chunk catalogs (catalog_*.json.enc).
        "path": "encrypted catalog only",
        "identifier_columns_in_parquet": report["by_class"].get(
            "identifier", 0),
        "finding": ("identifier-class columns inside standardized parquet — "
                    "review before any de-identified release"
                    if has_identifiers else None),
    }


def run_discovery(account: str, state: dict, repo_root: str | Path,
                  put_encrypted: Callable[[str, bytes], None],
                  put_plain: Callable[[str, bytes], None],
                  sample_n: int = 20) -> dict:
    """Phases 1-7 for every project with parquet in state. Never raises."""
    from tools.crypto import encrypt_bytes

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    ontology = load_ontology(repo_root)
    print(f"[stage2] ontology: {len(ontology)} standard variables", flush=True)

    dictionary: Dict[str, dict] = {}
    review: Dict[str, List[str]] = {}
    digest_projects: Dict[str, dict] = {}
    issue_lines: List[str] = []

    legacy = state.get("legacy", {}) or {}
    for code in sorted(legacy):
        files = legacy[code].get("files") or {}
        n_pq = sum(1 for f in files.values()
                   if isinstance(f, dict) and f.get("status") == "ok"
                   and f.get("kind") == "parquet")
        if not n_pq:
            continue
        try:
            rep = discover_project(account, code, files, ontology,
                                   sample_n=sample_n)
        except Exception as exc:  # noqa: BLE001
            print(f"[stage2] {code}: discovery failed: {exc}", flush=True)
            continue
        linkage = assess_linkage(rep)
        rep["linkage"] = linkage
        dictionary[code] = rep
        unknowns = sorted(n for n, c in rep["columns"].items()
                          if c["class"] == "unknown")
        if unknowns:
            review[code] = unknowns
        digest_projects[code] = {
            "n_parquet_files": rep["n_parquet_files"],
            "sampled": rep["sampled"],
            "devices": rep["devices"],
            "total_rows_sampled": rep["total_rows_sampled"],
            "n_columns": rep["n_columns"],
            "by_class": rep["by_class"],
            "signals": rep["signals"],
            # Per-signal aggregates for the website feed (standard names +
            # counts/fractions only — no patient data, safe for plaintext).
            "signal_detail": {
                name: {
                    "files_seen": rep["columns"][name].get("files_seen_in", 0),
                    "mean_missing_frac": (rep["missingness"].get(name)
                                          or {}).get("mean_missing_frac"),
                    "worst_kind": (rep["missingness"].get(name)
                                   or {}).get("worst_kind"),
                }
                for name in rep["signals"]
            },
            "n_unknown": len(unknowns),
            "schema_drift": len(rep["schema_drift"]),
            "time_range": rep["time_range"],
            "linkage_path": linkage["path"],
            "sampling": {
                dev: {"n_files_measured": s["n_files_measured"],
                      "nominal_hz_range": s["nominal_hz_range"],
                      "mixed_time_bases": s["mixed_time_bases"],
                      "mean_gap_fraction": s["mean_gap_fraction"]}
                for dev, s in rep["sampling"].items()},
            "missingness_kinds": {
                kind: sum(1 for m in rep["missingness"].values()
                          if m["worst_kind"] == kind)
                for kind in _MISSING_ORDER},
        }
        if linkage["finding"]:
            issue_lines.append(f"- {code}: {linkage['finding']}")
        for dev, s in rep["sampling"].items():
            if s["mixed_time_bases"]:
                issue_lines.append(
                    f"- {code}: device {dev} shows mixed time bases "
                    f"(nominal Hz range {s['nominal_hz_range']}) — "
                    f"one file != one sampling rate")
        for col, m in rep["missingness"].items():
            if m["worst_kind"] == "entirely_missing":
                issue_lines.append(
                    f"- {code}: column {col} entirely missing in measured "
                    f"sample ({m['device']})")
        print(f"[stage2] {code}: {rep['n_parquet_files']} parquets, "
              f"{rep['n_columns']} columns {rep['by_class']}, "
              f"{len(unknowns)} unknown", flush=True)

    digest = {"generated_utc": ts, "projects": digest_projects,
              "review_queue_size": sum(len(v) for v in review.values())}
    # Guard: never write a silently empty digest. If projects have parquet
    # files but discovery produced nothing, that is a broken run, not a
    # clean one — say so loudly and fail the job in main().
    n_expected = sum(
        1 for code in legacy
        if sum(1 for f in (legacy[code].get("files") or {}).values()
               if isinstance(f, dict) and f.get("status") == "ok"
               and f.get("kind") == "parquet"))
    empty_guard_fired = bool(n_expected and not digest_projects)
    if empty_guard_fired:
        msg = (f"no projects discovered although {n_expected} have parquet "
               f"files — discovery is broken, do not trust this digest")
        issue_lines.append(f"- {msg}")
        print(f"[stage2] GUARD: {msg}", flush=True)
    put_plain(f"{STAGE2_PREFIX}/digest_{ts}.json",
              json.dumps(digest, indent=1).encode("utf-8"))
    put_encrypted(f"{STAGE2_PREFIX}/dictionary_{ts}.json.enc",
                  encrypt_bytes(json.dumps(dictionary, indent=1).encode("utf-8")))
    if review:
        put_encrypted(f"{STAGE2_PREFIX}/review_queue_{ts}.json.enc",
                      encrypt_bytes(json.dumps(review, indent=1).encode("utf-8")))
        print(f"[stage2] review queue: {digest['review_queue_size']} unknown "
              f"columns need classification", flush=True)
    return {"digest": digest, "issue_lines": issue_lines,
            "empty_guard_fired": empty_guard_fired}


def main() -> int:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import azure_auth
    from tools.crypto import encrypt_bytes
    from tools.daily_pipeline import ACCOUNT, PROCESSED, load_state

    repo_root = Path(__file__).resolve().parents[1]
    state = load_state(ACCOUNT)
    azure_auth.ensure_container(
        azure_auth.get_blob_service_client(ACCOUNT), PROCESSED)

    def _blob(name: str):
        return azure_auth.get_blob_service_client(ACCOUNT).get_blob_client(
            container=PROCESSED, blob=name)

    result = run_discovery(
        ACCOUNT, state, repo_root,
        put_encrypted=lambda name, data: _blob(name).upload_blob(
            data, overwrite=True, metadata={"enc": "fernet"}),
        put_plain=lambda name, data: _blob(name).upload_blob(
            data, overwrite=True),
        sample_n=int(os.getenv("STAGE2_SAMPLE_N", "20")),
    )

    # Issue channel for findings needing K (same pattern as the supervisor).
    lines = result["issue_lines"]
    if lines:
        outdir = Path("stage2_out")
        outdir.mkdir(exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
        body = ("Bonjour K,\n\nStage 2 discovery found items needing review "
                "(nothing was changed):\n\n" + "\n".join(lines) +
                "\n\n— stage 2 discovery (read-only)")
        (outdir / "body.txt").write_text(body, encoding="utf-8")
        (outdir / "subject.txt").write_text(
            f"[Stage2] discovery findings {ts}", encoding="utf-8")
        print(f"[stage2] {len(lines)} findings -> stage2_out/body.txt",
              flush=True)
    print("[stage2] done", flush=True)
    if result.get("empty_guard_fired"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
