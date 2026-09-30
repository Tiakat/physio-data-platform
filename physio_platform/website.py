"""Website feed: build the JSON index + figure set the lab site reads.

``build_feed`` scans the processed store for one project and writes::

    <feed_dir>/<project>/feed.json      # projects, patients, figures, stats
    <feed_dir>/<project>/figures/...    # copied per-patient PNGs

The website only ever reads this directory: it never touches raw or
processed data directly. Only aggregated/de-identified content should be
copied here (adapters decide which figures are safe to publish).
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

from .config import ProjectConfig

FEED_NAME = "feed.json"


def build_feed(cfg: ProjectConfig,
               out_root: str | Path | None = None) -> Path:
    out_root = Path(out_root or cfg.outputs.root)
    run_dir = out_root / cfg.name / cfg.fingerprint
    feed_dir = Path(cfg.website.feed_dir) / cfg.name
    feed_dir.mkdir(parents=True, exist_ok=True)
    (feed_dir / "figures").mkdir(exist_ok=True)

    manifest_path = run_dir / "run_manifest.json"
    run = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    patients = []
    for pid, prec in (run.get("patients") or {}).items():
        if prec.get("status") != "ok":
            continue
        pdir = run_dir / pid
        figs = []
        for png in sorted((pdir / "figures").glob("*.png")):
            dest = feed_dir / "figures" / f"{pid}_{png.name}"
            shutil.copy2(png, dest)
            figs.append(f"figures/{pid}_{png.name}")
        patients.append({
            "patient_id": pid,
            "figures": figs,
            "n_samples": prec.get("n_samples"),
            "duration_s": prec.get("duration_s"),
        })

    stats_path = run_dir / "cohort_stats.parquet"
    feed = {
        "project": cfg.name,
        "description": cfg.description,
        "config_fingerprint": cfg.fingerprint,
        "code_version": run.get("code_version"),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_patients": len(patients),
        "n_failed": run.get("n_failed", 0),
        "patients": patients,
        "has_cohort_stats": stats_path.exists(),
        "sampling_rate_hz": cfg.sampling_rate_hz,
        "channels": cfg.channels,
    }
    out = feed_dir / FEED_NAME
    out.write_text(json.dumps(feed, indent=2))
    return out
