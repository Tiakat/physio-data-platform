"""Delete ALL Azure containers except 1-Raw. K's clean restart.

K: "all the other containers erase them we will start from new"

Deletes every container except "1-raw":
    rawdata, processed, processed-new, graphs, graphes, reports,
    filtered-csv, analysis, raw, $logs (attempted; system container may
    refuse - non-fatal)

Token-gated: --confirm YES-ERASE-ALL (refuses otherwise).

Sanity check: 1-Raw must exist and be non-empty before erasing anything.
This prevents wiping Azure before the clean structure is built.

Usage:
    python -m tools.erase_all_except_1raw --confirm YES-ERASE-ALL

Without the exact confirmation string the tool refuses to run.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402

KEEP = "1-raw"
CONFIRM_TOKEN = "YES-ERASE-ALL"
SYSTEM_CONTAINER = "$logs"


def count_blobs(svc, container: str) -> int:
    try:
        cc = svc.get_container_client(container)
        return sum(1 for _ in cc.list_blobs())
    except Exception:
        return -1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--confirm", default="",
                    help=f"Must be exactly '{CONFIRM_TOKEN}'")
    args = ap.parse_args()

    if args.confirm != CONFIRM_TOKEN:
        print(f"[erase-all] REFUSED: pass --confirm {CONFIRM_TOKEN} to proceed.",
              flush=True)
        sys.exit(2)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)

    # 1. Sanity check: 1-Raw must exist and hold data.
    n = count_blobs(svc, KEEP)
    print(f"[erase-all] sanity check: '{KEEP}' has {n} blobs", flush=True)
    if n < 0:
        print(f"[erase-all] ABORT: container '{KEEP}' is missing. "
              f"Build it first with build-1raw.", flush=True)
        sys.exit(1)
    if n == 0:
        print(f"[erase-all] ABORT: '{KEEP}' is empty - migration incomplete.",
              flush=True)
        sys.exit(1)

    # 2. List all containers, delete everything except 1-Raw.
    deleted: list[str] = []
    failed: list[str] = []
    for c in svc.list_containers():
        name = c["name"] if isinstance(c, dict) else c.name
        if name == KEEP:
            print(f"[erase-all] keeping: {name}", flush=True)
            continue
        try:
            svc.delete_container(name)
            print(f"[erase-all] deleted container: {name}", flush=True)
            deleted.append(name)
        except Exception as exc:
            # $logs is a system container and may refuse - non-fatal.
            print(f"[erase-all] could not delete '{name}': {exc}", flush=True)
            failed.append(name)

    print(f"[erase-all] DONE. Deleted {len(deleted)} containers: {deleted}",
          flush=True)
    if failed:
        print(f"[erase-all] could not delete (non-fatal): {failed}",
              flush=True)
    print(f"[erase-all] Remaining: '{KEEP}' with {count_blobs(svc, KEEP)} blobs",
          flush=True)


if __name__ == "__main__":
    main()
