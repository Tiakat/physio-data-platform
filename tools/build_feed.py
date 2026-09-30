"""
Build the de-identified website feed from processed ETT exams.

    python -m tools.build_feed --store <ett_store root> --out <reports/feed>

Reads per-exam ``features/stats.parquet`` + ``manifest.json`` and writes
aggregate JSON only. Hard privacy rules:

- never patient-level rows, never typed IDs (no patient_id, no exam_guid,
  no session_guid, no filenames)
- never free text (no annotations, forms, paths)
- counts below k=5 are suppressed (reported as null with suppressed=true)

Output::

    reports/feed/feed.json            <- whole platform summary
    reports/feed/<project>.json       <- per-project detail

The liam-v2 physio-data page consumes these files and nothing else.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

K_ANONYMITY = 5


def _suppress(n: int):
    """Return the count, or None if below the k-anonymity threshold."""
    return n if n >= K_ANONYMITY else None


def build_project(project_dir: Path) -> dict:
    exams = sorted(project_dir.iterdir())
    manifests = []
    feat_frames = []
    for exam_dir in exams:
        mf = exam_dir / "manifest.json"
        st = exam_dir / "features" / "stats.parquet"
        if not mf.exists():
            continue
        manifests.append(json.loads(mf.read_text(encoding="utf-8")))
        if st.exists():
            df = pd.read_parquet(st)
            feat_frames.append(df)

    n_exams = len(manifests)
    suppressed = _suppress(n_exams) is None

    verdicts: dict[str, int] = {}
    ett_versions: dict[str, int] = {}
    total_rows = 0
    for m in manifests:
        v = m.get("validation", {}).get("verdict", "UNKNOWN")
        verdicts[v] = verdicts.get(v, 0) + 1
        ev = m.get("ett_version", "?") or "?"
        ett_versions[ev] = ett_versions.get(ev, 0) + 1
        total_rows += m.get("rows", 0)

    signals: dict[str, dict] = {}
    if feat_frames and not suppressed:
        allf = pd.concat(feat_frames, ignore_index=True)
        for signal, g in allf.groupby("signal", observed=True):
            n = int(g["exam_guid"].nunique())
            if _suppress(n) is None:
                continue
            signals[str(signal)] = {
                "n_exams": n,
                "mean_duration_s": round(float(g["duration_s"].mean()), 1),
                "total_hours": round(float(g["duration_s"].sum()) / 3600, 1),
                "null_fraction": round(float(g["n_null"].sum() / g["n"].sum()), 4)
                if g["n"].sum() else 0.0,
            }

    return {
        "n_exams": _suppress(n_exams),
        "suppressed": suppressed,
        "total_rows": _suppress(total_rows),
        "qc_verdicts": {k: _suppress(v) for k, v in verdicts.items()},
        "ett_versions": ett_versions if not suppressed else {},
        "signals": signals,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="Build de-identified website feed.")
    ap.add_argument("--store", required=True,
                    help="ett_store root (contains processed/<project>/<exam>)")
    ap.add_argument("--out", default="reports/feed",
                    help="output directory for feed JSON")
    args = ap.parse_args()

    processed = Path(args.store) / "processed"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    feed: dict = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "privacy": {
            "note": "De-identified aggregates only. No patient-level rows, "
                    "no identifiers, no free text.",
            "k_anonymity": K_ANONYMITY,
        },
        "projects": {},
    }

    if processed.exists():
        for project_dir in sorted(processed.iterdir()):
            if not project_dir.is_dir():
                continue
            summary = build_project(project_dir)
            feed["projects"][project_dir.name] = summary
            (out / f"{project_dir.name}.json").write_text(
                json.dumps({"generated_at": feed["generated_at"],
                            "project": project_dir.name,
                            **summary}, indent=2),
                encoding="utf-8")
            print(f"  {project_dir.name}: "
                  f"{summary['n_exams']} exams "
                  f"({'suppressed' if summary['suppressed'] else 'published'})")

    (out / "feed.json").write_text(json.dumps(feed, indent=2), encoding="utf-8")
    print(f"\nfeed  {out / 'feed.json'}")


if __name__ == "__main__":
    main()
