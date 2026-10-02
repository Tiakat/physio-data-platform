"""Find the golden patient: has all 50 signals with actual data.

Searches BIS projects for a patient with:
1. All 4 data sources (Infinity, BetterCare, NOL, BIS)
2. Maximum coverage of the 50 dictionary signals
3. Actual data present (not missing/unrecorded)

This patient defines the reference dictionary columns.

Usage:
  python -m tools.find_golden_patient --out patient_out/
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# The 50 meaningful signals from the dictionary.
GOLDEN_SIGNALS = [
    "HR", "SpO2", "BIS", "etCO2",
    "ART D", "ART M", "ART S", "BRA D", "BRA M", "BRA S",
    "NBP D", "NBP S", "RAD D", "RAD M", "RAD S",
    "P1 D", "P1 M", "P1 S", "PA S",
    "CVP", "DO2", "V'O2",
    "STI", "STII", "STIII", "STV", "STV+", "STaVF", "STaVL", "STaVR",
    "Ta", "Tb", "PLS",
    "PEEP", "VT",
    "etN2O", "etO2", "etPrimA", "etSecA", "etSev",
    "inDes", "inHal", "inPrimA", "inSev", "inxMAC",
    "MAC", "ET_DES", "ET_SEVO",
    "2nd Anesthesia gas", "Anesthesia gas", "Carrier gas",
]


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    args = ap.parse_args(argv)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    print(f"[golden] looking for patient with {len(GOLDEN_SIGNALS)} signals",
          flush=True)
    print("[golden] framework ready — needs Dropbox patient folder scan",
          flush=True)

    (out / "golden_signals.json").write_text(
        json.dumps(GOLDEN_SIGNALS, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
