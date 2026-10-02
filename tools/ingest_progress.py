"""Report ingest progress: how many files are in Azure vs total in Dropbox.

Compares Azure blob count against Dropbox file count per project.
Outputs a percentage for Phase 1 tracking.

Usage:
  python -m tools.ingest_progress --project DEXREM
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="DEXREM")
    args = ap.parse_args(argv)

    # TODO: Count blobs in Azure rawdata/<PROJECT>/parquet/
    # TODO: Compare against known totals (DEXREM: 257)
    # For now, framework only.

    totals = {
        "DEXREM": 257,  # 219 CSV + 38 BIS .r2a
    }
    total = totals.get(args.project, 0)
    print(f"[ingest-progress] {args.project}: total={total}", flush=True)
    print("[ingest-progress] framework ready — needs Azure blob count",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
