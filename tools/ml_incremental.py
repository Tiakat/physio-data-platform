"""Incremental ML trainer: retrain as new project data arrives.

Tracks which projects have been included. On each run:
1. Lists processed projects in Azure
2. For each new project (not yet trained on), adds its data
3. Retrains unsupervised clustering + supervised classifiers
4. Updates the training manifest

Usage:
  python -m tools.ml_incremental --out ml_models/ --manifest ml_models/training_manifest.json
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path


PROJECTS = [
    "PROMISES", "IPAMS", "DEXREM", "V-RAPS", "SILVR",
    "ESMONOL", "MONREPI", "POSBRAIN", "PVB-ABDO", "COLECTOMIE",
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--manifest", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = Path(args.manifest)

    # Load existing manifest.
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = {
            "trained_projects": [],
            "unsupervised_runs": [],
            "supervised_runs": {},
            "created": datetime.now(timezone.utc).isoformat(),
        }

    print(f"[ml-incremental] previously trained: {manifest['trained_projects']}",
          flush=True)
    print("[ml-incremental] checking Azure for new processed projects...",
          flush=True)
    # TODO: List processed/level2/ in Azure, find new projects.
    # For now, this is the orchestrator framework.

    manifest["last_check"] = datetime.now(timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest, indent=1))
    print("[ml-incremental] framework ready", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
