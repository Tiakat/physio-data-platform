"""Automatic pipeline chain: Dropbox -> 1-raw -> 2-processed -> 3-graphes -> 4-analysis.

Each stage is INCREMENTAL: it lists patients in the source container,
lists patients in the destination container, and only processes the
missing ones (by EXACT patient folder name string match).

    Stage 1: Dropbox -> 1-raw
        List Dropbox patients per project, list 1-raw patients,
        ingest missing (decrypt rawdata parquet -> CSV).
        NOTE: Stage 1 currently reuses tools/build_1raw.py logic.
        This module provides stages 2-4 and the orchestrator.

    Stage 2: 1-raw -> 2-processed
        For each patient in 1-raw not in 2-processed:
        load CSVs -> smart_filter_frame -> save filtered CSVs to 2-processed.

    Stage 3: 2-processed -> 3-graphes
        For each patient in 2-processed not in 3-graphes:
        load filtered CSVs -> generate per-column PNGs -> 3-graphes.

    Stage 4: 2-processed -> 4-analysis
        For each patient in 2-processed not in 4-analysis:
        compute per-column statistics -> Excel + summary PNG -> 4-analysis.

Containers (all lowercase, numbered for order):
    1-raw, 2-processed, 3-graphes, 4-analysis

Patient names: EXACT strings as in Dropbox / 1-raw. Never modified,
never provisional numbers.

Usage:
    python -m tools.pipeline_chain --stage 2 --project COLECTOMIE
    python -m tools.pipeline_chain --stage all --project all
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth  # noqa: E402

import pandas as pd  # noqa: E402

C_RAW = "1-raw"
C_PROC = "2-processed"
C_GRAPH = "3-graphes"
C_ANALYSIS = "4-analysis"
ALL_CONTAINERS = [C_RAW, C_PROC, C_GRAPH, C_ANALYSIS]


def ensure_all_containers(svc):
    for name in ALL_CONTAINERS:
        azure_auth.ensure_container(svc, name)
    print(f"[chain] containers ready: {ALL_CONTAINERS}", flush=True)


def list_patients(svc, container: str, project: str) -> set[str]:
    """List patient folder names under {container}/{project}/.

    Returns exact folder name strings.
    """
    cont = svc.get_container_client(container)
    patients: set[str] = set()
    prefix = f"{project}/"
    try:
        for b in cont.list_blobs(name_starts_with=prefix):
            name = b["name"] if isinstance(b, dict) else b.name
            parts = name.split("/")
            if len(parts) >= 2:
                patients.add(parts[1])
    except Exception as e:
        print(f"[chain] list_patients {container}/{project}: {e}", flush=True)
    return patients


def list_projects(svc, container: str) -> list[str]:
    cont = svc.get_container_client(container)
    codes: set[str] = set()
    try:
        for b in cont.list_blobs():
            name = b["name"] if isinstance(b, dict) else b.name
            parts = name.split("/")
            if parts:
                codes.add(parts[0].upper())
    except Exception:
        pass
    return sorted(codes)


def blob_exists(container, blob: str) -> bool:
    try:
        container.get_blob_client(blob).get_blob_properties()
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Stage 2: 1-raw -> 2-processed (smart filter)
# ---------------------------------------------------------------------------

def stage2_process_patient(svc, project: str, patient: str) -> dict:
    """Load all CSVs for a patient from 1-raw, smart-filter, save to 2-processed."""
    from tools.smart_filter import smart_filter_frame

    src = svc.get_container_client(C_RAW)
    dst = svc.get_container_client(C_PROC)
    prefix = f"{project}/{patient}/"
    result = {"patient": patient, "files": 0, "errors": []}

    for b in src.list_blobs(name_starts_with=prefix):
        blob = b["name"] if isinstance(b, dict) else b.name
        # blob: {project}/{patient}/{device}/{file}.csv
        parts = blob.split("/")
        if len(parts) < 4:
            continue
        device = parts[2]
        fname = parts[3]
        dest = f"{project}/{patient}/{device}/{fname}"
        if blob_exists(dst, dest):
            continue
        try:
            raw = src.get_blob_client(blob).download_blob().readall()
            df = pd.read_csv(io.BytesIO(raw))
            # Smart filter: needs a time column; use first column as fallback
            t = None
            for c in df.columns:
                if "time" in c.lower() or "ms" in c.lower():
                    t = df[c].values
                    break
            filtered, qc = smart_filter_frame(df, t)
            # Save filtered + QC side by side
            out = filtered.copy()
            for c in qc.columns:
                out[f"{c}__qc"] = qc[c].values
            csv_data = out.to_csv(index=False).encode("utf-8")
            dst.get_blob_client(dest).upload_blob(
                csv_data, overwrite=True,
                content_settings={"content_type": "text/csv"},
            )
            result["files"] += 1
            del df, filtered, qc, out, csv_data, raw
        except Exception as exc:
            result["errors"].append(f"{blob}: {exc}")
    return result


def run_stage2(svc, project: str) -> dict:
    """1-raw -> 2-processed for missing patients."""
    src_patients = list_patients(svc, C_RAW, project)
    dst_patients = list_patients(svc, C_PROC, project)
    missing = sorted(src_patients - dst_patients)
    print(f"[chain][s2] {project}: {len(src_patients)} in 1-raw, "
          f"{len(dst_patients)} in 2-processed, {len(missing)} to process",
          flush=True)
    summary = {"project": project, "processed": [], "skipped": len(dst_patients)}
    for patient in missing:
        print(f"[chain][s2] {project}/{patient}...", flush=True)
        r = stage2_process_patient(svc, project, patient)
        summary["processed"].append(r)
    return summary


# ---------------------------------------------------------------------------
# Stage 3: 2-processed -> 3-graphes (PNG per column)
# ---------------------------------------------------------------------------

def stage3_graph_patient(svc, project: str, patient: str,
                         max_points: int = 20000) -> dict:
    """Generate per-column PNGs for a patient from 2-processed -> 3-graphes."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    src = svc.get_container_client(C_PROC)
    dst = svc.get_container_client(C_GRAPH)
    prefix = f"{project}/{patient}/"
    result = {"patient": patient, "graphs": 0, "errors": []}

    for b in src.list_blobs(name_starts_with=prefix):
        blob = b["name"] if isinstance(b, dict) else b.name
        parts = blob.split("/")
        if len(parts) < 4 or not blob.endswith(".csv"):
            continue
        device = parts[2]
        stem = parts[3].rsplit(".", 1)[0]
        try:
            raw = src.get_blob_client(blob).download_blob().readall()
            df = pd.read_csv(io.BytesIO(raw))
        except Exception as exc:
            result["errors"].append(f"{blob}: load {exc}")
            continue

        # Time column for x-axis
        t = None
        for c in df.columns:
            if "time" in c.lower():
                t = pd.to_numeric(df[c], errors="coerce").values
                break
        if t is None:
            t = np.arange(len(df))

        for col in df.columns:
            if col.endswith("__qc") or "time" in col.lower():
                continue
            dest = f"{project}/{patient}/{device}_{stem}_{col}.png"
            if blob_exists(dst, dest):
                continue
            try:
                y = pd.to_numeric(df[col], errors="coerce").values
                mask = ~np.isnan(y)
                if mask.sum() == 0:
                    continue
                # Decimate for plotting
                idx = np.where(mask)[0]
                if len(idx) > max_points:
                    idx = idx[:: len(idx) // max_points]
                fig, ax = plt.subplots(figsize=(10, 3))
                ax.plot(t[idx], y[idx], color="gray", lw=0.5, label="raw")
                # Overlay QC-flagged points in black if QC column exists
                qc_col = f"{col}__qc"
                if qc_col in df.columns:
                    bad = (df[qc_col].astype(str) != "VALID").values[idx]
                    if bad.any():
                        ax.scatter(t[idx][bad], y[idx][bad],
                                   color="black", s=4, label="flagged")
                ax.set_title(f"{project} {patient} {device} {col}")
                ax.set_xlabel("time")
                ax.legend(fontsize=8)
                fig.tight_layout()
                buf = io.BytesIO()
                fig.savefig(buf, format="png", dpi=80)
                plt.close(fig)
                buf.seek(0)
                dst.get_blob_client(dest).upload_blob(
                    buf.read(), overwrite=True,
                    content_settings={"content_type": "image/png"},
                )
                result["graphs"] += 1
            except Exception as exc:
                result["errors"].append(f"{col}: {exc}")
    return result


def run_stage3(svc, project: str) -> dict:
    """2-processed -> 3-graphes for missing patients."""
    src_patients = list_patients(svc, C_PROC, project)
    dst_patients = list_patients(svc, C_GRAPH, project)
    missing = sorted(src_patients - dst_patients)
    print(f"[chain][s3] {project}: {len(src_patients)} in 2-processed, "
          f"{len(dst_patients)} in 3-graphes, {len(missing)} to process",
          flush=True)
    summary = {"project": project, "processed": [], "skipped": len(dst_patients)}
    for patient in missing:
        print(f"[chain][s3] {project}/{patient}...", flush=True)
        r = stage3_graph_patient(svc, project, patient)
        summary["processed"].append(r)
    return summary


# ---------------------------------------------------------------------------
# Stage 4: 2-processed -> 4-analysis (stats -> Excel)
# ---------------------------------------------------------------------------

def stage4_analyze_patient(svc, project: str, patient: str) -> dict:
    """Compute per-column stats -> Excel + summary -> 4-analysis."""
    src = svc.get_container_client(C_PROC)
    dst = svc.get_container_client(C_ANALYSIS)
    prefix = f"{project}/{patient}/"
    result = {"patient": patient, "files": 0, "errors": []}

    rows = []
    for b in src.list_blobs(name_starts_with=prefix):
        blob = b["name"] if isinstance(b, dict) else b.name
        parts = blob.split("/")
        if len(parts) < 4 or not blob.endswith(".csv"):
            continue
        device = parts[2]
        fname = parts[3]
        try:
            raw = src.get_blob_client(blob).download_blob().readall()
            df = pd.read_csv(io.BytesIO(raw))
        except Exception as exc:
            result["errors"].append(f"{blob}: load {exc}")
            continue
        for col in df.columns:
            if col.endswith("__qc") or "time" in col.lower():
                continue
            try:
                y = pd.to_numeric(df[col], errors="coerce")
                n = int(y.notna().sum())
                if n == 0:
                    continue
                rows.append({
                    "project": project, "patient": patient,
                    "device": device, "file": fname, "column": col,
                    "n": n,
                    "mean": float(y.mean()),
                    "std": float(y.std()),
                    "min": float(y.min()),
                    "max": float(y.max()),
                    "median": float(y.median()),
                    "q25": float(y.quantile(0.25)),
                    "q75": float(y.quantile(0.75)),
                })
            except Exception:
                pass

    if rows:
        stats_df = pd.DataFrame(rows)
        dest = f"{project}/{patient}/statistics.xlsx"
        if not blob_exists(dst, dest):
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine="openpyxl") as xw:
                stats_df.to_excel(xw, sheet_name="columns", index=False)
            buf.seek(0)
            dst.get_blob_client(dest).upload_blob(
                buf.read(), overwrite=True,
                content_settings={
                    "content_type": "application/vnd.openxmlformats-"
                                    "officedocument.spreadsheetml.sheet"},
            )
            result["files"] = 1
    return result


def run_stage4(svc, project: str) -> dict:
    """2-processed -> 4-analysis for missing patients."""
    src_patients = list_patients(svc, C_PROC, project)
    dst_patients = list_patients(svc, C_ANALYSIS, project)
    missing = sorted(src_patients - dst_patients)
    print(f"[chain][s4] {project}: {len(src_patients)} in 2-processed, "
          f"{len(dst_patients)} in 4-analysis, {len(missing)} to process",
          flush=True)
    summary = {"project": project, "processed": [], "skipped": len(dst_patients)}
    for patient in missing:
        print(f"[chain][s4] {project}/{patient}...", flush=True)
        r = stage4_analyze_patient(svc, project, patient)
        summary["processed"].append(r)
    return summary


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

STAGE_RUNNERS = {
    2: run_stage2,
    3: run_stage3,
    4: run_stage4,
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", default="all",
                    help="'all' or comma-separated stages: 2,3,4 "
                         "(stage 1 is tools/build_1raw.py)")
    ap.add_argument("--project", default="all",
                    help="Project code or 'all'")
    ap.add_argument("--out", default="chain_out",
                    help="Local dir for the chain manifest")
    args = ap.parse_args()

    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    ensure_all_containers(svc)

    if args.stage.strip().lower() == "all":
        stages = [2, 3, 4]
    else:
        stages = [int(s.strip()) for s in args.stage.split(",")]

    projects = list_projects(svc, C_RAW) \
        if args.project.strip().lower() == "all" \
        else [args.project.strip().upper()]
    print(f"[chain] stages={stages} projects={projects}", flush=True)

    manifest: dict = {"stages": stages, "projects": {}}
    for project in projects:
        manifest["projects"][project] = {}
        for stage in stages:
            print(f"[chain] === stage {stage} / {project} ===", flush=True)
            summary = STAGE_RUNNERS[stage](svc, project)
            manifest["projects"][project][f"stage{stage}"] = {
                "processed": len(summary["processed"]),
                "skipped": summary["skipped"],
            }

    outdir = Path(args.out)
    outdir.mkdir(parents=True, exist_ok=True)
    mpath = outdir / f"chain_manifest_{args.project}.json"
    mpath.write_text(json.dumps(manifest, indent=2, default=str))
    print(f"[chain] manifest: {mpath}", flush=True)
    print("[chain] DONE", flush=True)


if __name__ == "__main__":
    main()
