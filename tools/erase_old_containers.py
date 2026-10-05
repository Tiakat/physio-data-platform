"""Delete old Azure containers after K confirms the 4-container migration.

K's order: "Everything else erased." This runs ONLY after she verifies the new
raw / processed / graphes / analysis containers and gives explicit confirmation.

Deletes: rawdata, processed (old level2 layout), graphs, reports, filtered-csv
($logs is attempted too; it is a system container and may refuse - not fatal).

Then performs the processed swap, because Azure has no container rename:
  1. create new empty 'processed' container
  2. server-side copy every blob processed-new/* -> processed/*
  3. delete 'processed-new'

Final state: exactly 4 containers - raw, processed, graphes, analysis.

Usage:
    python -m tools.erase_old_containers --confirm YES-DELETE-OLD

Without the exact confirmation string the tool refuses to run.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402

OLD_CONTAINERS = ["rawdata", "processed", "graphs", "reports", "filtered-csv"]
SYSTEM_CONTAINER = "$logs"
STAGING = "processed-new"
FINAL = ["raw", "processed", "graphes", "analysis"]
CONFIRM_TOKEN = "YES-DELETE-OLD"


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
        print(f"[erase-old] REFUSED: pass --confirm {CONFIRM_TOKEN} to proceed.",
              flush=True)
        sys.exit(2)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    account_key = os.environ.get("AZURE_STORAGE_KEY", "")
    svc = azure_auth.get_blob_service_client(account)

    # 1. Sanity check: new containers must exist and hold data.
    print("[erase-old] sanity check on new containers:", flush=True)
    ok = True
    for c in ["raw", "graphes", "analysis", STAGING]:
        n = count_blobs(svc, c)
        print(f"  {c}: {n} blobs", flush=True)
        if n < 0:
            print(f"[erase-old] ABORT: container '{c}' missing.", flush=True)
            ok = False
    if not ok:
        sys.exit(1)
    # raw + graphes + staging must be non-empty; analysis may be empty by design.
    for c in ["raw", "graphes", STAGING]:
        if count_blobs(svc, c) == 0:
            print(f"[erase-old] ABORT: '{c}' is empty - migration incomplete.",
                  flush=True)
            sys.exit(1)

    # 2. Delete old containers.
    deleted = []
    for c in OLD_CONTAINERS + [SYSTEM_CONTAINER]:
        try:
            svc.delete_container(c)
            print(f"[erase-old] deleted container: {c}", flush=True)
            deleted.append(c)
        except Exception as exc:
            print(f"[erase-old] could not delete '{c}': {exc}", flush=True)

    # 3. Processed swap: processed-new -> processed (server-side copy).
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    staging_cc = svc.get_container_client(STAGING)
    azure_auth.ensure_container(svc, "processed")
    print("[erase-old] created fresh 'processed' container", flush=True)
    dest_cc = svc.get_container_client("processed")

    def sas_url(blob_name: str) -> str:
        sas = generate_blob_sas(
            account_name=svc.account_name,
            container_name=STAGING,
            blob_name=blob_name,
            account_key=account_key,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=6),
        )
        return (f"https://{svc.account_name}.blob.core.windows.net/"
                f"{STAGING}/{blob_name}?{sas}")

    blobs = [b.name for b in staging_cc.list_blobs()]
    print(f"[erase-old] copying {len(blobs)} blobs processed-new -> processed...",
          flush=True)
    for i, name in enumerate(blobs, 1):
        dest = dest_cc.get_blob_client(name)
        copy = dest.start_copy_from_url(sas_url(name))
        # Poll briefly; same-account copies are near-instant.
        import time
        for _ in range(30):
            props = dest.get_blob_properties()
            if props.copy.status != "pending":
                break
            time.sleep(2)
        if i % 200 == 0:
            print(f"[erase-old] copied {i}/{len(blobs)}...", flush=True)
    print(f"[erase-old] copied {len(blobs)} blobs", flush=True)

    # Verify counts match before dropping staging.
    n_src = count_blobs(svc, STAGING)
    n_dst = count_blobs(svc, "processed")
    print(f"[erase-old] verify: processed-new={n_src} processed={n_dst}",
          flush=True)
    if n_src != n_dst:
        print("[erase-old] ABORT: count mismatch, keeping processed-new.",
              flush=True)
        sys.exit(1)

    svc.delete_container(STAGING)
    print(f"[erase-old] deleted staging container: {STAGING}", flush=True)

    print("[erase-old] final containers:", flush=True)
    for c in svc.list_containers():
        cname = c["name"] if isinstance(c, dict) else c.name
        print(f"  {cname}: {count_blobs(svc, cname)} blobs", flush=True)
    print("[erase-old] DONE. Remaining:", ", ".join(FINAL), flush=True)


if __name__ == "__main__":
    main()
