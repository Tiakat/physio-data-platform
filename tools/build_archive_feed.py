"""Build the de-identified archive-projects website feed.

Reads the latest plaintext Stage 2 digest (``processed/stage2/digest_*.json``)
— counts and standard signal names only, never patient data — and writes
``reports/feed/archive.json`` in the exact shape liam-v2's physio-data page
expects (see ``components/physio-data/FeedView.tsx``)::

    {generated_at, privacy: {note, k_anonymity}, projects: {CODE: {...}}}

Privacy rules (same as tools/build_feed.py):

- never patient-level rows, never typed IDs, never free text
- counts below k=5 are suppressed (reported as null, project flagged)
- standard signal names only (from the shared ontology)

Per-signal ``total_hours`` is an ESTIMATE from discovery sampling
(sampled rows x file coverage / nominal rate). It is documented as such in
the feed's privacy note and will be replaced by measured statistics in
Phase 15. Nothing here is a clinical or scientific result.

Usage::

    python -m tools.build_archive_feed --digest <digest.json> --out <archive.json>
    python -m tools.build_archive_feed --digest <digest.json> --out <feed.json> --merge <old_feed.json>
    python -m tools.build_archive_feed   # Azure: latest digest -> reports/feed/archive.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

K_ANONYMITY = 5


def _suppress(n):
    """Return the count, or None if below the k-anonymity threshold."""
    if n is None:
        return None
    return int(n) if int(n) >= K_ANONYMITY else None


def _parse_generated(ts: str) -> str:
    try:
        return datetime.strptime(ts, "%Y%m%d_%H%M").replace(
            tzinfo=timezone.utc).isoformat()
    except Exception:  # noqa: BLE001
        return datetime.now(timezone.utc).isoformat()


def _median_hz(sampling: dict) -> float | None:
    mids = []
    for s in (sampling or {}).values():
        rng = (s or {}).get("nominal_hz_range")
        if rng and rng[0]:
            mids.append((rng[0] + rng[1]) / 2.0)
    if not mids:
        return None
    mids.sort()
    return mids[len(mids) // 2]


def _project_entry(code: str, p: dict) -> dict:
    n_files = p.get("n_parquet_files") or 0
    if _suppress(n_files) is None:
        return {"n_exams": None, "suppressed": True, "total_rows": None,
                "qc_verdicts": {}, "ett_versions": {}, "signals": {}}

    kinds = p.get("missingness_kinds") or {}
    qc = {f"columns_{k}": _suppress(v) for k, v in kinds.items()}
    qc["schema_drift_columns"] = _suppress(p.get("schema_drift"))
    qc["unknown_columns"] = _suppress(p.get("n_unknown"))

    hz = _median_hz(p.get("sampling"))
    sampled = p.get("sampled") or 0
    total_rows = p.get("total_rows_sampled") or 0
    signals = {}
    for name, det in (p.get("signal_detail") or {}).items():
        files_seen = _suppress((det or {}).get("files_seen"))
        if files_seen is None:
            continue  # signal too rare to report per-signal
        null_frac = (det or {}).get("mean_missing_frac") or 0.0
        hours = 0.0
        if hz and sampled and total_rows:
            hours = round(total_rows * (files_seen / sampled) / hz / 3600, 1)
        signals[name] = {"n_exams": files_seen, "mean_duration_s": 0,
                         "total_hours": hours,
                         "null_fraction": round(float(null_frac), 4)}

    return {"n_exams": int(n_files), "suppressed": False,
            "total_rows": _suppress(total_rows),
            "qc_verdicts": qc, "ett_versions": {}, "signals": signals}


def build_feed(digest: dict) -> dict:
    """Digest dict -> FeedView-shaped feed dict. Pure function (testable)."""
    projects = {}
    for code in sorted((digest.get("projects") or {})):
        projects[code] = _project_entry(code, digest["projects"][code])
    return {
        "generated_at": _parse_generated(digest.get("generated_utc", "")),
        "privacy": {
            "note": ("Discovery-stage aggregates for the LIAM archive "
                     "projects: file/signal counts and data-quality "
                     "indicators only. No patient-level data. Per-signal "
                     "hours are estimates from discovery sampling and will "
                     "be replaced by measured statistics in Phase 15."),
            "k_anonymity": K_ANONYMITY,
        },
        "projects": projects,
    }


def _latest_digest_azure(account: str) -> dict:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import azure_auth
    from tools.daily_pipeline import PROCESSED
    svc = azure_auth.get_blob_service_client(account)
    blobs = sorted(
        b.name for b in svc.get_container_client(PROCESSED).list_blobs(
            name_starts_with="stage2/digest_")
        if b.name.endswith(".json"))
    if not blobs:
        raise RuntimeError("no stage2 digest found in processed/stage2/")
    data = svc.get_blob_client(
        container=PROCESSED, blob=blobs[-1]).download_blob().readall()
    print(f"[archive-feed] using digest {blobs[-1]}", flush=True)
    return json.loads(data)


def _upload_azure(account: str, payload: bytes) -> None:
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import azure_auth
    from tools.daily_pipeline import REPORTS
    svc = azure_auth.get_blob_service_client(account)
    azure_auth.ensure_container(svc, REPORTS)
    svc.get_blob_client(container=REPORTS,
                        blob="feed/archive.json").upload_blob(
                            payload, overwrite=True)
    print("[archive-feed] uploaded reports/feed/archive.json", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--digest", help="local stage2 digest JSON path")
    ap.add_argument("--out", help="local output path for the feed JSON")
    ap.add_argument("--merge", help="existing feed JSON to merge projects into")
    ap.add_argument("--account",
                    default="labdataplatform",
                    help="Azure storage account (Azure mode)")
    args = ap.parse_args()

    if args.digest:
        digest = json.loads(Path(args.digest).read_text(encoding="utf-8"))
    else:
        import os
        digest = _latest_digest_azure(
            os.getenv("AZURE_STORAGE_ACCOUNT", args.account))

    feed = build_feed(digest)

    if args.merge:
        mp = Path(args.merge)
        if mp.exists():
            old = json.loads(mp.read_text(encoding="utf-8"))
            merged_projects = dict(old.get("projects") or {})
            merged_projects.update(feed["projects"])
            old["projects"] = merged_projects
            old["generated_at"] = feed["generated_at"]
            feed = old
            print(f"[archive-feed] merged into {args.merge}", flush=True)

    payload = json.dumps(feed, indent=1).encode("utf-8")
    if args.out:
        Path(args.out).write_bytes(payload)
        print(f"[archive-feed] wrote {args.out} "
              f"({len(feed['projects'])} projects)", flush=True)
    else:
        import os
        _upload_azure(os.getenv("AZURE_STORAGE_ACCOUNT", args.account),
                      payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
