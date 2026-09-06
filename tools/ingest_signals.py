from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sqlalchemy import text

# ---------------------------------------------------------------------
# Project setup
# ---------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from db.database import engine  # noqa: E402


PROJECT = "V-RAPS"
PIPELINE_VERSION = "0.1.0"
PARQUET_DIR = PROJECT_ROOT / "local_store" / PROJECT / "parquet"
CATALOG_PATH = PROJECT_ROOT / "local_store" / PROJECT / "catalog.json"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def load_catalog() -> list[dict]:
    """Load the ingestion catalog containing provenance and QC results."""
    if not CATALOG_PATH.exists():
        raise FileNotFoundError(f"Catalog not found: {CATALOG_PATH}")

    with CATALOG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def get_recording_id(conn, sha256: str) -> int:
    """Return the database recording_id for a catalog SHA-256."""
    row = conn.execute(
        text(
            """
            SELECT recording_id
            FROM core.recordings
            WHERE sha256 = :sha256
            """
        ),
        {"sha256": sha256},
    ).fetchone()

    if row is None:
        raise RuntimeError(
            f"No database recording found for SHA-256: {sha256}"
        )

    return row[0]


def get_sha256_from_filename(path: Path) -> str:
    """
    Extract the catalog SHA-256 prefix from a Parquet filename.

    Example:
        P-0037_infinity_5f153c84.parquet
        -> 5f153c84
    """
    parts = path.stem.split("_")

    if len(parts) < 3:
        raise ValueError(f"Unexpected Parquet filename: {path.name}")

    return parts[-1]


def find_catalog_entry(catalog: list[dict], prefix: str) -> dict:
    """Find the unique catalog entry matching a Parquet SHA prefix."""
    matches = [
        entry
        for entry in catalog
        if entry.get("sha256", "").startswith(prefix)
        and entry.get("parquet")
    ]

    if len(matches) == 0:
        raise RuntimeError(
            f"No catalog entry found for Parquet SHA prefix: {prefix}"
        )

    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple catalog entries match SHA prefix {prefix}"
        )

    return matches[0]


def prepare_signal_rows(
    df: pd.DataFrame,
    recording_id: int,
) -> pd.DataFrame:
    """Convert a timestamp-indexed wide DataFrame to signal long format."""

    if not isinstance(df.index, pd.DatetimeIndex):
        raise TypeError("Parquet file must contain a DatetimeIndex.")

    if df.index.tz is None:
        raise ValueError("Signal timestamps must be timezone-aware.")

    timestamps = df.index.tz_convert("UTC")

    rows = []

    for variable in df.columns:
        values = pd.to_numeric(df[variable], errors="coerce")

        finite = np.isfinite(
            values.to_numpy(dtype=float, na_value=np.nan)
        )

        for ts, value, is_finite in zip(
            timestamps,
            values,
            finite,
        ):
            rows.append(
                {
                    "recording_id": recording_id,
                    "ts": ts.to_pydatetime(),
                    "variable": str(variable),
                    "value": float(value) if is_finite else None,
                    "valid": bool(is_finite),
                }
            )

    return pd.DataFrame(
        rows,
        columns=[
            "recording_id",
            "ts",
            "variable",
            "value",
            "valid",
        ],
    )


# ---------------------------------------------------------------------
# QC ingestion
# ---------------------------------------------------------------------

def qc_result_exists(
    conn,
    recording_id: int,
    flag: str,
    severity: str,
    variable: str | None,
    measured: float | None,
    threshold: float | None,
) -> bool:
    """Check whether an equivalent QC result is already stored."""

    row = conn.execute(
        text(
            """
            SELECT 1
            FROM core.qc_results
            WHERE recording_id = :recording_id
              AND flag = :flag
              AND severity = :severity
              AND variable IS NOT DISTINCT FROM :variable
              AND (
                    (measured IS NULL AND :measured IS NULL)
                    OR measured = CAST(:measured AS numeric)
                  )
              AND (
                    (threshold IS NULL AND :threshold IS NULL)
                    OR threshold = CAST(:threshold AS numeric)
                  )
            LIMIT 1
            """
        ),
        {
            "recording_id": recording_id,
            "flag": flag,
            "severity": severity,
            "variable": variable,
            "measured": measured,
            "threshold": threshold,
        },
    ).fetchone()

    return row is not None


def ingest_qc_results(
    conn,
    recording_id: int,
    catalog_entry: dict,
) -> tuple[int, int]:
    """
    Insert QC flags from catalog.json into core.qc_results.

    Returns:
        (prepared_qc_rows, inserted_qc_rows)
    """

    flags = catalog_entry.get("qc_flags", [])

    prepared = 0
    inserted = 0

    for flag_data in flags:
        prepared += 1

        flag = str(flag_data.get("flag", "UNKNOWN"))
        severity = str(flag_data.get("severity", "unknown"))

        measured = flag_data.get("measured")
        threshold = flag_data.get("threshold")
        variable = flag_data.get("variable")

        measured = float(measured) if measured is not None else None
        threshold = float(threshold) if threshold is not None else None
        variable = str(variable) if variable is not None else None

        if qc_result_exists(
            conn,
            recording_id,
            flag,
            severity,
            variable,
            measured,
            threshold,
        ):
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
                "flag": flag,
                "severity": severity,
                "measured": measured,
                "threshold": threshold,
                "variable": variable,
            },
        )

        inserted += 1

    return prepared, inserted


# ---------------------------------------------------------------------
# Processing run logging
# ---------------------------------------------------------------------

def create_processing_run(
    conn,
    recording_id: int,
    started_at: datetime,
) -> int:
    """Create a processing_runs record for the ingestion stage."""

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
                    :started_at,
                    :status,
                    :message
                )
            RETURNING run_id
            """
        ),
        {
            "recording_id": recording_id,
            "stage": "signal_ingestion",
            "pipeline_version": PIPELINE_VERSION,
            "parameters": json.dumps(
                {
                    "project": PROJECT,
                    "source": "Parquet",
                    "target": "core.signals",
                }
            ),
            "started_at": started_at,
            "status": "running",
            "message": "Signal ingestion started.",
        },
    ).scalar_one()

    return row


def finish_processing_run(
    conn,
    run_id: int,
    status: str,
    message: str,
) -> None:
    """Mark a processing run as finished."""

    conn.execute(
        text(
            """
            UPDATE core.processing_runs
            SET finished_at = :finished_at,
                status = :status,
                message = :message
            WHERE run_id = :run_id
            """
        ),
        {
            "run_id": run_id,
            "finished_at": datetime.now(timezone.utc),
            "status": status,
            "message": message,
        },
    )


# ---------------------------------------------------------------------
# Signal ingestion
# ---------------------------------------------------------------------

def ingest_file(
    conn,
    parquet_path: Path,
    catalog: list[dict],
) -> tuple[int, int, int, int]:
    """
    Ingest one Parquet file.

    Returns:
        signal_prepared,
        signal_inserted,
        qc_prepared,
        qc_inserted
    """

    print(f"\nFILE: {parquet_path.name}")

    sha_prefix = get_sha256_from_filename(parquet_path)
    catalog_entry = find_catalog_entry(catalog, sha_prefix)

    full_sha256 = catalog_entry["sha256"]
    recording_id = get_recording_id(conn, full_sha256)

    print(f"  SHA-256:      {full_sha256}")
    print(f"  recording_id: {recording_id}")

    df = pd.read_parquet(parquet_path)

    if df.empty:
        print("  WARNING: empty Parquet file")
        return 0, 0, 0, 0

    signal_rows = prepare_signal_rows(df, recording_id)

    print(f"  variables:    {df.shape[1]}")
    print(f"  source rows:  {len(df)}")
    print(f"  signal rows:  {len(signal_rows)}")
    print(f"  time start:   {df.index.min()}")
    print(f"  time end:     {df.index.max()}")

    sql = text(
        """
        INSERT INTO core.signals
            (recording_id, ts, variable, value, valid)
        VALUES
            (:recording_id, :ts, :variable, :value, :valid)
        ON CONFLICT (recording_id, variable, ts)
        DO NOTHING
        """
    )

    records = signal_rows.to_dict("records")

    before = conn.execute(
        text(
            """
            SELECT COUNT(*)
            FROM core.signals
            WHERE recording_id = :recording_id
            """
        ),
        {"recording_id": recording_id},
    ).scalar_one()

    conn.execute(sql, records)

    after = conn.execute(
        text(
            """
            SELECT COUNT(*)
            FROM core.signals
            WHERE recording_id = :recording_id
            """
        ),
        {"recording_id": recording_id},
    ).scalar_one()

    signal_inserted = after - before

    print(f"  signal inserted: {signal_inserted}")

    qc_prepared, qc_inserted = ingest_qc_results(
        conn,
        recording_id,
        catalog_entry,
    )

    print(f"  QC prepared:     {qc_prepared}")
    print(f"  QC inserted:     {qc_inserted}")

    return (
        len(signal_rows),
        signal_inserted,
        qc_prepared,
        qc_inserted,
    )


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main() -> None:
    if not PARQUET_DIR.exists():
        raise FileNotFoundError(
            f"Parquet directory not found: {PARQUET_DIR}"
        )

    parquet_files = sorted(PARQUET_DIR.glob("*.parquet"))

    if not parquet_files:
        raise RuntimeError(
            f"No Parquet files found in {PARQUET_DIR}"
        )

    catalog = load_catalog()

    print("=" * 70)
    print("SIGNAL INGESTION")
    print("=" * 70)
    print(f"Project: {PROJECT}")
    print(f"Files:   {len(parquet_files)}")

    total_signal_prepared = 0
    total_signal_inserted = 0
    total_qc_prepared = 0
    total_qc_inserted = 0

    for parquet_path in parquet_files:
        started_at = datetime.now(timezone.utc)

        sha_prefix = get_sha256_from_filename(parquet_path)

        # Create the processing-run record in its own transaction so that
        # a later ingestion failure cannot erase the failure record.
        with engine.begin() as conn:
            catalog_entry = find_catalog_entry(catalog, sha_prefix)
            recording_id = get_recording_id(
                conn,
                catalog_entry["sha256"],
            )

            run_id = create_processing_run(
                conn,
                recording_id,
                started_at,
            )

        try:
            # Signal and QC ingestion use their own transaction.
            with engine.begin() as conn:
                (
                    signal_prepared,
                    signal_inserted,
                    qc_prepared,
                    qc_inserted,
                ) = ingest_file(
                    conn,
                    parquet_path,
                    catalog,
                )

            success_message = (
                f"Signal rows prepared={signal_prepared}, "
                f"inserted={signal_inserted}; "
                f"QC rows prepared={qc_prepared}, "
                f"inserted={qc_inserted}."
            )

            # Finish the processing run in a separate transaction.
            with engine.begin() as conn:
                finish_processing_run(
                    conn,
                    run_id,
                    "success",
                    success_message,
                )

            total_signal_prepared += signal_prepared
            total_signal_inserted += signal_inserted
            total_qc_prepared += qc_prepared
            total_qc_inserted += qc_inserted

        except Exception as exc:
            error_message = f"{type(exc).__name__}: {exc}"

            # The ingestion transaction has already rolled back.
            # This separate transaction preserves the failed run.
            with engine.begin() as conn:
                finish_processing_run(
                    conn,
                    run_id,
                    "failed",
                    error_message,
                )

            print(f"  ERROR: {error_message}")

    print()
    print("=" * 70)
    print("INGESTION COMPLETE")
    print("=" * 70)
    print(f"Prepared signal rows: {total_signal_prepared}")
    print(f"Inserted signal rows: {total_signal_inserted}")
    print(f"Prepared QC rows:     {total_qc_prepared}")
    print(f"Inserted QC rows:     {total_qc_inserted}")


if __name__ == "__main__":
    main()
