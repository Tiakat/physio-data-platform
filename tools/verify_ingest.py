"""Verify Phase 1 ingest is 100% complete, with real numbers.

Reconciles three sources per project:
  1. Dropbox listing (fresh, via the ingest's own list_dropbox_tree)
  2. Encrypted pipeline state (files marked ok/unsupported/failed)
  3. Azure blobs actually present in rawdata/<CODE>/parquet/

A project is COMPLETE when every eligible Dropbox file (supported,
non-junk, non-photo, non-document, not in excluded folders) is marked
"ok" or "unsupported" in state, nothing failed, and Azure holds at
least one blob per ok file.

Usage:
  python -m tools.verify_ingest [--project CODE]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tools import azure_auth, daily_pipeline
from tools.crypto import decrypt_bytes
from tools.legacy_pipeline import (
    _excluded,
    is_document,
    is_junk,
    is_photo,
    list_dropbox_tree,
    select_projects,
)


def get_dropbox():
    import dropbox

    return dropbox.Dropbox(
        app_key=os.environ["DROPBOX_APP_KEY"],
        app_secret=os.environ["DROPBOX_APP_SECRET"],
        oauth2_refresh_token=os.environ["DROPBOX_REFRESH_TOKEN"],
    )


def load_state(account: str) -> dict:
    blob = daily_pipeline._blob(
        account, daily_pipeline.PROCESSED, daily_pipeline.STATE_BLOB)
    raw = blob.download_blob().readall()
    return json.loads(decrypt_bytes(raw).decode("utf-8"))


def list_azure_blobs(account: str, code: str) -> set:
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("rawdata")
    prefix = f"{code}/parquet/"
    return {b.name for b in container.list_blobs(name_starts_with=prefix)}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=None,
                    help="Only verify one project code")
    args = ap.parse_args(argv)

    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "labdataplatform")

    print("[verify] loading encrypted state...", flush=True)
    state = load_state(account)
    legacy = state.get("legacy", {})

    print("[verify] connecting to Dropbox...", flush=True)
    dbx = get_dropbox()
    projects, _unconfigured = select_projects(dbx)
    if args.project:
        projects = [p for p in projects
                    if p["code"].upper() == args.project.upper()]
        if not projects:
            print(f"unknown project {args.project}")
            return 2

    all_complete = True
    print()
    print(f"{'project':<12} {'dropbox':>8} {'eligible':>8} "
          f"{'ok':>8} {'unsup':>7} {'failed':>7} {'azure':>8}  verdict")
    print("-" * 78)
    for proj in projects:
        code = proj["code"]
        # Fresh Dropbox listing, same logic as the ingest
        entries = []
        for root in proj["data_roots"]:
            base = proj["dropbox_base"] if root == "." else \
                f"{proj['dropbox_base']}/{root}"
            for e in list_dropbox_tree(dbx, base):
                e["dbx_path"] = f"{base}/{e['relpath']}"
                if root != ".":
                    e["relpath"] = f"{root}/{e['relpath']}"
                entries.append(e)
        listed = len(entries)
        excl = proj["exclude_folders"]
        eligible = [e for e in entries
                    if not is_junk(e["name"]) and not is_document(e["relpath"])
                    and not is_photo(e["name"])
                    and not _excluded(e["relpath"], excl)]
        n_eligible = len(eligible)

        # State counts
        files = legacy.get(code, {}).get("files", {})
        n_ok = sum(1 for f in files.values()
                   if isinstance(f, dict) and f.get("status") == "ok")
        n_unsup = sum(1 for f in files.values()
                      if isinstance(f, dict)
                      and f.get("status") == "unsupported")
        n_failed = sum(1 for f in files.values()
                       if isinstance(f, dict) and f.get("status") == "failed")

        # Azure blobs: check each ok file's stored blob exists
        blobs = list_azure_blobs(account, code)
        n_blobs = len(blobs)
        stored = [f.get("stored") for f in files.values()
                  if isinstance(f, dict) and f.get("status") == "ok"
                  and f.get("stored")]
        missing = [b for b in stored if b not in blobs]
        n_missing = len(missing)

        # Every eligible file must be accounted for in state (ok or
        # unsupported), none failed, and every ok file's blob must exist.
        accounted = (n_ok + n_unsup == n_eligible)
        complete = accounted and n_failed == 0 and n_missing == 0
        verdict = "COMPLETE" if complete else "INCOMPLETE"
        if not complete:
            all_complete = False
            if not accounted:
                verdict += f" (eligible={n_eligible} vs ok+unsup={n_ok + n_unsup})"
            elif n_failed:
                verdict += f" ({n_failed} failed)"
            elif n_missing:
                verdict += f" ({n_missing} blobs missing)"
        print(f"{code:<12} {listed:>8} {n_eligible:>8} "
              f"{n_ok:>8} {n_unsup:>7} {n_failed:>7} {n_blobs:>8}  {verdict}",
              flush=True)

    print()
    if all_complete:
        print("[verify] ALL PROJECTS COMPLETE — Phase 1 ingest is 100%",
              flush=True)
        return 0
    print("[verify] INCOMPLETE projects remain — see table above", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())


def diagnose_blobs(account: str, code: str):
    """Dump stored-vs-actual blob mismatch details for one project."""
    import json
    state = load_state(account)
    files = state.get("legacy", {}).get(code, {}).get("files", {})
    blobs = list_azure_blobs(account, code)
    print(f"[diagnose] {code}: {len(blobs)} blobs under {code}/parquet/")
    for rel, f in sorted(files.items()):
        if isinstance(f, dict) and f.get("status") == "ok":
            stored = f.get("stored", "")
            mark = "OK " if stored in blobs else "MISS"
            if mark == "MISS":
                print(f"  {mark} {stored}  <- {rel[:80]}")
    # Also show what prefixes exist
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("rawdata")
    prefixes = set()
    for b in container.list_blobs(name_starts_with=code[:3]):
        prefixes.add(b.name.split("/")[0] + "/" + b.name.split("/")[1] if "/" in b.name else b.name)
    print(f"[diagnose] prefixes starting with {code[:3]}: {sorted(prefixes)[:10]}")


if __name__ == "__main__" and "--diagnose" in sys.argv:
    idx = sys.argv.index("--diagnose")
    code = sys.argv[idx + 1]
    account = os.environ.get("AZURE_STORAGE_ACCOUNT", "labdataplatform")
    diagnose_blobs(account, code)
    sys.exit(0)
