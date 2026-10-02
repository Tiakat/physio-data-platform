"""Find a patient with all four data types: Infinity, BetterCare, NOL, BIS.

Searches Dropbox patient folders in the BIS projects (COLECTOMIE, POSBRAIN,
PVB-ABDO, MONREPI) for a patient having all four.

Usage:
  python -m tools.find_complete_patient --out patient_out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sync_dropbox_cloud import get_dropbox_client  # noqa: E402

BIS_PROJECTS = ["COLECTOMIE", "POSBRAIN", "PVB-ABDO", "MONREPI"]
DROPBOX_ROOT = "/Liam/Projets actifs"


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    dbx = get_dropbox_client()
    print("[find-patient] Dropbox auth OK", flush=True)

    results = []
    for proj in BIS_PROJECTS:
        proj_path = f"{DROPBOX_ROOT}/{proj}"
        try:
            # List patient folders (first level)
            res = dbx.files_list_folder(proj_path)
            entries = res.entries
            print(f"[find-patient] {proj}: {len(entries)} entries", flush=True)
            # TODO: drill into patient folders, check for the 4 file types
            results.append({"project": proj, "entries": len(entries),
                            "status": "listed"})
        except Exception as e:
            print(f"[find-patient] {proj} failed: {e}", flush=True)
            results.append({"project": proj, "status": f"error: {e}"})

    (out / "results.json").write_text(json.dumps(results, indent=1))
    print("[find-patient] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
