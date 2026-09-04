"""
Validation, before any preprocessing.

Two levels, and they answer different questions.

FILE level, one validator per device type, shared by every project.
    Is this file readable, does it have a usable timestamp column whatever it
    is called, how long is the recording, how many rows, what is the sampling
    interval, and does it carry the physiological variables this project needs.

PATIENT level, adaptive per project.
    Does this patient have the recordings that patients in THIS project usually
    have. The expected set is not hard coded: it is learned from the project by
    prevalence, and cross checked against the declared expectation in
    ingest/rules.yaml. A patient missing something 90 percent of the project has
    is flagged. A patient missing something only 20 percent have is not.

Validation never deletes and never excludes. It labels.

File outcomes form a taxonomy, not a single pass or fail:

    PASS                         readable, parsed, structurally sound
    WARNING                      readable and useful, one non blocking problem
    WARNING_EMPTY                a structurally valid export with no data rows
    WARNING_MISSING_MODALITY     the project's primary signal is absent, e.g. no
                                 arterial line because the patient never had one
    FAIL_UNREADABLE              the file cannot be opened or decoded
    FAIL_CORRUPT                 the content is not a valid export of this device
    FAIL_INVALID_STRUCTURE       header present but unusable time axis or shape

Only the FAIL_* outcomes mean the file will not survive preprocessing. An empty
export or a patient recorded without their arterial line is real information,
not a broken file, and the report says so.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

import numpy as np
import pandas as pd

from .parsers import sniff as sniff_device

# Structural facts about each device's exports, shared by every project.
#
# Physiological column requirements are deliberately NOT here. Which variables a
# recording must carry is a project decision, declared as required_variables in
# profiles/<project>.yaml, and a missing one is coverage information
# (WARNING_MISSING_MODALITY), never a corrupt file: the Draeger exports in
# V-RAPS carry no ART columns because those patients never had an A line.
#
# min_rows / min_minutes stay as advisory thresholds: a short recording is a
# warning, not corruption. A file that parses to nothing is WARNING_EMPTY.
DEVICE_CONTRACT = {
    "infinity": {
        "time_candidates": ["OBSERVATION_DATETIME", "Time", "Timestamp", "DateTime"],
        "interval_s": 1.0,
        "min_rows": 600,          # 10 minutes at 1 Hz, advisory
        "min_minutes": 10,
    },
    "bettercare": {
        "time_candidates": ["Time (msecs)", "Time", "time_ms", "Time(ms)"],
        "interval_s": 1.0,        # after downsampling from 200 Hz
        "min_rows": 600,          # advisory
        "min_minutes": 10,
    },
    "nol": {
        "time_candidates": ["Abs Time Vector", "Absolute Time", "Time"],
        "interval_s": 5.0,
        "min_rows": 120,          # 10 minutes at 0.2 Hz, advisory
        "min_minutes": 10,
    },
    "bis": {
        "time_candidates": ["Time", "Timestamp"],
        "interval_s": None,
        "min_rows": 0,
        "min_minutes": 0,
        "vendor_binary": True,    # .ara .o_a .m_a cannot be validated as text
    },
    "pump": {
        "time_candidates": ["Time", "Date", "Heure", "DateTime"],
        "interval_s": None,
        "min_rows": 1,
        "min_minutes": 0,
        "event_based": True,
    },
}

PASS = "PASS"
WARNING = "WARNING"
WARNING_EMPTY = "WARNING_EMPTY"
WARNING_MODALITY = "WARNING_MISSING_MODALITY"
FAIL = "FAIL"                          # backwards compatible alias for FAIL_CORRUPT
FAIL_UNREADABLE = "FAIL_UNREADABLE"
FAIL_CORRUPT = "FAIL_CORRUPT"
FAIL_INVALID_STRUCTURE = "FAIL_INVALID_STRUCTURE"

WARNING_VERDICTS = (WARNING, WARNING_EMPTY, WARNING_MODALITY)
FAIL_VERDICTS = (FAIL, FAIL_UNREADABLE, FAIL_CORRUPT, FAIL_INVALID_STRUCTURE)

_SEVERITY = {PASS: 0,
             WARNING: 1, WARNING_EMPTY: 1, WARNING_MODALITY: 1,
             FAIL_UNREADABLE: 2, FAIL_CORRUPT: 2,
             FAIL_INVALID_STRUCTURE: 2, FAIL: 2}


def _check(name, result, detail, measured=None, threshold=None):
    return {"check": name, "result": result, "detail": detail,
            "measured": measured, "threshold": threshold}


# ============================================================ FILE level

def validate_file(path, device: str, profile: dict, frame=None, meta=None,
                  parser_name: str | None = None) -> dict:
    """
    Validate one file. If frame and meta are supplied, the parser has already
    run and the structural checks use its output. If not, the device parser's
    own header sniff is used, which understands each vendor's real layout
    (stacked headers, mixed delimiters, compressed exports).
    """
    contract = DEVICE_CONTRACT.get(device)
    checks: list[dict] = []

    if contract is None:
        return {"device": device, "verdict": WARNING, "checks": [
            _check("device_known", WARNING,
                   f"no validator exists for device {device!r}; "
                   f"file preserved, not validated")]}

    if contract.get("vendor_binary"):
        return {"device": device, "verdict": WARNING, "checks": [
            _check("vendor_binary", WARNING,
                   f"{device} export is a proprietary binary; "
                   f"integrity only, contents cannot be validated")]}

    # ---- header level, always
    if frame is None:
        header_result = _inspect_header(path, device, profile, parser_name)
        checks.extend(header_result)
        return {"device": device, "verdict": _worst(checks), "checks": checks,
                "source_columns": {}}

    # ---- structural, when the parser has run
    present = set(frame.columns)

    required = (profile.get("devices", {}).get(device, {})
                .get("required_variables", []))
    missing_required = [v for v in required if v not in present]
    if missing_required:
        # A patient can legitimately have no A line, no NOL clamp, etc. That is
        # coverage information: the recording is still valid, the cohort filter
        # decides later whether it is usable for a given analysis.
        checks.append(_check("required_variables", WARNING_MODALITY,
                             f"primary signal absent: {', '.join(missing_required)}"))
    else:
        checks.append(_check("required_variables", PASS,
                             f"all present: {', '.join(required) or 'none'}"))

    # ---- timestamp usability. The column name varies; what matters is that a
    # monotonic, parseable time axis exists.
    if frame.empty:
        note = (meta or {}).get("note", "header present, no data rows")
        checks.append(_check("data_rows", WARNING_EMPTY, note))
    else:
        idx = frame.index
        if not isinstance(idx, pd.DatetimeIndex):
            checks.append(_check("timestamps", FAIL_INVALID_STRUCTURE,
                                 "index is not a time axis"))
        else:
            monotonic = idx.is_monotonic_increasing
            checks.append(_check("timestamps", PASS if monotonic else WARNING,
                                 "increasing" if monotonic
                                 else "not strictly increasing after sorting"))

    # ---- advisory duration and row count: short is a warning, not corruption
    minutes = (meta.get("duration_s") or 0) / 60.0
    floor = contract.get("min_minutes")
    if floor:
        checks.append(_check("duration", PASS if minutes >= floor else WARNING,
                             f"{minutes:.1f} min recorded, {floor} min minimum",
                             round(minutes, 1), floor))

    rows = meta.get("rows") or 0
    min_rows = contract.get("min_rows")
    if min_rows:
        checks.append(_check("row_count", PASS if rows >= min_rows else WARNING,
                             f"{rows} rows, {min_rows} minimum",
                             rows, min_rows))

    # ---- sampling interval against what this device does
    expected_interval = contract.get("interval_s")
    observed = meta.get("interval_s")
    if expected_interval and observed:
        drift = abs(observed - expected_interval) / expected_interval
        checks.append(_check("sampling_interval",
                             PASS if drift <= 0.25 else WARNING,
                             f"{observed:.2f} s observed, {expected_interval} s expected",
                             round(observed, 2), expected_interval))

    # ---- how much of the file is actually data
    if not frame.empty:
        filled = float(frame.notna().any(axis=1).mean())
        checks.append(_check("rows_with_data", PASS if filled >= 0.5 else WARNING,
                             f"{filled:.0%} of rows carry at least one value",
                             round(filled, 3), 0.5))

    return {"device": device, "verdict": _worst(checks), "checks": checks,
            "columns_found": sorted(present),
            "source_columns": meta.get("source_columns", {})}


def _inspect_header(path, device, profile, parser_name) -> list[dict]:
    """
    Cheap check: open the header only and let the device parser answer whether
    this file is one of its exports. The parsers own this because every format
    is different: nol has two stacked headers, bettercare changes delimiter per
    file, infinity has compressed exports on top of full ones.
    """
    checks = []
    cfg = profile.get("devices", {}).get(device, {})
    info = {}
    if parser_name:
        try:
            info = sniff_device(parser_name)(path, profile, cfg)
        except Exception as exc:                                   # noqa: BLE001
            info = {"readable": False, "error": f"{type(exc).__name__}: {exc}"}
    if not info:
        info = _generic_header_sniff(path)

    if info.get("readable") is False:
        return [_check("readable", FAIL_UNREADABLE,
                       f"cannot open: {info.get('error') or 'unknown error'}")]
    checks.append(_check("readable", PASS, "opens and has a header"))

    header = info.get("header") or []
    if not header:
        return [_check("structure", FAIL_CORRUPT, "file is empty or has no header row")]
    checks.append(_check("structure", PASS,
                         f"{len(header)} columns, {info.get('delimiter')!r} delimited"))

    time_found = info.get("time_column")
    if time_found:
        checks.append(_check("time_column", PASS, f"found {time_found!r}"))
    else:
        # An unrecognised layout is worth a warning, not a failure: the export
        # format may simply be one nobody has seen yet, and the data must not
        # be labelled corrupt on a guess.
        checks.append(_check("time_column", WARNING,
                             "no recognisable timestamp column in the header"))

    required = (profile.get("devices", {}).get(device, {})
                .get("required_variables", []))
    recognised = set(info.get("recognised") or [])
    missing_required = [v for v in required if v not in recognised]
    if missing_required:
        checks.append(_check("required_variables", WARNING_MODALITY,
                             f"primary signal not in header: {', '.join(missing_required)}"))
    else:
        checks.append(_check("required_variables", PASS,
                             f"all present: {', '.join(required) or 'none'}"))

    if info.get("data_rows") == 0:
        checks.append(_check("data_rows", WARNING_EMPTY, "header present, no data rows"))
    return checks


def _generic_header_sniff(path) -> dict:
    """Fallback when no parser is registered: first line only, delimiter by count."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
            second = fh.readline()
    except OSError as exc:
        return {"readable": False, "error": str(exc), "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}
    if not first.strip():
        return {"readable": True, "error": None, "header": [],
                "recognised": set(), "time_column": None, "data_rows": 0}
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    header = [h.strip().strip('"') for h in first.split(delimiter)]
    return {"readable": True, "error": None, "delimiter": delimiter,
            "header": header, "recognised": set(), "time_column": None,
            "data_rows": 1 if second.strip() else 0}


def _worst(checks) -> str:
    """Highest severity outcome; within a severity the first one encountered wins,
    so the most specific label is preserved."""
    worst = PASS
    for c in checks:
        result = c["result"]
        if _SEVERITY.get(result, 0) > _SEVERITY[worst]:
            worst = result
    return worst


# ============================================================ PATIENT level

def learn_expected_modalities(patient_devices: dict[str, set],
                              threshold: float = 0.6) -> dict:
    """
    Work out what this project normally records, from the project itself.

    patient_devices: {patient_id: {"infinity", "nol", ...}}

    A device present in at least `threshold` of patients is treated as expected
    for that project. This is what makes the same validation code behave
    differently for PROMISES than for DEXREM without any project specific
    branch.
    """
    total = len(patient_devices)
    if total == 0:
        return {"total_patients": 0, "prevalence": {}, "expected": [], "optional": []}

    counts = Counter()
    for devices in patient_devices.values():
        counts.update(devices)

    prevalence = {d: round(n / total, 3) for d, n in counts.items()}
    expected = sorted([d for d, p in prevalence.items() if p >= threshold])
    optional = sorted([d for d, p in prevalence.items() if p < threshold])
    return {"total_patients": total, "prevalence": prevalence,
            "expected": expected, "optional": optional, "threshold": threshold}


def validate_patient(patient_id: str, devices: set, learned: dict) -> dict:
    """
    Compare one patient against what the project normally has.

    Never a FAIL (except the no-recording case). A patient who is missing a
    recording is a fact to record, not a broken file. Inclusion in any
    particular analysis is decided later, by the cohort filter.

    The comparison is against the project's OBSERVED expectation (learned by
    prevalence). What ingest/rules.yaml declares is surfaced at project level,
    not per patient, so a stale declaration cannot label a fully recorded
    patient "incomplete".
    """
    expected = set(learned.get("expected", []))
    missing = sorted(expected - devices)
    extra = sorted(devices - expected - set(learned.get("optional", [])))

    checks = []
    if not devices:
        checks.append(_check("has_any_recording", FAIL,
                             "no recognised recording found for this patient"))
    elif missing:
        prev = learned.get("prevalence", {})
        detail = ", ".join("{} in {:.0%} of patients".format(d, prev.get(d, 0))
                           for d in missing)
        checks.append(_check("project_coverage", WARNING,
                             "missing " + ", ".join(missing) + "; " + detail))
    else:
        checks.append(_check("project_coverage", PASS,
                             f"has everything this project usually records: "
                             f"{', '.join(sorted(expected))}"))

    if extra:
        checks.append(_check("unexpected_modality", WARNING,
                             f"has {', '.join(extra)}, uncommon in this project"))

    return {"patient": patient_id, "devices": sorted(devices),
            "missing": missing, "verdict": _worst(checks), "checks": checks}


# ============================================================ PROJECT level

def validate_project(file_results: list[dict], patient_devices: dict[str, set],
                     declared: list | None = None, threshold: float = 0.6) -> dict:
    """
    Roll everything up, and surface the two things that need a human.

    unknown_devices  files whose device has no validator, so a script is needed
    failures         files that will not survive preprocessing
    """
    learned = learn_expected_modalities(patient_devices, threshold)

    patients = [validate_patient(pid, devs, learned)
                for pid, devs in sorted(patient_devices.items())]

    # A declared modality that is rare in the archive is a stale sync rule, a
    # project level fact worth seeing once. It must not mark fully recorded
    # patients incomplete, so it lives here and not in per patient verdicts.
    declared_gap = {}
    if declared:
        for device in declared:
            prevalence = learned["prevalence"].get(device, 0.0)
            if prevalence < threshold:
                declared_gap[device] = prevalence

    verdicts = Counter(r.get("verdict") for r in file_results)
    unknown = sorted({r["device"] for r in file_results
                      if any(c["check"] == "device_known" for c in r.get("checks", []))})

    by_device = defaultdict(Counter)
    for r in file_results:
        by_device[r["device"]][r["verdict"]] += 1

    # Which device combinations actually exist, from whatever was discovered.
    # Validators are independent of this; a patient missing a modality is not a
    # validation failure, it is a fact for the cohort filter.
    coverage = Counter(tuple(sorted(devs)) for devs in patient_devices.values())

    return {
        "learned_expectation": learned,
        "declared_expectation": declared,
        "declared_gap": declared_gap,
        "files": {"total": len(file_results), **dict(verdicts)},
        "files_by_device": {d: dict(c) for d, c in by_device.items()},
        "devices_without_a_validator": unknown,
        "patients": patients,
        "patients_complete": sum(1 for p in patients if p["verdict"] == PASS),
        "patients_incomplete": sum(1 for p in patients if p["verdict"] != PASS),
        "coverage": {",".join(devs) or "(none)": n for devs, n in sorted(coverage.items())},
    }


def format_report(report: dict, project: str) -> str:
    """Plain text summary, for the console and for the sync report."""
    out = [f"\n{'=' * 68}", f"VALIDATION  {project}", "=" * 68]

    learned = report["learned_expectation"]
    out.append(f"\nWhat this project normally records "
               f"({learned['total_patients']} patients, "
               f"{learned.get('threshold', 0.6):.0%} threshold)")
    for device, prev in sorted(learned["prevalence"].items(), key=lambda kv: -kv[1]):
        mark = "expected" if device in learned["expected"] else "occasional"
        out.append(f"    {device:<12} {prev:>6.0%}   {mark}")
    if report.get("declared_expectation"):
        out.append(f"    declared in rules.yaml: "
                   f"{', '.join(report['declared_expectation'])}")
    else:
        out.append("    no declared expectation in rules.yaml")
    gap = report.get("declared_gap") or {}
    if gap:
        out.append("    declared but rare in this project (worth a look in "
                   "rules.yaml): "
                   + "; ".join(f"{d} {p:.0%}" for d, p in sorted(gap.items())))

    f = report["files"]
    n_pass = f.get(PASS, 0)
    n_warn = sum(f.get(v, 0) for v in WARNING_VERDICTS)
    n_fail = sum(f.get(v, 0) for v in FAIL_VERDICTS)
    out.append(f"\nFiles      {f['total']} checked   "
               f"pass {n_pass}   warning {n_warn}   fail {n_fail}")
    for extra in (WARNING_EMPTY, WARNING_MODALITY,
                  FAIL_UNREADABLE, FAIL_CORRUPT, FAIL_INVALID_STRUCTURE):
        if f.get(extra):
            out.append(f"      {extra:<28} {f[extra]}")
    for device, counts in sorted(report["files_by_device"].items()):
        detail = "  ".join(f"{k.lower().replace('warning_', 'w_').replace('fail_', 'f_')} {v}"
                           for k, v in sorted(counts.items()))
        out.append(f"    {device:<12} {detail}")

    out.append(f"\nPatients   {report['patients_complete']} complete   "
               f"{report['patients_incomplete']} incomplete")
    for p in report["patients"]:
        if p["verdict"] != PASS:
            out.append(f"    {p['patient']:<16} has {', '.join(p['devices']) or 'nothing'}"
                       f"{'   missing ' + ', '.join(p['missing']) if p['missing'] else ''}")

    cover = report.get("coverage", {})
    if cover:
        out.append("\nCoverage by pattern (discovered files, verdict independent)")
        for pattern, n in cover.items():
            out.append(f"    {n:>3}  {pattern}")

    if report["devices_without_a_validator"]:
        out.append("\nACTION NEEDED, no validator exists for these devices:")
        for d in report["devices_without_a_validator"]:
            out.append(f"    {d}      add a contract to DEVICE_CONTRACT and a parser")

    return "\n".join(out)
