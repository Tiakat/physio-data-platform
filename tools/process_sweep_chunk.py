"""Process one sweep chunk: each file through tools/process_patient.

Runs process_patient.main() in-process for every {project, blob, label}
in the chunk JSON. Continues past individual failures, recording them
in the chunk report.
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.process_patient import main as process_patient_main  # noqa: E402


def _already_done(project, label):
    """True if this patient already has outputs in Azure (resumable retry).

    process_patient uploads lineage.json after the encrypted outputs, so
    its presence means the patient is fully processed. Lets a re-dispatch
    skip completed patients instead of redoing hours of work — and lets
    chunks resume after a runner kill (Oct 2026: 3 jobs SIGTERM-killed).
    """
    try:
        account = os.environ["AZURE_STORAGE_ACCOUNT"]
    except KeyError:
        return False
    try:
        svc = azure_auth.get_blob_service_client(account)
        safe = "".join(ch if ch.isalnum() else "_" for ch in label).strip("_")
        blob = f"processed/level2/{project}/{safe}/lineage.json"
        return svc.get_blob_client(
            container="processed", blob=blob).exists()
    except Exception:
        return False


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk", required=True,
                    help="JSON list of {project, blob, label}.")
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    items = json.loads(args.chunk)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    report = {"ts": datetime.now(timezone.utc).isoformat(),
              "items": []}
    force = os.environ.get("SWEEP_FORCE", "0") == "1"
    for it in items:
        project = it["project"]
        blob = it["blob"]
        label = it.get("label", "patient ?")
        if not force and _already_done(project, label):
            status = "skipped_done"
            print(f"[sweep-chunk] {project} {label}: {status}", flush=True)
        else:
            item_out = out / project / label.replace(" ", "_")
            item_out.mkdir(parents=True, exist_ok=True)
            try:
                rc = process_patient_main([
                    "--project", project,
                    "--patient", label,
                    "--blob", blob,
                    "--out", str(item_out),
                ])
                status = "ok" if rc == 0 else f"exit_{rc}"
            except Exception as exc:  # noqa: BLE001
                status = f"error:{type(exc).__name__}:{str(exc)[:120]}"
            print(f"[sweep-chunk] {project} {label}: {status}", flush=True)
            # Release Agg figure buffers / frame memory between patients so
            # a long chunk does not accumulate RAM until the runner dies.
            gc.collect()
        report["items"].append({
            "project": project, "blob": blob, "label": label,
            "status": status,
        })
    (out / "_chunk_report.json").write_text(json.dumps(report, indent=1))
    n_ok = sum(1 for r in report["items"]
               if r["status"] in ("ok", "skipped_done"))
    print(f"[sweep-chunk] {n_ok}/{len(items)} ok", flush=True)
    return 0 if n_ok == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
