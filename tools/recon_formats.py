"""Recon: what file formats live in each data project?

Read-only Dropbox walk. Counts files by extension per project (plus total
bytes per extension). Writes a plaintext digest — extensions, counts and
sizes carry no patient information.

Output: reports/recon/formats_<ts>.json  (+ log lines)

Used to plan parser coverage for projects without a profile
(MONREPI, POSBRAIN, COLECTOMIE, PVB-ABDO) and to see which device formats
(BIS, NOL, ...) are currently marked unsupported.
"""

from __future__ import annotations

import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import sync_dropbox_cloud
    from tools.daily_pipeline import ACCOUNT, REPORTS
    from tools import azure_auth
    from tools.legacy_pipeline import (
        list_dropbox_tree, select_projects, is_junk, is_document, is_photo,
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    dbx = sync_dropbox_cloud.get_dropbox_client()
    projects, _ = select_projects(dbx)

    result = {}
    for proj in sorted(projects, key=lambda p: p["code"]):
        code = proj["code"]
        ext_count: Counter = Counter()
        ext_bytes: Counter = Counter()
        n_files = 0
        excl = proj["exclude_folders"]
        for root in proj["data_roots"]:
            base = proj["dropbox_base"] if root == "." else \
                f"{proj['dropbox_base']}/{root}"
            try:
                entries = list_dropbox_tree(dbx, base)
            except Exception as exc:  # noqa: BLE001
                print(f"[recon] {code}: cannot list {base}: {exc}",
                      flush=True)
                continue
            for e in entries:
                rel = f"{root}/{e['relpath']}" if root != "." else e["relpath"]
                if is_junk(e["name"]) or is_document(rel) or is_photo(e["name"]):
                    continue
                if any(rel == x or rel.startswith(x + "/") for x in excl):
                    continue
                n_files += 1
                ext = os.path.splitext(e["name"])[1].lower() or "(none)"
                ext_count[ext] += 1
                ext_bytes[ext] += e.get("size", 0)
        result[code] = {
            "profile": bool(proj["profile"]),
            "n_files": n_files,
            "extensions": [
                {"ext": ext, "count": c,
                 "gb": round(ext_bytes[ext] / 1e9, 2)}
                for ext, c in ext_count.most_common()
            ],
        }
        top = ", ".join(f"{r['ext']}:{r['count']}" for r in
                        result[code]["extensions"][:6])
        print(f"[recon] {code}: {n_files} files | {top}", flush=True)

    payload = json.dumps({"generated_utc": ts, "projects": result},
                         indent=1).encode("utf-8")
    azure_auth.ensure_container(
        azure_auth.get_blob_service_client(ACCOUNT), REPORTS)
    azure_auth.get_blob_service_client(ACCOUNT).get_blob_client(
        container=REPORTS,
        blob=f"recon/formats_{ts}.json").upload_blob(payload, overwrite=True)
    print(f"[recon] wrote reports/recon/formats_{ts}.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
