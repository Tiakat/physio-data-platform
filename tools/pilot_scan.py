"""Pilot: identifier value-scan on a small sample of real decrypted parquets.

Decrypts a few encrypted parquets per project inside GitHub Actions
(PIPELINE_DATA_KEY never leaves the runner), runs tools/scan_identifiers,
and writes a COUNTS-ONLY report -- no values, no filenames, SHA-8 tokens.

Usage:
  python -m tools.pilot_scan --per-project 2 --report reports/recon/pilot_scan.json

Exit code mirrors scan_identifiers: 0 = clean, 2 = findings need review.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes  # noqa: E402
from tools.scan_identifiers import main as scan_main  # noqa: E402


def _project_of(blob: str) -> str:
    parts = blob.split("/")
    return parts[0] if parts else "unknown"


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Pilot identifier scan on sampled real parquets.")
    ap.add_argument("--per-project", type=int, default=2)
    ap.add_argument("--max-total", type=int, default=12)
    ap.add_argument("--report", required=True)
    args = ap.parse_args(argv)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("rawdata")

    by_project: dict[str, list[str]] = {}
    for b in container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith(".parquet.enc"):
            continue
        by_project.setdefault(_project_of(name), []).append(name)

    chosen = []
    for code in sorted(by_project):
        chosen.extend(sorted(by_project[code])[: args.per_project])
        if len(chosen) >= args.max_total:
            break
    chosen = chosen[: args.max_total]
    print(f"[pilot-scan] {len(by_project)} projects with parquets; "
          f"sampling {len(chosen)} file(s)", flush=True)

    with tempfile.TemporaryDirectory(prefix="pilot_scan_") as tmp:
        tmpdir = Path(tmp)
        for i, blob in enumerate(chosen):
            raw = svc.get_blob_client(
                container="rawdata", blob=blob).download_blob().readall()
            (tmpdir / f"sample_{i:03d}.parquet").write_bytes(
                decrypt_bytes(raw))
        scan_report = tmpdir / "scan.json"
        code = scan_main(["--input", str(tmpdir), "--report",
                          str(scan_report)])
        import json
        scan_data = json.loads(scan_report.read_text())

    report = {
        "tool": "pilot_scan",
        "ts": datetime.now(timezone.utc).isoformat(),
        "projects_sampled": sorted(by_project),
        "files_sampled": len(chosen),
        "scan_exit": code,
    }
    out = Path(args.report)
    out.parent.mkdir(parents=True, exist_ok=True)
    # Merge counts-only scan results (they carry no values/filenames).
    report["files_scanned"] = scan_data["files_scanned"]
    report["total_findings"] = scan_data["total_findings"]
    # Keep per-pattern aggregates only, drop per-file detail.
    agg: dict[str, int] = {}
    for r in scan_data["results"]:
        for f_ in r["findings"]:
            p = f_.get("pattern", "unknown")
            agg[p] = agg.get(p, 0) + f_.get("count", 0)
    report["findings_by_pattern"] = agg
    out.write_text(json.dumps(report, indent=1))
    print(f"[pilot-scan] findings={report['total_findings']} "
          f"-> {out} (exit {code})", flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
