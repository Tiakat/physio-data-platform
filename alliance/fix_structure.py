#!/usr/bin/env python3
"""
fix_structure.py — reorganize a project tree into the lab template layout.

    ~/cryptenv/bin/python alliance/fix_structure.py <src> <dst> [--dry-run]

- <src>: plaintext or .enc tree (e.g. dropbox_enc/ with <PROJECT>/... inside).
- <dst>: the fixed tree is written here. Nothing in <src> is modified.
- Patient folders become "Patient <N>" (number taken from the Dropbox name,
  so Patient 1 stays Patient 1). The original name is kept in the manifest.
- Device folders are normalized to the canonical vocabulary
  (BIS, BetterCare, Infinity, NOL, Pump, TOF, Audio, Oximetry).
- Target layout per project:
      Database/RawData/Patient <N>/            (files directly under patient)
      Database/ExtractedData/<DEVICE>/Patient <N>/   (device exports)
      Database/AnalyzedData/Patient <N>/       (derived outputs)
      Database/Photos/
- Anything unmappable goes to Database/_review/ (never silently dropped).
- A continuity manifest (original path, new path, plaintext SHA256, size)
  is written to <dst>/_manifest.jsonl.
- For .enc sources, PIPELINE_DATA_KEY must be set. Files are decrypted in
  memory ONLY to compute the plaintext SHA; the ciphertext bytes are copied
  unchanged (no re-encryption).
- PROMISES top-level ExtractedData/ duplicates are detected by SHA against
  RawData/ and recorded as duplicates (not copied twice).

In --dry-run, prints a per-project summary and samples of _review paths,
writes nothing.
"""
import argparse
import hashlib
import json
import os
import re
import sys

DEVICE_MAP = {
    "bis": "BIS",
    "bettercare": "BetterCare",
    "better care": "BetterCare",
    "bettercare1": "BetterCare",
    "infinity": "Infinity",
    "infinity_brute": "Infinity",
    "nol": "NOL",
    "medasense_data": "NOL",
    "medasense": "NOL",
    "oximetry": "Oximetry",
    "oxymétrie": "Oximetry",
    "tof": "TOF",
    "audio": "Audio",
    "pump": "Pump",
    "remi": "Pump",
    "propofol": "Pump",
}

BIS_EXTS = {".r2a", ".spa", ".ara", ".e_a", ".f_a", ".h_a", ".m_a",
            ".o_a", ".t_a", ".s_a"}
BIS_WRAPPER_RE = re.compile(r"^(M-|DH|L)[\w-]*$")


def strip_enc(name):
    return name[:-4] if name.endswith(".enc") else name


def canonical_device(name):
    return DEVICE_MAP.get(name.strip().lower())


def extract_patient_number(name):
    name = strip_enc(name)
    m = re.search(r"[Pp]atient[_\s]*(\d+)", name)
    if m:
        return int(m.group(1))
    m = re.match(r"^\s*(\d+)\s*$", name)
    if m:
        return int(m.group(1))
    m = re.search(r"PROMISES[_\s]+(\d+)", name, re.I)
    if m:
        return int(m.group(1))
    return None


def flatten_bis_wrappers(sub):
    """Drop BIS export wrapper dirs (M-*, DH*, L*), keep the files."""
    if len(sub) > 1 and all(BIS_WRAPPER_RE.match(s) for s in sub[:-1]):
        return [sub[-1]]
    return sub


# ---------------------------------------------------------------------------
# Per-project mappers. Each takes the path components under <PROJECT>/
# (filename WITHOUT .enc) and returns the new components under <PROJECT>/
# (still without .enc), or None if this mapper cannot handle the path.
# ---------------------------------------------------------------------------

def map_standard(parts):
    """Clean projects: RawData/<n>/[ExtractedData/<device>/...].

    Covers ESMONOL, IPAMS, MONREPI, POSBRAIN, PVB-ABDO, SILVR, V-RAPS,
    COLECTOMIE (plus COLECTOMIE's stray 53/ExtractedData/...)."""
    if parts[0] == "RawData" and len(parts) >= 2:
        n = extract_patient_number(parts[1])
        if n is None:
            return None
        patient = f"Patient {n}"
        rest = parts[2:]
        if rest and rest[0].lower() == "extracteddata":
            if len(rest) < 2:
                return None
            dev = canonical_device(rest[1])
            if dev is None:
                return None
            sub = flatten_bis_wrappers(rest[2:]) if dev == "BIS" else rest[2:]
            return ["Database", "ExtractedData", dev, patient] + sub
        # files directly under RawData/<n>/, or under a stray subfolder:
        # scan for a device folder anywhere in rest (e.g. V-RAPS 14/VRAPS 14/)
        for i, comp in enumerate(rest):
            dev = canonical_device(comp)
            if dev is not None:
                sub = rest[i + 1:]
                if dev == "BIS":
                    sub = flatten_bis_wrappers(sub)
                return ["Database", "ExtractedData", dev, patient] + sub
        return ["Database", "RawData", patient] + rest
    # stray top-level "<n>/ExtractedData/<device>/..." (COLECTOMIE's 53/)
    n = extract_patient_number(parts[0])
    if n is not None and len(parts) >= 3 and parts[1].lower() == "extracteddata":
        patient = f"Patient {n}"
        dev = canonical_device(parts[2])
        if dev is None:
            return None
        sub = flatten_bis_wrappers(parts[3:]) if dev == "BIS" else parts[3:]
        return ["Database", "ExtractedData", dev, patient] + sub
    return None


def route_by_extension(filename, patient):
    base = strip_enc(filename)
    ext = os.path.splitext(base)[1].lower()
    if ext in BIS_EXTS:
        return ["Database", "ExtractedData", "BIS", patient, filename]
    if ext == ".med":
        return ["Database", "ExtractedData", "NOL", patient, filename]
    return ["Database", "RawData", patient, filename]


def map_dexrem(parts):
    """DEXREM: messy patient folders, lowercase device names, pump folders."""
    n = extract_patient_number(parts[0])
    if n is None:
        return None
    patient = f"Patient {n}"
    if len(parts) == 1:
        return None
    rest = parts[1:]
    # files directly under the patient folder -> route by extension
    if len(rest) == 1:
        return route_by_extension(rest[0], patient)
    # BIS export wrappers directly under patient (no bis/ folder)
    if BIS_WRAPPER_RE.match(rest[0]):
        sub = flatten_bis_wrappers(rest)
        return ["Database", "ExtractedData", "BIS", patient] + sub
    dev = canonical_device(rest[0])
    if dev is None:
        return None
    sub = rest[1:]
    if dev == "Pump":
        drug_raw = rest[0].strip().lower()
        drug = ("remifentanil" if drug_raw == "remi"
                else "propofol" if drug_raw == "propofol" else drug_raw)
        return ["Database", "ExtractedData", "Pump", drug, patient] + sub
    if dev == "BIS":
        sub = flatten_bis_wrappers(sub)
    # NOL (Medasense_Data): keep the date subfolder, filenames may repeat
    return ["Database", "ExtractedData", dev, patient] + sub


def map_promises_analyzed(parts):
    """PROMISES AnalyzedData/ reorganization."""
    sub = parts[1]
    if sub.startswith("1-Patients"):
        if len(parts) >= 3:
            n = extract_patient_number(parts[2])
            if n is not None:
                return (["Database", "AnalyzedData", f"Patient {n}", "infinity"]
                        + parts[3:])
            # study-level aggregates ("Promises", "results_10mmhg.ect", ...)
            if (parts[2].lower() == "promises"
                    or parts[2].startswith("results_")):
                return (["Database", "AnalyzedData", "_study", parts[2]]
                        + parts[3:])
        return None
    if sub.startswith("2-BetterCare"):
        # BetterCare_Combined/Patient_38/...
        if len(parts) >= 4 and parts[2] == "BetterCare_Combined":
            n = extract_patient_number(parts[3])
            if n is not None:
                return (["Database", "AnalyzedData", f"Patient {n}", "bettercare"]
                        + parts[4:])
        # MAP_Integration/, comparison_bettercare_infinity(cleaned)/ -> review
        return ["Database", "AnalyzedData", "_review"] + parts[1:]
    if sub == "infinity_filtered":
        if len(parts) >= 3:
            n = extract_patient_number(parts[2])
            if n is not None:
                return (["Database", "AnalyzedData", f"Patient {n}",
                         "infinity_filtered"] + parts[3:])
        return None
    # 4-filtered-only-..., results_10mmhg.ect, Promises/ -> review
    return ["Database", "AnalyzedData", "_review"] + parts[1:]


def map_promises_top_extracted(parts):
    """PROMISES top-level ExtractedData/ (deduped by SHA in main()).

    Returns (new_parts, patient_or_None)."""
    # ExtractedData/Infinity/Included Patients/<patient>/...
    # ExtractedData/Infinity/infinity_brute/<patient>/...
    # ExtractedData/Superposition/Patient_*/...
    # ExtractedData/bettercare/<n>/... or bettercare/BetterCare1/<patient>/...
    # files directly under ExtractedData/ (e.g. PROMISES_DATA_LABELS_*.csv)
    if len(parts) == 2:
        return ["Database", "AnalyzedData", "_study"] + parts[1:]
    if len(parts) < 3:
        return None
    if parts[1] == "Infinity" and parts[2] in ("Included Patients",
                                               "infinity_brute"):
        if len(parts) >= 4:
            n = extract_patient_number(parts[3])
            if n is not None:
                return ["Database", "ExtractedData", "Infinity",
                        f"Patient {n}"] + parts[4:]
            # files directly under infinity_brute/ = study-level aggregates
            if parts[2] == "infinity_brute":
                return (["Database", "AnalyzedData", "_study", "infinity_brute"]
                        + parts[3:])
        return None
    if parts[1] == "Superposition":
        if len(parts) >= 3:
            n = extract_patient_number(parts[2])
            if n is not None:
                return (["Database", "AnalyzedData", f"Patient {n}",
                         "superposition"] + parts[3:])
        return ["Database", "AnalyzedData", "_review", "superposition"
                ] + parts[2:]
    if parts[1] == "bettercare":
        rest = parts[2:]
        if not rest:
            return None
        # bettercare/<n>/... or bettercare/BetterCare1/<patient>/...
        idx = 1 if rest[0] == "BetterCare1" else 0
        if len(rest) > idx:
            n = extract_patient_number(rest[idx])
            if n is not None:
                return (["Database", "AnalyzedData", f"Patient {n}",
                         "bettercare"] + rest[idx + 1:])
        return ["Database", "AnalyzedData", "_review", "bettercare"] + rest
    return None


def map_promises(parts):
    if parts[0] == "RawData":
        return map_standard(parts)
    if parts[0] == "AnalyzedData" and len(parts) >= 2:
        return map_promises_analyzed(parts)
    # top-level ExtractedData/ handled with dedupe in main()
    return None


MAPPERS = {
    "COLECTOMIE": map_standard,
    "DEXREM": map_dexrem,
    "ESMONOL": map_standard,
    "IPAMS": map_standard,
    "MONREPI": map_standard,
    "POSBRAIN": map_standard,
    "PROMISES": map_promises,
    "PVB-ABDO": map_standard,
    "SILVR": map_standard,
    "V-RAPS": map_standard,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def iter_files(src):
    """Yield (relpath, abspath) for every file, RawData/ first (for dedupe)."""
    all_files = []
    for root, _dirs, files in os.walk(src):
        for f in files:
            ap = os.path.join(root, f)
            rel = os.path.relpath(ap, src)
            all_files.append((rel, ap))

    def sort_key(item):
        rel = item[0]
        parts = rel.split(os.sep)
        # project, then RawData first, then rest
        sub = parts[1] if len(parts) > 1 else ""
        return (parts[0], 0 if sub == "RawData" else 1, rel)

    all_files.sort(key=sort_key)
    return all_files


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    # detect encrypted tree
    encrypted = False
    for _rel, ap_ in iter_files(args.src):
        encrypted = ap_.endswith(".enc")
        break

    fernet = None
    if encrypted and not args.dry_run:
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                        ".."))
        from tools.crypto import get_fernet
        fernet = get_fernet()  # fails fast on bad/missing key

    stats = {}
    review_samples = {}
    manifest = []
    seen_sha = {}  # sha -> new_rel (for PROMISES dedupe)
    n_copied = n_dup = n_review = 0

    files = iter_files(args.src)
    for rel, ap_ in files:
        parts = rel.split(os.sep)
        project = parts[0]
        sub = parts[1:]
        had_enc = sub[-1].endswith(".enc")
        work = [strip_enc(p) if i == len(sub) - 1 else p
                for i, p in enumerate(sub)]

        mapper = MAPPERS.get(project)
        new_parts = mapper(work) if mapper else None

        # PROMISES top-level ExtractedData/: dedupe path
        is_promises_top = (project == "PROMISES" and work
                           and work[0] == "ExtractedData")
        if new_parts is None and is_promises_top:
            new_parts = map_promises_top_extracted(work)

        st = stats.setdefault(project, {"total": 0, "mapped": 0,
                                        "review": 0, "dup": 0})
        st["total"] += 1

        if new_parts is None:
            new_parts = ["Database", "_review"] + work
            st["review"] += 1
            n_review += 1
            review_samples.setdefault(project, []).append(rel)
        else:
            st["mapped"] += 1

        new_rel = os.path.join(project, *new_parts)
        if had_enc:
            new_rel += ".enc"

        if args.dry_run:
            continue

        with open(ap_, "rb") as fh:
            blob = fh.read()
        if encrypted:
            sha = hashlib.sha256(fernet.decrypt(blob)).hexdigest()
        else:
            sha = hashlib.sha256(blob).hexdigest()

        # dedupe: PROMISES top-level ExtractedData duplicates of RawData
        if is_promises_top and sha in seen_sha:
            manifest.append({
                "original_path": rel, "new_path": None,
                "sha256": sha, "size": len(blob),
                "duplicate_of": seen_sha[sha], "status": "duplicate",
            })
            st["dup"] += 1
            n_dup += 1
            continue
        seen_sha.setdefault(sha, new_rel)

        dst_ap = os.path.join(args.dst, new_rel)
        if not (os.path.exists(dst_ap)
                and os.path.getsize(dst_ap) == len(blob)):
            os.makedirs(os.path.dirname(dst_ap), exist_ok=True)
            with open(dst_ap, "wb") as fh:
                fh.write(blob)
        manifest.append({
            "original_path": rel, "new_path": new_rel,
            "sha256": sha, "size": len(blob), "status": "ok",
        })
        n_copied += 1

    if args.dry_run:
        print("DRY RUN — nothing written\n")
        for proj in sorted(stats):
            st = stats[proj]
            print(f"=== {proj}: {st['total']} files | "
                  f"mapped {st['mapped']} | review {st['review']}")
            for sample in (review_samples.get(proj) or [])[:5]:
                print(f"    REVIEW: {sample}")
        return

    mp = os.path.join(args.dst, "_manifest.jsonl")
    os.makedirs(args.dst, exist_ok=True)
    with open(mp, "w") as fh:
        for entry in manifest:
            fh.write(json.dumps(entry) + "\n")
    print(f"done: {n_copied} copied, {n_dup} duplicates skipped, "
          f"{n_review} to _review")
    print(f"manifest: {mp}")


if __name__ == "__main__":
    main()
