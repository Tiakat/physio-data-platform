"""Dump identifier-class column NAMES per project (names only, no values).

Reads the latest encrypted stage2 dictionary from Azure (processed
container, stage2/dictionary_<ts>.json.enc), decrypts it with
PIPELINE_DATA_KEY, and prints every column classified as "identifier"
per project, with its arrow type.

Privacy-safe by design: column NAMES only, never values. This feeds K's
review for the stage-2 finding "identifier-class columns inside
standardized parquet — review before any de-identified release"
(Issue #2), which gates the de-identified website feed.

Usage:
  python -m tools.dump_identifier_columns
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402
from tools.crypto import decrypt_bytes  # noqa: E402

ACCOUNT = os.getenv("AZURE_STORAGE_ACCOUNT", "labdataplatform")
PROCESSED = "processed"
PREFIX = "stage2/dictionary_"


def main() -> int:
    svc = azure_auth.get_blob_service_client(ACCOUNT)
    container = svc.get_container_client(PROCESSED)
    names = []
    for b in container.list_blobs(name_starts_with=PREFIX):
        name = b["name"] if isinstance(b, dict) else b.name
        if name.endswith(".json.enc"):
            names.append(name)
    if not names:
        print("[identifier-columns] no stage2 dictionary found under "
              f"{PREFIX} in {PROCESSED}", flush=True)
        return 1
    latest = sorted(names)[-1]
    print(f"[identifier-columns] reading {latest}", flush=True)
    raw = svc.get_blob_client(
        container=PROCESSED, blob=latest).download_blob().readall()
    dictionary = json.loads(decrypt_bytes(raw))

    total = 0
    for project in sorted(dictionary):
        columns = (dictionary[project] or {}).get("columns", {})
        ident = sorted(
            (name, info.get("arrow_type", "?"))
            for name, info in columns.items()
            if isinstance(info, dict) and info.get("class") == "identifier"
        )
        if not ident:
            continue
        print(f"== {project}: {len(ident)} identifier-class column(s) ==")
        for name, atype in ident:
            print(f"  {name}  [{atype}]")
        total += len(ident)
    print(f"[identifier-columns] {total} identifier-class column(s) total "
          f"across {len(dictionary)} project(s)", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
