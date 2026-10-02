"""Fetch a Dropbox shared folder as a zip (via dl=1).

Simpler than the API: a shared folder link with dl=1 downloads the whole
folder as a zip. No auth needed.

Usage:
  python -m tools.fetch_shared_zip --url "https://www.dropbox.com/scl/fo/..." --out fetched/
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

import requests


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    # Flip dl=0 to dl=1 for direct zip download.
    url = args.url.replace("dl=0", "dl=1")
    if "dl=" not in url:
        url += ("&" if "?" in url else "?") + "dl=1"

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    zip_path = out / "shared.zip"

    print(f"[fetch-shared-zip] downloading...", flush=True)
    resp = requests.get(url, stream=True, timeout=600, allow_redirects=True)
    resp.raise_for_status()

    total = 0
    with open(zip_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            f.write(chunk)
            total += len(chunk)
    print(f"[fetch-shared-zip] got {total} bytes", flush=True)

    # List contents (don't extract yet - zips may be large).
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    print(f"[fetch-shared-zip] {len(names)} entries:", flush=True)
    for n in names[:20]:
        print(f"  {n}", flush=True)

    (out / "_manifest.txt").write_text("\n".join(names))
    print("[fetch-shared-zip] done", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
