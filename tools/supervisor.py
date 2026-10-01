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
# Email report (real paths — the recipient is K herself, so no redaction).
# Only generated when something actually needs her attention.
# ---------------------------------------------------------------------------

_DEVICE_HOME = {
    "bettercare": "Database/ExtractedData/BetterCare/",
    "infinity": "Database/ExtractedData/Infinity/",
    "nol": "Database/ExtractedData/NOL/",
    "bis": "Database/ExtractedData/BIS/",
}

_DOC_HOME = {
    "version finale approuvée": "Documents/Version finale approuvée/",
    "fic-crf-short protocol": "Documents/FIC-CRF-Short Protocol/",
    "soumission ethique": "Documents/Soumission ethique/",
    "contrats legaux": "Documents/Contrats legaux/",
}

_PARTICIPANT_LIKE_RE = re.compile(
    r"^(?:patient|pt|sujet)[\s_]*(?P<n>\d{1,3})(?!\d).*$")


def suggest_home(seg: str) -> Optional[str]:
    """Suggest where an unexpected folder probably belongs (template terms)."""
    low = seg.strip().lower()
    if low in _DEVICE_HOME:
        return _DEVICE_HOME[low]
    if low in _DOC_HOME:
        return _DOC_HOME[low]
    m = _PARTICIPANT_LIKE_RE.match(low)
    if m:
        return f"Database/RawData/{m.group('n')}/  (or AnalyzedData — please decide)"
    if _PARTICIPANT_RE.match(low):
        return "Database/RawData/<n>/  (or AnalyzedData — please decide)"
    return None


def _needs_attention(findings: dict) -> bool:
    return bool(findings.get("missing") or findings.get("unexpected")
                or findings.get("stray_files") or findings.get("layout_note")
                or findings.get("unreachable"))


def render_email(audit: Dict[str, dict], run_stats: dict,
                 date_str: str) -> Optional[Tuple[str, str]]:
    """Return (subject, body) if any project needs K's attention, else None."""
    bad = {c: f for c, f in audit.items() if _needs_attention(f)}
    if not bad:
        return None
    subject = (f"[Supervisor] Dropbox audit {date_str} — "
               f"{len(bad)} project(s) deviate from the lab template")
    lines = [
        "Bonjour K,",
        "",
        "The supervisor checked every project's Dropbox folders against the",
        "lab template. The projects below deviate.",
        "Nothing was moved or renamed — please fix these manually in Dropbox.",
        "",
    ]
    for code in sorted(bad):
        f = bad[code]
        lines.append(f"== {code} ==")
        if f.get("unreachable"):
            lines.append(f"  Dropbox folder unreachable: {f.get('layout_note')}")
        if f.get("layout_note"):
            lines.append(f"  Layout: {f['layout_note']}")
        for m in f.get("missing", []):
            lines.append(f"  Missing section: {m}")
        for u in f.get("unexpected", [])[:10]:
            seg = u.rstrip("/").rsplit("/", 1)[-1]
            sug = suggest_home(seg)
            if sug:
                lines.append(f"  Misplaced: '{u}'  -> suggested home: {sug}")
            else:
                lines.append(f"  Misplaced: '{u}'  -> please decide where it belongs")
        if len(f.get("unexpected", [])) > 10:
            lines.append(f"  ... and {len(f['unexpected']) - 10} more")
        if f.get("stray_files"):
            lines.append(f"  Stray files at top level: {f['stray_files']}")
        lines.append("")
    lines.append(f"Run stats: {run_stats['files_ok']} files ok, "
                 f"{run_stats['files_failed']} failed.")
    lines.append("Full encrypted audit: reports/supervisor/ "
                 f"(audit_{date_str}.json.enc)")
    lines.append("")
    lines.append("— your supervisor (no files were touched)")
    return subject, "\n".join(lines)


# ---------------------------------------------------------------------------
# Ingest health checks (run AFTER ingest).
#
# Each check is small, pure where possible, and never raises: a failing
# check becomes a finding, never a crash. Findings use counts and hash
# blob names only — safe for the plaintext digest.
# ---------------------------------------------------------------------------

def check_project_health(code: str, entry: dict) -> List[str]:
    """Stall + failure-spike detection from the project's run history."""
    findings: List[str] = []
    hist = entry.get("run_history") or []
    if not hist:
        return findings
    last = hist[-1]

    # Stall: work remains but nothing selected, repeatedly.
    remaining = max(0, last.get("eligible", 0) - last.get("ok_total", 0))
    if remaining > 0 and last.get("selected", 0) == 0:
        streak = 0
        for rec in reversed(hist):
            rem = max(0, rec.get("eligible", 0) - rec.get("ok_total", 0))
            if rem > 0 and rec.get("selected", 0) == 0:
                streak += 1
            else:
                break
        if streak >= 2:
            findings.append(
                f"stall? {remaining} files eligible but 0 selected for "
                f"{streak} consecutive runs — the selector may be blind "
                f"to them")

    # Failure spike: this run's failure rate far above its own history
    # (usually a new file format appearing).
    ok_new = last.get("ok_new", 0)
    failed_new = last.get("failed_new", 0)
    total_new = ok_new + failed_new
    if failed_new >= 3 and total_new > 0:
        rate = failed_new / total_new
        past = []
        for rec in hist[:-1]:
            t = rec.get("ok_new", 0) + rec.get("failed_new", 0)
            if t > 0:
                past.append(rec.get("failed_new", 0) / t)
        baseline = sorted(past)[len(past) // 2] if past else 0.0
        if rate >= 0.25 and rate > 3 * baseline + 0.05:
            findings.append(
                f"failure spike: {failed_new}/{total_new} failed this run "
                f"({rate:.0%}) vs baseline {baseline:.0%} — new file format?")
    return findings


def validate_parquet_samples(account: str, projects: List[dict], state: dict,
                             per_project: int = 3) -> Tuple[List[str], int]:
    """Download, decrypt and open a few parquet blobs per project.

    Returns (findings, n_checked). Blob names are content hashes — safe.
    """
    from tools import azure_auth
    from tools.crypto import decrypt_bytes
    import io

    findings: List[str] = []
    checked = 0
    svc = azure_auth.get_blob_service_client(account)
    for proj in projects:
        code = proj["code"]
        files = (state.get("legacy", {}) or {}).get(code, {}).get("files") or {}
        blobs = [f["stored"] for f in files.values()
                 if isinstance(f, dict) and f.get("status") == "ok"
                 and f.get("kind") == "parquet" and f.get("stored")]
        for blob in blobs[-per_project:]:
            checked += 1
            try:
                raw = svc.get_blob_client(
                    container="rawdata", blob=blob).download_blob().readall()
                import pandas as pd
                df = pd.read_parquet(io.BytesIO(decrypt_bytes(raw)))
                if len(df) == 0 or len(df.columns) == 0:
                    findings.append(f"{code}: {blob} decrypted but is empty")
            except Exception as exc:  # noqa: BLE001
                findings.append(
                    f"{code}: {blob} failed validation: {str(exc)[:120]}")
    return findings, checked


def run_health_checks(dbx, account: str, projects: List[dict], state: dict,
                      put_encrypted: Callable[[str, bytes], None],
                      put_plain: Callable[[str, bytes], None],
                      email_dir: Optional[str],
                      started_at,
                      timeout_min: float = 120,
                      sample_parquets: bool = True) -> dict:
    """Post-ingest health checks. Never raises; findings go to the digest,
    the log, and (if any) the email/issue report."""
    import os as _os
    from datetime import datetime as _dt, timezone as _tz

    ts = _dt.now(_tz.utc).strftime("%Y%m%d_%H%M")
    findings: Dict[str, List[str]] = {}
    for proj in projects:
        code = proj["code"]
        try:
            entry = (state.get("legacy", {}) or {}).get(code, {})
            found = check_project_health(code, entry)
            if found:
                findings[code] = found
        except Exception as exc:  # noqa: BLE001
            findings[code] = [f"health check error: {exc}"[:150]]

    pq_checked = 0
    if sample_parquets:
        try:
            pq_found, pq_checked = validate_parquet_samples(
                account, projects, state)
            if pq_found:
                findings["_parquet"] = pq_found
        except Exception as exc:  # noqa: BLE001
            findings["_parquet"] = [f"parquet sampling error: {exc}"[:150]]

    elapsed_min = (_dt.now(_tz.utc) - started_at).total_seconds() / 60
    timeout_finding = None
    if elapsed_min > 0.85 * timeout_min:
        timeout_finding = (f"run at {elapsed_min:.0f}/{timeout_min:.0f} min "
                           f"({elapsed_min / timeout_min:.0%}) of the "
                           f"workflow timeout — backlog may not fit")

    digest_projects = {}
    for proj in projects:
        code = proj["code"]
        last = (state.get("legacy", {}) or {}).get(code, {}).get("last_run", {})
        digest_projects[code] = {
            "eligible": last.get("eligible", 0),
            "selected": last.get("selected", 0),
            "ok_new": last.get("ok_new", 0),
            "failed_new": last.get("failed_new", 0),
            "ok_total": last.get("ok_total", 0),
            "budget_capped": last.get("budget_capped", False),
        }
    digest = {"generated_utc": ts, "elapsed_min": round(elapsed_min, 1),
              "parquet_samples_checked": pq_checked,
              "projects": digest_projects, "findings": findings,
              "timeout": timeout_finding}
    put_plain(f"supervisor/health_{ts}.json",
              json.dumps(digest, indent=1).encode("utf-8"))

    n_find = sum(len(v) for v in findings.values()) + \
        (1 if timeout_finding else 0)
    print(f"[supervisor] health: {n_find} findings, "
          f"{pq_checked} parquet samples checked, "
          f"elapsed {elapsed_min:.0f} min", flush=True)
    for code in sorted(findings):
        for f in findings[code]:
            print(f"[supervisor] health {code}: {f}", flush=True)
    if timeout_finding:
        print(f"[supervisor] health: {timeout_finding}", flush=True)

    if n_find and email_dir:
        _os.makedirs(email_dir, exist_ok=True)
        section = ["", "", "== Ingest health ==", ""]
        if timeout_finding:
            section.append(f"- {timeout_finding}")
        for code in sorted(findings):
            for f in findings[code]:
                section.append(f"- {code}: {f}")
        section.append("")
        body_path = _os.path.join(email_dir, "body.txt")
        existing = ""
        if _os.path.exists(body_path):
            with open(body_path, encoding="utf-8") as fh:
                existing = fh.read()
        with open(body_path, "w", encoding="utf-8") as fh:
            fh.write(existing + "\n".join(section))
        subj_path = _os.path.join(email_dir, "subject.txt")
        if _os.path.exists(subj_path):
            with open(subj_path, encoding="utf-8") as fh:
                subj = fh.read().strip() + " + ingest health"
        else:
            subj = f"[Supervisor] ingest health {ts} — {n_find} findings"
        with open(subj_path, "w", encoding="utf-8") as fh:
            fh.write(subj)
    return digest


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_supervisor(dbx, projects: List[dict], state: dict,
                   put_encrypted: Callable[[str, bytes], None],
                   put_plain: Callable[[str, bytes], None],
                   new_folders: Optional[List[str]] = None,
                   email_dir: Optional[str] = None) -> dict:
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

    # Email files for the workflow: written ONLY when K needs to act.
    # Silence = everything matches the template.
    if email_dir:
        import os as _os
        _os.makedirs(email_dir, exist_ok=True)
        rendered = render_email(audit, run_stats, ts)
        if rendered:
            subject, body = rendered
            with open(_os.path.join(email_dir, "subject.txt"), "w",
                      encoding="utf-8") as fh:
                fh.write(subject)
            with open(_os.path.join(email_dir, "body.txt"), "w",
                       encoding="utf-8") as fh:
                fh.write(body)

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
