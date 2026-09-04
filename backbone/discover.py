"""
Archive discovery.

Run this FIRST, before writing any profile and before ingesting anything. It walks a
folder tree, opens the head of every text file it finds, and reports what is actually
there: file types, delimiters, encodings, column sets, date formats and duplicates.

The output is what you paste into a project profile. It replaces guessing.

Usage
-----
    python -m backbone.discover --root "D:/Dropbox/Projects actifs/IPAMS" --out reports/
    python -m backbone.discover --root "D:/Dropbox/Projects actifs" --out reports/ --by-project

Produces, in the output folder:
    discovery_files.csv      one row per file: path, type, size, sha256, delimiter, ncols
    discovery_columns.csv    one row per distinct column name, with where it was seen
    discovery_summary.md     human readable report, read this one first
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

TEXT_EXT = {".csv", ".txt", ".tsv"}
BINARY_KEEP = {".med", ".enc", ".ara", ".o_a", ".m_a", ".pdf", ".jpeg", ".jpg",
               ".png", ".mat", ".pmd", ".xlsx", ".xls", ".docx"}
IGNORE_NAMES = {".DS_Store", "Thumbs.db", "desktop.ini"}
IGNORE_PREFIX = ("~$", "._")

HEAD_BYTES = 262144          # 256 KB is plenty to see the header and a few rows
SHA_BYTES = 8 * 1024 * 1024  # hash first 8 MB only, for speed during discovery

DATE_PATTERNS = [
    (re.compile(r"^\d{14}$"),                          "YYYYMMDDHHMMSS"),
    (re.compile(r"^\d{14}\.\d+$"),                     "YYYYMMDDHHMMSS.f"),
    (re.compile(r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}"), "ISO datetime"),
    (re.compile(r"^\d{2}-[A-Za-z]{3}-\d{4} \d{2}:"),   "DD-Mon-YYYY HH:MM:SS"),
    (re.compile(r"^\d{2}/\d{2}/\d{4}"),                "DD/MM/YYYY"),
    (re.compile(r"^\d{2}:\d{2}:\d{2}$"),               "HH:MM:SS"),
    (re.compile(r"^\d+$"),                             "integer, maybe elapsed ms"),
]

MISSING_TOKENS = {"", "na", "n/a", "nan", "null", "none", "-1401", "-1500",
                  "9885", "9999", "-999", "-1"}


def sniff_delimiter(head: str) -> str:
    first = head.split("\n", 1)[0]
    counts = {d: first.count(d) for d in [";", ",", "\t", "|"]}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ","


def read_head(path: Path):
    """Return (text, encoding) reading only the first HEAD_BYTES."""
    raw = path.open("rb").read(HEAD_BYTES)
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1 (with errors)"


def partial_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        h.update(fh.read(SHA_BYTES))
    return h.hexdigest()


def guess_date_format(sample: str) -> str:
    s = sample.strip().strip('"')
    for pattern, label in DATE_PATTERNS:
        if pattern.match(s):
            return label
    return "unrecognised"


def analyse_text_file(path: Path) -> dict:
    """Open the head of a delimited file and describe its shape."""
    text, encoding = read_head(path)
    lines = text.split("\n")
    nonblank = [i for i, l in enumerate(lines) if l.strip()]
    if not nonblank:
        return {"error": "file is empty or all blank"}

    delim = sniff_delimiter(text)

    # Detect a second header block, as in the NOL export, by looking for a later
    # line whose fields are all non numeric while earlier data rows were numeric.
    header_rows = []
    for i in nonblank[:12]:
        fields = [f.strip().strip('"') for f in lines[i].split(delim)]
        fields = [f for f in fields if f]
        if not fields:
            continue
        numericish = sum(1 for f in fields
                         if re.fullmatch(r"-?\d+(\.\d+)?", f) or f.lower() in MISSING_TOKENS)
        if numericish <= len(fields) * 0.34:
            header_rows.append(i)

    header_index = header_rows[0] if header_rows else nonblank[0]
    columns = [c.strip().strip('"') for c in lines[header_index].split(delim)]
    columns = [c for c in columns if c != ""]

    # first data row after the last detected header
    data_index = None
    start = (header_rows[-1] if header_rows else header_index) + 1
    for i in range(start, len(lines)):
        if lines[i].strip():
            data_index = i
            break

    first_field = None
    date_fmt = "no data row found"
    time_column = None
    ncols_data = None
    if data_index is not None:
        cells = [c.strip().strip('"') for c in lines[data_index].split(delim)]
        ncols_data = len(cells)
        # Prefer a column whose NAME looks like a time, then fall back to the first
        # cell that PARSES as a date. Infinity puts its timestamp in column 3, not 0.
        name_hint = re.compile(r"(time|date|datetime|horod|abs time)", re.I)
        candidates = [i for i, c in enumerate(columns) if name_hint.search(c)]
        if not candidates:
            candidates = [i for i, c in enumerate(cells)
                          if guess_date_format(c) not in ("unrecognised",
                                                          "integer, maybe elapsed ms")]
        if not candidates and cells:
            candidates = [0]
        if candidates:
            i = candidates[0]
            time_column = columns[i] if i < len(columns) else f"column {i}"
            if i < len(cells):
                first_field = cells[i]
                date_fmt = guess_date_format(cells[i])

    # missing value tokens actually present in the sampled rows
    seen_missing = set()
    for l in lines[start:start + 200]:
        for cell in l.split(delim):
            c = cell.strip().strip('"').lower()
            if c in MISSING_TOKENS:
                seen_missing.add(c if c else "(blank)")

    return {
        "encoding": encoding,
        "delimiter": {";": "semicolon", ",": "comma", "\t": "tab", "|": "pipe"}[delim],
        "header_row_indices": header_rows[:4],
        "stacked_headers": len(header_rows) > 1,
        "n_columns_header": len(columns),
        "n_columns_first_data_row": ncols_data,
        "columns": columns,
        "time_column": time_column,
        "first_time_value": first_field,
        "time_format_guess": date_fmt,
        "missing_tokens_seen": sorted(seen_missing),
    }


def classify(path: Path) -> str:
    name = path.name
    if name in IGNORE_NAMES or name.startswith(IGNORE_PREFIX):
        return "C"
    if path.suffix.lower() in TEXT_EXT:
        return "A"
    return "B"


def walk(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for fn in sorted(filenames):
            yield Path(dirpath) / fn


def discover(root: Path, out: Path, label: str) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    files_rows = []
    column_seen = defaultdict(list)      # column name -> [(file, project)]
    ext_counter = Counter()
    tier_counter = Counter()
    delim_counter = Counter()
    fmt_counter = Counter()
    schema_signatures = defaultdict(list)
    sha_seen = defaultdict(list)
    total_bytes = 0
    errors = []

    for path in walk(root):
        try:
            size = path.stat().st_size
        except OSError as exc:
            errors.append(f"{path}: {exc}")
            continue
        tier = classify(path)
        ext = path.suffix.lower() or "(none)"
        ext_counter[ext] += 1
        tier_counter[tier] += 1
        total_bytes += size

        row = {
            "path": str(path.relative_to(root)),
            "name": path.name,
            "ext": ext,
            "tier": tier,
            "size_bytes": size,
        }

        if tier == "C" or size == 0:
            row["note"] = "ignored" if tier == "C" else "empty file"
            files_rows.append(row)
            continue

        try:
            row["sha256_head"] = partial_sha256(path)
            sha_seen[row["sha256_head"]].append(str(path.relative_to(root)))
        except OSError as exc:
            errors.append(f"{path}: {exc}")

        if tier == "A":
            try:
                info = analyse_text_file(path)
            except Exception as exc:                      # never stop the scan
                errors.append(f"{path}: {exc}")
                info = {"error": str(exc)}
            if "error" in info:
                row["note"] = info["error"]
            else:
                row.update({
                    "encoding": info["encoding"],
                    "delimiter": info["delimiter"],
                    "n_columns": info["n_columns_header"],
                    "stacked_headers": info["stacked_headers"],
                    "time_column": info["time_column"],
                    "time_format": info["time_format_guess"],
                    "first_time_value": info["first_time_value"],
                    "missing_tokens": "|".join(info["missing_tokens_seen"]),
                })
                delim_counter[info["delimiter"]] += 1
                fmt_counter[info["time_format_guess"]] += 1
                sig = (info["delimiter"], tuple(info["columns"]))
                schema_signatures[sig].append(str(path.relative_to(root)))
                for c in info["columns"]:
                    column_seen[c].append(str(path.relative_to(root)))
        files_rows.append(row)

    # ---- write the file inventory
    fieldnames = ["path", "name", "ext", "tier", "size_bytes", "sha256_head", "encoding",
                  "delimiter", "n_columns", "stacked_headers", "time_column",
                  "time_format", "first_time_value", "missing_tokens", "note"]
    with (out / f"discovery_files_{label}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        w.writerows(files_rows)

    # ---- write the column inventory
    with (out / f"discovery_columns_{label}.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["column_name", "times_seen", "example_file"])
        for name, where in sorted(column_seen.items(), key=lambda kv: -len(kv[1])):
            w.writerow([name, len(where), where[0]])

    duplicates = {k: v for k, v in sha_seen.items() if len(v) > 1}

    # ---- human readable summary
    lines = []
    add = lines.append
    add(f"# Discovery report: {label}")
    add("")
    add(f"Root: `{root}`")
    add(f"Scanned: {datetime.now():%Y-%m-%d %H:%M}")
    add("")
    add(f"- Files: {len(files_rows)}")
    add(f"- Total size: {total_bytes/1e9:.2f} GB")
    add(f"- Tier A parsed: {tier_counter['A']}  |  Tier B kept: {tier_counter['B']}"
        f"  |  Tier C ignored: {tier_counter['C']}")
    add("")
    add("## File types")
    add("")
    add("| Extension | Count |")
    add("|---|---|")
    for ext, n in ext_counter.most_common():
        add(f"| `{ext}` | {n} |")
    add("")
    add("## Delimiters seen in text files")
    add("")
    for d, n in delim_counter.most_common():
        add(f"- {d}: {n} files")
    add("")
    add("## Time formats seen in the detected time column")
    add("")
    for f, n in fmt_counter.most_common():
        add(f"- {f}: {n} files")
    add("")
    add(f"## Distinct column layouts: {len(schema_signatures)}")
    add("")
    add("Each layout below needs either an alias entry in the profile, or its own parser.")
    add("")
    for i, (sig, paths) in enumerate(
            sorted(schema_signatures.items(), key=lambda kv: -len(kv[1])), start=1):
        delim, cols = sig
        add(f"### Layout {i}: {len(paths)} files, {delim} delimited, {len(cols)} columns")
        add("")
        add(f"Example: `{paths[0]}`")
        add("")
        preview = list(cols)[:40]
        add("```")
        for c in preview:
            add(c)
        if len(cols) > 40:
            add(f"... and {len(cols)-40} more")
        add("```")
        add("")
    if duplicates:
        add(f"## Duplicate content: {len(duplicates)} groups")
        add("")
        add("Same content under different paths. Checksum ingestion will keep one.")
        add("")
        for h, paths in list(duplicates.items())[:25]:
            add(f"- `{h[:12]}`")
            for p in paths:
                add(f"    - {p}")
        add("")
    if errors:
        add(f"## Errors: {len(errors)}")
        add("")
        for e in errors[:40]:
            add(f"- {e}")
        add("")
    (out / f"discovery_summary_{label}.md").write_text("\n".join(lines), encoding="utf-8")

    return {
        "label": label, "files": len(files_rows), "bytes": total_bytes,
        "layouts": len(schema_signatures), "duplicates": len(duplicates),
        "errors": len(errors), "tiers": dict(tier_counter),
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description="Scan an archive and describe what is in it.")
    ap.add_argument("--root", required=True, help="Folder to scan")
    ap.add_argument("--out", default="reports", help="Where to write the report")
    ap.add_argument("--by-project", action="store_true",
                    help="Treat each immediate subfolder of root as a separate project")
    args = ap.parse_args(argv)

    root = Path(args.root).expanduser()
    out = Path(args.out).expanduser()
    if not root.is_dir():
        sys.exit(f"Not a folder: {root}")

    targets = ([p for p in sorted(root.iterdir()) if p.is_dir()]
               if args.by_project else [root])

    summaries = []
    for t in targets:
        label = re.sub(r"[^A-Za-z0-9_-]+", "_", t.name) or "root"
        print(f"scanning {t} ...", flush=True)
        summaries.append(discover(t, out, label))

    print()
    print(f"{'project':<28}{'files':>8}{'GB':>8}{'layouts':>9}{'dupes':>7}{'errors':>8}")
    for s in summaries:
        print(f"{s['label']:<28}{s['files']:>8}{s['bytes']/1e9:>8.2f}"
              f"{s['layouts']:>9}{s['duplicates']:>7}{s['errors']:>8}")
    print()
    print(f"Reports written to {out.resolve()}")
    print("Read discovery_summary_<project>.md first.")


if __name__ == "__main__":
    main()
