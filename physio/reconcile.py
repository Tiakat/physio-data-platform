"""
Phase 2 - reconcile the Dropbox manifest against the Azure inventory.

    python -m physio.reconcile \
        --dropbox reports/phase1/dropbox_inventory.csv \
        --azure   reports/phase1/azure_inventory.csv      # or the older azure_full_inventory.csv

Every in-scope Dropbox file gets exactly one status:

    MATCHED        same Dropbox content_hash found on an Azure blob (content identity)
    MATCHED_SIZE   same path + same size, but the blob carries no hash yet
                   (legacy upload; `physio.sync --verify-legacy` upgrades it)
    CONFLICT       same path, different size/hash  -> never overwritten, human decides
    MISSING        not in Azure                    -> uploaded by physio.sync
    DUPLICATE      identical content to another Dropbox file already handled
and an action:  none | upload | review | conflict | link

Azure blobs that no Dropbox file explains are reported as AZURE_ONLY.
Nothing is uploaded, moved or deleted here.
"""
from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

from physio.common import norm_key

OUT_FIELDS = ["project", "status", "action", "reason", "dropbox_path", "relative_path",
              "size_bytes", "content_hash", "subject", "source", "stage", "confidence",
              "target_path", "azure_path", "azure_size", "duplicate_of"]


def legacy_paths(project: str, rel: str) -> list[str]:
    """Where the old sync put a file: PROJECT/<path below Database/RawData>."""
    out = [f"{project}/{rel}"]
    low = rel.lower()
    if low.startswith("database/rawdata/"):
        out.insert(0, f"{project}/{rel[len('Database/RawData/'):]}")
    return out


def load_azure(path: Path):
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    by_path, by_hash = {}, defaultdict(list)
    for r in rows:
        r["size_bytes"] = int(r.get("size_bytes") or 0)
        by_path[norm_key(r["full_path"])] = r
        h = r.get("dropbox_content_hash")
        if h:
            by_hash[h].append(r)
    return rows, by_path, by_hash


def needs_review(r: dict) -> str | None:
    if r["status"] == "review":
        return "project layout not confirmed (status: review)"
    if r["status"] == "UNCONFIGURED":
        return "project missing from config/projects.yaml"
    if not r.get("subject"):
        return "subject not found in path or name"
    if not r.get("source") and r.get("stage") not in ("METADATA", "DERIVED"):
        return "source unknown"
    if r.get("stage") in ("UNKNOWN", "", None):
        return "no classification rule"
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dropbox", default="reports/phase1/dropbox_inventory.csv")
    ap.add_argument("--azure", default="reports/phase1/azure_inventory.csv")
    ap.add_argument("--out", default="reports/phase2")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)

    az_rows, az_by_path, az_by_hash = load_azure(Path(a.azure))
    dbx = [r for r in csv.DictReader(open(a.dropbox, encoding="utf-8-sig"))
           if r["in_scope"] == "True" and r.get("stage") != "IGNORE"]
    print(f"Dropbox in-scope files: {len(dbx)}   Azure blobs: {len(az_rows)}")

    used_azure, seen_hash, targets = set(), {}, defaultdict(set)
    for r in dbx:                                    # distinct CONTENTS per proposed name
        targets[norm_key(r["canonical_path"] or "")].add(r["content_hash"])

    results = []
    for r in sorted(dbx, key=lambda x: x["dropbox_path"]):
        size, h, proj = int(r["size_bytes"]), r["content_hash"], r["project"]
        target = r["canonical_path"]
        if target and len(targets[norm_key(target)]) > 1:        # two sources -> same name: keep origin path
            layer = target.split("/")[2]
            target = "/".join([proj, "Database", layer, r["subject"] or "_unassigned",
                               r["source"] or "_unknown", r["relative_path"]])
        res = {k: r.get(k, "") for k in ("project", "dropbox_path", "relative_path", "subject",
                                          "source", "stage", "confidence")}
        res.update(size_bytes=size, content_hash=h, target_path=target, azure_path="",
                   azure_size="", duplicate_of="", reason="")

        hit = next((b for b in az_by_hash.get(h, []) if b["size_bytes"] == size), None)
        if hit:
            res.update(status="MATCHED", action="none", azure_path=hit["full_path"])
        else:
            cand = [c for c in [target] + legacy_paths(proj, r["relative_path"]) if c]
            blob = next((az_by_path[norm_key(c)] for c in cand if norm_key(c) in az_by_path), None)
            if blob and blob["size_bytes"] == size and not blob.get("dropbox_content_hash"):
                res.update(status="MATCHED_SIZE", action="none", azure_path=blob["full_path"])
            elif blob:
                res.update(status="CONFLICT", action="conflict", azure_path=blob["full_path"],
                           azure_size=blob["size_bytes"],
                           reason="same path, different size" if blob["size_bytes"] != size
                           else "same path, different content hash")
            elif h in seen_hash:
                res.update(status="DUPLICATE", action="link", duplicate_of=seen_hash[h])
            else:
                why = needs_review(r)
                res.update(status="MISSING", action="review" if why else "upload", reason=why or "")
        if res["azure_path"]:
            used_azure.add(norm_key(res["azure_path"]))
        seen_hash.setdefault(h, r["dropbox_path"])
        results.append(res)

    with open(out / "reconciliation.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_FIELDS)
        w.writeheader()
        w.writerows(results)

    azure_only = [b for b in az_rows if norm_key(b["full_path"]) not in used_azure]
    with open(out / "azure_only.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["full_path", "size_bytes"])
        w.writerows([[b["full_path"], b["size_bytes"]] for b in azure_only])

    # ---- summary
    by_proj = defaultdict(Counter)
    for x in results:
        by_proj[x["project"]][x["status"]] += 1
        by_proj[x["project"]]["upload_bytes"] += x["size_bytes"] if x["action"] == "upload" else 0
    az_only_proj = Counter(b["full_path"].split("/")[0] for b in azure_only)
    cols = ["MATCHED", "MATCHED_SIZE", "MISSING", "CONFLICT", "DUPLICATE"]
    lines = ["# Dropbox ↔ Azure reconciliation", "",
             "| project | " + " | ".join(cols) + " | to upload | review | upload GB | AZURE_ONLY |",
             "|---|" + "---:|" * (len(cols) + 4)]
    act = defaultdict(Counter)
    for x in results:
        act[x["project"]][x["action"]] += 1
    for p in sorted(set(by_proj) | set(az_only_proj)):
        c = by_proj[p]
        lines.append(f"| {p} | " + " | ".join(str(c[k]) for k in cols)
                     + f" | {act[p]['upload']} | {act[p]['review']} | {c['upload_bytes'] / 1e9:.2f} | {az_only_proj[p]} |")
    rev = Counter(x["reason"] for x in results if x["action"] == "review")
    lines += ["", "## Why files need review", "", "| reason | files |", "|---|---:|"]
    lines += [f"| {k} | {v} |" for k, v in rev.most_common()]
    (out / "reconciliation_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
