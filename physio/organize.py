"""
Keep every study in Dropbox organised like the lab template (config/template.yaml).

    python -m physio.organize plan  [--study PROMISES] [--inventory reports/phase1/dropbox_inventory.csv]
    python -m physio.organize apply [--study PROMISES] [--limit 200]      # moves files in Dropbox
    python -m physio.organize watch                                        # keeps fixing new files
    python -m physio.organize undo  --run <run_id>                         # moves a run back

Study layout (per study under /Liam/Projets actifs):

    Database/RawData/<n>/Photos
    Database/RawData/<n>/ExtractedData/{BetterCare,Infinity,NOL,BIS,Pump,TOF,Oximetry,Audio,EEG,...}
    Database/AnalyzedData/<n>
    Documents/Version finale approuvée/{FIC,CRF,Short,Protocol}
    Documents/Soumission ethique/            (approval letter)
    Documents/Soumission ethique/{Soumission initiale,Amendement 1,Amendement 2,...}
    Documents/Contrats legaux
    /Liam/Archives/<study>/Anciens documents  (old versions)

Rules
  * A file is moved only when its place is certain (device file-name signature +
    participant number, or a clear document rule). Everything else stays and is
    listed in reports/organize/unsure.csv.
  * Never overwrites: a move whose destination exists is skipped and reported.
  * Folder names with the wrong case ("database", "bettercare") are renamed.
  * Every move is logged in data/_organize/moves.jsonl with a run id -> `undo`.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import time
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from physio.classify import Classifier
from physio.common import CONFIG, ROOT, dropbox_client, load_projects

TPL = yaml.safe_load((CONFIG / "template.yaml").read_text(encoding="utf-8"))
STUDIES = TPL["studies_root"].rstrip("/")
ARCHIVES = TPL["archives_root"].rstrip("/")
DEV = TPL["devices"]
D = {k: re.compile(v) for k, v in TPL["documents"].items()}
MOVES_LOG = ROOT / "data/_organize/moves.jsonl"
REPORT = Path("reports/organize")
VF = "Documents/Version finale approuvée"
SE = "Documents/Soumission ethique"
ETHICS_CONTAINER = re.compile(r"^(soumission )?(ethique|cer|ces|comite( d.?)?(ethique)?)\b")
MONTHS = {"janvier": 1, "fevrier": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6, "juillet": 7,
          "aout": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12}


def plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().lower()
    return re.sub(r"\s+", " ", s).strip()


def date_key(name: str) -> str:
    n = plain(name)
    m = re.search(r"(20\d{2})[-_ ]?(\d{2})[-_ ]?(\d{2})", n)
    if m:
        return "".join(m.groups())
    m = re.search(r"(?<!\d)(\d{1,2})-(\d{2})-(20\d{2})", n)
    if m:
        return f"{m.group(3)}{m.group(2)}{int(m.group(1)):02d}"
    m = re.search(r"(?:(\d{1,2}) )?(" + "|".join(MONTHS) + r") (20\d{2})", n)
    if m:
        return f"{m.group(3)}{MONTHS[m.group(2)]:02d}{int(m.group(1) or 1):02d}"
    m = re.search(r"(20\d{2})", n)
    return f"{m.group(1)}0000" if m else "99999999"


# ======================================================================= planning
class Planner:
    def __init__(self):
        self.clf = Classifier()
        self.code_of = {v["dropbox"].lower(): k for k, v in load_projects()["projects"].items()}

    def data_target(self, study: str, rel: str):
        code = self.code_of.get(study.lower(), study)
        r = self.clf.classify(f"{code}/{rel}")
        if study in TPL.get("data_frozen", []) or D["never_touch"].search("/" + plain(rel)):
            return None, r
        generic = {None, "generic_pdf", "generic_csv", "master_database", "text_report", "recap_stats"}
        if r.rule_id in generic or r.confidence not in ("high", "medium") or not r.subject:
            return None, r
        if r.rule_id == "photo" and r.subject_evidence != "folder":
            return None, r
        n = r.subject
        if r.stage == "DERIVED":                      # keep the analysis name (e.g. infinity_filtered)
            pre = f"{r.pre}/" if r.pre else ""
            return f"Database/AnalyzedData/{n}/{pre}{r.tail}", r
        if r.source == "Photos":
            return f"Database/RawData/{n}/Photos/{r.tail}", r
        dev = DEV.get(r.source)
        if dev and r.stage in ("RAW", "EXTRACTED", "DOCUMENT"):
            return f"Database/RawData/{n}/ExtractedData/{dev}/{r.tail}", r
        return None, r

    def doc_target(self, rel: str, amend_no: dict):
        parts = rel.split("/")
        folders, name = parts[:-1], parts[-1]
        pf = [plain(f) for f in folders]
        full = "/".join(pf + [plain(name)])
        if D["never_touch"].search(full):
            return None, "system folder"

        def after(idx, keep_matched=False):
            start = idx if keep_matched else idx + 1
            return "/".join(folders[start:] + [name])

        def first(rx):
            return next((i for i, f in enumerate(pf) if rx.search("/" + f + "/")), None)

        i = first(D["old_versions"])
        if i is not None:
            return f"@ARCHIVES/Anciens documents/{after(i)}", "old version"
        i = first(D["amendment"])
        if i is not None:
            return f"{SE}/Amendement {amend_no[folders[i]]}/{after(i)}", "amendment"
        in_final = any(D["final_version"].search(f) for f in pf)
        if D["approval_name"].search(plain(name)):
            return f"{SE}/{name}", "approval letter"
        if in_final:
            sub = next((k.upper() if k in ("fic", "crf") else k.capitalize()
                        for k in ("fic", "crf", "short", "protocol") if D[k].search(plain(name))), None)
            return (f"{VF}/{sub}/{name}" if sub else f"{VF}/{name}"), "final approved version"
        i = first(D["ethics"])
        if i is not None:
            keep = not ETHICS_CONTAINER.match(pf[i])
            return f"{SE}/Soumission initiale/{after(i, keep_matched=keep)}", "ethics submission"
        i = first(D["contracts"])
        if i is not None:
            return f"Documents/Contrats legaux/{after(i)}", "contract"
        if D["contracts"].search(plain(name)):
            return f"Documents/Contrats legaux/{name}", "contract"
        return None, "no rule"

    def plan(self, files: list[dict]) -> list[dict]:
        """files: dicts with study, relative_path, dropbox_path, size_bytes, content_hash."""
        # amendment numbering per study
        amend = defaultdict(set)
        for f in files:
            for fo in f["relative_path"].split("/")[:-1]:      # outermost amendment folder only
                if D["amendment"].search("/" + plain(fo) + "/"):
                    amend[f["study"]].add(fo)
                    break
        amend_no = {}
        for study, names in amend.items():
            def key(n):
                m = re.search(r"am+ende?ment\s*(\d)(?![\d-])", plain(n))
                return (0, int(m.group(1)), "") if m else (1, 0, date_key(n) + plain(n))
            ordered = sorted(names, key=key)
            used, nums = set(), {}
            for n in ordered:
                m = re.search(r"am+ende?ment\s*(\d)(?![\d-])", plain(n))
                k = int(m.group(1)) if m else None
                if k is None or k in used:
                    k = max(used | {0}) + 1
                used.add(k)
                nums[n] = k
            amend_no[study] = nums

        rows = []
        for f in files:
            study, rel = f["study"], f["relative_path"]
            target, why, rule = None, "", ""
            t, r = self.data_target(study, rel)
            if t:
                target, why, rule = t, f"{r.source or ''} {r.stage.lower()} (participant {r.subject})", r.rule_id
            elif not rel.lower().startswith("database/"):
                t, why = self.doc_target(rel, amend_no.get(study, {}))
                target, rule = t, "document"
            else:
                why = "; ".join(r.notes) or "no confident rule"
            if target:
                full = (f"{ARCHIVES}/{study}/" + target[len("@ARCHIVES/"):] if target.startswith("@ARCHIVES/")
                        else f"{STUDIES}/{study}/{target}")
            else:
                full = ""
            rows.append({"study": study, "from": f["dropbox_path"], "to": full, "rule": rule,
                         "why": why, "size_bytes": f.get("size_bytes", ""),
                         "hash": f.get("content_hash", "")})

        # status: ok / move / conflict / leave
        existing = {r["from"].lower() for r in rows}
        groups = defaultdict(list)
        for r in rows:
            if r["to"]:
                groups[r["to"].lower()].append(r)
        dest = {}
        for k, g in groups.items():
            if len({x["hash"] for x in g}) == 1 and len(g) > 1:
                # identical copies: keep the one already in place / shortest path, others are duplicates
                g.sort(key=lambda x: (x["from"].lower() != k, len(x["from"])))
                for x in g[1:]:
                    x["status"], x["why"] = "duplicate", f"identical to {g[0]['from']}"
                dest[k] = 1
            else:
                dest[k] = len(g)
        for r in rows:
            if r.get("status") == "duplicate":
                continue
            if not r["to"]:
                r["status"] = "leave"
            elif r["to"] == r["from"]:
                r["status"] = "ok"
            elif r["to"].lower() == r["from"].lower():
                r["status"] = "case"          # fixed by folder rename
            elif dest[r["to"].lower()] > 1:
                r["status"], r["why"] = "conflict", "several files would get this name"
            elif r["to"].lower() in existing:
                r["status"], r["why"] = "conflict", "destination already exists"
            else:
                r["status"] = "move"
        return rows


def folder_renames(rows: list[dict]) -> list[tuple[str, str]]:
    """Existing folders whose name differs from the template only by case/spelling of case."""
    want = {}
    for r in rows:
        if r["to"] and r["status"] in ("move", "case", "ok"):
            parts = r["to"].split("/")
            for k in range(1, len(parts)):
                p = "/".join(parts[:k])
                want[p.lower()] = p
    have = set()
    for r in rows:
        parts = r["from"].split("/")
        for k in range(1, len(parts)):
            have.add("/".join(parts[:k]))
    out = []
    for p in sorted(have, key=lambda x: x.count("/")):
        w = want.get(p.lower())
        if w and w != p and w.rsplit("/", 1)[-1] != p.rsplit("/", 1)[-1]:
            out.append((p, w))
    return out


# ======================================================================= Dropbox I/O
def list_study_files(dbx, study: str | None):
    import dropbox
    res = dbx.files_list_folder(STUDIES, recursive=False)
    studies = []
    while True:
        studies += [e.name for e in res.entries if isinstance(e, dropbox.files.FolderMetadata)]
        if not res.has_more:
            break
        res = dbx.files_list_folder_continue(res.cursor)
    if study:
        studies = [s for s in studies if s.lower() == study.lower()]
    files = []
    for s in studies:
        base = f"{STUDIES}/{s}"
        res = dbx.files_list_folder(base, recursive=True, limit=2000)
        while True:
            for e in res.entries:
                if isinstance(e, dropbox.files.FileMetadata):
                    files.append({"study": s, "relative_path": e.path_display[len(base) + 1:],
                                  "dropbox_path": e.path_display, "size_bytes": e.size,
                                  "content_hash": e.content_hash})
            if not res.has_more:
                break
            res = dbx.files_list_folder_continue(res.cursor)
    return files


def files_from_inventory(path: str, study: str | None):
    out = []
    for r in csv.DictReader(open(path, encoding="utf-8-sig")):
        if study and r["dropbox_folder"].lower() != study.lower():
            continue
        out.append({"study": r["dropbox_folder"], "relative_path": r["relative_path"],
                    "dropbox_path": r["dropbox_path"], "size_bytes": r["size_bytes"],
                    "content_hash": r["content_hash"]})
    return out


def log_move(entry):
    MOVES_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(MOVES_LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def move_batch(dbx, pairs: list[tuple[str, str]], run_id: str, kind="file"):
    """Move (from, to) pairs with the batch API; never overwrite; log each result."""
    import dropbox
    from dropbox.files import RelocationPath
    done = Counter()
    for i in range(0, len(pairs), 500):
        chunk = pairs[i:i + 500]
        job = dbx.files_move_batch_v2([RelocationPath(a, b) for a, b in chunk], autorename=False)
        if job.is_async_job_id():
            jid = job.get_async_job_id()
            while True:
                st = dbx.files_move_batch_check_v2(jid)
                if not st.is_in_progress():
                    break
                time.sleep(2)
        else:
            st = job
        entries = st.get_complete().entries if st.is_complete() else []
        for (a, b), e in zip(chunk, entries):
            ok = e.is_success()
            err = "" if ok else str(e.get_failure())
            log_move({"run": run_id, "kind": kind, "from": a, "to": b, "ok": ok, "error": err,
                      "at": datetime.now(timezone.utc).isoformat()})
            done["moved" if ok else "failed"] += 1
        if st.is_failed():
            for a, b in chunk:
                log_move({"run": run_id, "kind": kind, "from": a, "to": b, "ok": False,
                          "error": str(st.get_failed())})
            done["failed"] += len(chunk)
        print(f"  {kind}s {i + len(chunk)}/{len(pairs)}  {dict(done)}")
    return done


def rename_folder(dbx, a, b, run_id):
    """Case-only rename needs a hop through a temporary name on some clients."""
    try:
        dbx.files_move_v2(a, b, autorename=False)
    except Exception:
        tmp = a + "__tmp_rename"
        dbx.files_move_v2(a, tmp, autorename=False)
        dbx.files_move_v2(tmp, b, autorename=False)
    log_move({"run": run_id, "kind": "folder", "from": a, "to": b, "ok": True,
              "at": datetime.now(timezone.utc).isoformat()})


# ======================================================================= commands
def write_reports(rows):
    REPORT.mkdir(parents=True, exist_ok=True)
    fields = ["study", "status", "from", "to", "rule", "why", "size_bytes", "hash"]
    for name, keep in (("plan.csv", lambda r: r["status"] in ("move", "case")),
                       ("unsure.csv", lambda r: r["status"] in ("leave", "conflict")),
                       ("duplicates.csv", lambda r: r["status"] == "duplicate"),
                       ("all.csv", lambda r: True)):
        with open(REPORT / name, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(r for r in rows if keep(r))
    c = defaultdict(Counter)
    for r in rows:
        c[r["study"]][r["status"]] += 1
    cols = ["ok", "move", "case", "duplicate", "conflict", "leave"]
    lines = ["| study | already OK | to move | case fix | duplicate (stays) | conflict (stays) | stays (unsure) |",
             "|---|---:|---:|---:|---:|---:|---:|"]
    lines += [f"| {s} | " + " | ".join(str(c[s][k]) for k in cols) + " |" for s in sorted(c)]
    t = Counter()
    for s in c.values():
        t.update(s)
    lines.append("| **total** | " + " | ".join(f"**{t[k]}**" for k in cols) + " |")
    why = Counter(r["why"] for r in rows if r["status"] == "leave")
    lines += ["", "Top reasons a file stays:", ""] + [f"- {v:6}  {k}" for k, v in why.most_common(12)]
    (REPORT / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\nDetails: {REPORT / 'plan.csv'}  |  {REPORT / 'unsure.csv'}")


def apply(dbx, rows, limit=None, run_id=None):
    run_id = run_id or datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    renames = folder_renames(rows)
    moves = [(r["from"], r["to"]) for r in rows if r["status"] == "move"]
    if limit:
        moves = moves[:limit]
    print(f"run {run_id}: {len(renames)} folder renames, {len(moves)} file moves")
    for a, b in renames:
        try:
            rename_folder(dbx, a, b, run_id)
            print(f"  renamed {a} -> {b}")
        except Exception as exc:
            print(f"  rename failed {a}: {exc}")
    if moves:
        move_batch(dbx, moves, run_id)
    print(f"undo with:  python -m physio.organize undo --run {run_id}")
    return run_id


def cmd_undo(dbx, run_id):
    entries = [json.loads(l) for l in open(MOVES_LOG, encoding="utf-8")]
    back = [(e["to"], e["from"]) for e in entries if e.get("run") == run_id and e.get("ok") and e["kind"] == "file"]
    folders = [(e["to"], e["from"]) for e in entries if e.get("run") == run_id and e.get("ok") and e["kind"] == "folder"]
    print(f"undo {run_id}: {len(back)} files, {len(folders)} folders")
    move_batch(dbx, back[::-1], run_id + "-undo")
    for a, b in folders[::-1]:
        rename_folder(dbx, a, b, run_id + "-undo")


def cmd_watch(dbx, planner, interval: int):
    """Long-poll Dropbox; whenever something changes, re-plan the touched studies and fix them."""
    cursor = dbx.files_list_folder_get_latest_cursor(STUDIES, recursive=True).cursor
    print(f"watching {STUDIES} (Ctrl+C to stop)")
    while True:
        lp = dbx.files_list_folder_longpoll(cursor, timeout=480)
        if lp.changes:
            time.sleep(interval)             # let an upload of many files finish
            touched = set()
            res = dbx.files_list_folder_continue(cursor)
            while True:
                for e in res.entries:
                    rel = e.path_display[len(STUDIES) + 1:] if e.path_display else ""
                    if "/" in rel:
                        touched.add(rel.split("/", 1)[0])
                cursor = res.cursor
                if not res.has_more:
                    break
                res = dbx.files_list_folder_continue(cursor)
            for study in sorted(touched):
                rows = planner.plan(list_study_files(dbx, study))
                todo = [r for r in rows if r["status"] == "move"] or folder_renames(rows)
                if todo:
                    print(f"{datetime.now():%H:%M:%S} {study}: fixing")
                    apply(dbx, rows)
            cursor = dbx.files_list_folder_get_latest_cursor(STUDIES, recursive=True).cursor
        if lp.backoff:
            time.sleep(lp.backoff)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=["plan", "apply", "watch", "undo", "skeleton", "cleanup"])
    ap.add_argument("--study", help="Dropbox study folder name, e.g. 'PVB abdo'")
    ap.add_argument("--inventory", help="plan from an inventory CSV instead of live Dropbox")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--run")
    ap.add_argument("--settle", type=int, default=60, help="watch: seconds to wait after a change")
    ap.add_argument("--confirm", action="store_true", help="cleanup: really delete empty folders")
    a = ap.parse_args()
    planner = Planner()

    if a.cmd == "plan" and a.inventory:
        write_reports(planner.plan(files_from_inventory(a.inventory, a.study)))
        return
    dbx = dropbox_client()
    if a.cmd == "plan":
        write_reports(planner.plan(list_study_files(dbx, a.study)))
    elif a.cmd == "apply":
        rows = planner.plan(list_study_files(dbx, a.study))
        write_reports(rows)
        apply(dbx, rows, a.limit)
    elif a.cmd == "undo":
        cmd_undo(dbx, a.run)
    elif a.cmd == "watch":
        cmd_watch(dbx, planner, a.settle)
    elif a.cmd == "skeleton":
        make_skeleton(dbx, a.study)
    elif a.cmd == "cleanup":
        cleanup_empty(dbx, a.study, a.confirm)


def make_skeleton(dbx, study=None):
    """Create the empty template folders (top level + each study)."""
    import dropbox
    paths = [f"{TPL['root']}/{t}" for t in TPL["top_folders"]]
    res = dbx.files_list_folder(STUDIES)
    studies = [e.name for e in res.entries if isinstance(e, dropbox.files.FolderMetadata)]
    for s in studies if not study else [study]:
        paths += [f"{STUDIES}/{s}/{p}" for p in TPL["study"]]
    for p in paths:
        try:
            dbx.files_create_folder_v2(p)
            print("created", p)
        except dropbox.exceptions.ApiError:
            pass                                   # already exists


def cleanup_empty(dbx, study=None, confirm=False):
    """Delete folders that contain no file at any depth (template folders are kept).
    Dropbox keeps deleted folders in 'Deleted files', so this can be restored."""
    import dropbox
    keep = {p.lower() for p in TPL["study"]}
    bases = [f"{STUDIES}/{study}"] if study else [STUDIES]
    folders, has_file = set(), set()
    for base in bases:
        res = dbx.files_list_folder(base, recursive=True, limit=2000)
        while True:
            for e in res.entries:
                if isinstance(e, dropbox.files.FolderMetadata):
                    folders.add(e.path_display)
                elif isinstance(e, dropbox.files.FileMetadata):
                    parts = e.path_display.split("/")
                    for k in range(1, len(parts)):
                        has_file.add("/".join(parts[:k]).lower())
            if not res.has_more:
                break
            res = dbx.files_list_folder_continue(res.cursor)
    empty = []
    for f in sorted(folders, key=len):
        rel = f[len(STUDIES) + 1:].split("/", 1)
        if len(rel) < 2 or f.lower() in has_file:
            continue
        if any(t.startswith(rel[1].lower()) for t in keep):      # template folder or its parent
            continue
        if any(f.lower().startswith(e.lower() + "/") for e in empty):
            continue                                              # parent already listed
        empty.append(f)
    print(f"{len(empty)} empty non-template folders")
    for f in empty[:40]:
        print("  ", f)
    if not confirm:
        print("Nothing deleted. Re-run with --confirm to delete them (recoverable from Dropbox 'Deleted files').")
        return
    for f in empty:
        dbx.files_delete_v2(f)
        log_move({"run": "cleanup", "kind": "delete_empty_folder", "from": f, "to": "", "ok": True,
                  "at": datetime.now(timezone.utc).isoformat()})
    print("done")


if __name__ == "__main__":
    main()
