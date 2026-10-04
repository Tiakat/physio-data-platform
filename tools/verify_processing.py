"""Verify processing coverage: every ingested parquet has processed outputs.

For each project:
  1. List all .parquet.enc in rawdata/{PROJECT}/parquet/
  2. For each, determine expected output: processed/level2/{PROJECT}/{safe_label}/lineage.json
  3. Report per-project: total, done, missing
"""
import os, sys, json
from pathlib import Path
from collections import defaultdict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools import azure_auth

def main():
    account = os.environ["AZURE_STORAGE_ACCOUNT"]
    svc = azure_auth.get_blob_service_client(account)
    raw_container = svc.get_container_client("rawdata")
    proc_container = svc.get_container_client("processed")

    # 1. List all ingested parquets by project
    by_project = defaultdict(list)
    for b in raw_container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.endswith(".parquet.enc"):
            continue
        parts = name.split("/")
        if len(parts) < 3:
            continue
        code = parts[0].upper()
        by_project[code].append(name)

    # 2. List all processed lineage.json by project
    done_by_project = defaultdict(set)
    for b in proc_container.list_blobs():
        name = b["name"] if isinstance(b, dict) else b.name
        if not name.startswith("processed/level2/") or not name.endswith("/lineage.json"):
            continue
        parts = name.split("/")
        # processed/level2/{PROJECT}/{safe_label}/lineage.json
        if len(parts) >= 4:
            code = parts[2].upper()
            label = parts[3]
            done_by_project[code].add(label)

    # 3. For each project, build expected labels and check
    print(f"{'Project':<12} {'Ingested':>8} {'Processed':>9} {'Missing':>8}  Verdict")
    print("-" * 60)
    all_complete = True
    total_ingested = 0
    total_processed = 0
    for code in sorted(by_project):
        blobs = sorted(by_project[code])
        total_ingested += len(blobs)
        # Expected labels (same logic as build_sweep_matrix)
        expected = set()
        for i, blob in enumerate(blobs, 1):
            label = f"patient {i}"
            safe = "".join(ch if ch.isalnum() else "_" for ch in label).strip("_")
            expected.add(safe)
        done = done_by_project[code]
        total_processed += len(done)
        # Missing = expected labels without lineage.json
        missing = expected - done
        # Extra = lineage.json without expected label (shouldn't happen, but check)
        extra = done - expected
        verdict = "COMPLETE" if not missing else f"MISSING {len(missing)}"
        if missing:
            all_complete = False
        print(f"{code:<12} {len(blobs):>8} {len(done):>9} {len(missing):>8}  {verdict}")
        if extra:
            print(f"  WARNING: {len(extra)} extra lineage.json without matching input")

    print("-" * 60)
    print(f"{'TOTAL':<12} {total_ingested:>8} {total_processed:>9}")
    print()
    if all_complete:
        print("[verify] ALL PROJECTS COMPLETE - processing is 100%")
        return 0
    else:
        print("[verify] INCOMPLETE - some patients missing processed outputs")
        return 1

if __name__ == "__main__":
    sys.exit(main())
