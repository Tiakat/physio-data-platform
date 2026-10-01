"""Fetch a reports/recon/<prefix>/ prefix from Azure to a local directory.

Used to bring census/dictionary outputs back as a GitHub Actions artifact
(this runtime cannot reach Azure directly).

Usage:
  python -m tools.fetch_recon --prefix recon/schemas_20261001_2012/ \
      --out fetched/
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Download a reports/recon/ prefix from Azure.")
    ap.add_argument("--prefix", required=True,
                    help="blob prefix, e.g. recon/schemas_20261001_2012/")
    ap.add_argument("--out", required=True, help="local directory")
    args = ap.parse_args(argv)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("reports")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    n = 0
    for b in container.list_blobs(name_starts_with=args.prefix):
        name = b["name"] if isinstance(b, dict) else b.name
        rel = name[len(args.prefix):]
        if not rel:
            continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        data = svc.get_blob_client(
            container="reports", blob=name).download_blob().readall()
        dest.write_bytes(data)
        n += 1
    print(f"[fetch-recon] downloaded {n} blob(s) "
          f"{args.prefix} -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
