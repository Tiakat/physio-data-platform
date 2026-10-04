"""QC notifier: scan processed patients' lineage.json QC summaries and flag
data quality issues for K's review via a GitHub issue (label: qc-alert).

Reads plaintext lineage.json from processed/level2/*/*/lineage.json
(each contains a qc_summary with per-signal flag value_counts).
Flags:
  - artifact rate > 10% for any signal (artifact = FLATLINE, SPIKE,
    SATURATION, DEVICE_ARTIFACT, INVALID_RANGE, LOW_QUALITY)
  - missing rate > 50% (MISSING + GAP)

Outputs:
  - qc_report/issues.json : machine-readable list of findings
  - qc_report/summary.md  : markdown summary for the GitHub issue body
Prints "QC_ISSUES_FOUND=<n>" for the workflow to gate issue creation.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

ARTIFACT_FLAGS = {
    "FLATLINE", "SPIKE", "SATURATION",
    "DEVICE_ARTIFACT", "INVALID_RANGE", "LOW_QUALITY",
}
MISSING_FLAGS = {"MISSING", "GAP"}
ARTIFACT_RATE_THRESHOLD = 0.10
MISSING_RATE_THRESHOLD = 0.50


def analyze_qc_summary(qc_summary):
    """Return list of (signal, kind, rate, detail) findings."""
    findings = []
    for qc_col, counts in (qc_summary or {}).items():
        if not qc_col.endswith("__qc"):
            continue
        signal = qc_col[: -len("__qc")]
        total = sum(counts.values())
        if total == 0:
            continue
        n_artifact = sum(counts.get(f, 0) for f in ARTIFACT_FLAGS)
        n_missing = sum(counts.get(f, 0) for f in MISSING_FLAGS)
        n_observed = total - n_missing
        artifact_rate = (n_artifact / n_observed) if n_observed > 0 else 0.0
        missing_rate = n_missing / total
        if artifact_rate > ARTIFACT_RATE_THRESHOLD:
            findings.append({
                "signal": signal,
                "kind": "high_artifact_rate",
                "artifact_rate": round(artifact_rate, 4),
                "missing_rate": round(missing_rate, 4),
                "n_observed": n_observed,
                "description": (
                    f"{artifact_rate:.1%} of observed samples flagged as "
                    f"artifact ({n_artifact:,}/{n_observed:,})"
                ),
            })
        if missing_rate > MISSING_RATE_THRESHOLD:
            findings.append({
                "signal": signal,
                "kind": "mostly_missing",
                "artifact_rate": round(artifact_rate, 4),
                "missing_rate": round(missing_rate, 4),
                "n_observed": n_observed,
                "description": (
                    f"{missing_rate:.1%} of samples missing "
                    f"({n_missing:,}/{total:,})"
                ),
            })
    return findings


def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    container = svc.get_container_client("processed")

    outdir = Path("qc_report")
    outdir.mkdir(exist_ok=True)

    all_findings = []
    n_patients = 0
    for b in container.list_blobs(name_starts_with="processed/level2/"):
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith("/lineage.json"):
            continue
        parts = name.split("/")
        # processed/level2/{PROJECT}/{safe_patient}/lineage.json
        if len(parts) < 5:
            continue
        project, safe_patient = parts[2], parts[3]
        try:
            raw = container.get_blob_client(name).download_blob().readall()
            lineage = json.loads(raw)
        except Exception as e:
            print(f"[qc-notify] WARN: could not read {name}: {e}", flush=True)
            continue
        n_patients += 1
        patient_label = lineage.get("patient_label", safe_patient)
        findings = analyze_qc_summary(lineage.get("qc_summary"))
        for f in findings:
            f["project"] = project
            f["patient"] = patient_label
            all_findings.append(f)

    print(f"[qc-notify] scanned {n_patients} patients, "
          f"{len(all_findings)} findings", flush=True)

    (outdir / "issues.json").write_text(
        json.dumps({
            "n_patients_scanned": n_patients,
            "n_findings": len(all_findings),
            "artifact_rate_threshold": ARTIFACT_RATE_THRESHOLD,
            "missing_rate_threshold": MISSING_RATE_THRESHOLD,
            "findings": all_findings,
        }, indent=1))

    # Markdown summary for the GitHub issue.
    lines = [
        "# QC Alert — Processing Quality Flags",
        "",
        f"Scanned **{n_patients}** processed patients. "
        f"**{len(all_findings)}** findings "
        f"(artifact rate > {ARTIFACT_RATE_THRESHOLD:.0%}, "
        f"missing rate > {MISSING_RATE_THRESHOLD:.0%}).",
        "",
    ]
    if all_findings:
        lines += [
            "| Project | Patient | Signal | Issue | Rate | Detail |",
            "|---|---|---|---|---|---|",
        ]
        for f in sorted(all_findings,
                        key=lambda x: (x["project"], x["patient"], x["signal"])):
            rate = (f["artifact_rate"] if f["kind"] == "high_artifact_rate"
                    else f["missing_rate"])
            kind = ("high artifact rate" if f["kind"] == "high_artifact_rate"
                    else "mostly missing")
            lines.append(
                f"| {f['project']} | {f['patient']} | {f['signal']} "
                f"| {kind} | {rate:.1%} | {f['description']} |")
        lines += [
            "",
            "_Thresholds: artifact rate > 10%, missing rate > 50%. "
            "Review flagged patients' graphs in the `graphs` container "
            "before trusting downstream stats._",
        ]
    else:
        lines.append("No QC issues found — all patients within thresholds.")
    (outdir / "summary.md").write_text("\n".join(lines) + "\n")

    print(f"QC_ISSUES_FOUND={len(all_findings)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
