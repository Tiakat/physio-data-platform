"""Build the sweep matrix: every encrypted parquet, chunked for workers.

Lists rawdata/<CODE>/parquet/*.parquet.enc across all (or selected)
projects and groups them into chunks. Output is a JSON list of chunks,
each chunk a list of {project, blob, label}.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--projects", default="all",
                    help="Comma-separated codes or 'all'.")
    ap.add_argument("--chunk-size", type=int, default=10)
    ap.add_argument("--out", required=True)
    ap.add_argument("--only", default="",
                    help=("Targeted retry: comma-separated 'PROJECT:label' "
                          "pairs, e.g. 'COLECTOMIE:patient 103'. Only those "
                          "files are included in the matrix."))
    args = ap.parse_args(argv)

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("rawdata")

    wanted = None
    if args.projects.strip().lower() != "all":
        wanted = {p.strip().upper()
                  for p in args.projects.split(",") if p.strip()}

    by_project: dict[str, list[str]] = {}
    for b in container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith(".parquet.enc"):
            continue
        parts = name.split("/")
        if len(parts) < 3:
            continue
        code = parts[0].upper()
        if wanted and code not in wanted:
            continue
        by_project.setdefault(code, []).append(name)

    only = None
    if args.only.strip():
        only = set()
        for piece in args.only.split(","):
            piece = piece.strip()
            if ":" in piece:
                proj, lab = piece.split(":", 1)
                only.add((proj.strip().upper(), lab.strip().lower()))

    # Flatten into (project, blob) pairs, then chunk.
    pairs = []
    for code in sorted(by_project):
        for i, blob in enumerate(sorted(by_project[code]), 1):
            label = f"patient {i}"
            if only is not None and (code, label.lower()) not in only:
                continue
            pairs.append({
                "project": code,
                "blob": blob,
                # Provisional label until patient linkage is resolved
                # from the ingest state (blob sha -> Dropbox path).
                "label": label,
            })
    chunks = [pairs[i:i + args.chunk_size]
              for i in range(0, len(pairs), args.chunk_size)]
    Path(args.out).write_text(json.dumps(chunks))
    print(f"[sweep-matrix] {len(pairs)} files -> {len(chunks)} chunks",
          flush=True)
    for code in sorted(by_project):
        print(f"  {code}: {len(by_project[code])} files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
