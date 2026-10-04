"""Continuity Checker: verify the patient journey across pipeline stages.

K's requirement: "I don't want all the steps to be completely isolated.
I want them to be like a story, a continuation."

This tool traces each patient/file through every stage:
  1. DROPBOX (ground truth) → did the file get ingested?
  2. INGEST (rawdata/*.parquet.enc) → does the blob exist in Azure?
  3. PROCESSING (processed/level2/*/lineage.json) → was it processed?
  4. GRAPHS (graphs/*/*.png) → were graphs generated and uploaded?

Checks performed:
- Patient preservation: every ingested file must have a lineage.json.
  Reports any patient lost at any stage.
- Stage linkage: each lineage.json records its source_blob — verify that
  blob actually exists in rawdata (unbroken chain).
- Data modification tracking: lineage.json records input `rows`. Flags
  patients where input had 0 rows or where the chain references a
  non-existent source.
- Graph coverage: every processed patient should have graphs in the
  `graphs` container.

Design: uses blob listings + lineage.json only. Does NOT download/decrypt
parquet data (avoids massive downloads). Does NOT touch Dropbox beyond
what verify_ingest already established (read-only ground truth).

Outputs:
- continuity/report.json — per-project stage-by-stage preservation stats
  + list of broken links
- continuity/report.md — human-readable summary

Exit code always 0 (reports issues in files, never crashes — K's no-noise rule).
"""
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth


def safe_label(label: str) -> str:
    return "".join(ch if ch.isalnum() else "_" for ch in label).strip("_")


def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    raw_c = svc.get_container_client("rawdata")
    proc_c = svc.get_container_client("processed")
    try:
        graphs_c = svc.get_container_client("graphs")
        graphs_available = True
    except Exception:
        graphs_available = False

    out_dir = Path("continuity")
    out_dir.mkdir(exist_ok=True)

    print("[continuity] Listing rawdata blobs...", flush=True)
    # Stage 2: ingested files by project
    ingested = defaultdict(list)  # project -> [blob names]
    for b in raw_c.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith(".parquet.enc"):
            continue
        parts = name.split("/")
        if len(parts) < 3:
            continue
        ingested[parts[0].upper()].append(name)
    print(f"[continuity] {sum(len(v) for v in ingested.values())} ingested parquets "
          f"across {len(ingested)} projects", flush=True)

    print("[continuity] Listing lineage.json files...", flush=True)
    # Stage 3: processed lineage by project
    # lineage_details: project -> {safe_label: blob_name}
    lineage_details = defaultdict(dict)
    for b in proc_c.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.startswith("processed/level2/"):
            continue
        if not name.endswith("/lineage.json"):
            continue
        parts = name.split("/")
        if len(parts) >= 4:
            proj = parts[2].upper()
            label = parts[3]
            lineage_details[proj][label] = name

    # lineage_labels: project -> [safe_label, ...]
    lineage_labels = {proj: list(labels.keys())
                      for proj, labels in lineage_details.items()}

    print("[continuity] Listing graphs...", flush=True)
    # Stage 4: graphs by project/patient
    graphs_by_patient = defaultdict(set)  # (proj, patient) -> {png names}
    if graphs_available:
        try:
            for b in graphs_c.list_blobs():
                name = b["name"] if isinstance(b, dict) else b.name
                # Expected: {PROJECT}/{patient}/{column}.png
                parts = name.split("/")
                if len(parts) >= 3 and name.endswith(".png"):
                    graphs_by_patient[(parts[0].upper(), parts[1])].add(name)
        except Exception as e:
            print(f"[continuity] WARNING: could not list graphs: {e}", flush=True)
            graphs_available = False
    print(f"[continuity] Graphs found for {len(graphs_by_patient)} patients",
          flush=True)

    # Build set of all rawdata blob names for chain verification
    all_raw_blobs = set()
    for blobs in ingested.values():
        all_raw_blobs.update(blobs)

    # Per-project continuity analysis
    projects = {}
    all_breaks = []

    for proj in sorted(set(list(ingested.keys()) + list(lineage_labels.keys()))):
        blobs = sorted(ingested.get(proj, []))
        # Expected labels use same logic as verify_processing / build_sweep_matrix
        expected_labels = []
        for i in range(1, len(blobs) + 1):
            expected_labels.append(safe_label(f"patient {i}"))

        done_labels = set(lineage_labels.get(proj, []))
        expected_set = set(expected_labels)

        missing_processing = sorted(expected_set - done_labels)
        extra_lineage = sorted(done_labels - expected_set)

        # Graph coverage for processed patients
        with_graphs = 0
        without_graphs = []
        for label in done_labels:
            key = (proj, label)
            if key in graphs_by_patient and graphs_by_patient[key]:
                with_graphs += 1
            else:
                without_graphs.append(label)

        # Chain integrity: sample lineage.json files, verify source_blob exists
        chain_broken = []
        chain_checked = 0
        details = lineage_details.get(proj, {})
        # Check up to 10 per project to avoid excessive downloads
        for label in sorted(details.keys())[:10]:
            blob_name = details[label]
            try:
                data = proc_c.get_blob_client(blob_name).download_blob().readall()
                lin = json.loads(data)
                chain_checked += 1
                src = lin.get("source_blob", "")
                if src and src not in all_raw_blobs:
                    chain_broken.append({
                        "patient": label,
                        "source_blob": src,
                        "issue": "lineage references non-existent rawdata blob",
                    })
                # Data sanity: 0 rows means nothing to process
                if lin.get("rows", -1) == 0:
                    chain_broken.append({
                        "patient": label,
                        "issue": "lineage reports 0 input rows",
                    })
            except Exception as e:
                chain_broken.append({
                    "patient": label,
                    "issue": f"could not read lineage.json: {e}",
                })

        for cb in chain_broken:
            cb["project"] = proj
            all_breaks.append(cb)

        projects[proj] = {
            "ingested_files": len(blobs),
            "processed_patients": len(done_labels),
            "missing_processing": len(missing_processing),
            "missing_processing_labels": missing_processing[:20],
            "extra_lineage": len(extra_lineage),
            "with_graphs": with_graphs,
            "without_graphs": len(without_graphs),
            "without_graphs_labels": without_graphs[:20],
            "chain_checked": chain_checked,
            "chain_broken": len(chain_broken),
            "continuity_ok": (len(missing_processing) == 0
                              and len(chain_broken) == 0),
        }

        status = "OK" if projects[proj]["continuity_ok"] else "BREAK"
        print(f"[continuity] {proj}: {len(blobs)} ingested → "
              f"{len(done_labels)} processed → {with_graphs} with graphs "
              f"[{status}]", flush=True)

    total_ingested = sum(p["ingested_files"] for p in projects.values())
    total_processed = sum(p["processed_patients"] for p in projects.values())
    total_with_graphs = sum(p["with_graphs"] for p in projects.values())
    total_breaks = len(all_breaks)

    report = {
        "tool": "continuity_check",
        "summary": {
            "total_ingested": total_ingested,
            "total_processed": total_processed,
            "total_with_graphs": total_with_graphs,
            "preservation_rate": (total_processed / total_ingested
                                  if total_ingested else 0),
            "graph_coverage": (total_with_graphs / total_processed
                               if total_processed else 0),
            "total_chain_breaks": total_breaks,
            "continuity_ok": total_breaks == 0 and all(
                p["continuity_ok"] for p in projects.values()),
        },
        "projects": projects,
        "chain_breaks": all_breaks,
        "notes": [
            "Patient labels are provisional (patient 1, 2, ... by sorted blob order). "
            "Real Dropbox patient-folder linkage is still pending.",
            "Graphs container may not exist yet if graph backfill hasn't run.",
            "Data modification tracking (raw vs filtered row counts) requires "
            "lineage.json to record filtered rows — currently only input rows "
            "are recorded. Recommend adding 'filtered_rows' to lineage.",
        ],
    }

    (out_dir / "report.json").write_text(json.dumps(report, indent=1))

    # Markdown
    lines = ["# Continuity Check Report", ""]
    s = report["summary"]
    lines.append(f"**Ingested:** {s['total_ingested']} → "
                 f"**Processed:** {s['total_processed']} → "
                 f"**With graphs:** {s['total_with_graphs']}")
    lines.append(f"**Preservation rate:** {s['preservation_rate']:.1%} | "
                 f"**Graph coverage:** {s['graph_coverage']:.1%} | "
                 f"**Chain breaks:** {s['total_chain_breaks']}")
    lines.append(f"**Continuity:** {'OK' if s['continuity_ok'] else 'BREAKS FOUND'}")
    lines.append("")
    lines.append("| Project | Ingested | Processed | Missing | With graphs | Chain | Status |")
    lines.append("|---|---|---|---|---|---|---|")
    for proj in sorted(projects):
        p = projects[proj]
        st = "OK" if p["continuity_ok"] else "BREAK"
        lines.append(f"| {proj} | {p['ingested_files']} | {p['processed_patients']} | "
                     f"{p['missing_processing']} | {p['with_graphs']} | "
                     f"{p['chain_checked']} checked, {p['chain_broken']} broken | {st} |")
    lines.append("")
    if all_breaks:
        lines.append("## Chain Breaks")
        lines.append("")
        for b in all_breaks[:30]:
            lines.append(f"- **{b.get('project', '?')}/{b.get('patient', '?')}**: "
                         f"{b['issue']}")
        lines.append("")
    lines.append("## Notes")
    lines.append("")
    for n in report["notes"]:
        lines.append(f"- {n}")
    lines.append("")

    (out_dir / "report.md").write_text("\n".join(lines))
    print(f"[continuity] Done. Continuity: "
          f"{'OK' if s['continuity_ok'] else 'BREAKS FOUND'}", flush=True)
    print(f"[continuity] Report in {out_dir}/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
