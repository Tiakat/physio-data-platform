"""
Phase 1a - inventory EVERY project folder in Dropbox (the migration source of truth).

    python -m physio.inventory_dropbox                    # all folders under DROPBOX_ROOT
    python -m physio.inventory_dropbox --project PVB-ABDO # one project (code or Dropbox name)

Why this exists: the old sync only looked at  <project>/Database/RawData, so any
project laid out differently (Colectomie, PVB abdo, POEGEA ...) failed silently
and only 8 projects reached Azure. Here every top-level folder is listed and
reported, configured or not:

    status data       -> in scope, will be synced
    status review     -> listed + classified, synced only with --include-review
    status docs_only  -> listed, not synced
    UNCONFIGURED      -> folder missing from config/projects.yaml -> fix the config

Outputs (reports/phase1/):
    dropbox_inventory.csv   one row per file, with classification
    dropbox_projects.csv    one row per top-level folder (files, bytes, in-scope)

Identity: Dropbox `content_hash` (block SHA-256) is recorded for every file
without downloading anything; SHA-256 of the bytes is computed at upload time.
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import time
from collections import defaultdict
from pathlib import Path

from physio.classify import Classifier
from physio.common import DROPBOX_ROOT, dropbox_client, load_projects

FIELDS = ["project", "dropbox_folder", "status", "in_scope", "data_root", "dropbox_path",
          "relative_path", "name", "size_bytes", "content_hash", "server_modified", "file_id",
          "subject", "subject_evidence", "session_date", "source", "source_evidence", "stage",
          "rule_id", "confidence", "loose", "canonical_path", "notes"]


def list_recursive(dbx, path):
    import dropbox
    for attempt in range(5):
        try:
            res = dbx.files_list_folder(path, recursive=True, limit=2000)
            break
        except dropbox.exceptions.ApiError:
            raise
        except Exception as exc:                      # network hiccup -> retry
            print(f"   retry list ({exc})")
            time.sleep(2 ** attempt)
    else:
        raise RuntimeError(f"cannot list {path}")
    while True:
        for e in res.entries:
            if isinstance(e, dropbox.files.FileMetadata):
                yield e
        if not res.has_more:
            return
        res = dbx.files_list_folder_continue(res.cursor)


def top_folders(dbx):
    import dropbox
    res = dbx.files_list_folder(DROPBOX_ROOT)
    out = []
    while True:
        out += [e.name for e in res.entries if isinstance(e, dropbox.files.FolderMetadata)]
        if not res.has_more:
            return sorted(out)
        res = dbx.files_list_folder_continue(res.cursor)


def scope_of(rel: str, cfg: dict) -> tuple[bool, str]:
    """Is `rel` (path inside the project folder) under a data root and not excluded?"""
    if cfg.get("status") not in ("data", "review"):
        return False, ""
    parts = rel.split("/")
    for pat in cfg.get("exclude_folders", []):
        if any(fnmatch.fnmatch(p.lower(), pat.lower()) for p in parts[:-1]):
            return False, ""
    roots = cfg.get("data_roots") or [""]
    for root in roots:
        r = root.strip("/")
        if not r or rel.lower().startswith(r.lower() + "/"):
            return True, r
    return False, ""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="project code or Dropbox folder name")
    ap.add_argument("--out", default="reports/phase1")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    pcfg = load_projects()["projects"]
    by_folder = {v["dropbox"].lower(): (code, v) for code, v in pcfg.items()}
    clf = Classifier()
    dbx = dropbox_client()
    print(f"Dropbox account: {dbx.users_get_current_account().name.display_name}")

    folders = top_folders(dbx)
    if a.project:
        want = a.project.lower()
        folders = [f for f in folders
                   if f.lower() == want or by_folder.get(f.lower(), ("",))[0].lower() == want]
    print(f"{len(folders)} top-level folders under {DROPBOX_ROOT}")

    summary = defaultdict(lambda: defaultdict(int))
    inv_path = out / ("dropbox_inventory.csv" if not a.project else f"dropbox_inventory_{a.project}.csv")
    with open(inv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for folder in folders:
            code, cfg = by_folder.get(folder.lower(), (None, {"status": "UNCONFIGURED"}))
            code = code or folder
            status = cfg.get("status")
            base = f"{DROPBOX_ROOT}/{folder}"
            print(f"\n== {folder}  ->  {code}  [{status}]")
            n = 0
            for e in list_recursive(dbx, base):
                rel = e.path_display[len(base):].lstrip("/")
                in_scope, root = scope_of(rel, cfg)
                row = {"project": code, "dropbox_folder": folder, "status": status,
                       "in_scope": in_scope, "data_root": root, "dropbox_path": e.path_display,
                       "relative_path": rel, "name": e.name, "size_bytes": e.size,
                       "content_hash": e.content_hash,
                       "server_modified": e.server_modified.isoformat(), "file_id": e.id}
                if in_scope:
                    r = clf.classify(f"{code}/{rel}").row()
                    for k in ("subject", "subject_evidence", "session_date", "source",
                              "source_evidence", "stage", "rule_id", "confidence", "loose",
                              "canonical_path", "notes"):
                        row[k] = r[k]
                w.writerow(row)
                s = summary[(code, folder, status)]
                s["files"] += 1
                s["bytes"] += e.size
                s["in_scope_files"] += int(in_scope)
                s["in_scope_bytes"] += e.size if in_scope else 0
                n += 1
                if n % 2000 == 0:
                    print(f"   {n} files ...")
            print(f"   {n} files")

    with open(out / "dropbox_projects.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["project", "dropbox_folder", "status", "files", "gb", "in_scope_files", "in_scope_gb"])
        for (code, folder, status), s in sorted(summary.items()):
            w.writerow([code, folder, status, s["files"], round(s["bytes"] / 1e9, 3),
                        s["in_scope_files"], round(s["in_scope_bytes"] / 1e9, 3)])
            flag = "  <-- add to config/projects.yaml" if status == "UNCONFIGURED" else ""
            print(f"{code:22} {status:13} {s['files']:7} files  in-scope {s['in_scope_files']:7}{flag}")
    print(f"\nDONE -> {inv_path}")


if __name__ == "__main__":
    main()
