#!/usr/bin/env python3
"""Entry point: run the platform for one project.

    python run.py --project intraop-eeg --ingest --process --feed

Stages (pick any subset):
  --ingest    download new/changed raw files from Dropbox to staging
  --process   run the adapter over every staged patient (-> Parquet)
  --stats     aggregate per-patient features into cohort_stats.parquet
  --feed      build the website feed (JSON index + figures)

Needs DROPBOX_TOKEN in the environment for --ingest.
"""
from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

import pandas as pd

from physio_platform import config as C
from physio_platform import ingest as I
from physio_platform import runner as R
from physio_platform import store as S
from physio_platform import website as W

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("run")


def cmd_stats(cfg: C.ProjectConfig) -> Path | None:
    """Generic cohort aggregation: concatenate per-patient features and take
    the median per phase. Projects with fancier stats can replace this."""
    run_dir = Path(cfg.outputs.root) / cfg.name / cfg.fingerprint
    frames = []
    for pid_dir in sorted(run_dir.iterdir()):
        if not pid_dir.is_dir():
            continue
        f = pid_dir / "features.parquet"
        if f.exists():
            df = pd.read_parquet(f)
            df["patient_id"] = pid_dir.name
            frames.append(df)
    if not frames:
        log.warning("no per-patient features found; skipping stats")
        return None
    allf = pd.concat(frames, ignore_index=True)
    by = "phase" if "phase" in allf.columns else "patient_id"
    num = allf.select_dtypes("number").columns.tolist()
    stats = allf.groupby(by)[num].median(numeric_only=True).reset_index()
    stats["n_patients"] = allf["patient_id"].nunique()
    out = S.write_cohort_stats(cfg.outputs.root, cfg.name, cfg.fingerprint, stats)
    log.info("cohort stats -> %s (%d rows)", out, len(stats))
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="physio-data-platform runner")
    ap.add_argument("--project", required=True,
                    help="project id (matches projects/<id>.yaml)")
    ap.add_argument("--projects-dir", default="projects")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--process", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--feed", action="store_true")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --ingest: only list what would download")
    args = ap.parse_args()

    cfg = C.load_project(Path(args.projects_dir) / f"{args.project}.yaml")
    log.info("project=%s fingerprint=%s adapter=%s",
             cfg.name, cfg.fingerprint, cfg.processing.adapter)

    staged = []
    if args.ingest:
        staged = I.ingest_project(cfg, dry_run=args.dry_run)
    elif args.process or args.stats or args.feed:
        staged = [I.StagedFile(**v) for k, v in I.load_manifest(cfg).items()
                  if k != "__meta__"]

    if args.process:
        if not staged:
            log.warning("nothing staged; run --ingest first")
        else:
            R.run_project(cfg, staged)

    if args.stats:
        cmd_stats(cfg)

    if args.feed:
        out = W.build_feed(cfg)
        log.info("website feed -> %s", out)
        print(out)


if __name__ == "__main__":
    main()
