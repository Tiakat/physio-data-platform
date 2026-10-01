"""Value-pattern identifier scan (Phase 9+ gate).

Header-level review cannot prove values are de-identified. This tool scans
DECRYPTED standardized parquets for value patterns that look like direct
identifiers: names in free text, email/phone, long digit strings (record
numbers), dates in unexpected places.

Privacy-safe by design: the report contains column names, pattern names,
hit counts and row counts -- NEVER actual values, filenames, or paths.
Files are referenced by SHA-8 content token only.

A hit does not prove identification; it queues the column for human review
before any feed activation. Zero hits does not prove de-identification
either -- it is one gate among several.

Usage:
  python tools/scan_identifiers.py --input <parquet-or-dir> --report <out.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import pandas as pd

PATTERNS = {
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"\+?\d[\d .\-()]{7,}\d"),
    "long_digit_string": re.compile(r"\d{6,}"),
    "date_like": re.compile(
        r"\b(19|20)\d{2}[-/.](0[1-9]|1[0-2])[-/.](0[1-9]|[12]\d|3[01])\b"),
    # Two+ capitalized words in a row: possible person name in free text.
    "name_like": re.compile(r"\b[A-ZÀ-Þ][a-zà-þ]+(?: [A-ZÀ-Þ][a-zà-þ]+){1,3}\b"),
}

# Columns where date_like / long_digit_string hits are expected and benign
# (timestamps, monitor counters) -- still reported, but flagged expected.
EXPECTED_CONTEXT = {
    "date_like": {"timestamp"},
    "long_digit_string": {"timestamp"},
}


def _file_token(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def scan_series(col: str, s: pd.Series) -> list[dict]:
    """Scan one column; return finding dicts (no values)."""
    findings = []
    n = len(s)
    if s.dtype == object or str(s.dtype).startswith("string"):
        vals = s.dropna().astype(str)
        if len(vals) == 0:
            return findings
        n_unique = int(vals.nunique())
        avg_len = float(vals.str.len().mean())
        max_len = int(vals.str.len().max())
        # Free-text heuristic: high cardinality + long strings.
        free_text = n_unique > max(10, 0.5 * len(vals)) and avg_len > 20
        for pname, rx in PATTERNS.items():
            hits = int(vals.str.contains(rx, regex=True).sum())
            if hits:
                findings.append({
                    "column": col,
                    "pattern": pname,
                    "n_hits": hits,
                    "n_rows": n,
                    "expected_in_context": col in EXPECTED_CONTEXT.get(pname, set()),
                    "free_text_column": free_text,
                })
        if free_text and not findings:
            findings.append({
                "column": col,
                "pattern": "free_text_no_known_pattern",
                "n_hits": n_unique,
                "n_rows": n,
                "expected_in_context": False,
                "free_text_column": True,
            })
    return findings


def scan_parquet(path: Path) -> dict:
    df = pd.read_parquet(path)
    findings = []
    for col in df.columns:
        findings.extend(scan_series(str(col), df[col]))
    return {
        "file_token": _file_token(path),
        "n_rows": int(len(df)),
        "n_columns": int(len(df.columns)),
        "findings": findings,
        "n_findings": len(findings),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Scan decrypted parquets for identifier-like values.")
    ap.add_argument("--input", required=True,
                    help="decrypted parquet file or directory")
    ap.add_argument("--report", required=True, help="output JSON path")
    args = ap.parse_args(argv)

    src = Path(args.input)
    files = ([src] if src.is_file()
             else sorted(p for p in src.rglob("*.parquet")))
    results = [scan_parquet(p) for p in files]
    total_hits = sum(r["n_findings"] for r in results)
    report = {
        "tool": "scan_identifiers",
        "files_scanned": len(results),
        "total_findings": total_hits,
        "results": results,
    }
    Path(args.report).write_text(json.dumps(report, indent=2))
    print(f"scanned {len(results)} file(s); {total_hits} finding(s) -> "
          f"{args.report}")
    return 0 if total_hits == 0 else 2  # exit 2 = findings need review


if __name__ == "__main__":
    sys.exit(main())
