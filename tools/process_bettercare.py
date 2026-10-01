"""Process BetterCare exports with the signal dictionary (Phase 9-11).

BetterCare specifics handled here:
- Mixed ~200 Hz waveforms and ~1 Hz parameters in one file: the sampling
  rate is measured per column, so each column gets its own signal's filter.
- Blanks between ~1 Hz parameter samples are STRUCTURAL (the device only
  writes a parameter when it updates), not ordinary missing data: they are
  never interpolated, only flagged.
- Duplicate columns (HR, HR.1, ... HR.11) are preserved, never collapsed:
  each is matched to the HR signal config and processed independently.

Raw is never modified. Outputs: filtered parquet + QC flag parquet.
Columns with no dictionary entry go to the review queue (and from there to
the daily report) instead of being force-fit through another filter.
"""

from __future__ import annotations

import sys

from signal_processing import run_source_script


def main(argv: list[str] | None = None) -> int:
    return run_source_script("bettercare", argv)


if __name__ == "__main__":
    sys.exit(main())
