import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from db.database import engine


def section(title):
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def query(conn, sql):
    rows = conn.execute(text(sql)).fetchall()

    for row in rows:
        print(" | ".join(str(value) for value in row))

    if not rows:
        print("(none)")

    return rows


def main():
    with engine.connect() as conn:

        section("INGESTION VERIFICATION")

        row = conn.execute(
            text(
                """
                SELECT
                    (SELECT COUNT(*) FROM core.recordings),
                    (SELECT COUNT(*) FROM core.validation),
                    (SELECT COUNT(*) FROM core.qc_results),
                    (SELECT COUNT(*) FROM core.processing_runs)
                """
            )
        ).one()

        print(f"Recordings      : {row[0]}")
        print(f"Validation rows : {row[1]}")
        print(f"QC findings     : {row[2]}")
        print(f"Processing runs : {row[3]}")

        section("RECORDINGS BY TIER")

        query(
            conn,
            """
            SELECT tier, COUNT(*)
            FROM core.recordings
            GROUP BY tier
            ORDER BY tier
            """
        )

        section("RECORDINGS BY PARSE STATUS")

        query(
            conn,
            """
            SELECT parse_status, COUNT(*)
            FROM core.recordings
            GROUP BY parse_status
            ORDER BY parse_status
            """
        )

        section("RECORDINGS BY PIPELINE STATUS")

        query(
            conn,
            """
            SELECT status, COUNT(*)
            FROM core.recordings
            GROUP BY status
            ORDER BY status
            """
        )

        section("VALIDATION RESULTS")

        query(
            conn,
            """
            SELECT result, COUNT(*)
            FROM core.validation
            GROUP BY result
            ORDER BY result
            """
        )

        section("QC FINDINGS BY TYPE")

        query(
            conn,
            """
            SELECT flag, COUNT(*)
            FROM core.qc_results
            GROUP BY flag
            ORDER BY flag
            """
        )

        section("QC FINDINGS BY SEVERITY")

        query(
            conn,
            """
            SELECT severity, COUNT(*)
            FROM core.qc_results
            GROUP BY severity
            ORDER BY severity
            """
        )

        section("RECORDINGS AFFECTED BY QC")

        query(
            conn,
            """
            SELECT
                COUNT(DISTINCT recording_id),
                COUNT(*)
            FROM core.qc_results
            """
        )

        section("RECORDINGS BY DEVICE")

        query(
            conn,
            """
            SELECT
                COALESCE(d.code, '[NO DEVICE]'),
                COUNT(*)
            FROM core.recordings r
            LEFT JOIN core.devices d
                ON d.device_id = r.device_id
            GROUP BY d.code
            ORDER BY 1
            """
        )

        section("STORAGE AND PROVENANCE COVERAGE")

        query(
            conn,
            """
            SELECT
                COUNT(*),
                COUNT(*) FILTER (
                    WHERE sha256 IS NULL OR sha256 = ''
                ),
                COUNT(*) FILTER (
                    WHERE raw_uri IS NULL OR raw_uri = ''
                ),
                COUNT(*) FILTER (
                    WHERE source_path IS NULL OR source_path = ''
                ),
                COUNT(*) FILTER (
                    WHERE file_name IS NULL OR file_name = ''
                )
            FROM core.recordings
            """
        )

        section("SHA-256 DUPLICATES")

        duplicates = query(
            conn,
            """
            SELECT sha256, COUNT(*)
            FROM core.recordings
            GROUP BY sha256
            HAVING COUNT(*) > 1
            ORDER BY COUNT(*) DESC
            """
        )

        print(f"Duplicate SHA-256 groups: {len(duplicates)}")

        section("REFERENTIAL INTEGRITY")

        print("Recordings with missing session:")
        query(
            conn,
            """
            SELECT COUNT(*)
            FROM core.recordings r
            LEFT JOIN core.sessions s
                ON s.session_id = r.session_id
            WHERE s.session_id IS NULL
            """
        )

        print("Recordings with invalid device:")
        query(
            conn,
            """
            SELECT COUNT(*)
            FROM core.recordings r
            LEFT JOIN core.devices d
                ON d.device_id = r.device_id
            WHERE r.device_id IS NOT NULL
              AND d.device_id IS NULL
            """
        )

        section("VALIDATION FAILURES")

        query(
            conn,
            """
            SELECT
                v.recording_id,
                r.file_name,
                v.check_name,
                v.result,
                v.detail
            FROM core.validation v
            JOIN core.recordings r
                ON r.recording_id = v.recording_id
            WHERE v.result = 'FAIL'
            ORDER BY v.recording_id
            """
        )

        section("ERROR-LEVEL QC FINDINGS")

        query(
            conn,
            """
            SELECT
                q.recording_id,
                r.file_name,
                q.flag,
                q.variable,
                q.measured,
                q.threshold
            FROM core.qc_results q
            JOIN core.recordings r
                ON r.recording_id = q.recording_id
            WHERE q.severity = 'error'
            ORDER BY q.recording_id
            """
        )

        print()
        print("=" * 60)
        print("Verification complete.")
        print("=" * 60)


if __name__ == "__main__":
    main()