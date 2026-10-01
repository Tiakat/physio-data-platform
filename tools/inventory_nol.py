"""Inventory the NOL (Medasense PMD-200) companion files -- structural only.

The ExcelData.csv export is parsed by backbone/parsers/nol_medasense.py.
The companion files (.pmd, .med, .mat, metadata.json, PMD_LOG.csv, .enc)
have no public documentation (see docs/nol_format_research.md). This tool
records STRUCTURE -- keys, columns, variable names, shapes -- and never
values, so nothing is interpreted before its semantics are verified.

Per-file verdicts:
  metadata.json -> parseable container: top-level keys + JSON types
  PMD_LOG.csv   -> parseable container: header columns + row count
  *.mat         -> parseable container: variable names + shapes + dtypes
                   (v7.3 needs h5py; reported, not crashed on)
  *.pmd/*.med   -> QUARANTINE: size + byte fingerprint only, never parsed
  *.enc         -> ENCRYPTED_NEEDS_OWNER: name + size only

Output: de-identified JSON (SHA-8 file tokens, no values, no paths).

Usage:
  python tools/inventory_nol.py --input <nol-folder-or-file> --report <out.json>
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

QUARANTINE_EXTS = (".pmd", ".med")
ENCRYPTED_EXTS = (".enc",)


def _sha8_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def _json_type(value) -> str:
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if value is None:
        return "null"
    return "unknown"


def inventory_metadata_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        return {"kind": "metadata.json", "parseable": False,
                "error": type(exc).__name__}
    keys = (list(data.keys()) if isinstance(data, dict)
            else [f"[{i}]" for i in range(len(data))])
    types = ({k: _json_type(data[k]) for k in keys}
             if isinstance(data, dict) else {})
    return {"kind": "metadata.json", "parseable": True,
            "top_level_keys": keys, "key_types": types,
            "is_list": not isinstance(data, dict)}


def inventory_pmd_log_csv(path: Path) -> dict:
    import csv
    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            sample = fh.read(65536)
    except OSError as exc:
        return {"kind": "PMD_LOG.csv", "parseable": False,
                "error": str(exc)[:80]}
    lines = [ln for ln in sample.splitlines() if ln.strip()]
    if not lines:
        return {"kind": "PMD_LOG.csv", "parseable": True,
                "columns": [], "n_rows_in_head": 0}
    header = None
    for delim in (";", ",", "\t", "|"):
        try:
            row = next(csv.reader([lines[0]], delimiter=delim))
        except Exception:
            continue
        if len(row) >= 2:
            header = [c.strip() for c in row]
            break
    return {"kind": "PMD_LOG.csv", "parseable": header is not None,
            "columns": header or [],
            "n_rows_in_head": max(len(lines) - 1, 0)}


def inventory_mat(path: Path) -> dict:
    raw = path.read_bytes()
    if raw[:8] == b"MATLAB 7" or b"HDF" in raw[:512]:
        # v7.3 = HDF5; without h5py we report, not crash
        try:
            import h5py  # noqa
            has_h5py = True
        except ImportError:
            has_h5py = False
        if not has_h5py:
            return {"kind": ".mat", "parseable": False,
                    "mat_version": "v7.3 (HDF5)",
                    "note": "needs h5py; quarantined until readable"}
    try:
        from scipy.io import loadmat
        md = loadmat(path, squeeze_me=False, struct_as_record=False)
    except Exception as exc:  # noqa: BLE001
        return {"kind": ".mat", "parseable": False,
                "error": type(exc).__name__}
    variables = {}
    for name, arr in md.items():
        if name.startswith("__"):
            continue
        try:
            shape = list(getattr(arr, "shape", []))
            dtype = str(getattr(arr, "dtype", type(arr).__name__))
        except Exception:
            shape, dtype = [], "unknown"
        variables[name] = {"shape": shape, "dtype": dtype}
    return {"kind": ".mat", "parseable": True, "variables": variables}


def inventory_quarantine(path: Path) -> dict:
    head = path.read_bytes()[:64]
    return {"kind": path.suffix.lower(), "parseable": False,
            "verdict": "QUARANTINE",
            "head_sha8": hashlib.sha256(head).hexdigest()[:8],
            "null_ratio_head": round(head.count(b"\x00") / max(len(head), 1), 4)}


def inventory_encrypted(path: Path) -> dict:
    return {"kind": path.suffix.lower(), "parseable": False,
            "verdict": "ENCRYPTED_NEEDS_OWNER"}


def inventory_file(path: Path) -> dict:
    suffix = path.suffix.lower()
    name = path.name.lower()
    base = {"file_token": _sha8_file(path),
            "size_bytes": path.stat().st_size,
            "suffix": suffix}
    if name == "metadata.json":
        base.update(inventory_metadata_json(path))
    elif name == "pmd_log.csv":
        base.update(inventory_pmd_log_csv(path))
    elif suffix == ".mat":
        base.update(inventory_mat(path))
    elif suffix in QUARANTINE_EXTS:
        base.update(inventory_quarantine(path))
    elif suffix in ENCRYPTED_EXTS:
        base.update(inventory_encrypted(path))
    else:
        base.update({"kind": suffix or "no-extension", "parseable": False,
                     "verdict": "UNKNOWN_NOL_COMPANION"})
    return base


def iter_nol_files(src: Path):
    if src.is_file():
        yield src
        return
    for p in sorted(src.rglob("*")):
        if p.is_file() and not p.name.startswith("."):
            yield p


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Structural inventory of NOL companion files.")
    ap.add_argument("--input", required=True,
                    help="NOL folder or single companion file")
    ap.add_argument("--report", required=True, help="output JSON path")
    args = ap.parse_args(argv)

    src = Path(args.input)
    results = [inventory_file(p) for p in iter_nol_files(src)]
    by_verdict: dict[str, int] = {}
    for r in results:
        v = r.get("verdict") or ("parseable" if r.get("parseable") else "unparseable")
        by_verdict[v] = by_verdict.get(v, 0) + 1
    report = {"tool": "inventory_nol",
              "files_inventoried": len(results),
              "by_verdict": by_verdict,
              "files": results}
    Path(args.report).write_text(json.dumps(report, indent=1))
    print(f"inventoried {len(results)} file(s): {by_verdict} -> {args.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
