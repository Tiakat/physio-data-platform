import os
from pathlib import Path

import dropbox
from dotenv import load_dotenv


load_dotenv()

token = os.getenv("DROPBOX_ACCESS_TOKEN")

if not token:
    raise RuntimeError("DROPBOX_ACCESS_TOKEN is missing from .env")

dbx = dropbox.Dropbox(token)

MAIN_ROOT = "/Liam/Projects actifs/V-RAPS/Database/RawData"
NESTED_ROOT = "/Liam/Projects actifs/V-RAPS/Database/RawData/RawData"

REPORT_DIR = Path("sync_reports")
REPORT_DIR.mkdir(exist_ok=True)

REPORT_FILE = REPORT_DIR / "dropbox_rawdata_mirror_comparison.csv"


def list_files(root):
    """Return Dropbox files indexed by relative path."""

    files = {}

    result = dbx.files_list_folder(root, recursive=True)

    while True:
        for entry in result.entries:
            if isinstance(entry, dropbox.files.FileMetadata):
                relative = entry.path_display[len(root):].lstrip("/")

                files[relative] = {
                    "size": entry.size,
                    "content_hash": entry.content_hash,
                    "modified": entry.server_modified,
                }

        if not result.has_more:
            break

        result = dbx.files_list_folder_continue(result.cursor)

    return files


print("Dropbox authentication successful.")

print("\nScanning main RawData...")
main_files = list_files(MAIN_ROOT)
print(f"Main files: {len(main_files)}")

print("\nScanning nested RawData...")
nested_files = list_files(NESTED_ROOT)
print(f"Nested files: {len(nested_files)}")


all_paths = sorted(set(main_files) | set(nested_files))

results = []

for path in all_paths:

    main = main_files.get(path)
    nested = nested_files.get(path)

    if main is None:
        status = "ONLY_IN_NESTED"

    elif nested is None:
        status = "ONLY_IN_MAIN"

    elif main["size"] != nested["size"]:
        status = "SIZE_MISMATCH"

    elif main["content_hash"] != nested["content_hash"]:
        status = "HASH_MISMATCH"

    else:
        status = "IDENTICAL"

    results.append(
        {
            "relative_path": path,
            "status": status,
            "main_size": main["size"] if main else "",
            "nested_size": nested["size"] if nested else "",
            "main_hash": main["content_hash"] if main else "",
            "nested_hash": nested["content_hash"] if nested else "",
            "main_modified": main["modified"] if main else "",
            "nested_modified": nested["modified"] if nested else "",
        }
    )


import csv

with REPORT_FILE.open("w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(
        f,
        fieldnames=[
            "relative_path",
            "status",
            "main_size",
            "nested_size",
            "main_hash",
            "nested_hash",
            "main_modified",
            "nested_modified",
        ],
    )

    writer.writeheader()
    writer.writerows(results)


counts = {}

for row in results:
    counts[row["status"]] = counts.get(row["status"], 0) + 1


print("\n" + "=" * 70)
print("COMPARISON RESULTS")
print("=" * 70)

for status in [
    "IDENTICAL",
    "ONLY_IN_MAIN",
    "ONLY_IN_NESTED",
    "SIZE_MISMATCH",
    "HASH_MISMATCH",
]:
    print(f"{status}: {counts.get(status, 0)}")

print("\nReport:")
print(REPORT_FILE)

print("\nNo Dropbox files were downloaded.")