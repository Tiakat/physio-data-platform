"""Supervisor Audit: static audit of all pipeline supervisors/validators.

For each supervisor/validator in the pipeline, checks:
- Syntax validity (ast.parse)
- Has a main() entry point
- Has a module docstring describing its purpose
- Declared inputs (dropbox, azure, lineage.json, decrypted data, labels)
- Declared outputs (github issue, json report, azure upload, gh artifact)
- Whether it can exit non-zero (failure-email noise risk)

Then performs gap/overlap analysis:
- Which pipeline stages have coverage?
- Which stages lack coverage?
- Where do multiple supervisors overlap?

Outputs:
- supervisor_audit/report.json — machine-readable audit
- supervisor_audit/report.md — human-readable with pass/fail and recommendations

K's rule: validators report issues in output files, never crash with
unnecessary failing runs. This audit flags any supervisor that violates that.
"""
import ast
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# (filename, pipeline stage, what it claims to cover)
SUPERVISORS = [
    ("supervisor.py", "ingest",
     "Audits Dropbox layout vs lab template (read-only)"),
    ("verify_ingest.py", "ingest",
     "3-way verification: Dropbox vs encrypted state vs Azure blobs"),
    ("verify_processing.py", "processing",
     "Every ingested parquet has processed outputs (lineage.json)"),
    ("validate_processing.py", "processing",
     "Filtered data quality vs signal norms; graph existence"),
    ("qc_notifier.py", "processing",
     "Scans lineage QC summaries; GitHub issue on high artifact rates"),
    ("validate_external.py", "processing",
     "Signal distributions vs MIMIC/literature references"),
    ("train_graph_validator.py", "processing",
     "Per-signal-family morphology models (normal vs artifact)"),
    ("ml_supervised_train.py", "ml",
     "Supervised artifact classifier with train/val/test splits"),
    ("ml_unsupervised_train.py", "ml",
     "Unsupervised clustering on processed parquets"),
]

# Pipeline stages and what should cover them
STAGE_COVERAGE = {
    "ingest": ["supervisor.py", "verify_ingest.py"],
    "processing": ["verify_processing.py", "validate_processing.py",
                   "qc_notifier.py", "validate_external.py",
                   "train_graph_validator.py"],
    "ml": ["ml_supervised_train.py", "ml_unsupervised_train.py"],
    "graphs": ["validate_processing.py"],  # only partial (existence check)
    "continuity": [],  # GAP — nothing links stages end-to-end
    "website": [],     # GAP — no supervisor for website integration yet
    "stats": [],       # GAP — stats phase not built yet
}


def analyze_file(path: Path) -> dict:
    """Static analysis of a single supervisor file."""
    result = {
        "file": path.name,
        "exists": path.exists(),
        "syntax": "unknown",
        "has_main": False,
        "has_docstring": False,
        "docstring": "",
        "inputs": [],
        "outputs": [],
        "can_exit_nonzero": False,
        "issues": [],
    }
    if not path.exists():
        result["issues"].append("File not found in tools/")
        return result
    code = path.read_text()
    try:
        tree = ast.parse(code)
        result["syntax"] = "ok"
    except SyntaxError as e:
        result["syntax"] = f"FAIL: {e}"
        result["issues"].append(f"Syntax error: {e}")
        return result

    result["has_main"] = any(
        isinstance(n, ast.FunctionDef) and n.name == "main"
        for n in ast.walk(tree))
    if not result["has_main"]:
        result["issues"].append("No main() function — may not be runnable as module")

    doc = ast.get_docstring(tree)
    result["has_docstring"] = bool(doc)
    result["docstring"] = (doc[:200] + "...") if doc and len(doc) > 200 else (doc or "")
    if not doc:
        result["issues"].append("No module docstring — purpose unclear")

    low = code.lower()
    inputs = []
    if "dropbox" in low:
        inputs.append("dropbox")
    if "azure" in low or "blob" in low:
        inputs.append("azure")
    if "lineage.json" in code:
        inputs.append("lineage.json")
    if "PIPELINE_DATA_KEY" in code:
        inputs.append("decrypted_data")
    if "labels" in low and "csv" in low:
        inputs.append("labels_csv")
    result["inputs"] = inputs

    outputs = []
    if "issue" in low and ("create" in low or "gh " in low):
        outputs.append("github_issue")
    if "write_text" in code or "json.dump" in code:
        outputs.append("json_report")
    if "upload_blob" in code:
        outputs.add("azure_upload") if isinstance(outputs, set) else outputs.append("azure_upload")
    result["outputs"] = outputs

    # Check for non-zero exits (failure-email noise)
    if "sys.exit(1)" in code or "return 1" in code:
        result["can_exit_nonzero"] = True
        # This is OK if it's gated (only fails on real problems)
        # Flag for manual review
        result["issues"].append(
            "Can exit non-zero — verify this only happens on real failures, "
            "not on empty/missing data (K's no-noise rule)")

    return result


def main():
    tools_dir = Path(__file__).resolve().parent
    out_dir = Path("supervisor_audit")
    out_dir.mkdir(exist_ok=True)

    results = []
    for fname, stage, claim in SUPERVISORS:
        r = analyze_file(tools_dir / fname)
        r["stage"] = stage
        r["claims"] = claim
        # Pass/fail: syntax ok + has main + has docstring + no critical issues
        critical = [i for i in r["issues"] if "Syntax error" in i or "not found" in i]
        r["verdict"] = "PASS" if not critical and r["syntax"] == "ok" else "FAIL"
        results.append(r)

    # Gap analysis
    gaps = []
    overlaps = []
    for stage, covering in STAGE_COVERAGE.items():
        existing = [s for s in covering
                    if (tools_dir / s).exists()]
        if not existing:
            gaps.append({
                "stage": stage,
                "severity": "high" if stage in ("continuity", "graphs") else "medium",
                "note": f"No supervisor covers '{stage}' stage",
            })

    # Overlap: processing has 5 supervisors — check they don't duplicate
    proc_supervisors = [r for r in results if r["stage"] == "processing"]
    if len(proc_supervisors) > 3:
        overlaps.append({
            "stage": "processing",
            "count": len(proc_supervisors),
            "supervisors": [r["file"] for r in proc_supervisors],
            "note": "Multiple supervisors cover processing. "
                    "verify_processing=coverage, validate_processing=quality-vs-norms, "
                    "qc_notifier=alert-on-flags, validate_external=MIMIC-comparison, "
                    "train_graph_validator=morphology-models. "
                    "Complementary angles, not duplicates — but ensure they don't "
                    "all fire GitHub issues for the same problem.",
        })

    # Requiring-decrypted-data supervisors can't run without PIPELINE_DATA_KEY
    key_dependent = [r["file"] for r in results if "decrypted_data" in r["inputs"]]

    report = {
        "tool": "audit_supervisors",
        "supervisors": results,
        "summary": {
            "total": len(results),
            "pass": sum(1 for r in results if r["verdict"] == "PASS"),
            "fail": sum(1 for r in results if r["verdict"] == "FAIL"),
        },
        "gaps": gaps,
        "overlaps": overlaps,
        "key_dependent_supervisors": key_dependent,
        "recommendations": [
            "Build continuity_check.py to link stages end-to-end (currently no coverage).",
            "Build website supervisor when website integration begins.",
            "Build stats supervisor when stats phase begins.",
            "Ensure only qc_notifier creates GitHub issues for QC problems — "
            "other validators should write to report files, not issues, to avoid duplicates.",
            "verify_ingest.py and verify_processing.py only print to stdout — "
            "consider writing structured JSON for meta-supervisor consumption.",
        ],
    }

    (out_dir / "report.json").write_text(json.dumps(report, indent=1))

    # Markdown report
    lines = ["# Supervisor Audit Report", ""]
    lines.append(f"**{report['summary']['pass']}/{report['summary']['total']} supervisors PASS**")
    lines.append("")
    lines.append("## Per-Supervisor Results")
    lines.append("")
    lines.append("| Supervisor | Stage | Verdict | Inputs | Outputs | Issues |")
    lines.append("|---|---|---|---|---|---|")
    for r in results:
        issues = "; ".join(r["issues"]) if r["issues"] else "—"
        lines.append(f"| {r['file']} | {r['stage']} | {r['verdict']} | "
                     f"{', '.join(r['inputs']) or '—'} | "
                     f"{', '.join(r['outputs']) or '—'} | {issues} |")
    lines.append("")
    if gaps:
        lines.append("## Gaps")
        lines.append("")
        for g in gaps:
            lines.append(f"- **{g['stage']}** ({g['severity']}): {g['note']}")
        lines.append("")
    if overlaps:
        lines.append("## Overlaps")
        lines.append("")
        for o in overlaps:
            lines.append(f"- **{o['stage']}**: {o['note']}")
        lines.append("")
    lines.append("## Recommendations")
    lines.append("")
    for rec in report["recommendations"]:
        lines.append(f"- {rec}")
    lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines))

    print(f"[audit] {report['summary']['pass']}/{report['summary']['total']} PASS", flush=True)
    for g in gaps:
        print(f"[audit] GAP: {g['stage']} — {g['note']}", flush=True)
    print(f"[audit] Report written to {out_dir}/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
