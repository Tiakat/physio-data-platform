from __future__ import annotations

import sys
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
PARQUET_DIR = PROJECT_ROOT / "local_store" / PROJECT / "parquet"


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

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


def find_full_sha256(prefix: str) -> str:
    """Find the unique catalog SHA-256 matching a Parquet filename prefix."""
    catalog_path = PROJECT_ROOT / "local_store" / PROJECT / "catalog.json"

    if not catalog_path.exists():
        raise FileNotFoundError(f"Catalog not found: {catalog_path}")

    import json

    with catalog_path.open("r", encoding="utf-8") as f:
        catalog = json.load(f)

    matches = [
        entry["sha256"]
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
            f"Multiple catalog entries match SHA prefix {prefix}: {matches}"
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

    # Ensure UTC.
    timestamps = df.index.tz_convert("UTC")

    rows = []

    for variable in df.columns:
        values = pd.to_numeric(df[variable], errors="coerce")

        finite = np.isfinite(values.to_numpy(dtype=float, na_value=np.nan))

        for ts, value, is_finite in zip(timestamps, values, finite):
            if is_finite:
                rows.append(
                    {
                        "recording_id": recording_id,
                        "ts": ts.to_pydatetime(),
                        "variable": str(variable),
                        "value": float(value),
                        "valid": True,
                    }
                )
            else:
                rows.append(
                    {
                        "recording_id": recording_id,
                        "ts": ts.to_pydatetime(),
                        "variable": str(variable),
                        "value": None,
                        "valid": False,
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
# Main ingestion
# ---------------------------------------------------------------------

def ingest_file(conn, parquet_path: Path) -> tuple[int, int]:
    """Ingest one Parquet file and return (rows_prepared, rows_inserted)."""

    print(f"\nFILE: {parquet_path.name}")

    sha_prefix = get_sha256_from_filename(parquet_path)
    sha256 = find_full_sha256(sha_prefix)

    recording_id = get_recording_id(conn, sha256)

    print(f"  SHA-256:      {sha256}")
    print(f"  recording_id: {recording_id}")

    df = pd.read_parquet(parquet_path)

    if df.empty:
        print("  WARNING: empty Parquet file")
        return 0, 0

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

    inserted = after - before

    print(f"  inserted:     {inserted}")

    return len(signal_rows), inserted


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

    print("=" * 70)
    print("SIGNAL INGESTION")
    print("=" * 70)
    print(f"Project: {PROJECT}")
    print(f"Files:   {len(parquet_files)}")

    total_prepared = 0
    total_inserted = 0

    with engine.begin() as conn:
        for parquet_path in parquet_files:
            prepared, inserted = ingest_file(conn, parquet_path)
            total_prepared += prepared
            total_inserted += inserted

    print("\n" + "=" * 70)
    print("INGESTION COMPLETE")
    print("=" * 70)
    print(f"Prepared rows: {total_prepared}")
    print(f"Inserted rows: {total_inserted}")


if __name__ == "__main__":
    main()