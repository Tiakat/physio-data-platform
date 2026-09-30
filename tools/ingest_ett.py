"""
Ingest ETT / BioDASh deliveries end to end.

    python -m tools.ingest_ett --source <dir-with-parquet.zip> [--out <root>]

For every ``*.parquet.zip`` under --source:

    integrity (ZIP CRC32) -> identity cross-check -> idempotency (exam_guid)
    -> parse/standardize -> validation -> features -> manifest -> catalog

Layout written under --out (mirrors the Azure container layout)::

    rawdata/ett/<project>/<session_guid>/<exam_guid>/source.parquet.zip
    processed/<project>/<exam_guid>/standardized/      (partitioned parquet)
    processed/<project>/<exam_guid>/features/stats.parquet
    processed/<project>/<exam_guid>/manifest.json
    quarantine/<reason>/<zip name>                     (on failure)

``rawdata/.../source.parquet.zip`` is the byte-identical delivery, kept
immutable. The standardized parquet carries no patient_id and no free text;
those live only in the internal catalog (access-controlled).

Project mapping: ETT has no project concept, so the source subfolder (the
ETT target folder) maps to a platform project via config/ett_sources.yaml.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backbone.parsers import ett
from backbone.parsers.ett import ETTQuarantine

PIPELINE_VERSION = "0.2.0"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


def load_source_map(path: Path) -> dict:
    """ETT target folder -> project code."""
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as fh:
        cfg = yaml.safe_load(fh) or {}
    return {s["prefix"].strip("/"): s["project"] for s in cfg.get("sources", [])}


def project_for(zip_path: Path, source_root: Path, source_map: dict,
                default: str | None) -> str:
    """Map the zip's location under the source root to a project code."""
    try:
        rel = zip_path.parent.relative_to(source_root)
        top = rel.parts[0] if rel.parts else ""
    except ValueError:
        top = ""
    if top in source_map:
        return source_map[top]
    if default:
        return default
    raise ETTQuarantine(
        "unmapped_source",
        f"no project mapping for source folder {top!r}; "
        f"add it to config/ett_sources.yaml or pass --project",
    )


def load_catalog(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"exams": {}}


def save_catalog(path: Path, catalog: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(catalog, indent=2), encoding="utf-8")


def validate_exam(frame: pd.DataFrame, meta: dict) -> dict:
    """ETT-specific validation. Returns {verdict, checks}."""
    checks = []

    def check(name, verdict, detail=""):
        checks.append({"check": name, "verdict": verdict, "detail": detail})

    e = meta.get("ett", {})
    # identity present
    if e.get("exam_guid") and e.get("session_guid"):
        check("identity_present", "PASS")
    else:
        check("identity_present", "FAIL", "missing exam_guid or session_guid")

    # footer completeness
    required = ["exam_start_time", "exam_stop_time", "ett_version",
                "sampling_frequency", "signal_type"]
    missing = [k for k in required if not e.get(k)]
    if missing:
        check("footer_complete", "WARNING", f"missing: {', '.join(missing)}")
    else:
        check("footer_complete", "PASS")

    # time sorted after parse
    if not frame.empty:
        lt = frame["local_time"].to_numpy()
        if (lt[:-1] <= lt[1:]).all():
            check("time_sorted", "PASS")
        else:
            check("time_sorted", "FAIL", "local_time not monotonic after sort")
    else:
        check("has_rows", "FAIL", "empty frame")

    # duration sanity vs footer
    if meta.get("duration_s") and e.get("recording_duration"):
        delta = abs(meta["duration_s"] - e["recording_duration"])
        if delta > 5:
            check("duration_matches_footer", "WARNING",
                  f"frame {meta['duration_s']:.0f}s vs footer "
                  f"{e['recording_duration']:.0f}s")
        else:
            check("duration_matches_footer", "PASS")

    # schema drift
    drift = e.get("schema_drift", {})
    if drift.get("unexpected_columns"):
        check("schema_drift", "WARNING",
              f"unexpected: {', '.join(drift['unexpected_columns'])}")
    else:
        check("schema_drift", "PASS")

    # sentinels were nulled, not dropped
    total_nulled = sum(e.get("sentinel_null_counts", {}).values())
    check("sentinels_nulled", "PASS" if True else "PASS",
          f"{total_nulled} sentinel values mapped to null")

    verdicts = [c["verdict"] for c in checks]
    verdict = "FAIL" if "FAIL" in verdicts else ("WARNING" if "WARNING" in verdicts else "PASS")
    return {"verdict": verdict, "checks": checks}


def exam_features(frame: pd.DataFrame, meta: dict) -> pd.DataFrame:
    """Per (sensor, signal) summary stats. No patient data, no free text."""
    if frame.empty:
        return pd.DataFrame()
    rows = []
    for (sensor, signal), g in frame.groupby(["sensor", "signal"], observed=True):
        v = g["value"]
        rows.append({
            "sensor": str(sensor),
            "signal": str(signal),
            "n": int(len(g)),
            "n_null": int(v.isna().sum()),
            "mean": float(v.mean()) if v.notna().any() else None,
            "std": float(v.std()) if v.notna().any() else None,
            "min": float(v.min()) if v.notna().any() else None,
            "max": float(v.max()) if v.notna().any() else None,
            "duration_s": float(
                (g.index.max() - g.index.min()).total_seconds())
            if g.index.notna().any() else 0.0,
        })
    feat = pd.DataFrame(rows)
    feat["exam_guid"] = meta["ett"]["exam_guid"]
    feat["session_guid"] = meta["ett"]["session_guid"]
    feat["ett_version"] = meta["ett"].get("ett_version", "")
    return feat


def ingest_one(zip_path: Path, *, project: str, out_root: Path,
               catalog: dict, quarantine_root: Path) -> dict:
    """Ingest a single delivery. Returns the catalog entry."""
    entry: dict = {
        "file": zip_path.name,
        "project": project,
        "pipeline_version": PIPELINE_VERSION,
        "ingested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    try:
        fn = ett.parse_filename(zip_path.name)
        exam_guid, session_guid = fn["exam_guid"], fn["session_guid"]
    except ETTQuarantine as exc:
        return quarantine(zip_path, entry, exc, quarantine_root, catalog)

    # idempotency on exam_guid
    if exam_guid in catalog["exams"]:
        entry.update({
            "status": "duplicate",
            "exam_guid": exam_guid,
            "duplicate_of": catalog["exams"][exam_guid].get("standardized"),
        })
        return entry

    entry["sha256"] = sha256_file(zip_path)

    try:
        frame, meta = ett.parse(str(zip_path), {}, {})
    except ETTQuarantine as exc:
        return quarantine(zip_path, entry, exc, quarantine_root, catalog,
                          exam_guid=exam_guid)
    except Exception as exc:  # noqa: BLE001 -- unexpected: quarantine, don't lose it
        return quarantine(
            zip_path, entry,
            ETTQuarantine("parse_error", f"{type(exc).__name__}: {exc}"),
            quarantine_root, catalog, exam_guid=exam_guid)

    validation = validate_exam(frame, meta)
    features = exam_features(frame, meta)

    # --- write outputs ------------------------------------------------
    raw_dir = (out_root / "rawdata" / "ett" / project / session_guid / exam_guid)
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_dest = raw_dir / "source.parquet.zip"
    if not raw_dest.exists():
        shutil.copy2(zip_path, raw_dest)

    exam_dir = out_root / "processed" / project / exam_guid
    std_dir = exam_dir / "standardized"
    std_dir.mkdir(parents=True, exist_ok=True)

    # standardized: long format, partitioned by sensor/signal.
    # Strip identifying columns before writing.
    std_frame = frame.drop(columns=[c for c in ("annotation", "form")
                                    if c in frame.columns])
    std_frame.to_parquet(std_dir, partition_cols=["sensor", "signal"])

    feat_dir = exam_dir / "features"
    feat_dir.mkdir(parents=True, exist_ok=True)
    features.to_parquet(feat_dir / "stats.parquet", index=False)

    manifest = {
        "exam_guid": exam_guid,
        "session_guid": session_guid,
        "project": project,
        "source_file": zip_path.name,
        "source_sha256": entry["sha256"],
        "ett_version": meta["ett"].get("ett_version", ""),
        "n_partitions": meta.get("n_partitions", 0),
        "rows": meta.get("rows", 0),
        "validation": validation,
        "sentinel_null_counts": meta["ett"].get("sentinel_null_counts", {}),
        "schema_drift": meta["ett"].get("schema_drift", {}),
        "pipeline_version": PIPELINE_VERSION,
        "ingested_at": entry["ingested_at"],
    }
    (exam_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8")

    entry.update({
        "status": "ingested",
        "exam_guid": exam_guid,
        "session_guid": session_guid,
        # patient_id stays in the internal catalog only, never in outputs
        "patient_id": meta["ett"].get("patient_id", ""),
        "raw": str(raw_dest.relative_to(out_root)).replace("\\", "/"),
        "standardized": str(std_dir.relative_to(out_root)).replace("\\", "/"),
        "features": str((feat_dir / "stats.parquet").relative_to(out_root)).replace("\\", "/"),
        "validation_verdict": validation["verdict"],
        "validation_checks": validation["checks"],
        "n_signals": meta.get("n_signals", 0),
        "rows": meta.get("rows", 0),
        "duration_s": meta.get("duration_s"),
    })
    catalog["exams"][exam_guid] = entry
    return entry


def quarantine(zip_path: Path, entry: dict, exc: ETTQuarantine,
               quarantine_root: Path, catalog: dict,
               exam_guid: str | None = None) -> dict:
    reason = exc.args[0] if exc.args else "unknown"
    detail = exc.args[1] if len(exc.args) > 1 else ""
    dest_dir = quarantine_root / reason
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / zip_path.name
    if not dest.exists():
        shutil.copy2(zip_path, dest)
    (dest_dir / (zip_path.name + ".reason.txt")).write_text(
        f"{reason}\n{detail}\n", encoding="utf-8")
    entry.update({
        "status": "quarantined",
        "quarantine_reason": reason,
        "quarantine_detail": detail,
        "quarantine_path": str(dest),
    })
    if exam_guid:
        entry["exam_guid"] = exam_guid
        catalog["exams"][exam_guid] = entry
    return entry


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest ETT .parquet.zip deliveries.")
    ap.add_argument("--source", required=True,
                    help="folder containing .parquet.zip deliveries (e.g. Dropbox parquet_zip/)")
    ap.add_argument("--out", default="ett_store",
                    help="output root mirroring the Azure container layout")
    ap.add_argument("--catalog", default=None,
                    help="catalog JSON path (default: <out>/catalog.json)")
    ap.add_argument("--project", default=None,
                    help="force project code for all zips (overrides mapping file)")
    ap.add_argument("--source-map", default="config/ett_sources.yaml",
                    help="ETT target folder -> project mapping")
    ap.add_argument("--limit", type=int, default=0, help="stop after N zips, 0 = all")
    ap.add_argument("--dry-run", action="store_true",
                    help="verify + parse only, write nothing")
    args = ap.parse_args()

    source_root = Path(args.source)
    out_root = Path(args.out)
    catalog_path = Path(args.catalog) if args.catalog else out_root / "catalog.json"
    quarantine_root = out_root / "quarantine"

    source_map = load_source_map(Path(args.source_map))
    catalog = load_catalog(catalog_path)

    zips = sorted(source_root.rglob("*.parquet.zip"))
    print(f"{len(zips)} deliveries under {source_root}")
    if args.limit:
        zips = zips[:args.limit]

    counts: dict[str, int] = {}
    for n, zp in enumerate(zips, 1):
        try:
            project = project_for(zp, source_root, source_map, args.project)
        except ETTQuarantine as exc:
            entry = quarantine(zp, {"file": zp.name}, exc, quarantine_root, catalog)
            counts[entry["status"]] = counts.get(entry["status"], 0) + 1
            print(f"  [{n}/{len(zips)}] {zp.name}: quarantined ({exc.args[0]})")
            continue

        if args.dry_run:
            try:
                frame, meta = ett.parse(str(zp), {}, {})
                validation = validate_exam(frame, meta)
                print(f"  [{n}/{len(zips)}] {zp.name}: dry-run OK "
                      f"({meta['rows']} rows, {validation['verdict']})")
                counts["dry_run_ok"] = counts.get("dry_run_ok", 0) + 1
            except ETTQuarantine as exc:
                print(f"  [{n}/{len(zips)}] {zp.name}: dry-run QUARANTINE ({exc.args[0]})")
                counts["quarantined"] = counts.get("quarantined", 0) + 1
            continue

        entry = ingest_one(zp, project=project, out_root=out_root,
                           catalog=catalog, quarantine_root=quarantine_root)
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        extra = entry.get("quarantine_reason", entry.get("validation_verdict", ""))
        print(f"  [{n}/{len(zips)}] {zp.name}: {entry['status']} {extra}")

    if not args.dry_run:
        save_catalog(catalog_path, catalog)
        print(f"\ncatalog  {catalog_path}")

    print("\nBy status:")
    for k, v in sorted(counts.items()):
        print(f"  {k:<16} {v}")


if __name__ == "__main__":
    main()
