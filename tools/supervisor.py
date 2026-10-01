"""Supervisor v1: lab-template audit + run digest.

Read-only. Never modifies Dropbox — it reports deviations from the lab
folder template (docs/lab_folder_template.md), it never moves/renames/
deletes anything.

Two outputs per run, uploaded to the ``reports`` container:
  supervisor/audit_<ts>.json.enc  full audit with REAL folder names (Fernet)
  supervisor/digest_<ts>.json     de-identified digest (counts + template
                                  section names; unknown path segments are
                                  redacted as <label>)

The digest is also printed to the workflow log so K never has to read raw
logs to know the pipeline's health.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Tuple

import dropbox

# ---------------------------------------------------------------------------
# Lab template as data (docs/lab_folder_template.md).
# (parent_rel -> {child_lower_name: kind})
# kinds: "section" fixed name, "participants" numeric folders, "any" free-form
# ---------------------------------------------------------------------------

EXPECTED: Dict[str, Dict[str, str]] = {
    "": {"database": "section", "documents": "section"},
    "database": {
        "rawdata": "section",
        "photos": "section",
        "extracteddata": "section",
        "analyzeddata": "section",
    },
    "database/rawdata": {"*": "participants"},
    "database/analyzeddata": {"*": "participants"},
    # Device folders are optional: a study may only use some devices.
    "database/extracteddata": {
        "bettercare": "optional",
        "infinity": "optional",
        "nol": "optional",
        "bis": "optional",
    },
    "database/photos": {"*": "any"},
    "documents": {"*": "any"},
}

TEMPLATE_VOCAB = {
    name for children in EXPECTED.values() for name in children if name != "*"
} | set(EXPECTED)

_PARTICIPANT_RE = re.compile(r"^\d{1,4}$")


# ---------------------------------------------------------------------------
# Dropbox tree listing (folders + file counts, read-only)
# ---------------------------------------------------------------------------

def list_tree(dbx, base: str) -> Optional[Dict[str, dict]]:
    """Return {rel_lower: {"name": original_rel, "files": n}} for every folder
    under ``base`` (recursive).  Returns None if the base cannot be listed."""
    base_low = base.lower()
    folders: Dict[str, dict] = {}
    try:
        res = dbx.files_list_folder(base, recursive=True)
    except Exception:  # noqa: BLE001
        return None

    def _rel(path_lower: str) -> str:
        if path_lower.startswith(base_low + "/"):
            return path_lower[len(base_low) + 1:]
        return path_lower.rsplit("/", 1)[-1]

    def _register_folder(rel_low: str, orig: str) -> None:
        # register the folder and all its ancestors
        parts_low = rel_low.split("/")
        parts_orig = orig.split("/")
        for i in range(1, len(parts_low) + 1):
            key = "/".join(parts_low[:i])
            if key not in folders:
                folders[key] = {"name": "/".join(parts_orig[:i]), "files": 0}

    while True:
        for e in res.entries:
            if isinstance(e, dropbox.files.FolderMetadata):
                rel_low = _rel(e.path_lower)
                _register_folder(rel_low, _rel(e.path_display))
            elif isinstance(e, dropbox.files.FileMetadata):
                rel_low = _rel(e.path_lower)
                parent = rel_low.rsplit("/", 1)[0] if "/" in rel_low else ""
                if parent:
                    _register_folder(parent, _rel(e.path_display).rsplit("/", 1)[0])
                    folders[parent]["files"] += 1
                else:
                    folders.setdefault(
                        "", {"name": "", "files": 0})["files"] += 1
        if not res.has_more:
            break
        res = dbx.files_list_folder_continue(res.cursor)
    return folders


def _children(tree: Dict[str, dict], parent: str) -> List[str]:
    """Lower-cased relpaths of direct children of ``parent``."""
    if parent == "":
        return [r for r in tree if "/" not in r]
    prefix = parent + "/"
    out = []
    for r in tree:
        if r.startswith(prefix):
            rest = r[len(prefix):]
            if "/" not in rest:
                out.append(r)
    return out


# ---------------------------------------------------------------------------
# Template comparison
# ---------------------------------------------------------------------------

def audit_project(tree: Optional[Dict[str, dict]],
                  data_roots: List[str]) -> dict:
    """Compare one project's Dropbox tree against the lab template."""
    if tree is None:
        return {"unreachable": True, "missing": [], "unexpected": [],
                "stray_files": 0, "layout_note": "Dropbox folder not found"}

    missing: List[str] = []
    unexpected: List[str] = []

    for parent, expected_children in EXPECTED.items():
        actual = _children(tree, parent)
        actual_names = {a.rsplit("/", 1)[-1] for a in actual}
        for exp_name, kind in expected_children.items():
            if exp_name == "*":
                continue
            if kind == "section" and exp_name not in actual_names:
                missing.append(f"{parent}/{exp_name}/".lstrip("/"))
        for child in actual:
            seg = child.rsplit("/", 1)[-1]
            if seg in expected_children:
                continue
            kind = expected_children.get("*")
            if kind == "participants" and _PARTICIPANT_RE.match(seg):
                continue
            if kind == "any":
                continue
            unexpected.append(tree[child]["name"] + "/")

    # Stray files: data-looking files sitting directly at the project root
    # or directly under Database/ (template says they belong deeper).
    stray = 0
    for key in ("", "database"):
        if key in tree:
            stray += tree[key]["files"]

    # Layout note: do the configured data_roots follow the template?
    template_data = ("database/rawdata", "database/extracteddata",
                     "database/analyzeddata")
    roots_low = [r.lower().strip("./") for r in data_roots]
    follows = any(any(r == t or r.startswith(t + "/") for t in template_data)
                  for r in roots_low)
    layout_note = ""
    if not follows:
        layout_note = (f"data_roots={data_roots} do not follow the template "
                       f"(expected Database/RawData, Database/ExtractedData/*, "
                       f"Database/AnalyzedData)")

    return {"unreachable": False,
            "missing": sorted(set(missing)),
            "unexpected": sorted(set(unexpected)),
            "stray_files": stray,
            "layout_note": layout_note}


# ---------------------------------------------------------------------------
# De-identification for the digest
# ---------------------------------------------------------------------------

def redact_segment(seg: str) -> str:
    s = seg.strip()
    if not s:
        return s
    if s.lower() in TEMPLATE_VOCAB:
        return seg  # template section name: safe
    if _PARTICIPANT_RE.match(s):
        return seg  # participant number, no identity
    return "<label>"


def redact_path(rel: str) -> str:
    return "/".join(redact_segment(p) for p in rel.split("/"))


def build_digest(audit: Dict[str, dict],
                 run_stats: dict,
                 new_folders: List[str]) -> dict:
    """De-identified digest: counts + template names only."""
    projects = {}
    for code, findings in audit.items():
        projects[code] = {
            "missing": findings.get("missing", []),
            "unexpected": [redact_path(u) for u in
                           findings.get("unexpected", [])[:5]],
            "unexpected_count": len(findings.get("unexpected", [])),
            "stray_files": findings.get("stray_files", 0),
            "layout_note": findings.get("layout_note", ""),
            "unreachable": findings.get("unreachable", False),
        }
    return {
        "run": run_stats,
        "new_dropbox_folders": sorted(new_folders),
        "projects": projects,
    }


def classify_run(state: dict) -> dict:
    """Summarise ingest outcomes from the encrypted pipeline state."""
    legacy = state.get("legacy", {})
    total_ok = total_failed = 0
    per_project = {}
    for code, entry in legacy.items():
        files = entry.get("files", {}) or {}
        ok = sum(1 for f in files.values()
                 if isinstance(f, dict) and f.get("status") == "ok")
        failed = sum(1 for f in files.values()
                     if isinstance(f, dict) and f.get("status") == "failed")
        total_ok += ok
        total_failed += failed
        per_project[code] = {"ok": ok, "failed": failed}
    return {"files_ok": total_ok, "files_failed": total_failed,
            "projects": per_project}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_supervisor(dbx, projects: List[dict], state: dict,
                   put_encrypted: Callable[[str, bytes], None],
                   put_plain: Callable[[str, bytes], None],
                   new_folders: Optional[List[str]] = None) -> dict:
    """Audit every project against the lab template and publish reports.

    ``put_encrypted`` / ``put_plain`` upload to the reports container, e.g.
    ``lambda name, data: ...``.  Never raises: audit failures are reported
    as findings, never as crashes.
    """
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")
    audit: Dict[str, dict] = {}
    for proj in projects:
        code = proj["code"]
        try:
            tree = list_tree(dbx, proj["dropbox_base"])
            audit[code] = audit_project(tree, proj.get("data_roots") or [])
        except Exception as exc:  # noqa: BLE001
            audit[code] = {"unreachable": True, "missing": [],
                           "unexpected": [], "stray_files": 0,
                           "layout_note": f"audit error: {exc}"[:200]}

    run_stats = classify_run(state)
    digest = build_digest(audit, run_stats, new_folders or [])

    full_payload = json.dumps(
        {"generated_utc": ts, "audit": audit}, indent=1).encode("utf-8")
    digest_payload = json.dumps(digest, indent=1).encode("utf-8")

    put_encrypted(f"supervisor/audit_{ts}.json.enc", full_payload)
    put_plain(f"supervisor/digest_{ts}.json", digest_payload)

    deviating = sum(1 for f in audit.values()
                    if f.get("missing") or f.get("unexpected")
                    or f.get("layout_note"))
    print(f"[supervisor] audit: {len(audit)} projects, "
          f"{deviating} deviate from template", flush=True)
    for code, findings in sorted(audit.items()):
        bits = []
        if findings.get("unreachable"):
            bits.append("unreachable: " + findings.get("layout_note", ""))
        if findings.get("missing"):
            bits.append("missing: " + ", ".join(findings["missing"][:3]))
        if findings.get("unexpected"):
            bits.append(f"unexpected folders: {len(findings['unexpected'])}")
        if findings.get("layout_note"):
            bits.append(findings["layout_note"])
        if bits:
            print(f"[supervisor] {code}: " + "; ".join(bits), flush=True)
    print(f"[supervisor] run: {run_stats['files_ok']} files ok, "
          f"{run_stats['files_failed']} failed", flush=True)
    print(f"[supervisor] digest: supervisor/digest_{ts}.json", flush=True)
    return digest
