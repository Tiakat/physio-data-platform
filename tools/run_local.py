"""
Run the whole pipeline on a folder, with no database, no Docker and no cloud.

This exists so the pipeline can be proved on real data on day one, before any
infrastructure is set up. It writes a small result set that the Streamlit app
can read directly, so the researcher interface also works immediately.

    python -m tools.run_local --project IPAMS --root "D:/Dropbox/.../IPAMS/Database/RawData"

Outputs, under ./local_store/<PROJECT>/ :
    parquet/<recording_id>.parquet    standardised signals
    catalog.json                      one entry per file, with validation, QC and provenance
    summary.csv                       one row per patient, analysis ready
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backbone import qc, validate                    # noqa: E402
from backbone.config import (                        # noqa: E402
    classify_file,
    device_from_dirname,
    load_profile,
    normalise_key,
)
from backbone.parsers import get as get_parser       # noqa: E402
from backbone.parsers._common import (               # noqa: E402
    file_sequence_number,
)

PIPELINE_VERSION = "0.1.0"


def sha256(path: Path, chunk=1 << 20) -> str:
    """Calculate the SHA-256 hash of a file."""

    digest = hashlib.sha256()

    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)

    return digest.hexdigest()


def find_patient(profile: dict, relative: Path):
    """
    Resolve a patient code from the path, using whichever rule the profile
    declares. Handles both patient first and device first layouts.
    """

    layout = profile.get("layout", {})

    patterns = layout.get("patient_dir_patterns")

    if not patterns:
        patterns = [
            layout.get(
                "patient_dir_regex",
                r"^(?P<patient>\d+)$",
            )
        ]

    excl = layout.get("exclude_dir_regex")

    for part in relative.parts:

        if excl and re.match(
            excl,
            part,
            re.IGNORECASE,
        ):
            continue

        for pattern in patterns:

            match = re.match(pattern, part)

            if match and match.groupdict().get("patient"):

                number = match.group("patient")

                fmt = layout.get(
                    "patient_code_format",
                    "P-{patient:0>4}",
                )

                return fmt.format(
                    patient=number
                ), part

    return None, None


def find_device(
    profile: dict,
    relative: Path,
    declared: str | None,
):
    """Resolve the device associated with a file."""

    if declared:
        return declared

    for part in reversed(relative.parts[:-1]):

        code = device_from_dirname(
            profile,
            part,
        )

        if code:
            return code

    return "other"


def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--project",
        required=True,
        help="project code, e.g. IPAMS",
    )

    ap.add_argument(
        "--root",
        required=True,
        help="folder to scan",
    )

    ap.add_argument(
        "--out",
        default="local_store",
    )

    ap.add_argument(
        "--limit",
        type=int,
        default=0,
        help="stop after N files, 0 = all",
    )

    args = ap.parse_args()

    profile = load_profile(args.project)

    root = Path(args.root)

    out = (
        Path(args.out)
        / profile["project"]["code"]
    )

    (
        out / "parquet"
    ).mkdir(
        parents=True,
        exist_ok=True,
    )

    catalog: list[dict] = []

    seen_hashes: dict[str, str] = {}

    bettercare_groups: dict[
        tuple,
        list[Path],
    ] = {}

    files = sorted(
        p
        for p in root.rglob("*")
        if p.is_file()
    )

    print(
        f"{len(files)} files under {root}"
    )

    for n, path in enumerate(
        files,
        1,
    ):

        if args.limit and n > args.limit:
            break

        relative = path.relative_to(root)

        tier, declared_device, parser_name = (
            classify_file(
                profile,
                path.name,
            )
        )

        if tier == "C":
            continue

        patient, source_label = find_patient(
            profile,
            relative,
        )

        device = find_device(
            profile,
            relative,
            declared_device,
        )

        entry = {
            "file": str(relative).replace(
                "\\",
                "/",
            ),
            "name": path.name,
            "patient": patient,
            "source_label": source_label,
            "device": device,
            "tier": tier,
            "size_bytes": path.stat().st_size,
            "pipeline_version": PIPELINE_VERSION,
            "ingested_at": datetime.now(
                timezone.utc
            ).isoformat(
                timespec="seconds"
            ),
        }

        digest = sha256(path)

        entry["sha256"] = digest

        # -------------------------------------------------------------
        # Duplicate detection
        # -------------------------------------------------------------

        if digest in seen_hashes:

            entry["parse_status"] = "duplicate"

            entry["duplicate_of"] = (
                seen_hashes[digest]
            )

            catalog.append(entry)

            continue

        seen_hashes[digest] = entry["file"]

        # -------------------------------------------------------------
        # Tier B files
        # -------------------------------------------------------------

        if tier == "B" and not parser_name:

            entry["parse_status"] = "not_supported"

            entry["note"] = (
                "kept and indexed, no parser "
                "for this format"
            )

            catalog.append(entry)

            continue

        # -------------------------------------------------------------
        # BetterCare grouped recordings
        # -------------------------------------------------------------

        if parser_name == "bettercare":

            key = (
                patient,
                re.sub(
                    r"-\d+\.csv$",
                    "",
                    path.name,
                    flags=re.I,
                ),
            )

            bettercare_groups.setdefault(
                key,
                [],
            ).append(path)

            entry["parse_status"] = (
                "deferred_group"
            )

            catalog.append(entry)

            continue

        # -------------------------------------------------------------
        # Normal parser
        # -------------------------------------------------------------

        _parse_one(
            entry,
            [path],
            parser_name,
            device,
            profile,
            out,
            catalog,
        )

        if n % 25 == 0:

            print(
                f"  {n}/{len(files)}"
            )

    # -------------------------------------------------------------
    # Grouped BetterCare recordings
    # -------------------------------------------------------------

    for (
        patient,
        stem,
    ), paths in bettercare_groups.items():

        paths = sorted(
            paths,
            key=lambda p: file_sequence_number(
                p.name
            ),
        )

        entry = {
            "file": str(
                paths[0].relative_to(root)
            ).replace(
                "\\",
                "/",
            ),
            "name": (
                f"{stem} "
                f"[{len(paths)} files]"
            ),
            "patient": patient,
            "device": "bettercare",
            "tier": "A",
            "size_bytes": sum(
                p.stat().st_size
                for p in paths
            ),
            "sha256": hashlib.sha256(
                "".join(
                    sorted(
                        sha256(p)
                        for p in paths
                    )
                ).encode()
            ).hexdigest(),
            "pipeline_version": PIPELINE_VERSION,
            "ingested_at": datetime.now(
                timezone.utc
            ).isoformat(
                timespec="seconds"
            ),
        }

        _parse_one(
            entry,
            paths,
            "bettercare",
            "bettercare",
            profile,
            out,
            catalog,
        )

    # -------------------------------------------------------------
    # Write catalog
    # -------------------------------------------------------------

    (
        out / "catalog.json"
    ).write_text(
        json.dumps(
            catalog,
            indent=2,
        ),
        encoding="utf-8",
    )

    # -------------------------------------------------------------
    # Build patient summary
    # -------------------------------------------------------------

    summary = build_summary(
        catalog,
        profile,
    )

    summary.to_csv(
        out / "summary.csv",
        index=False,
    )

    print()

    print(
        f"catalog  {out / 'catalog.json'}"
    )

    print(
        f"summary  {out / 'summary.csv'}"
    )

    _report(
        catalog,
        summary,
    )


def _parse_one(
    entry,
    paths,
    parser_name,
    device,
    profile,
    out,
    catalog,
):
    """
    Parse one recording, validate it, run QC, and write Parquet.

    Validation is performed after parsing because several validation checks
    depend on the parsed frame and metadata.
    """

    cfg = profile.get(
        "devices",
        {},
    ).get(
        device,
        {},
    )

    try:

        # -------------------------------------------------------------
        # Parsing
        # -------------------------------------------------------------

        parser = get_parser(
            parser_name
        )

        if parser_name == "bettercare":

            from backbone.parsers.bettercare import (
                parse_group,
            )

            frame, meta = parse_group(
                paths,
                profile,
                cfg,
            )

        else:

            frame, meta = parser(
                paths[0],
                profile,
                cfg,
            )

        # -------------------------------------------------------------
        # Validation
        # -------------------------------------------------------------

        validation = validate.validate_file(
            paths[0],
            device,
            profile,
            frame=frame,
            meta=meta,
            parser_name=parser_name,
        )

        # -------------------------------------------------------------
        # Existing QC
        # -------------------------------------------------------------

        flags = qc.run(
            frame,
            meta,
            profile,
            device,
        )

        # -------------------------------------------------------------
        # Catalog entry
        # -------------------------------------------------------------

        entry.update(
            {
                "parse_status": "parsed",

                "rows": meta.get(
                    "rows"
                ),

                "duration_min": round(
                    (
                        meta.get(
                            "duration_s"
                        )
                        or 0
                    )
                    / 60,
                    1,
                ),

                "interval_s": meta.get(
                    "interval_s"
                ),

                "variables": meta.get(
                    "variables",
                    [],
                ),

                "source_columns": meta.get(
                    "source_columns",
                    {},
                ),

                "location": meta.get(
                    "location"
                ),

                "wall_clock_recovered": meta.get(
                    "wall_clock_recovered"
                ),

                # -------------------------------------------------
                # Validation
                # -------------------------------------------------

                "validation_verdict": (
                    validation.get(
                        "verdict"
                    )
                ),

                "validation_checks": (
                    validation.get(
                        "checks",
                        [],
                    )
                ),

                # -------------------------------------------------
                # QC
                # -------------------------------------------------

                "qc_verdict": qc.verdict(
                    flags
                ),

                "qc_flags": flags,
            }
        )

        # -------------------------------------------------------------
        # Standardized Parquet
        # -------------------------------------------------------------

        if not frame.empty:

            key = (
                f"{entry['patient'] or 'unknown'}"
                f"_{device}_"
                f"{entry['sha256'][:8]}"
            )

            frame.to_parquet(
                out
                / "parquet"
                / f"{key}.parquet"
            )

            entry["parquet"] = (
                f"parquet/{key}.parquet"
            )

    except Exception as exc:  # noqa: BLE001

        entry.update(
            {
                "parse_status": "failed",
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
                "traceback": traceback.format_exc(
                    limit=3
                ),
            }
        )

    catalog.append(entry)


def build_summary(
    catalog,
    profile,
):
    """
    One row per patient: coverage, duration, QC state and validation state.
    This is what the researcher interface reads.
    """

    expected = (
        profile.get(
            "coverage",
            {},
        ).get(
            "expected",
            [],
        )
    )

    required = (
        profile.get(
            "coverage",
            {},
        ).get(
            "primary_analysis_requires",
            [],
        )
    )

    rows: dict[str, dict] = {}

    for e in catalog:

        pid = e.get(
            "patient"
        )

        if not pid:
            continue

        row = rows.setdefault(
            pid,
            {
                "patient": pid,
                "files": 0,
                "bytes": 0,
                "devices": set(),
                "parsed": 0,
                "failed": 0,
                "not_supported": 0,
                "duplicates": 0,
                "review": 0,
                "validation_fail": 0,
                "validation_warning": 0,
                "duration_min": 0.0,
            },
        )

        row["files"] += 1

        row["bytes"] += (
            e.get(
                "size_bytes",
                0,
            )
        )

        status = e.get(
            "parse_status"
        )

        # -------------------------------------------------------------
        # Parsed
        # -------------------------------------------------------------

        if status == "parsed":

            row["parsed"] += 1

            row["devices"].add(
                e["device"]
            )

            row["duration_min"] += (
                e.get(
                    "duration_min"
                )
                or 0
            )

            if e.get(
                "qc_verdict"
            ) == "REVIEW":

                row["review"] += 1

            validation_verdict = e.get(
                "validation_verdict"
            )

            if validation_verdict in (
                "FAIL",
                "FAIL_CORRUPT",
                "FAIL_UNREADABLE",
                "FAIL_INVALID_STRUCTURE",
            ):

                row[
                    "validation_fail"
                ] += 1

            elif validation_verdict in (
                "WARNING",
                "WARNING_EMPTY",
                "WARNING_MISSING_MODALITY",
            ):

                row[
                    "validation_warning"
                ] += 1

        # -------------------------------------------------------------
        # Failed parser
        # -------------------------------------------------------------

        elif status == "failed":

            row["failed"] += 1

        # -------------------------------------------------------------
        # Unsupported formats
        # -------------------------------------------------------------

        elif status == "not_supported":

            row["not_supported"] += 1

            # A kept binary export is itself coverage: a patient with only
            # BIS files has a BIS recording, even though no parser reads
            # .ara/.o_a.

            device = e.get(
                "device"
            )

            if (
                e.get("tier") == "B"
                and device
                and device not in (
                    "photo",
                    "unknown",
                    "other",
                )
            ):

                row["devices"].add(
                    device
                )

        # -------------------------------------------------------------
        # Duplicate
        # -------------------------------------------------------------

        elif status == "duplicate":

            row["duplicates"] += 1

    out = []

    for pid, row in sorted(
        rows.items()
    ):

        devices = row.pop(
            "devices"
        )

        row["devices"] = ",".join(
            sorted(devices)
        )

        # -------------------------------------------------------------
        # Expected device coverage
        # -------------------------------------------------------------

        for dev in expected:

            row[
                f"has_{dev}"
            ] = dev in devices

        # -------------------------------------------------------------
        # Primary analysis readiness
        # -------------------------------------------------------------

        row[
            "complete_for_primary"
        ] = all(
            d in devices
            for d in required
        )

        # -------------------------------------------------------------
        # Human-readable size
        # -------------------------------------------------------------

        row["bytes_mb"] = round(
            row.pop(
                "bytes"
            )
            / 1e6,
            1,
        )

        row["duration_min"] = round(
            row["duration_min"],
            1,
        )

        out.append(
            row
        )

    return pd.DataFrame(
        out
    )


def _report(
    catalog,
    summary,
):
    """Print a compact pipeline report."""

    from collections import Counter

    status = Counter(
        e.get(
            "parse_status"
        )
        for e in catalog
    )

    print(
        "\nFiles by status"
    )

    for key, count in status.most_common():

        print(
            f"  {str(key):<16} {count}"
        )

    if not summary.empty:

        print(
            f"\nPatients: {len(summary)}"
        )

        cov = [
            c
            for c in summary.columns
            if c.startswith("has_")
        ]

        for c in cov:

            print(
                f"  {c:<20} "
                f"{int(summary[c].sum())} "
                f"of {len(summary)}"
            )

        if "complete_for_primary" in summary:

            n = int(
                summary[
                    "complete_for_primary"
                ].sum()
            )

            print(
                f"  {'ready for primary':<20} "
                f"{n} of {len(summary)}"
            )

        if "validation_fail" in summary:

            print(
                f"  {'validation failures':<20} "
                f"{int(summary['validation_fail'].sum())}"
            )

        if "validation_warning" in summary:

            print(
                f"  {'validation warnings':<20} "
                f"{int(summary['validation_warning'].sum())}"
            )

    failed = [
        e
        for e in catalog
        if e.get(
            "parse_status"
        ) == "failed"
    ]

    if failed:

        print(
            f"\n{len(failed)} failures, first three:"
        )

        for e in failed[:3]:

            print(
                f"  {e['file']}\n"
                f"    {e.get('error')}"
            )


if __name__ == "__main__":
    main()