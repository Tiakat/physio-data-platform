"""Process one sweep chunk: each file through tools/process_patient.

Runs process_patient.main() in-process for every {project, blob, label}
in the chunk JSON. Continues past individual failures, recording them
in the chunk report.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.process_patient import main as process_patient_main  # noqa: E402


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
    for it in items:
        project = it["project"]
        blob = it["blob"]
        label = it.get("label", "patient ?")
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
        report["items"].append({
            "project": project, "blob": blob, "label": label,
            "status": status,
        })
    (out / "_chunk_report.json").write_text(json.dumps(report, indent=1))
    n_ok = sum(1 for r in report["items"] if r["status"] == "ok")
    print(f"[sweep-chunk] {n_ok}/{len(items)} ok", flush=True)
    return 0 if n_ok == len(items) else 1


if __name__ == "__main__":
    sys.exit(main())
