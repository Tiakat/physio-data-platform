"""Validate every project and produce a manual comparison checklist.

Two steps:

    python -m tools.checklist_report --run
        runs tools.validate_project (header only, fast) on every project and
        leaves one JSON report per project in validation_reports/.

    python -m tools.checklist_report --build
        reads those reports plus the project profiles and writes a
        per-project and per-patient checklist under validation_checklist/.

Both steps together:  python -m tools.checklist_report --all

The checklist is a list of what the validator found, patient by patient, so a
human can compare it against their own records.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backbone.config import load_profile                              # noqa: E402
from tools.sync_dropbox import load_rules, project_rules              # noqa: E402

REPO = Path(__file__).resolve().parent.parent
BASE = Path(r"C:\Users\katia\Dropbox\Liam\Projects actifs")
REPORT_DIR = REPO / "validation_reports"
OUT_DIR = REPO / "validation_checklist"

# project code -> RawData root. V-RAPS points at the nested mirror because the
# top level also contains a byte-for-byte copy of itself; pointing at the
# mirror gives the same data with no double counting.
PROJECTS = {
    "IPAMS": BASE / "IPAMS" / "Database" / "RawData",
    "SILVR": BASE / "SILVR" / "Database" / "RawData",
    "DEXREM": BASE / "DEXREM" / "Database" / "RawData",
    "ESMONOL": BASE / "ESMONOL" / "Database" / "RawData",
    "PROMISES": BASE / "PROMISES" / "Database" / "RawData",
    "V-RAPS": BASE / "V-RAPS" / "Database" / "RawData" / "RawData",
}


def run_all():
    import subprocess
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    for code, root in PROJECTS.items():
        print(f"[{code}] validating {root} ...", flush=True)
        log = REPORT_DIR / f"_run_{code}.log"
        with open(log, "w", encoding="utf-8") as fh:
            subprocess.run(
                [sys.executable, "-m", "tools.validate_project",
                 "--project", code, "--root", str(root), "--no-dedup",
                 "--out", str(REPORT_DIR)],
                cwd=REPO, stdout=fh, stderr=subprocess.STDOUT, check=False)
        print(f"[{code}] done -> {log}", flush=True)


def latest_report(code: str) -> dict:
    candidates = sorted(REPORT_DIR.glob(f"{code}_*.json"),
                        key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise FileNotFoundError(f"no report for {code} in {REPORT_DIR}")
    path = candidates[-1]
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def load_declared(code: str):
    rules = project_rules(load_rules(), code)
    return rules.get("expect_modalities") or []


def build_all():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    master_patients = []
    master_projects = []

    for code in PROJECTS:
        report = latest_report(code)
        build_project(code, report)
        master_projects.append(_project_row(code, report))
        for row in _patient_rows(code, report):
            master_patients.append(row)

    _write_csv(OUT_DIR / "00_SUMMARY_ALL_PROJECTS.csv", master_projects)
    _write_csv(OUT_DIR / "01_ALL_PATIENTS.csv", master_patients)
    print(f"checklists written to {OUT_DIR}")
def _project_row(code, report):
    f = report["files"]
    warn = sum(f.get(v, 0) for v in ("WARNING", "WARNING_EMPTY", "WARNING_MISSING_MODALITY"))
    fail = sum(f.get(v, 0) for v in ("FAIL", "FAIL_UNREADABLE",
                                     "FAIL_CORRUPT", "FAIL_INVALID_STRUCTURE"))
    gap = report.get("declared_gap") or {}
    return {
        "project": code,
        "patients_checked": len(report["patients"]),
        "patients_complete": report["patients_complete"],
        "patients_incomplete": report["patients_incomplete"],
        "files_total": f["total"],
        "files_pass": f.get("PASS", 0),
        "files_warning": warn,
        "files_fail": fail,
        "files_missing_modality": f.get("WARNING_MISSING_MODALITY", 0),
        "declared_but_rare": "; ".join(f"{d} {p:.0%}" for d, p in sorted(gap.items())),
        "coverage_patterns": "; ".join(f"{n}x {pat}" for pat, n in
                                       sorted(report.get("coverage", {}).items())),
    }


def _worst_of(verdicts):
    """Best representative verdict for a list: PASS < WARNING* < FAIL*."""
    order = {"PASS": 0, "WARNING": 1, "WARNING_EMPTY": 1,
             "WARNING_MISSING_MODALITY": 1,
             "FAIL": 2, "FAIL_UNREADABLE": 2, "FAIL_CORRUPT": 2,
             "FAIL_INVALID_STRUCTURE": 2}
    return max(set(verdicts), key=lambda v: order.get(v, 1))


def _patient_rows(code, report):
    profile = load_profile(code)
    expected = profile.get("coverage", {}).get("expected", [])
    primary = profile.get("coverage", {}).get("primary_analysis_requires", [])
    declared = load_declared(code)

    by_patient = defaultdict(list)
    for r in report["file_results"]:
        by_patient[r.get("patient")].append(r)

    rows = []
    for p in report["patients"]:
        pid = p["patient"]
        device_verdicts: dict[str, list] = defaultdict(list)
        n_files = 0
        issues = []
        for r in by_patient.get(pid, []):
            n_files += 1
            device_verdicts[r["device"]].append(r["verdict"])
            if r["verdict"] != "PASS":
                det = "; ".join(c["detail"] for c in r["checks"] if c["result"] != "PASS")
                issues.append(f"{r['device']}|{r['verdict']}|"
                              f"{Path(r['file']).name}|{det[:120]}")
        dev_summary = {d: _worst_of(vs) for d, vs in device_verdicts.items()}
        for d in expected:
            dev_summary.setdefault(d, "ABSENT")

        missing = [d for d in expected if d not in p["devices"]]
        complete_primary = all(d in p["devices"] for d in primary)
        rows.append({
            "project": code,
            "patient": pid,
            "devices_found": ",".join(sorted(p["devices"])) or "(none)",
            "device_verdicts": "; ".join(f"{d}={dev_summary[d]}"
                                         for d in sorted(dev_summary)),
            "has_infinity": "infinity" in p["devices"],
            "has_bettercare": "bettercare" in p["devices"],
            "has_nol": "nol" in p["devices"],
            "has_bis": "bis" in p["devices"],
            "has_pump": "pump" in p["devices"],
            "expected": ",".join(expected),
            "declared_in_rules": ",".join(declared),
            "missing_expected": ",".join(missing) or "",
            "complete_for_primary": complete_primary,
            "patient_verdict": p["verdict"],
            "files": n_files,
            "issues": " // ".join(issues[:8]),
        })
    return rows


def build_project(code, report):
    # ---- patients CSV
    patients = _patient_rows(code, report)
    _write_csv(OUT_DIR / f"{code}_patients.csv", patients)

    # ---- files CSV (only non-PASS, so it stays a comparison checklist,
    # not a dump of thousands of healthy rows)
    file_rows = []
    for r in report["file_results"]:
        if r["verdict"] == "PASS":
            continue
        det = "; ".join(f"[{c['check']}] {c['detail']}"
                        for c in r.get("checks", []) if c["result"] != "PASS")
        file_rows.append({
            "project": code,
            "patient": r.get("patient") or "",
            "device": r["device"],
            "verdict": r["verdict"],
            "file": r["file"],
            "reason": det,
        })
    _write_csv(OUT_DIR / f"{code}_files.csv", file_rows)

    # ---- human readable text
    lines = [f"{'=' * 72}", f"VALIDATION CHECKLIST  {code}", "=" * 72]
    f = report["files"]
    warn = sum(f.get(v, 0) for v in ("WARNING", "WARNING_EMPTY", "WARNING_MISSING_MODALITY"))
    fail = sum(f.get(v, 0) for v in ("FAIL", "FAIL_UNREADABLE",
                                     "FAIL_CORRUPT", "FAIL_INVALID_STRUCTURE"))
    lines.append(f"files: {f['total']}  pass {f.get('PASS', 0)}  "
                 f"warning {warn}  fail {fail}")
    lines.append(f"patients: {report['patients_complete']} complete / "
                 f"{report['patients_incomplete']} incomplete")
    gap = report.get("declared_gap") or {}
    if gap:
        lines.append("declared in rules.yaml but rare here: "
                     + "; ".join(f"{d} {p:.0%}" for d, p in sorted(gap.items())))
    lines.append("")
    for p in report["patients"]:
        mark = "OK " if p["verdict"] == "PASS" else "?? "
        lines.append(f"{mark} patient {p['patient']:<6} has ["
                     f"{', '.join(p['devices']) or 'NOTHING'}]"
                     + (f"  missing {', '.join(p['missing'])}" if p["missing"] else ""))
        for c in p.get("checks", []):
            if c["result"] != "PASS":
                lines.append(f"      - {c['check']}: {c['detail']}")
    lines.append("")
    lines.append("Files with anything other than PASS:")
    non_pass = [r for r in report["file_results"] if r["verdict"] != "PASS"]
    if not non_pass:
        lines.append("  (none)")
    for r in non_pass:
        det = "; ".join(c["detail"] for c in r.get("checks", []) if c["result"] != "PASS")
        lines.append(f"  {r['patient'] or '?':<6} {r['device']:<11} {r['verdict']:<28} "
                     f"{r['file']}")
        if det:
            lines.append(f"           {det[:160]}")
    (OUT_DIR / f"{code}.txt").write_text("\n".join(lines), encoding="utf-8")


def _write_csv(path, rows):
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="run validations then build")
    ap.add_argument("--run", action="store_true", help="run validations only")
    ap.add_argument("--build", action="store_true", help="build checklists only")
    args = ap.parse_args()
    if args.all or args.run or args.build:
        if args.all or args.run:
            run_all()
        if args.all or args.build:
            build_all()
    else:
        ap.print_help()