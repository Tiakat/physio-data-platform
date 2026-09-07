from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from db.database import engine

PIPELINE_VERSION = "0.1.0"


def load_catalog(path: Path) -> list[dict]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if not isinstance(data, list):
        raise ValueError("Catalog must contain a JSON list.")

    return data


def get_or_create_project(
    conn,
    project_code: str,
    project_name: str,
    profile_file: str,
) -> int:
    row = conn.execute(
        text("""
            SELECT project_id
            FROM core.projects
            WHERE code = :code
        """),
        {"code": project_code},
    ).fetchone()

    if row:
        return row[0]

    row = conn.execute(
        text("""
            INSERT INTO core.projects (
                code,
                name,
                profile_file,
                retention_days
            )
            VALUES (
                :code,
                :name,
                :profile_file,
                3650
            )
            RETURNING project_id
        """),
        {
            "code": project_code,
            "name": project_name,
            "profile_file": profile_file,
        },
    ).fetchone()

    return row[0]


def get_or_create_participant(
    conn,
    project_id: int,
    patient_code: str,
    source_label: str | None,
) -> int:
    row = conn.execute(
        text("""
            SELECT participant_id
            FROM core.participants
            WHERE project_id = :project_id
              AND code = :code
        """),
        {
            "project_id": project_id,
            "code": patient_code,
        },
    ).fetchone()

    if row:
        return row[0]

    row = conn.execute(
        text("""
            INSERT INTO core.participants (
                project_id,
                code,
                source_label
            )
            VALUES (
                :project_id,
                :code,
                :source_label
            )
            RETURNING participant_id
        """),
        {
            "project_id": project_id,
            "code": patient_code,
            "source_label": source_label,
        },
    ).fetchone()

    return row[0]


def get_or_create_session(conn, participant_id: int) -> int:
    row = conn.execute(
        text("""
            SELECT session_id
            FROM core.sessions
            WHERE participant_id = :participant_id
              AND number = 1
        """),
        {"participant_id": participant_id},
    ).fetchone()

    if row:
        return row[0]

    row = conn.execute(
        text("""
            INSERT INTO core.sessions (
                participant_id,
                number
            )
            VALUES (
                :participant_id,
                1
            )
            RETURNING session_id
        """),
        {"participant_id": participant_id},
    ).fetchone()

    return row[0]


def get_or_create_device(conn, device_code: str) -> int:
    row = conn.execute(
        text("""
            SELECT device_id
            FROM core.devices
            WHERE code = :code
        """),
        {"code": device_code},
    ).fetchone()

    if row:
        return row[0]

    row = conn.execute(
        text("""
            INSERT INTO core.devices (code)
            VALUES (:code)
            RETURNING device_id
        """),
        {"code": device_code},
    ).fetchone()

    return row[0]


def register_recording(
    conn,
    session_id: int,
    device_id: int,
    entry: dict,
) -> str:

    # Duplicate catalog entries are provenance records, not
    # independent database recordings. They must never overwrite
    # the canonical recording's parse status.
    if entry.get("parse_status") == "duplicate":
        return "duplicate_skipped"

    existing = conn.execute(
        text("""
            SELECT recording_id
            FROM core.recordings
            WHERE sha256 = :sha256
        """),
        {"sha256": entry["sha256"]},
    ).fetchone()

    if existing:
        conn.execute(
            text("""
                UPDATE core.recordings
                SET
                    session_id = :session_id,
                    device_id = :device_id,
                    file_name = :file_name,
                    source_path = :source_path,
                    size_bytes = :size_bytes,
                    tier = :tier,
                    parse_status = :parse_status,
                    pipeline_version = :pipeline_version
                WHERE recording_id = :recording_id
            """),
            {
                "recording_id": existing[0],
                "session_id": session_id,
                "device_id": device_id,
                "file_name": entry["name"],
                "source_path": entry["file"],
                "size_bytes": entry.get("size_bytes"),
                "tier": entry["tier"],
                "parse_status": entry.get("parse_status", "pending"),
                "pipeline_version": entry.get(
                    "pipeline_version",
                    PIPELINE_VERSION,
                ),
            },
        )

        return "updated"

    conn.execute(
        text("""
            INSERT INTO core.recordings (
                session_id,
                device_id,
                file_name,
                source_path,
                sha256,
                size_bytes,
                tier,
                parse_status,
                status,
                pipeline_version
            )
            VALUES (
                :session_id,
                :device_id,
                :file_name,
                :source_path,
                :sha256,
                :size_bytes,
                :tier,
                :parse_status,
                :status,
                :pipeline_version
            )
        """),
        {
            "session_id": session_id,
            "device_id": device_id,
            "file_name": entry["name"],
            "source_path": entry["file"],
            "sha256": entry["sha256"],
            "size_bytes": entry.get("size_bytes"),
            "tier": entry["tier"],
            "parse_status": entry.get("parse_status", "pending"),
            "status": "RECEIVED",
            "pipeline_version": entry.get(
                "pipeline_version",
                PIPELINE_VERSION,
            ),
        },
    )

    return "inserted"


def register_catalog(
    catalog_path: Path,
    project_code: str,
    project_name: str,
    profile_file: str,
) -> None:

    catalog = load_catalog(catalog_path)

    inserted = 0
    updated = 0
    duplicate_skipped = 0
    skipped = 0

    with engine.begin() as conn:

        project_id = get_or_create_project(
            conn,
            project_code,
            project_name,
            profile_file,
        )

        for entry in catalog:

            patient = entry.get("patient")
            device = entry.get("device")

            if not patient:
                print(f"SKIP: no patient: {entry.get('file')}")
                skipped += 1
                continue

            if not device:
                print(f"SKIP: no device: {entry.get('file')}")
                skipped += 1
                continue

            participant_id = get_or_create_participant(
                conn,
                project_id,
                patient,
                entry.get("source_label"),
            )

            session_id = get_or_create_session(
                conn,
                participant_id,
            )

            device_id = get_or_create_device(
                conn,
                device,
            )

            result = register_recording(
                conn,
                session_id,
                device_id,
                entry,
            )

            if result == "inserted":
                inserted += 1
                print(
                    f"INSERTED: "
                    f"{project_code} / {patient} / {device} / "
                    f"{entry['name']}"
                )

            elif result == "updated":
                updated += 1
                print(
                    f"UPDATED: "
                    f"{project_code} / {patient} / {device} / "
                    f"{entry['name']} / "
                    f"parse_status={entry.get('parse_status', 'pending')}"
                )

            elif result == "duplicate_skipped":
                duplicate_skipped += 1

    print()
    print("Registration complete.")
    print(f"Catalog entries : {len(catalog)}")
    print(f"Inserted        : {inserted}")
    print(f"Updated         : {updated}")
    print(f"Duplicates     : {duplicate_skipped}")
    print(f"Skipped         : {skipped}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Register and synchronize a local catalog in PostgreSQL."
    )

    parser.add_argument(
        "--project",
        default="V-RAPS",
    )

    parser.add_argument(
        "--catalog",
        default="local_store/V-RAPS/catalog.json",
    )

    parser.add_argument(
        "--name",
        default="Video assisted regional anaesthesia pain study",
    )

    parser.add_argument(
        "--profile",
        default="profiles/v-raps.yaml",
    )

    args = parser.parse_args()

    register_catalog(
        Path(args.catalog),
        args.project,
        args.name,
        args.profile,
    )


if __name__ == "__main__":
    main()