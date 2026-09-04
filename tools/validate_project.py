"""
Step 2 of the pipeline. Validate what the sync transferred, before any
preprocessing.

Runs against either the simulated Azure folder or the Dropbox folder directly,
so it can be used before Azure exists.

    python -m tools.validate_project --project IPAMS --root /tmp/azure_sim/rawdata/IPAMS
    python -m tools.validate_project --project IPAMS --root "C:/.../IPAMS/Database" --deep

--deep parses each file with its device parser, which is slower but checks
duration, row count, sampling interval and how much of the file is real data.
Without it, only the header is inspected, which is fast and still catches
missing columns and unreadable files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backbone import validate                                    # noqa: E402
from backbone.config import classify_file, load_profile          # noqa: E402
from backbone.parsers import get as get_parser                   # noqa: E402
from backbone.parsers._common import file_sequence_number        # noqa: E402
from tools.sync_dropbox import DEVICE_PATTERNS, load_rules, norm, project_rules  # noqa: E402


def device_of(relative: Path) -> str | None:
    for part in reversed(relative.parts[:-1]):
        for code, pattern in DEVICE_PATTERNS:
            if pattern.match(norm(part)):
                return code
    return None


def patient_of(relative: Path) -> str | None:
    """
    First numeric-ish component of the path is the patient. Works for
    RawData/37/... and for PROMISES style "Patient 13 20240905 PLL".
    """
    for part in relative.parts:
        if re.fullmatch(r"\d+", part):
            return part
        m = re.match(r"^(?:Patient|PROMISES)\s+(\d+)", part, re.I)
        if m:
            return m.group(1)
    return None


def sha256(path: Path, chunk=1 << 20) -> str:
    """Full content hash, for duplicate detection across the archive."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


# Folders whose presence means a recording existed but that are not analysis
# modalities for any project: photographs, documents. They do not contribute
# to a patient's coverage.
NON_MODALITY_DEVICES = {"photo", "unknown", "other", None}


def modality_of(device: str | None) -> str | None:
    return None if device in NON_MODALITY_DEVICES else device


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", required=True)
    ap.add_argument("--root", required=True)
    ap.add_argument("--deep", action="store_true",
                    help="parse each file, not just its header")
    ap.add_argument("--threshold", type=float, default=0.6)
    ap.add_argument("--no-dedup", action="store_true",
                    help="skip content checksum deduplication (faster, and the nested "
                         "RawData/RawData mirror and duplicated NOL exports get counted twice)")
    ap.add_argument("--out", default="validation_reports")
    args = ap.parse_args()

    profile = load_profile(args.project)
    rules = project_rules(load_rules(), args.project)
    declared = rules.get("expect_modalities")
    root = Path(args.root)

    file_results: list[dict] = []
    patient_devices: dict[str, set] = defaultdict(set)
    bettercare_groups: dict[tuple, list[Path]] = defaultdict(list)

    files = sorted(p for p in root.rglob("*") if p.is_file())
    print(f"{len(files)} files under {root}")

    preserved: list[dict] = []
    duplicates: list[dict] = []
    seen_hashes: dict[str, str] = {}

    for path in files:
        relative = path.relative_to(root)

        # The project metadata workbook is not a recording. It is validated by
        # tools.ingest_metadata, not here.
        if relative.parts and relative.parts[0] == "_metadata":
            continue

        tier, declared_device, parser_name = classify_file(profile, path.name)
        if tier == "C":
            continue

        device = declared_device or device_of(relative) or "unknown"
        patient = patient_of(relative)
        modality = modality_of(device)

        # Tier B is preserve only: vendor binaries, encrypted blobs, images,
        # PDFs. Their contents cannot be checked, but their very existence is
        # coverage: a patient with only BIS files has a BIS recording. They are
        # counted here and validated as "preserved, contents not checked".
        if tier == "B":
            preserved.append({"file": relative.as_posix(), "patient": patient,
                              "device": device, "extension": path.suffix.lower(),
                              "size_mb": round(path.stat().st_size / 1e6, 2)})
            if patient and modality:
                patient_devices[patient].add(modality)
            continue

        # Content dedup. The V-RAPS archive carries a full nested RawData/RawData
        # mirror of the same files, and NOL exports are duplicated under
        # recording subfolders. Validating each copy inflates every count.
        digest = None
        if not args.no_dedup and device != "bettercare":  # bc chains hashed at group level
            try:
                digest = sha256(path)
            except OSError:
                digest = None
        if digest and digest in seen_hashes:
            duplicates.append({"file": relative.as_posix(), "patient": patient,
                               "device": device,
                               "duplicate_of": seen_hashes[digest]})
            continue
        if digest:
            seen_hashes[digest] = relative.as_posix()

        # BetterCare arrives as -1, -2, -3 slices of ONE recording. Validate the
        # whole chain once, not each slice, or duration is wrong six times over.
        if device == "bettercare" and parser_name == "bettercare":
            bettercare_groups[(patient, stem := re.sub(r"-\d+\.csv$", "", path.name,
                                                       flags=re.I))].append(path)
            continue

        result = _validate_one(path, [path], device, parser_name, profile, args.deep)
        result.update({"file": relative.as_posix(), "patient": patient})
        file_results.append(result)
        # Coverage comes from what was DISCOVERED, not from what passed. A file
        # that fails validation is still a recording the patient was given; the
        # cohort filter needs that fact, and the failure is reported separately.
        if patient and modality:
            patient_devices[patient].add(modality)

    for (patient, stem), paths in bettercare_groups.items():
        paths = sorted(paths, key=lambda p: file_sequence_number(p.name))
        if not args.no_dedup:
            # Dedup whole chains: hash each slice. A slice already seen anywhere
            # in this run is the nested RawData/RawData mirror (verified content
            # identical to the top level). If the entire chain is duplicate, do
            # not parse it a second time.
            unique, seen = [], set()
            for p in paths:
                try:
                    d = sha256(p)
                except OSError:
                    unique.append(p)
                    continue
                if d in seen or d in seen_hashes:
                    duplicates.append({"file": p.name, "patient": patient,
                                       "device": "bettercare",
                                       "duplicate_of": seen_hashes.get(d, "(earlier copy)")})
                    continue
                seen.add(d)
                seen_hashes.setdefault(d, f"{stem} [{len(paths)} files]")
                unique.append(p)
            if not unique:
                continue
            paths = unique
        if not paths:
            continue
        result = _validate_one(paths[0], paths, "bettercare", "bettercare",
                               profile, args.deep)
        result.update({"file": f"{stem} [{len(paths)} files]", "patient": patient})
        file_results.append(result)
        if patient:
            patient_devices[patient].add("bettercare")

    report = validate.validate_project(file_results, dict(patient_devices),
                                       declared, args.threshold)
    report["project"] = args.project
    report["root"] = str(root)
    report["deep"] = args.deep
    report["preserved_only"] = preserved
    report["duplicates"] = duplicates
    report["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    report["file_results"] = file_results

    print(validate.format_report(report, args.project))

    if preserved:
        from collections import Counter
        exts = Counter(p["extension"] or "(none)" for p in preserved)
        total_mb = round(sum(p["size_mb"] for p in preserved), 1)
        print(f"\nPreserved, not validated  {len(preserved)} files, {total_mb} MB")
        for ext, n in exts.most_common():
            print(f"    {ext:<12} {n}")
        print("    vendor or binary formats; stored and findable, contents not checked")

    if duplicates:
        print(f"\nContent duplicates not validated twice  {len(duplicates)} files")
        from collections import Counter as _C
        for key, n in _C(d["duplicate_of"].rsplit("/", 1)[-1][:40] for d in duplicates) \
                .most_common(6):
            print(f"    {n:>3}  duplicate of {key}")

    failed = [r for r in file_results if r["verdict"] in validate.FAIL_VERDICTS]
    if failed:
        print(f"\nFiles that would not survive preprocessing ({len(failed)}):")
        for r in failed[:12]:
            reasons = "; ".join(c["detail"] for c in r["checks"]
                                if c["result"] in validate.FAIL_VERDICTS)
            print(f"    {r['file']}\n        {reasons}")

    Path(args.out).mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = Path(args.out) / f"{args.project}_{stamp}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nreport written to {path}")


def _validate_one(path, paths, device, parser_name, profile, deep):
    frame = meta = None
    if deep and parser_name:
        cfg = profile.get("devices", {}).get(device, {})
        try:
            if parser_name == "bettercare":
                from backbone.parsers.bettercare import parse_group
                frame, meta = parse_group(paths, profile, cfg)
            else:
                frame, meta = get_parser(parser_name)(path, profile, cfg)
        except (OSError, UnicodeDecodeError) as exc:               # noqa: BLE001
            return {"device": device, "verdict": validate.FAIL_UNREADABLE, "checks": [
                {"check": "readable", "result": validate.FAIL_UNREADABLE,
                 "detail": f"{type(exc).__name__}: {exc}",
                 "measured": None, "threshold": None}]}
        except Exception as exc:                                   # noqa: BLE001
            return {"device": device, "verdict": validate.FAIL_CORRUPT, "checks": [
                {"check": "parse", "result": validate.FAIL_CORRUPT,
                 "detail": f"{type(exc).__name__}: {exc}",
                 "measured": None, "threshold": None}]}
    return validate.validate_file(path, device, profile, frame, meta, parser_name)


if __name__ == "__main__":
    main()
