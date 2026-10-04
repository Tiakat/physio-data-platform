"""Meta-Supervisor: the big supervisor for all supervisors.

K's requirement: "all the supervisors need to connect to each other.
And we need like a big supervisor for all of these supervisors."

This tool aggregates the outputs of every supervisor/validator into a
single pipeline health dashboard:

  1. Runs the lightweight supervisors directly:
     - audit_supervisors (static analysis, no Azure needed)
  2. Reads the latest outputs of the heavy supervisors:
     - continuity_check report (continuity/report.json)
     - qc_notifier report (qc_report/issues.json)
     - validate_processing report (validation_report/issues.json)
     - validate_external report (external_validation_report/report.json)
  3. Checks Azure directly for stage-level counts:
     - rawdata blobs (ingested)
     - processed/level2 lineage.json (processed)
     - graphs container (graphs)
     - processed/ml_models (models)
  4. Aggregates everything into meta_supervisor/health.json
  5. Creates/updates a single GitHub issue labeled `meta-supervisor`
     with the overall pipeline health table.

K sees one issue, one dashboard — not 9 separate reports.

Design:
- Never duplicates supervisor logic — calls them or reads their outputs.
- Heavy supervisors (needing decryption) are NOT re-run here; their
  latest committed outputs are read. Only lightweight Azure blob listings
  are done live.
- Exit code always 0 (K's no-noise rule). Problems go in health.json
  and the issue body, never as a crash.

Outputs:
- meta_supervisor/health.json — aggregated health dashboard
- GitHub issue labeled `meta-supervisor` (created or updated)
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

# Supervisors and where their latest output lives (relative to repo root
# when run as a recon task — outputs land in the working directory)
SUPERVISOR_OUTPUTS = {
    "audit_supervisors": {
        "report": "supervisor_audit/report.json",
        "summary_key": "summary",
    },
    "continuity_check": {
        "report": "continuity/report.json",
        "summary_key": "summary",
    },
    "qc_notifier": {
        "report": "qc_report/issues.json",
        "summary_key": None,  # list of issues
    },
    "validate_processing": {
        "report": "validation_report/issues.json",
        "summary_key": None,
    },
    "validate_external": {
        "report": "external_validation_report/report.json",
        "summary_key": None,
    },
}


def run_audit_supervisors() -> dict:
    """Run the static audit directly (no Azure/creds needed)."""
    print("[meta] Running audit_supervisors...", flush=True)
    try:
        from tools import audit_supervisors
        # Capture via subprocess to isolate
        r = subprocess.run(
            [sys.executable, "-m", "tools.audit_supervisors"],
            capture_output=True, text=True, timeout=120)
        print(r.stdout[-500:] if r.stdout else "", flush=True)
        p = Path("supervisor_audit/report.json")
        if p.exists():
            return json.loads(p.read_text())
        return {"error": "audit produced no report.json"}
    except Exception as e:
        return {"error": str(e)}


def read_supervisor_output(name: str) -> dict:
    """Read a supervisor's latest output file if present."""
    cfg = SUPERVISOR_OUTPUTS.get(name, {})
    p = Path(cfg.get("report", ""))
    if not p.exists():
        return {"status": "not_run", "note": f"No output at {p}"}
    try:
        data = json.loads(p.read_text())
        return {"status": "ok", "data": data}
    except Exception as e:
        return {"status": "error", "note": f"Could not parse {p}: {e}"}


def azure_stage_counts(svc) -> dict:
    """Lightweight live counts from Azure blob listings."""
    counts = {}
    try:
        raw_c = svc.get_container_client("rawdata")
        n_raw = sum(1 for b in raw_c.list_blobs()
                    if (b["name"] if isinstance(b, dict) else b.name)
                    .endswith(".parquet.enc"))
        counts["rawdata_parquets"] = n_raw
    except Exception as e:
        counts["rawdata_parquets"] = f"error: {e}"

    try:
        proc_c = svc.get_container_client("processed")
        n_lin = 0
        n_models = 0
        for b in proc_c.list_blobs():
            name = b["name"] if isinstance(b, dict) else b.name
            if name.endswith("/lineage.json"):
                n_lin += 1
            if "/ml_models/" in name:
                n_models += 1
        counts["lineage_json"] = n_lin
        counts["ml_model_files"] = n_models
    except Exception as e:
        counts["lineage_json"] = f"error: {e}"

    try:
        graphs_c = svc.get_container_client("graphs")
        n_graphs = sum(1 for _ in graphs_c.list_blobs())
        counts["graphs"] = n_graphs
    except Exception:
        counts["graphs"] = "container not found"

    return counts


def upsert_github_issue(body: str, health: dict):
    """Create or update the meta-supervisor GitHub issue."""
    n_problems = health["summary"]["total_problems"]
    title = (f"Meta-supervisor: pipeline health — "
             f"{n_problems} problems" if n_problems
             else "Meta-supervisor: pipeline health — OK")
    try:
        # Check for existing open issue
        r = subprocess.run(
            ["gh", "issue", "list", "--label", "meta-supervisor",
             "--state", "open", "--json", "number", "--jq", ".[0].number"],
            capture_output=True, text=True, timeout=30)
        existing = r.stdout.strip()
        if existing and existing != "null" and existing != "":
            subprocess.run(
                ["gh", "issue", "edit", existing,
                 "--title", title, "--body", body],
                capture_output=True, text=True, timeout=30)
            subprocess.run(
                ["gh", "issue", "comment", existing,
                 "--body", f"Health check updated {datetime.now(timezone.utc).isoformat()}"],
                capture_output=True, text=True, timeout=30)
            print(f"[meta] Updated issue #{existing}", flush=True)
        else:
            subprocess.run(
                ["gh", "issue", "create", "--label", "meta-supervisor",
                 "--title", title, "--body", body],
                capture_output=True, text=True, timeout=30)
            print("[meta] Created new meta-supervisor issue", flush=True)
    except Exception as e:
        print(f"[meta] WARNING: GitHub issue upsert failed: {e}", flush=True)


def main():
    out_dir = Path("meta_supervisor")
    out_dir.mkdir(exist_ok=True)

    print("[meta] Starting meta-supervisor aggregation...", flush=True)

    # 1. Run the static audit
    audit = run_audit_supervisors()

    # 2. Read other supervisors' latest outputs
    supervisor_status = {}
    for name in SUPERVISOR_OUTPUTS:
        if name == "audit_supervisors":
            supervisor_status[name] = {"status": "ok", "data": audit}
        else:
            supervisor_status[name] = read_supervisor_output(name)

    # 3. Live Azure stage counts
    print("[meta] Getting Azure stage counts...", flush=True)
    try:
        account = os.environ["AZURE_STORAGE_ACCOUNT"]
        svc = azure_auth.get_blob_service_client(account)
        stage_counts = azure_stage_counts(svc)
    except Exception as e:
        stage_counts = {"error": str(e)}
    print(f"[meta] Stage counts: {stage_counts}", flush=True)

    # 4. Aggregate problems
    problems = []

    # From audit: failed supervisors + gaps
    if isinstance(audit, dict) and "supervisors" in audit:
        for s in audit["supervisors"]:
            if s.get("verdict") == "FAIL":
                problems.append({
                    "source": "audit_supervisors",
                    "severity": "high",
                    "detail": f"{s['file']}: {'; '.join(s.get('issues', []))}",
                })
        for g in audit.get("gaps", []):
            problems.append({
                "source": "audit_supervisors",
                "severity": g.get("severity", "medium"),
                "detail": f"Gap: {g['stage']} — {g['note']}",
            })

    # From continuity: breaks
    cont = supervisor_status.get("continuity_check", {})
    if cont.get("status") == "ok" and isinstance(cont.get("data"), dict):
        cdata = cont["data"]
        s = cdata.get("summary", {})
        if not s.get("continuity_ok", True):
            problems.append({
                "source": "continuity_check",
                "severity": "high",
                "detail": f"Continuity breaks: {s.get('total_chain_breaks', '?')} "
                          f"chain breaks, preservation {s.get('preservation_rate', 0):.1%}",
            })
    elif cont.get("status") == "not_run":
        problems.append({
            "source": "continuity_check",
            "severity": "low",
            "detail": "continuity_check has not been run yet",
        })

    # From qc_notifier: issues found
    qc = supervisor_status.get("qc_notifier", {})
    if qc.get("status") == "ok" and isinstance(qc.get("data"), dict):
        qdata = qc["data"]
        issues = qdata.get("issues", []) if isinstance(qdata, dict) else []
        if issues:
            problems.append({
                "source": "qc_notifier",
                "severity": "medium",
                "detail": f"{len(issues)} QC issues flagged (see qc-alert issue)",
            })

    # From validate_processing / validate_external
    for vname in ("validate_processing", "validate_external"):
        v = supervisor_status.get(vname, {})
        if v.get("status") == "not_run":
            problems.append({
                "source": vname,
                "severity": "low",
                "detail": f"{vname} has not been run yet",
            })

    # Stage count sanity
    sc = stage_counts
    if isinstance(sc.get("rawdata_parquets"), int) and isinstance(sc.get("lineage_json"), int):
        if sc["lineage_json"] < sc["rawdata_parquets"]:
            problems.append({
                "source": "meta_supervisor",
                "severity": "medium",
                "detail": f"Processing incomplete: {sc['lineage_json']} lineage vs "
                          f"{sc['rawdata_parquets']} ingested",
            })

    health = {
        "tool": "meta_supervisor",
        "ts": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_problems": len(problems),
            "high": sum(1 for p in problems if p["severity"] == "high"),
            "medium": sum(1 for p in problems if p["severity"] == "medium"),
            "low": sum(1 for p in problems if p["severity"] == "low"),
            "overall": "HEALTHY" if not problems else (
                "CRITICAL" if any(p["severity"] == "high" for p in problems)
                else "WARNING"),
        },
        "stage_counts": stage_counts,
        "supervisor_status": {
            k: v.get("status", "unknown")
            for k, v in supervisor_status.items()
        },
        "problems": problems,
    }

    (out_dir / "health.json").write_text(json.dumps(health, indent=1))

    # 5. GitHub issue
    lines = ["# Pipeline Health Dashboard", ""]
    lines.append(f"**Overall: {health['summary']['overall']}** "
                 f"({health['summary']['total_problems']} problems)")
    lines.append(f"_Updated {health['ts']}_")
    lines.append("")
    lines.append("## Stage Counts (live from Azure)")
    lines.append("")
    lines.append("| Stage | Count |")
    lines.append("|---|---|")
    for k, v in stage_counts.items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    lines.append("## Supervisor Status")
    lines.append("")
    lines.append("| Supervisor | Status |")
    lines.append("|---|---|")
    for k, v in health["supervisor_status"].items():
        lines.append(f"| {k} | {v} |")
    lines.append("")
    if problems:
        lines.append("## Problems")
        lines.append("")
        for p in problems:
            lines.append(f"- **[{p['severity']}]** ({p['source']}) {p['detail']}")
        lines.append("")
    else:
        lines.append("No problems detected. All supervisors healthy.")
        lines.append("")

    upsert_github_issue("\n".join(lines), health)

    print(f"[meta] Done. Overall: {health['summary']['overall']} "
          f"({health['summary']['total_problems']} problems)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
