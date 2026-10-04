"""Parallel ingest shard: legacy ingest for a project subset.

Set LEGACY_PROJECTS to a comma-separated subset (e.g. "IPAMS,ESMONOL").
State is merged per-project into the shared encrypted blob with an
ETag-guarded read-modify-write, so shards never clobber each other.
Do not run concurrently with the daily pipeline's own ingest run; the
merge is a safety net, not a license to overlap writers.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from tools.daily_pipeline import (  # noqa: E402
    ACCOUNT,
    load_state,
    save_state_merge,
)
from tools import legacy_pipeline, sync_dropbox_cloud  # noqa: E402


def main() -> int:
    wanted = os.getenv("LEGACY_PROJECTS", "").strip()
    codes = [c.strip().upper() for c in wanted.split(",") if c.strip()]
    if not codes:
        print("[ingest-shard] LEGACY_PROJECTS is empty", flush=True)
        return 1
    dbx = sync_dropbox_cloud.get_dropbox_client()
    state = load_state(ACCOUNT)
    state = legacy_pipeline.run_ingest(
        dbx, ACCOUNT, state,
        progress_cb=lambda s: save_state_merge(ACCOUNT, s, codes))
    save_state_merge(ACCOUNT, state, codes)
    print(f"[ingest-shard] done for {codes}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
