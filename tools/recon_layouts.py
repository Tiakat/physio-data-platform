"""Recon: folder layouts + filename shapes for the four parser-less projects.

Read-only Dropbox walk. Records, per project:
  - directory tree (depth 4) with names redacted to shapes:
      known template names kept verbatim, digit-only -> <n>, else -> <label>
  - how many .csv files match each known tier-A parser pattern
    (infinity / bettercare / nol_medasense / pump)
  - how many files match the BIS binary export pattern
  - anonymized name shapes for unmatched files (digits -> '#')

Plaintext digest only: reports/recon/layouts_<ts>.json
No real names, no file contents, no patient information.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from fnmatch import fnmatch
from pathlib import Path

TARGETS = ("MONREPI", "POSBRAIN", "COLECTOMIE", "PVB-ABDO")

KNOWN = {"database", "rawdata", "photos", "extracteddata", "analyzeddata",
         "documents", "infinity", "bettercare", "nol", "bis", "devices",
         "the infinity", "superposition", "included patients"}

TIER_A = {
    "infinity": "*.infinity.data.*.csv",
    "bettercare": "1.2.826.*.csv",
    "nol_medasense": "*_ExcelData*.csv",
    "pump": "Perf_*.csv",
}
BIS_RE = re.compile(r"^[LS](\d{8})(-\d+)?\.(ara|h_a|m_a|r2a|o_a|spa|t_a|e_a|f_a)$")


def shape_seg(seg: str) -> str:
    if seg.lower() in KNOWN:
        return seg
    if re.fullmatch(r"\d+", seg):
        return "<n>"
    return "<label>"


def shape_name(name: str) -> str:
    stem, dot, ext = name.partition(".")
    stem = re.sub(r"\d+", "#", stem)
    return stem + (dot + ext if dot else "")


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tools import sync_dropbox_cloud
    from tools.daily_pipeline import ACCOUNT, REPORTS
    from tools import azure_auth
    from tools.legacy_pipeline import list_dropbox_tree, select_projects

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    dbx = sync_dropbox_cloud.get_dropbox_client()
    projects, _ = select_projects(dbx)
    projects = [p for p in projects if p["code"] in TARGETS]

    result = {}
    for proj in sorted(projects, key=lambda p: p["code"]):
        code = proj["code"]
        tree: Counter = Counter()
        tier_match: Counter = Counter()
        bis = 0
        unmatched_shapes: Counter = Counter()
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
                if any(rel == x or rel.startswith(x + "/") for x in excl):
                    continue
                parts = rel.split("/")
                tree["/".join(shape_seg(p) for p in parts[:4])] += 1
                if e.get("is_dir"):
                    continue
                n_files += 1
                name = e["name"]
                matched = False
                if name.lower().endswith(".csv"):
                    for parser, pat in TIER_A.items():
                        if fnmatch(name, pat):
                            tier_match[parser] += 1
                            matched = True
                            break
                if not matched and BIS_RE.match(name):
                    bis += 1
                    matched = True
                if not matched:
                    unmatched_shapes[shape_name(name)] += 1
        result[code] = {
            "data_roots": proj["data_roots"],
            "n_files": n_files,
            "tree_top": [[t, c] for t, c in tree.most_common(25)],
            "tier_a_matches": dict(tier_match),
            "bis_binary": bis,
            "unmatched_shapes": [[s, c]
                                 for s, c in unmatched_shapes.most_common(15)],
        }
        print(f"[recon] {code}: {n_files} files, "
              f"tierA={dict(tier_match)}, bis={bis}", flush=True)

    payload = json.dumps({"generated_utc": ts, "projects": result},
                         indent=1).encode("utf-8")
    svc = azure_auth.get_blob_service_client(ACCOUNT)
    azure_auth.ensure_container(svc, REPORTS)
    svc.get_blob_client(container=REPORTS,
                        blob=f"recon/layouts_{ts}.json").upload_blob(
                            payload, overwrite=True)
    print(f"[recon] wrote reports/recon/layouts_{ts}.json", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
