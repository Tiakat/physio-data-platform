"""Recon: find per-project demographic Excel files in Dropbox (read-only).

K's requirement: every project should have a demographic file per patient
(age, comorbidities, ...), usually Excel. If it is not with the patients it
may sit in Documents/ by mistake. Demographics power the cross-patient
statistics, so the pipeline must know they exist and where.

Privacy: paths and filenames can carry patient codes, so only hashes and a
location class are reported. The pipeline job re-walks Dropbox with the same
rules when it actually needs the files. Demographic CONTENT is never read
here -- existence and location only.

Location classes:
  - patient_tree  (Database / RawData / #participant areas)
  - documents     (Documents/ tree -- "put there by mistake")
  - other         (anywhere else under the project root)
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_summary(title: str, rows: list[tuple]) -> None:
    """Append a de-identified markdown table to the GitHub job summary.

    Only counts and class names -- no paths, filenames, or values -- so this
    is safe to show in the workflow UI. No-op outside GitHub Actions.
    """
    dest = os.environ.get("GITHUB_STEP_SUMMARY")
    if not dest:
        return
    lines = [f"## {title}", "",
             "| project | spreadsheets | name-hinted demographic | classes |",
             "| --- | ---: | ---: | --- |"]
    for code, n_files, n_hinted, classes in rows:
        lines.append(f"| {code} | {n_files} | {n_hinted} | {classes} |")
    lines.append("")
    with open(dest, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

EXCEL_EXTS = (".xlsx", ".xls", ".xlsm", ".xlsb", ".ods")

# Filename hints (French + English) that a spreadsheet holds demographics.
DEMO_HINTS = (
    "demograph", "demographic", "demo", "patient", "caracteristique",
    "caracteristiques", "donnees", "inclusion", "recrutement",
    "screening", "baseline",
)


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def location_class(project_root: str, path: str) -> str:
    rel = path[len(project_root):].lower()
    if "/documents" in rel or rel.startswith("documents"):
        return "documents"
    if "/database" in rel or "/rawdata" in rel:
        return "patient_tree"
    return "other"


def looks_demographic(filename: str) -> bool:
    low = filename.lower()
    return any(h in low for h in DEMO_HINTS)


def walk_excel(dbx, root: str):
    """Yield (path, size) for every spreadsheet under root."""
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            res = dbx.files_list_folder(cur, recursive=False)
        except Exception as exc:  # noqa: BLE001
            print(f"[demographics] list failed: {exc}", flush=True)
            continue
        entries = list(res.entries)
        while res.has_more:
            res = dbx.files_list_folder_continue(res.cursor)
            entries.extend(res.entries)
        for e in entries:
            if e.name.startswith(".") or e.name.startswith("~$"):
                continue
            if hasattr(e, "size"):  # file
                if e.name.lower().endswith(EXCEL_EXTS):
                    yield e.path_display, e.size
            else:  # folder
                stack.append(e.path_display)


def main() -> int:
    from tools import sync_dropbox_cloud
    from tools import azure_auth
    from tools.daily_pipeline import ACCOUNT, REPORTS
    from tools.legacy_pipeline import select_projects

    dbx = sync_dropbox_cloud.get_dropbox_client()
    projects, _ = select_projects(dbx)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")

    findings = []
    summary_rows = []
    for proj in sorted(projects, key=lambda p: p["code"]):
        code = proj["code"]
        root = proj.get("dropbox_base") or ""
        if not root:
            print(f"[demographics] {code}: no dropbox root configured",
                  flush=True)
            continue
        n_files = 0
        n_hinted = 0
        classes: dict[str, int] = {}
        for path, size in walk_excel(dbx, root):
            n_files += 1
            cls = location_class(root, path)
            classes[cls] = classes.get(cls, 0) + 1
            hinted = looks_demographic(path.rsplit("/", 1)[-1])
            n_hinted += 1 if hinted else 0
            findings.append({
                "project": code,
                "location_class": cls,
                "filename_sha8": _sha8(path),
                "size_bytes": size,
                "name_hint_demographic": hinted,
            })
        print(f"[demographics] {code}: {n_files} spreadsheets "
              f"({n_hinted} name-hinted demographic), classes={classes}",
              flush=True)
        summary_rows.append((code, n_files, n_hinted, classes))

    _write_summary("Demographics recon (de-identified)", summary_rows)

    svc = azure_auth.get_blob_service_client(ACCOUNT)
    blob = f"recon/demographics_{ts}.json"
    svc.get_blob_client(container=REPORTS,
                        blob=blob).upload_blob(
        json.dumps(findings, indent=1).encode("utf-8"), overwrite=True)
    print(f"[demographics] wrote reports/{blob} ({len(findings)} files)",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
