from pathlib import Path
import hashlib
import csv


def sha256_file(path: Path, chunk_size: int = 8 * 1024 * 1024) -> str:
    h = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)

    return h.hexdigest()


def build_inventory(root: Path):
    inventory = {}

    for path in root.rglob("*"):
        if not path.is_file():
            continue

        relative = path.relative_to(root)

        inventory[str(relative).replace("\\", "/")] = {
            "path": path,
            "size": path.stat().st_size,
        }

    return inventory


def main():
    rawdata = Path(
        r"C:\Users\katia\Dropbox\Liam\Projects actifs\V-RAPS\Database\RawData"
    )

    nested = rawdata / "RawData"

    print("=" * 72)
    print("RAW DATA MIRROR COMPARISON")
    print("=" * 72)

    print(f"Main RawData:   {rawdata}")
    print(f"Nested RawData: {nested}")
    print()

    if not rawdata.exists():
        raise FileNotFoundError(f"Main RawData does not exist: {rawdata}")

    if not nested.exists():
        raise FileNotFoundError(f"Nested RawData does not exist: {nested}")

    # Files outside the nested RawData directory
    main_files = {}

    for path in rawdata.rglob("*"):
        if not path.is_file():
            continue

        try:
            path.relative_to(nested)
            # If this succeeds, the file belongs to RawData\RawData.
            continue
        except ValueError:
            pass

        relative = path.relative_to(rawdata)

        main_files[str(relative).replace("\\", "/")] = {
            "path": path,
            "size": path.stat().st_size,
        }

    # Files inside nested RawData
    nested_files = build_inventory(nested)

    print(f"Main files outside nested RawData: {len(main_files)}")
    print(f"Nested RawData files:               {len(nested_files)}")
    print()

    main_paths = set(main_files)
    nested_paths = set(nested_files)

    only_main = sorted(main_paths - nested_paths)
    only_nested = sorted(nested_paths - main_paths)
    common = sorted(main_paths & nested_paths)

    print(f"Same relative path: {len(common)}")
    print(f"Only in main:       {len(only_main)}")
    print(f"Only in nested:     {len(only_nested)}")
    print()

    size_matches = 0
    size_mismatches = []

    for relative in common:
        main_size = main_files[relative]["size"]
        nested_size = nested_files[relative]["size"]

        if main_size == nested_size:
            size_matches += 1
        else:
            size_mismatches.append(
                (
                    relative,
                    main_size,
                    nested_size,
                )
            )

    print(f"Common files with identical size: {size_matches}")
    print(f"Common files with different size: {len(size_mismatches)}")
    print()

    # Only calculate SHA-256 for files that have the same path AND size.
    # This proves whether the content is actually identical.
    hash_matches = 0
    hash_mismatches = []

    print("Comparing SHA-256 hashes...")
    print()

    for index, relative in enumerate(common, start=1):

        main_info = main_files[relative]
        nested_info = nested_files[relative]

        if main_info["size"] != nested_info["size"]:
            continue

        print(
            f"[{index}/{len(common)}] HASHING: {relative}"
        )

        main_hash = sha256_file(main_info["path"])
        nested_hash = sha256_file(nested_info["path"])

        if main_hash == nested_hash:
            hash_matches += 1
        else:
            hash_mismatches.append(
                (
                    relative,
                    main_hash,
                    nested_hash,
                )
            )

    print()
    print("=" * 72)
    print("RESULT")
    print("=" * 72)

    print(f"Main files:                         {len(main_files)}")
    print(f"Nested files:                       {len(nested_files)}")
    print(f"Same relative path:                 {len(common)}")
    print(f"Only in main:                       {len(only_main)}")
    print(f"Only in nested:                     {len(only_nested)}")
    print(f"Same size:                          {size_matches}")
    print(f"Different size:                     {len(size_mismatches)}")
    print(f"Identical SHA-256:                  {hash_matches}")
    print(f"Different SHA-256:                  {len(hash_mismatches)}")
    print()

    # Save detailed report
    report_path = Path("sync_reports") / "rawdata_mirror_comparison.csv"
    report_path.parent.mkdir(exist_ok=True)

    with report_path.open(
        "w",
        newline="",
        encoding="utf-8",
    ) as f:

        writer = csv.writer(f)

        writer.writerow(
            [
                "relative_path",
                "status",
                "main_size",
                "nested_size",
                "main_sha256",
                "nested_sha256",
            ]
        )

        for relative in only_main:
            writer.writerow(
                [
                    relative,
                    "ONLY_MAIN",
                    main_files[relative]["size"],
                    "",
                    "",
                    "",
                ]
            )

        for relative in only_nested:
            writer.writerow(
                [
                    relative,
                    "ONLY_NESTED",
                    "",
                    nested_files[relative]["size"],
                    "",
                    "",
                ]
            )

        for relative in common:

            main_info = main_files[relative]
            nested_info = nested_files[relative]

            if main_info["size"] != nested_info["size"]:
                writer.writerow(
                    [
                        relative,
                        "SIZE_MISMATCH",
                        main_info["size"],
                        nested_info["size"],
                        "",
                        "",
                    ]
                )

        for relative in common:

            main_info = main_files[relative]
            nested_info = nested_files[relative]

            if main_info["size"] != nested_info["size"]:
                continue

            main_hash = sha256_file(main_info["path"])
            nested_hash = sha256_file(nested_info["path"])

            status = (
                "IDENTICAL"
                if main_hash == nested_hash
                else "HASH_MISMATCH"
            )

            writer.writerow(
                [
                    relative,
                    status,
                    main_info["size"],
                    nested_info["size"],
                    main_hash,
                    nested_hash,
                ]
            )

    print(f"Detailed report: {report_path}")
    print()

    if (
        len(only_main) == 0
        and len(only_nested) == 0
        and len(size_mismatches) == 0
        and len(hash_mismatches) == 0
        and len(main_files) == len(nested_files)
    ):
        print("CONCLUSION:")
        print("The nested RawData directory is an exact duplicate mirror.")
    else:
        print("CONCLUSION:")
        print("The two directories are NOT exact mirrors.")
        print("Review the CSV report before deleting or excluding anything.")


if __name__ == "__main__":
    main()