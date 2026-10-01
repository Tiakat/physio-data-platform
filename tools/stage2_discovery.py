"""Stage 2, phases 1-4: dataset discovery, integrity, column-level data
dictionary, participant/demographic linkage assessment.

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

    for i, blob in enumerate(sampled):
        try:
            data = _download_parquet(account, blob)
            pf = pq.ParquetFile(io.BytesIO(data))
            names = [f.name for f in pf.schema]
            total_rows += pf.metadata.num_rows
            if pf.metadata.num_rows == 0:
                empty_files += 1
            checked += 1
            for n in names:
                col_files.setdefault(n, set()).add(blob)
                if n not in columns:
                    cls, detail = classify_column(n, ontology)
                    columns[n] = {
                        "class": cls, "detail": detail,
                        "arrow_type": str(pf.schema.field(n).type),
                    }
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
        except Exception as exc:  # noqa: BLE001 -- record, keep going
            errors.append(f"{blob}: {str(exc)[:120]}")

    for n, seen in col_files.items():
        columns[n]["files_seen_in"] = len(seen)

    drift = sorted(n for n, seen in col_files.items() if len(seen) < checked)
    by_class: Dict[str, int] = {}
    for c in columns.values():
        by_class[c["class"]] = by_class.get(c["class"], 0) + 1

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
        "errors": errors,
        "columns": columns,
    }


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
    """Phases 1-4 for every project with parquet in state. Never raises."""
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
            "n_unknown": len(unknowns),
            "schema_drift": len(rep["schema_drift"]),
            "time_range": rep["time_range"],
            "linkage_path": linkage["path"],
        }
        if linkage["finding"]:
            issue_lines.append(f"- {code}: {linkage['finding']}")
        print(f"[stage2] {code}: {rep['n_parquet_files']} parquets, "
              f"{rep['n_columns']} columns {rep['by_class']}, "
              f"{len(unknowns)} unknown", flush=True)

    digest = {"generated_utc": ts, "projects": digest_projects,
              "review_queue_size": sum(len(v) for v in review.values())}
    put_plain(f"{STAGE2_PREFIX}/digest_{ts}.json",
              json.dumps(digest, indent=1).encode("utf-8"))
    put_encrypted(f"{STAGE2_PREFIX}/dictionary_{ts}.json.enc",
                  encrypt_bytes(json.dumps(dictionary, indent=1).encode("utf-8")))
    if review:
        put_encrypted(f"{STAGE2_PREFIX}/review_queue_{ts}.json.enc",
                      encrypt_bytes(json.dumps(review, indent=1).encode("utf-8")))
        print(f"[stage2] review queue: {digest['review_queue_size']} unknown "
              f"columns need classification", flush=True)
    return {"digest": digest, "issue_lines": issue_lines}


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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
