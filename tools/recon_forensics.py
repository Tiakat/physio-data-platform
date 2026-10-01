"""Recon forensics: structural fingerprints of header-unparseable CSVs (read-only).

289 `.csv` files failed header parsing with `content_unparseable` -- content
exists but parses to no columns. This tool downloads only the head of each
failing file and records BYTE-LEVEL structural fingerprints (no cell values,
no paths, no filenames) so the failure classes can be understood and given
dedicated readers without ever discarding the files.

Fingerprint fields per file:
  project, size_bytes, bom, null_ratio, printable_ratio, line_ending,
  n_lines_in_head, first_line_len, delim_counts, decode_ok, fingerprint_class

Classes (first match wins):
  undecodable          -- no tried encoding decodes the head
  binary_embedded      -- null-byte ratio > 5% (binary payload in a .csv)
  single_line          -- head is one giant line (no line breaks in 64KB)
  no_delimiter         -- first line has no ; , tab or | at all
  whitespace_soup      -- first non-blank line is mostly whitespace/punct
  unknown_structure    -- none of the above; needs human eyes on structure
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _write_summary(title: str, rows: list[tuple]) -> None:
    """Append a de-identified markdown table to the GitHub job summary.

    Only counts and fingerprint classes -- no paths, filenames, or values --
    so this is safe to show in the workflow UI. No-op outside GitHub Actions.
    """
    dest = os.environ.get("GITHUB_STEP_SUMMARY")
    if not dest:
        return
    lines = [f"## {title}", "",
             "| project | csvs | unparseable | fingerprint classes |",
             "| --- | ---: | ---: | --- |"]
    for code, n_csv, n_bad, classes in rows:
        lines.append(f"| {code} | {n_csv} | {n_bad} | {classes} |")
    lines.append("")
    with open(dest, "a", encoding="utf-8") as fh:
        fh.write("\n".join(lines))

HEAD_BYTES = 65536
ENCODINGS = ("utf-8-sig", "utf-8", "cp1252", "latin-1")


def _sha8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def head_bytes(dbx, dropbox_path: str, n: int = HEAD_BYTES) -> bytes:
    _, resp = dbx.files_download(dropbox_path)
    try:
        buf = bytearray()
        for chunk in resp.iter_content(16384):
            buf += chunk
            if len(buf) >= n:
                break
        return bytes(buf[:n])
    finally:
        resp.close()


def fingerprint(raw: bytes) -> dict:
    n = len(raw)
    fp: dict = {"n_head_bytes": n}
    if n == 0:
        fp["fingerprint_class"] = "empty_head"
        return fp
    # BOM
    bom = None
    for sig, name in ((b"\xef\xbb\xbf", "utf-8-sig"),
                      (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be")):
        if raw.startswith(sig):
            bom = name
            break
    fp["bom"] = bom
    nulls = raw.count(b"\x00")
    fp["null_ratio"] = round(nulls / n, 4)
    printable = sum(1 for b in raw
                    if 32 <= b <= 126 or b in (9, 10, 13))
    fp["printable_ratio"] = round(printable / n, 4)
    # line endings
    crlf = raw.count(b"\r\n")
    lf = raw.count(b"\n") - crlf
    cr = raw.count(b"\r") - crlf
    if crlf == 0 and lf == 0 and cr == 0:
        fp["line_ending"] = "none"
    else:
        fp["line_ending"] = ("crlf" if crlf >= lf and crlf >= cr
                             else "lf" if lf >= cr else "cr")
    lines = raw.split(b"\n")
    fp["n_lines_in_head"] = len(lines)
    first = lines[0].strip(b"\r")
    fp["first_line_len"] = len(first)
    sample = raw[:2048]
    fp["delim_counts"] = {
        "semicolon": sample.count(b";"),
        "comma": sample.count(b","),
        "tab": sample.count(b"\t"),
        "pipe": sample.count(b"|"),
    }
    # decode: full chain mirrors the census (latin-1 never fails);
    # strict chain (no latin-1) tells us if the text is really decodable.
    decode_ok = None
    for enc in ENCODINGS:
        try:
            raw.decode(enc)
            decode_ok = enc
            break
        except Exception:
            continue
    fp["decode_ok"] = decode_ok
    strict_ok = None
    for enc in ("utf-8-sig", "utf-8", "cp1252"):
        try:
            raw.decode(enc)
            strict_ok = enc
            break
        except Exception:
            continue
    fp["strict_decode_ok"] = strict_ok
    # class
    if strict_ok is None:
        cls = "undecodable"
    elif fp["null_ratio"] > 0.05:
        cls = "binary_embedded"
    elif fp["line_ending"] == "none":
        cls = "single_line"
    elif all(v == 0 for v in fp["delim_counts"].values()):
        cls = "no_delimiter"
    else:
        try:
            txt = raw.decode(decode_ok)
            fl = txt.splitlines()[0] if txt.splitlines() else ""
            stripped = fl.strip()
            punct = sum(1 for c in stripped if not c.isalnum())
            if stripped and punct / max(len(stripped), 1) > 0.8:
                cls = "whitespace_soup"
            else:
                cls = "unknown_structure"
        except Exception:
            cls = "unknown_structure"
    fp["fingerprint_class"] = cls
    return fp


def header_parseable(raw: bytes) -> bool:
    """Mirror of recon_schemas.extract_columns success/failure (cheap)."""
    import csv
    text = None
    for enc in ENCODINGS:
        try:
            text = raw.decode(enc)
            break
        except Exception:
            continue
    if text is None:
        return False
    lines = text.splitlines()
    for line in lines[:4]:
        if not line.strip():
            continue
        for delim in (";", ",", "\t", "|"):
            try:
                row = next(csv.reader(io.StringIO(line), delimiter=delim))
            except Exception:
                continue
            if len([c.strip() for c in row]) >= 2:
                return True
        return False
    return False


def main() -> int:
    from tools import sync_dropbox_cloud
    from tools import azure_auth
    from tools.daily_pipeline import ACCOUNT, REPORTS
    from tools.legacy_pipeline import list_dropbox_tree, select_projects

    dbx = sync_dropbox_cloud.get_dropbox_client()
    projects, _ = select_projects(dbx)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M")

    fps = []
    summary_rows = []
    for proj in sorted(projects, key=lambda p: p["code"]):
        code = proj["code"]
        n_csv = 0
        n_bad = 0
        classes: dict[str, int] = {}
        for root in proj["data_roots"]:
            base = (proj["dropbox_base"] if root == "."
                    else f"{proj['dropbox_base']}/{root}")
            try:
                entries = list_dropbox_tree(dbx, base)
            except Exception as exc:  # noqa: BLE001
                print(f"[forensics] {code}: cannot list {base}: {exc}",
                      flush=True)
                continue
            for e in entries:
                if not e["name"].lower().endswith(".csv"):
                    continue
                n_csv += 1
                dbx_path = f"{base}/{e['relpath']}"
                try:
                    raw = head_bytes(dbx, dbx_path)
                except Exception:
                    continue
                if header_parseable(raw):
                    continue
                n_bad += 1
                fp = fingerprint(raw)
                classes[fp["fingerprint_class"]] = \
                    classes.get(fp["fingerprint_class"], 0) + 1
                fps.append({
                    "project": code,
                    "file_sha8": _sha8(dbx_path),
                    "size_bytes": e["size"],
                    **fp,
                })
        print(f"[forensics] {code}: {n_csv} csvs, {n_bad} unparseable, "
              f"classes={classes}", flush=True)
        summary_rows.append((code, n_csv, n_bad, classes))

    _write_summary("Forensics recon (de-identified)", summary_rows)

    svc = azure_auth.get_blob_service_client(ACCOUNT)
    blob = f"recon/forensics_{ts}.json"
    svc.get_blob_client(container=REPORTS,
                        blob=blob).upload_blob(
        json.dumps(fps, indent=1).encode("utf-8"), overwrite=True)
    print(f"[forensics] wrote reports/{blob} ({len(fps)} files)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
