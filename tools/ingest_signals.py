from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from sqlalchemy import text

# Allow imports when executed as:
# python -m tools.ingest_signals
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from db.database import engine


PROJECT = "V-RAPS"
PIPELINE_VERSION = "0.1.0"
STAGE = "signal_ingestion"


def load_catalog(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        entries = data.get("entries")
        if isinstance(entries, list):
            return entries

    raise ValueError(f"Unsupported catalog format: {path}")


def find_recording_id(conn, sha256: str) -> int | None:
    row = conn.execute(
        text(
            """
            SELECT recording_id
            FROM core.recordings
            WHERE sha256 = :sha256
            LIMIT 1
            """
        ),
        {"sha256": sha256},
    ).fetchone()

    return int(row[0]) if row else None


def insert_validation_checks(
    conn,
    recording_id: int,
    validation_checks: list[dict[str, Any]],
) -> int:
    """
    Insert validation checks idempotently.

    A validation check is considered the same when:
      recording_id + check_name + result + detail
    are identical.

    Numerical measured/threshold values are intentionally not stored here
    because core.validation does not contain those columns.
    """

    inserted = 0

    for check in validation_checks:
        check_name = check.get("check")
        result = check.get("result")
        detail = check.get("detail")

        if not check_name or result is None:
            continue

        exists = conn.execute(
            text(
                """
                SELECT 1
                FROM core.validation
                WHERE recording_id = :recording_id
                  AND check_name = :check_name
                  AND result = :result
                  AND (
                        (
                            detail IS NULL
                            AND CAST(:detail AS text) IS NULL
                        )
                        OR detail = CAST(:detail AS text)
                      )
                LIMIT 1
                """
            ),
            {
                "recording_id": recording_id,
                "check_name": check_name,
                "result": result,
                "detail": detail,
            },
        ).fetchone()

        if exists:
            continue

        conn.execute(
            text(
                """
                INSERT INTO core.validation
                    (recording_id, check_name, result, detail)
                VALUES
                    (:recording_id, :check_name, :result, :detail)
                """
            ),
            {
                "recording_id": recording_id,
                "check_name": check_name,
                "result": result,
                "detail": detail,
            },
        )

        inserted += 1

    return inserted


def insert_signals(
    conn,
    recording_id: int,
    parquet_path: Path,
    recognised_columns: list[str] | None = None,
) -> tuple[int, int]:
    import io
    import pandas as pd

    if recognised_columns:
        frame = pd.read_parquet(
            parquet_path,
            columns=recognised_columns,
        )
    else:
        frame = pd.read_parquet(parquet_path)

    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError(
            f"Parquet index must be DatetimeIndex: {parquet_path}"
        )

    if frame.index.tz is None:
        raise ValueError(
            f"Parquet timestamps must be timezone-aware: {parquet_path}"
        )

    frame = frame.copy()
    frame.index = frame.index.tz_convert("UTC")

    prepared = int(frame.shape[0] * frame.shape[1])

    conn.execute(
        text(
            """
            CREATE TEMP TABLE signal_stage (
                recording_id bigint NOT NULL,
                ts timestamptz NOT NULL,
                variable text NOT NULL,
                value double precision,
                valid boolean NOT NULL
            ) ON COMMIT DROP
            """
        )
    )

    raw_connection = conn.connection.driver_connection

    chunk_size = 100_000

    with raw_connection.cursor().copy(
        """
        COPY signal_stage
            (recording_id, ts, variable, value, valid)
        FROM STDIN
        WITH (FORMAT CSV, NULL '\\N')
        """
    ) as copy:

        for start_row in range(0, len(frame), chunk_size):
            chunk = frame.iloc[start_row:start_row + chunk_size]

            long = (
                chunk
                .reset_index(names="ts")
                .melt(
                    id_vars="ts",
                    var_name="variable",
                    value_name="value",
                )
            )

            long["value"] = pd.to_numeric(
                long["value"],
                errors="coerce",
            )

            long["valid"] = long["value"].notna()

            long.insert(0, "recording_id", recording_id)

            buffer = io.StringIO()

            long.to_csv(
                buffer,
                index=False,
                header=False,
                na_rep="\\N",
            )

            copy.write(buffer.getvalue().encode("utf-8"))

    result = conn.execute(
        text(
            """
            INSERT INTO core.signals
                (recording_id, ts, variable, value, valid)
            SELECT
                recording_id,
                ts,
                variable,
                value,
                valid
            FROM signal_stage
            ON CONFLICT (recording_id, variable, ts)
            DO NOTHING
            """
        )
    )

    inserted = result.rowcount

    return prepared, inserted

def insert_qc_flags(
    conn,
    recording_id: int,
    qc_flags: list[dict[str, Any]],
) -> int:
    inserted = 0

    for flag in qc_flags:
        flag_name = flag.get("flag")
        severity = flag.get("severity")
        measured = flag.get("measured")
        threshold = flag.get("threshold")
        variable = flag.get("variable")
        detail = flag.get("detail")

        if not flag_name or not severity:
            continue

        exists = conn.execute(
            text(
                """
                SELECT 1
                FROM core.qc_results
                WHERE recording_id = :recording_id
                  AND flag = :flag
                  AND severity = :severity

                  AND variable IS NOT DISTINCT FROM
                      CAST(:variable AS text)

                  AND (
                        measured IS NOT DISTINCT FROM
                            CAST(:measured AS numeric)
                      )

                  AND (
                        threshold IS NOT DISTINCT FROM
                            CAST(:threshold AS numeric)
                      )

                LIMIT 1
                """
            ),
            {
                "recording_id": recording_id,
                "flag": flag_name,
                "severity": severity,
                "variable": variable,
                "measured": measured,
                "threshold": threshold,
            },
        ).fetchone()

        if exists:
            continue

        conn.execute(
            text(
                """
                INSERT INTO core.qc_results
                    (
                        recording_id,
                        flag,
                        severity,
                        measured,
                        threshold,
                        variable
                    )
                VALUES
                    (
                        :recording_id,
                        :flag,
                        :severity,
                        :measured,
                        :threshold,
                        :variable
                    )
                """
            ),
            {
                "recording_id": recording_id,
                "flag": flag_name,
                "severity": severity,
                "measured": measured,
                "threshold": threshold,
                "variable": variable,
            },
        )

        inserted += 1

    return inserted


def update_recording_status(
    conn,
    recording_id: int,
    status: str,
) -> None:
    conn.execute(
        text(
            """
            UPDATE core.recordings
            SET status = :status
            WHERE recording_id = :recording_id
            """
        ),
        {
            "recording_id": recording_id,
            "status": status,
        },
    )

def create_processing_run(conn, recording_id: int) -> int:
    row = conn.execute(
        text(
            """
            INSERT INTO core.processing_runs
                (
                    recording_id,
                    stage,
                    pipeline_version,
                    parameters,
                    started_at,
                    status,
                    message
                )
            VALUES
                (
                    :recording_id,
                    :stage,
                    :pipeline_version,
                    CAST(:parameters AS jsonb),
                    now(),
                    'running',
                    'Signal ingestion started'
                )
            RETURNING run_id
            """
        ),
        {
            "recording_id": recording_id,
            "stage": STAGE,
            "pipeline_version": PIPELINE_VERSION,
            "parameters": json.dumps(
                {
                    "project": PROJECT,
                }
            ),
        },
    ).fetchone()

    return int(row[0])


def finish_processing_run(
    conn,
    run_id: int,
    status: str,
    message: str,
) -> None:
    conn.execute(
        text(
            """
            UPDATE core.processing_runs
            SET
                finished_at = now(),
                status = :status,
                message = :message
            WHERE run_id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "status": status,
            "message": message,
        },
    )


def process_entry(conn, entry: dict[str, Any]) -> tuple[int, int]:
    sha256 = entry.get("sha256")
    parquet_rel = entry.get("parquet")

    if not sha256:
        raise ValueError("Catalog entry has no sha256")

    if not parquet_rel:
        raise ValueError("Catalog entry has no parquet path")

    parquet_path = ROOT / "local_store" / PROJECT / parquet_rel

    if not parquet_path.exists():
        raise FileNotFoundError(
            f"Parquet file not found: {parquet_path}"
        )

    recording_id = find_recording_id(conn, sha256)

    if recording_id is None:
        raise ValueError(
            f"No database recording found for SHA-256 {sha256}"
        )

    validation_checks = entry.get("validation_checks", [])
    qc_flags = entry.get("qc_flags", [])

    run_id = create_processing_run(conn, recording_id)

    # The processing run must survive even if ingestion fails.
    # Commit the running record before doing the actual work.
    conn.commit()

    try:
        with conn.begin():
            prepared_signals, inserted_signals = insert_signals(
                conn,
                recording_id,
                parquet_path,
                entry.get("recognised_columns", []),
            )

            inserted_validation = insert_validation_checks(
                conn,
                recording_id,
                validation_checks,
            )

            inserted_qc = insert_qc_flags(
                conn,
                recording_id,
                qc_flags,
            )

        with conn.begin():
            update_recording_status(
                conn,
                recording_id,
                "QC_PENDING",
            )
            finish_processing_run(
                conn,
                run_id,
                "success",
                (
                    f"signals prepared={prepared_signals}, "
                    f"signals inserted={inserted_signals}, "
                    f"validation inserted={inserted_validation}, "
                    f"qc inserted={inserted_qc}"
                ),
            )

        return inserted_validation, inserted_qc

    except Exception as exc:
        with conn.begin():
            update_recording_status(
                conn,
                recording_id,
                "FAILED",
            )
            finish_processing_run(
                conn,
                run_id,
                "failed",
                str(exc)[:2000],
            )

        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingest parsed V-RAPS signals into PostgreSQL.")
    parser.add_argument("--recording-id", type=int, help="Process only the specified database recording ID.")
    args = parser.parse_args()

    catalog_path = ROOT / "local_store" / PROJECT / "catalog.json"

    entries = load_catalog(catalog_path)

    total_validation = 0
    total_qc = 0
    processed = 0
    failed = 0

    parsed_count = 0

    with engine.connect() as conn:
        for entry in entries:
            if entry.get("parse_status") != "parsed":
                continue

            if not entry.get("parquet"):
                continue


            if args.recording_id is not None:
                sha256 = entry.get("sha256")
                if not sha256:
                    continue

                db_recording_id = find_recording_id(conn, sha256)

                if db_recording_id != args.recording_id:
                    continue

            patient = entry.get("patient", "?")
            device = entry.get("device", "?")

            try:
                validation_count, qc_count = process_entry(
                    conn,
                    entry,
                )

                total_validation += validation_count
                total_qc += qc_count
                processed += 1

                print(
                    f"{patient} {device}: "
                    f"validation inserted {validation_count}, "
                    f"qc inserted {qc_count}"
                )

            except Exception as exc:
                failed += 1

                print(
                    f"{patient} {device}: ERROR: {exc}"
                )

    print()
    print("Summary")
    print(f"  processed:             {processed}")
    print(f"  failed:                {failed}")
    print(f"  validation inserted:   {total_validation}")
    print(f"  qc inserted:           {total_qc}")


if __name__ == "__main__":
    main()
