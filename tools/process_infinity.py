"""Process Infinity exports with the signal dictionary (Phase 9-11).

Infinity specifics handled here:
- NOT schema-stable: the union of columns across files is processed; a
  column missing from a file is "not recorded", never an error.
- Column names embed units (e.g. ``ART M (mm(hg)^^ISO+)``): matched to the
  dictionary through the alias index, never renamed by guessing.
- Usually ~1 Hz monitor parameters: each column's sampling rate is measured
  from its own timestamps, so parameter-appropriate QC is applied.

Raw is never modified. Outputs: filtered parquet + QC flag parquet.
Columns with no dictionary entry go to the review queue (and from there to
the daily report) instead of being force-fit through another filter.
"""

from __future__ import annotations

import sys

from signal_processing import run_source_script


def main(argv: list[str] | None = None) -> int:
    return run_source_script("infinity", argv)


if __name__ == "__main__":
    sys.exit(main())
