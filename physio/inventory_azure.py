"""
Phase 1b - inventory every blob in the Azure raw container.

    python -m physio.inventory_azure                 # -> reports/phase1/azure_inventory.csv

Records name, size, last-modified, Content-MD5 and the metadata the sync
writes (sha256, dropbox_content_hash, dropbox_path ...), so reconciliation
can match Dropbox and Azure by CONTENT, not by name.
"""
from __future__ import annotations

import argparse
import base64
import csv
from pathlib import Path

from physio.common import azure_container

FIELDS = ["project", "full_path", "size_bytes", "last_modified", "content_md5",
          "sha256", "dropbox_content_hash", "dropbox_path", "layout"]


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="reports/phase1/azure_inventory.csv")
    ap.add_argument("--prefix", default=None, help="only blobs under this prefix, e.g. PROMISES/")
    a = ap.parse_args()
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    cc = azure_container()
    n = 0
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for b in cc.list_blobs(name_starts_with=a.prefix, include=["metadata"]):
            md = b.metadata or {}
            md5 = b.content_settings.content_md5
            parts = b.name.split("/")
            w.writerow({
                "project": parts[0],
                "full_path": b.name,
                "size_bytes": b.size,
                "last_modified": b.last_modified.isoformat() if b.last_modified else "",
                "content_md5": base64.b64encode(md5).decode() if md5 else "",
                "sha256": md.get("sha256", ""),
                "dropbox_content_hash": md.get("dropbox_content_hash", ""),
                "dropbox_path": md.get("dropbox_path", ""),
                "layout": "canonical" if len(parts) > 2 and parts[1] == "Database" else "legacy",
            })
            n += 1
            if n % 1000 == 0:
                print(f"  {n} blobs ...")
    print(f"DONE: {n} blobs -> {out}")


if __name__ == "__main__":
    main()
