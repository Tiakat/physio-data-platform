"""Fetch files from a Dropbox shared link (folder).

Uses the Dropbox API with the shared link to list contents and download
files. For K's analysis zips shared via link.

Usage:
  python -m tools.fetch_shared_link --url "https://www.dropbox.com/scl/fo/..." --out fetched/
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools.sync_dropbox_cloud import get_dropbox_client  # noqa: E402


def api_call(endpoint, args, access_token):
    """Call Dropbox API with shared link context."""
    resp = requests.post(
        f"https://api.dropboxapi.com/2/{endpoint}",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        json=args,
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def download_shared(access_token, shared_url, path, out_path):
    """Download a file from a shared link."""
    resp = requests.post(
        "https://content.dropboxapi.com/2/files/download",
        headers={
            "Authorization": f"Bearer {access_token}",
            "Dropbox-API-Arg": (
                '{"shared_link": {"url": "%s"}, "path": "%s"}'
                % (shared_url, path)
            ),
        },
        timeout=300,
    )
    resp.raise_for_status()
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    Path(out_path).write_bytes(resp.content)
    return len(resp.content)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="Shared link URL.")
    ap.add_argument("--out", required=True, help="Output directory.")
    ap.add_argument("--max-files", type=int, default=20)
    args = ap.parse_args(argv)

    # Get access token via the existing client machinery.
    dbx = get_dropbox_client()
    # The client object has the token; use it for raw API calls.
    token = dbx._oauth2_access_token

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    # List the shared folder.
    shared = {"url": args.url}
    result = api_call("files/list_folder",
                      {"shared_link": shared, "recursive": True}, token)
    entries = result.get("entries", [])
    while result.get("has_more"):
        result = api_call(
            "files/list_folder/continue",
            {"cursor": result["cursor"]}, token)
        entries += result.get("entries", [])

    files = [e for e in entries if e.get(".tag") == "file"]
    print(f"[fetch-shared] {len(files)} files in shared link", flush=True)
    for e in files[:10]:
        print(f"  {e['path_display']} ({e['size']} bytes)", flush=True)

    # Download (cap at max-files).
    manifest = []
    for e in files[:args.max_files]:
        rel = e["path_display"].lstrip("/")
        dest = out / rel
        try:
            n = download_shared(token, args.url, e["path_lower"], dest)
            print(f"[fetch-shared] got {rel} ({n} bytes)", flush=True)
            manifest.append({"path": rel, "size": n, "status": "ok"})
        except Exception as exc:  # noqa: BLE001
            print(f"[fetch-shared] FAILED {rel}: {exc}", flush=True)
            manifest.append({"path": rel, "status": f"error: {exc}"})

    import json
    (out / "_manifest.json").write_text(json.dumps(manifest, indent=1))
    print(f"[fetch-shared] done: {len(manifest)} files", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
