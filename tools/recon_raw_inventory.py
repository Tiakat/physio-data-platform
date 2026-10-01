"""Recon: inventory the legacy encrypted source-byte objects in Azure.

Lists every blob under rawdata/*/<raw>/ (the pre-2026-10-01 fallback that
uploaded encrypted source bytes). Read-only.

Outputs:
  processed/cleanup/raw_inventory_<ts>.json.enc  full blob list (ENCRYPTED:
      blob names embed Dropbox relative paths, which may carry patient codes)
  processed/cleanup/raw_inventory_<ts>.digest.json  plaintext: per-project
      counts + bytes only

K reviews the digest; deletion happens only through a separate,
explicitly confirmed step.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import azure_auth
    from tools.crypto import encrypt_bytes
    from tools.daily_pipeline import ACCOUNT, PROCESSED

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    svc = azure_auth.get_blob_service_client(ACCOUNT)
    azure_auth.ensure_container(svc, PROCESSED)
    container = svc.get_container_client("rawdata")

    by_project: dict[str, list] = defaultdict(list)
    total_bytes = 0
    for blob in container.list_blobs():
        name = blob.name
        parts = name.split("/")
        # rawdata/<CODE>/raw/<relpath>.enc
        if len(parts) >= 3 and parts[1] == "raw":
            code = parts[0]
            size = blob.size or 0
            by_project[code].append({"blob": name, "size": size})
            total_bytes += size

    digest_projects = {}
    for code in sorted(by_project):
        items = by_project[code]
        digest_projects[code] = {
            "n_objects": len(items),
            "gb": round(sum(i["size"] for i in items) / 1e9, 2),
        }
        print(f"[cleanup] {code}: {len(items)} objects, "
              f"{digest_projects[code]['gb']} GB", flush=True)

    full = {"generated_utc": ts,
            "objects": [i for code in sorted(by_project)
                        for i in sorted(by_project[code],
                                        key=lambda x: x["blob"])]}
    proc = svc.get_blob_client(container=PROCESSED,
                               blob=f"cleanup/raw_inventory_{ts}.json.enc")
    proc.upload_blob(
        encrypt_bytes(json.dumps(full, indent=1).encode("utf-8")),
        overwrite=True, metadata={"enc": "fernet"})
    digest = {"generated_utc": ts, "projects": digest_projects,
              "total_objects": sum(v["n_objects"]
                                   for v in digest_projects.values()),
              "total_gb": round(total_bytes / 1e9, 2)}
    svc.get_blob_client(
        container=PROCESSED,
        blob=f"cleanup/raw_inventory_{ts}.digest.json").upload_blob(
            json.dumps(digest, indent=1).encode("utf-8"), overwrite=True)
    print(f"[cleanup] total {digest['total_objects']} objects, "
          f"{digest['total_gb']} GB awaiting review", flush=True)
    print("[cleanup] review the digest, then confirm deletion explicitly",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
