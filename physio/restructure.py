"""
Phase 4 - move the legacy Azure layout to the canonical architecture, safely.

Legacy:     PROJECT/<subject>/<whatever folders>/file          (old sync)
Canonical:  PROJECT/Database/<RawData|ExtractedData|ProcessedData|Metadata>/<subject>/<source>/.../file

Four separate steps, each re-runnable; nothing is deleted until the last one:

    python -m physio.restructure plan                 # -> reports/phase4/restructure_plan.csv (read-only)
    python -m physio.restructure copy [--project X]   # server-side copy legacy -> canonical (no download)
    python -m physio.restructure verify               # size + MD5 + content-hash of every copy
    python -m physio.restructure delete-legacy --confirm   # only blobs whose copy is VERIFIED

Every action is appended to data/_ingestion/manifests/restructure_log.jsonl.
Blobs whose subject/source cannot be resolved stay where they are (action=review).
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from physio.classify import Classifier
from physio.common import ROOT, azure_container, norm_key

PLAN = Path("reports/phase4/restructure_plan.csv")
LOG = ROOT / "data/_ingestion/manifests/restructure_log.jsonl"
FIELDS = ["project", "action", "reason", "legacy_path", "target_path", "size_bytes",
          "content_md5", "dropbox_content_hash", "subject", "source", "stage", "confidence"]
TEMPLATE_NAMES = {".keep", ".gitkeep", "readme.md", "placeholder.txt", ".placeholder"}


def log(e: dict):
    e.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(e, ensure_ascii=False) + "\n")


def last_status() -> dict:
    st = {}
    if LOG.exists():
        for line in open(LOG, encoding="utf-8"):
            try:
                e = json.loads(line)
                st[e["legacy_path"]] = e
            except (json.JSONDecodeError, KeyError):
                pass
    return st


# --------------------------------------------------------------------------- plan
def make_plan(inventory: str, out: Path = PLAN) -> list[dict]:
    clf = Classifier()
    blobs = list(csv.DictReader(open(inventory, encoding="utf-8-sig")))
    existing = {norm_key(b["full_path"]): b for b in blobs}
    rows = []
    for b in blobs:
        parts = b["full_path"].split("/")
        if len(parts) > 1 and parts[1] == "Database":           # already canonical
            continue
        r = clf.classify(b["full_path"])
        row = {"project": parts[0], "legacy_path": b["full_path"], "size_bytes": b["size_bytes"],
               "content_md5": b.get("content_md5", ""),
               "dropbox_content_hash": b.get("dropbox_content_hash", ""),
               "subject": r.subject or "", "source": r.source or "", "stage": r.stage,
               "confidence": r.confidence, "target_path": r.canonical_path or "", "reason": ""}
        if r.stage == "IGNORE" or r.project not in clf.projects:
            row.update(action="skip", reason="; ".join(r.notes) or "ignored")
        elif not r.canonical_path:
            row.update(action="review", reason="no classification rule")
        elif not r.subject and r.stage not in ("METADATA",):
            row.update(action="review", reason="subject not resolvable")
        elif not r.source and r.stage in ("RAW", "EXTRACTED"):
            row.update(action="review", reason="source not resolvable")
        else:
            row["action"] = "copy"
        rows.append(row)

    # two different contents -> same target name: keep the original sub-path
    by_target = defaultdict(list)
    for row in rows:
        if row["action"] == "copy":
            by_target[norm_key(row["target_path"])].append(row)
    cid = lambda g: g["dropbox_content_hash"] or (g["content_md5"] + g["size_bytes"]) or g["legacy_path"]
    for group in by_target.values():
        firsts = {}
        for g in group:                              # identical bytes -> one copy, rest duplicates
            if cid(g) in firsts:
                g.update(action="duplicate", reason=f"same content as {firsts[cid(g)]['legacy_path']}")
            else:
                firsts[cid(g)] = g
        if len(firsts) > 1:                          # different bytes, same name: keep origin sub-path
            for g in firsts.values():
                layer = g["target_path"].split("/")[2]
                rel = g["legacy_path"].split("/", 1)[1]
                g["target_path"] = "/".join([g["project"], "Database", layer, g["subject"] or "_unassigned",
                                             g["source"] or "_unknown", "_from_legacy", rel])
                g["reason"] = "name collision (different content) -> original sub-path kept"
        for g in group:                              # duplicates verify against their twin's target
            if g["action"] == "duplicate":
                g["target_path"] = firsts[cid(g)]["target_path"]

    # target already exists (e.g. uploaded by physio.sync)?
    for row in rows:
        t = existing.get(norm_key(row["target_path"]))
        if row["action"] == "copy" and t:
            same = (t.get("dropbox_content_hash") and t["dropbox_content_hash"] == row["dropbox_content_hash"]) \
                or (t.get("content_md5") and t["content_md5"] == row["content_md5"])
            row.update(action="already_there" if same else "conflict",
                       reason="identical blob already at target" if same else "different blob at target")

    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)
    c = defaultdict(Counter)
    for r in rows:
        c[r["project"]][r["action"]] += 1
    acts = ["copy", "already_there", "duplicate", "review", "conflict", "skip"]
    lines = ["| project | " + " | ".join(acts) + " |", "|---|" + "---:|" * len(acts)]
    lines += [f"| {p} | " + " | ".join(str(c[p][a]) for a in acts) + " |" for p in sorted(c)]
    (out.parent / "restructure_summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return rows


# --------------------------------------------------------------------------- copy / verify
def source_url(cc, name: str) -> str:
    """Authorised URL of a blob in the same account (SAS signed locally with the account key)."""
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas
    blob = cc.get_blob_client(name)
    key = os.getenv("AZURE_STORAGE_KEY")
    exp = datetime.now(timezone.utc) + timedelta(hours=2)
    if key:
        sas = generate_blob_sas(cc.account_name, cc.container_name, name, account_key=key,
                                permission=BlobSasPermissions(read=True), expiry=exp)
    else:
        svc = cc._get_blob_service_client() if hasattr(cc, "_get_blob_service_client") else None
        udk = svc.get_user_delegation_key(datetime.now(timezone.utc), exp)
        sas = generate_blob_sas(cc.account_name, cc.container_name, name, user_delegation_key=udk,
                                permission=BlobSasPermissions(read=True), expiry=exp)
    return f"{blob.url}?{sas}"


def copy_one(cc, row: dict) -> dict:
    src, dst = cc.get_blob_client(row["legacy_path"]), cc.get_blob_client(row["target_path"])
    base = {"legacy_path": row["legacy_path"], "target_path": row["target_path"], "step": "copy"}
    if dst.exists():
        return {**base, "status": "EXISTS"}
    sp = src.get_blob_properties()
    md = dict(sp.metadata or {})
    md.update({"legacy_path": _ascii(row["legacy_path"]), "restructured_at": datetime.now(timezone.utc).isoformat(),
               "subject": row["subject"], "source": row["source"], "stage": row["stage"]})
    dst.start_copy_from_url(source_url(cc, row["legacy_path"]), metadata=md,
                            requires_sync=False)
    for _ in range(600):
        st = dst.get_blob_properties().copy.status
        if st != "pending":
            break
        time.sleep(2)
    return {**base, "status": "COPIED" if st == "success" else f"COPY_{st}".upper()}


def verify_one(cc, row: dict) -> dict:
    src, dst = cc.get_blob_client(row["legacy_path"]), cc.get_blob_client(row["target_path"])
    base = {"legacy_path": row["legacy_path"], "target_path": row["target_path"], "step": "verify"}
    try:
        s, d = src.get_blob_properties(), dst.get_blob_properties()
    except Exception as exc:
        return {**base, "status": "MISSING_COPY", "error": str(exc)}
    ok = s.size == d.size
    if s.content_settings.content_md5 and d.content_settings.content_md5:
        ok = ok and bytes(s.content_settings.content_md5) == bytes(d.content_settings.content_md5)
    sh, dh = (s.metadata or {}).get("dropbox_content_hash"), (d.metadata or {}).get("dropbox_content_hash")
    if sh and dh:
        ok = ok and sh == dh
    return {**base, "status": "VERIFIED" if ok else "MISMATCH"}


def _ascii(s: str) -> str:
    from urllib.parse import quote
    return quote(s)


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("step", choices=["plan", "copy", "verify", "delete-legacy"])
    ap.add_argument("--inventory", default="reports/phase1/azure_inventory.csv")
    ap.add_argument("--project")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--confirm", action="store_true", help="required for delete-legacy")
    a = ap.parse_args()

    if a.step == "plan":
        make_plan(a.inventory)
        return
    rows = list(csv.DictReader(open(PLAN, encoding="utf-8")))
    if a.project:
        rows = [r for r in rows if r["project"].lower() == a.project.lower()]
    st = last_status()
    cc = azure_container()

    if a.step == "copy":
        todo = [r for r in rows if r["action"] == "copy"
                and st.get(r["legacy_path"], {}).get("status") not in ("COPIED", "EXISTS", "VERIFIED", "DELETED")]
        fn = copy_one
    elif a.step == "verify":
        todo = [r for r in rows if r["action"] in ("copy", "already_there", "duplicate")
                and st.get(r["legacy_path"], {}).get("status") not in ("VERIFIED", "DELETED")]
        fn = verify_one
    else:
        if not a.confirm:
            ap.error("delete-legacy removes the old blobs: re-run with --confirm after reviewing verify results")
        ok = {p for p, e in st.items() if e.get("status") == "VERIFIED" and e.get("step") == "verify"}
        todo = [r for r in rows if r["legacy_path"] in ok]
        fn = None
    todo = todo[:a.limit] if a.limit else todo
    print(f"{a.step}: {len(todo)} blobs")
    counts = Counter()
    for i, r in enumerate(todo, 1):
        try:
            if fn:
                res = fn(cc, r)
            else:
                cc.get_blob_client(r["legacy_path"]).delete_blob()
                res = {"legacy_path": r["legacy_path"], "target_path": r["target_path"],
                       "step": "delete", "status": "DELETED"}
        except Exception as exc:
            res = {"legacy_path": r["legacy_path"], "target_path": r["target_path"], "step": a.step,
                   "status": "FAILED", "error": f"{type(exc).__name__}: {exc}"}
        log(res)
        counts[res["status"]] += 1
        if i % 100 == 0 or res["status"] not in ("COPIED", "VERIFIED", "DELETED", "EXISTS"):
            print(f"[{i}/{len(todo)}] {res['status']:12} {r['legacy_path']}")
    print("SUMMARY", dict(counts))


if __name__ == "__main__":
    main()
