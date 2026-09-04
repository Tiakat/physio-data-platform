from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from azure.core.exceptions import (
    AzureError,
    HttpResponseError,
    ResourceExistsError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.storage.blob import BlobServiceClient


# ============================================================
# Configuration
# ============================================================

load_dotenv()

CHUNK = 8 * 1024 * 1024  # 8 MB

PARSEABLE = {
    ".csv",
}

DEVICE_PATTERNS = {
    "bettercare": "bettercare",
    "infinity": "infinity",
    "nol": "nol",
    "bis": "bis",
    "pump": "pump",
    "photo": "photo",
}


DEFAULT_RULES = {
    "include_directories": [
        "RawData",
    ],
    "include_file_patterns": [
        "Données*.xls*",
        "Donnees*.xls*",
        "*Data*.xls*",
        "*database*.xls*",
    ],
    "exclude_directories": [
        "AnalyzedData",
        "Analyzed Data",
        "AnalysedData",
        "Analyse",
        "Analyses",
        "Documents",
        "Doc",
        "Protocole",
        "Protocol",
        "Articles",
        "Figures",
        "Scripts",
        "Extracted",
        "Processed",
        "Backup",
        "Old",
        "Archive",
        "Trash",
    ],
    "ignore_names": [
        ".DS_Store",
        "Thumbs.db",
        "desktop.ini",
        "~$*",
        "*.tmp",
        ".dropbox*",
        "Icon\r",
    ],
    "on_unknown_directory": "flag",
    "report_unparsed_files": True,
    "max_file_mb": 2048,
}


# ============================================================
# Rules
# ============================================================

def load_rules() -> dict[str, Any]:
    """
    Load ingest/rules.yaml.

    Falls back to DEFAULT_RULES if the YAML file does not exist.
    """
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "PyYAML is required. Install it with: pip install pyyaml"
        ) from exc

    rules_path = Path("ingest") / "rules.yaml"

    if not rules_path.exists():
        print(f"WARNING: {rules_path} not found.")
        print("Using DEFAULT_RULES.")
        return {
            "defaults": DEFAULT_RULES.copy(),
            "projects": {},
        }

    with rules_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    defaults = dict(DEFAULT_RULES)
    defaults.update(data.get("defaults", {}) or {})

    return {
        "defaults": defaults,
        "projects": data.get("projects", {}) or {},
    }


def project_rules(
    rules: dict[str, Any],
    project: str,
) -> dict[str, Any]:
    """
    Merge DEFAULT_RULES + YAML defaults + project-specific rules.
    """
    merged = dict(rules.get("defaults", DEFAULT_RULES))

    project_config = rules.get("projects", {}).get(project, {}) or {}

    # Copy lists so we don't mutate DEFAULT_RULES.
    for key, value in list(merged.items()):
        if isinstance(value, list):
            merged[key] = list(value)

    # Normal project-specific overrides.
    for key, value in project_config.items():
        if key == "exclude_directories_extra":
            continue
        merged[key] = value

    # Additional excluded directories.
    extra_excludes = project_config.get(
        "exclude_directories_extra",
        [],
    ) or []

    existing_excludes = list(
        merged.get("exclude_directories", [])
    )

    for directory in extra_excludes:
        if directory not in existing_excludes:
            existing_excludes.append(directory)

    merged["exclude_directories"] = existing_excludes

    return merged


# ============================================================
# General helpers
# ============================================================

def matches_name(
    name: str,
    patterns: list[str],
) -> bool:
    return any(
        fnmatch.fnmatch(name, pattern)
        for pattern in patterns
    )


def is_ignored(
    name: str,
    rules: dict[str, Any],
) -> bool:
    return matches_name(
        name,
        rules.get("ignore_names", []),
    )


def sha256_file(
    path: Path,
) -> str:
    """
    Calculate SHA-256 using streaming reads.
    """
    digest = hashlib.sha256()

    with path.open("rb") as f:
        while True:
            chunk = f.read(CHUNK)

            if not chunk:
                break

            digest.update(chunk)

    return digest.hexdigest()


def sha256_stream(
    stream,
) -> str:
    """
    Calculate SHA-256 from a readable stream.
    """
    digest = hashlib.sha256()

    while True:
        chunk = stream.read(CHUNK)

        if not chunk:
            break

        digest.update(chunk)

    return digest.hexdigest()


def detect_device(
    path: Path,
) -> str | None:
    """
    Detect device from path components / filename.
    """
    parts = [
        part.lower()
        for part in path.parts
    ]

    for part in parts:
        for pattern, device in DEVICE_PATTERNS.items():
            if pattern in part:
                return device

    return None


def patient_from_path(
    path: Path,
) -> str | None:
    """
    Return the first numeric directory component.

    Example:
        V-RAPS/19/ExtractedData/NOL/PMD_LOG.csv

    returns:
        19
    """
    for part in path.parts:
        if part.isdigit():
            return part

    return None


# ============================================================
# Discovery
# ============================================================

def scan_database_folder(
    database_root: Path,
    rules: dict[str, Any],
) -> dict[str, Any]:
    """
    Scan the Database directory.

    Only directories explicitly listed in include_directories
    are recursively imported.

    For your current setup this means:
        Database/RawData

    Administrative directories are not imported.
    """
    result = {
        "database_root": str(database_root),
        "included_directories": [],
        "excluded_directories": [],
        "unknown_directories": [],
        "metadata_files": [],
    }

    if not database_root.exists():
        raise FileNotFoundError(
            f"Database folder does not exist: {database_root}"
        )

    include_dirs = set(
        rules.get("include_directories", [])
    )

    exclude_dirs = set(
        rules.get("exclude_directories", [])
    )

    include_file_patterns = rules.get(
        "include_file_patterns",
        [],
    )

    for child in database_root.iterdir():
        if child.name in include_dirs:
            result["included_directories"].append(
                child.name
            )

        elif child.name in exclude_dirs:
            result["excluded_directories"].append(
                child.name
            )

        elif child.is_dir():
            result["unknown_directories"].append(
                child.name
            )

        elif child.is_file():
            if matches_name(
                child.name,
                include_file_patterns,
            ):
                result["metadata_files"].append(
                    str(child)
                )

    return result


def collect_files(
    rawdata_root: Path,
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    Recursively collect files under RawData.

    Excluded directories are pruned before traversal.
    """
    if not rawdata_root.exists():
        raise FileNotFoundError(
            f"RawData folder does not exist: {rawdata_root}"
        )

    records: list[dict[str, Any]] = []

    excluded = set(
        rules.get("exclude_directories", [])
    )

    max_file_mb = float(
        rules.get("max_file_mb", 2048)
    )

    max_file_bytes = int(
        max_file_mb * 1024 * 1024
    )

    for root, dirs, files in os.walk(rawdata_root):
        root_path = Path(root)

        # Prune excluded/ignored directories.
        dirs[:] = [
            d
            for d in dirs
            if (
                d not in excluded
                and not is_ignored(d, rules)
            )
        ]

        for filename in files:
            if is_ignored(filename, rules):
                continue

            path = root_path / filename

            try:
                stat = path.stat()
            except OSError as exc:
                print(
                    f"WARNING: Cannot stat {path}: {exc}"
                )
                continue

            relative = path.relative_to(rawdata_root)

            extension = path.suffix.lower()

            records.append(
                {
                    "path": str(path),
                    "relative": relative.as_posix(),
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "extension": extension,
                    "device": detect_device(relative),
                    "parseable": extension in PARSEABLE,
                    "patient_id": patient_from_path(relative),
                    "oversize": (
                        stat.st_size > max_file_bytes
                    ),
                }
            )

    return records


# ============================================================
# Azure target
# ============================================================

class Target:
    """
    Storage target.

    Supports:
        azure
        local

    Azure uploads are deliberately conservative because the
    current environment is a home/consumer network.
    """

    def __init__(
        self,
        target: str = "azure",
        local_root: str | Path = "rehearsal_target",
    ):
        self.target = target

        if target == "local":
            self.local_root = Path(local_root)
            self.local_root.mkdir(
                parents=True,
                exist_ok=True,
            )

            self.service = None
            self.container_client = None
            return

        if target != "azure":
            raise ValueError(
                f"Unknown target: {target}"
            )

        account = os.getenv(
            "AZURE_STORAGE_ACCOUNT"
        )

        key = os.getenv(
            "AZURE_STORAGE_KEY"
        )

        container = os.getenv(
            "AZURE_STORAGE_CONTAINER",
            "rawdata",
        )

        if not account:
            raise RuntimeError(
                "AZURE_STORAGE_ACCOUNT is not set."
            )

        if not key:
            raise RuntimeError(
                "AZURE_STORAGE_KEY is not set."
            )

        # ----------------------------------------------------
        # Conservative Azure networking configuration.
        #
        # max_concurrency=1 is intentionally used for uploads
        # below. This avoids opening multiple simultaneous
        # upload streams on the current network.
        # ----------------------------------------------------
        self.service = BlobServiceClient(
            account_url=(
                f"https://{account}.blob.core.windows.net"
            ),
            credential=key,

            # Socket-level timeouts.
            connection_timeout=30,
            read_timeout=60,

            # Prevent a single problematic file from retrying
            # for an extremely long time.
            retry_total=2,
            retry_connect=2,
            retry_read=2,
            retry_status=2,

            # Conservative block configuration.
            max_block_size=4 * 1024 * 1024,
            max_single_put_size=4 * 1024 * 1024,
        )

        self.container_client = (
            self.service.get_container_client(container)
        )

    # --------------------------------------------------------
    # Existence
    # --------------------------------------------------------

    def exists(
        self,
        key: str,
    ) -> bool:
        """
        Return True only if the blob/object actually exists.

        IMPORTANT:
        Network/authentication errors are NOT converted into
        False. That prevents a network outage from making the
        synchronizer think an existing blob is missing.
        """
        if self.target == "local":
            return (
                self.local_root / key
            ).exists()

        blob = (
            self.container_client
            .get_blob_client(key)
        )

        try:
            blob.get_blob_properties()
            return True

        except ResourceNotFoundError:
            return False

        except HttpResponseError as exc:
            if exc.status_code == 404:
                return False
            raise

    # --------------------------------------------------------
    # Size
    # --------------------------------------------------------

    def size(
        self,
        key: str,
    ) -> int | None:
        if self.target == "local":
            path = self.local_root / key

            if not path.exists():
                return None

            return path.stat().st_size

        blob = (
            self.container_client
            .get_blob_client(key)
        )

        try:
            properties = (
                blob.get_blob_properties()
            )
            return properties.size

        except ResourceNotFoundError:
            return None

        except HttpResponseError as exc:
            if exc.status_code == 404:
                return None
            raise

    # --------------------------------------------------------
    # SHA-256
    # --------------------------------------------------------

    def sha256(
        self,
        key: str,
    ) -> str | None:
        """
        Download the existing Azure blob and calculate SHA-256.

        Returns None only when the blob does not exist.
        Network failures are raised.
        """
        if self.target == "local":
            path = self.local_root / key

            if not path.exists():
                return None

            return sha256_file(path)

        blob = (
            self.container_client
            .get_blob_client(key)
        )

        try:
            stream = blob.download_blob(
                max_concurrency=1,
            )

            digest = hashlib.sha256()

            for chunk in stream.chunks():
                digest.update(chunk)

            return digest.hexdigest()

        except ResourceNotFoundError:
            return None

        except HttpResponseError as exc:
            if exc.status_code == 404:
                return None
            raise

    # --------------------------------------------------------
    # Upload
    # --------------------------------------------------------

    def upload(
        self,
        local_path: Path,
        key: str,
    ) -> None:
        """
        Upload without overwriting an existing object.

        Azure:
            - one concurrent upload stream
            - 4 MB blocks
            - explicit request timeout
            - overwrite=False
        """
        if self.target == "local":
            destination = (
                self.local_root / key
            )

            destination.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            if destination.exists():
                raise FileExistsError(
                    f"Target already exists: {destination}"
                )

            # Stream-copy rather than loading the whole file.
            with (
                local_path.open("rb") as source,
                destination.open("wb") as dest,
            ):
                while True:
                    chunk = source.read(CHUNK)

                    if not chunk:
                        break

                    dest.write(chunk)

            return

        blob = (
            self.container_client
            .get_blob_client(key)
        )

        with local_path.open("rb") as f:
            blob.upload_blob(
                f,

                # Raw data must never be overwritten.
                overwrite=False,

                # One connection at a time for the current network.
                max_concurrency=1,

                # Server-side operation timeout.
                timeout=120,
            )

    # --------------------------------------------------------
    # Versioning
    # --------------------------------------------------------

    def versioned_key(
        self,
        key: str,
        digest: str,
    ) -> str:
        """
        Generate a non-destructive versioned key.

        Example:
            file.csv
            file.v2-abcdef12.csv
            file.v3-abcdef12.csv
        """
        path = Path(key)

        stem = path.stem
        suffix = path.suffix

        # First candidate.
        candidate = (
            path.parent
            / f"{stem}.v2-{digest[:8]}{suffix}"
        )

        candidate_str = candidate.as_posix()

        if not self.exists(candidate_str):
            return candidate_str

        # Additional candidates.
        version = 3

        while True:
            candidate = (
                path.parent
                / (
                    f"{stem}.v{version}-"
                    f"{digest[:8]}{suffix}"
                )
            )

            candidate_str = candidate.as_posix()

            if not self.exists(candidate_str):
                return candidate_str

            version += 1


# ============================================================
# State
# ============================================================

def state_path(
    project: str,
) -> Path:
    directory = Path("sync_state")
    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    return directory / f"{project}.json"


def load_state(
    project: str,
) -> dict[str, Any]:
    path = state_path(project)

    if not path.exists():
        return {
            "project": project,
            "uploaded": {},
            "patient_ids": [],
            "decisions": {},
        }

    with path.open(
        "r",
        encoding="utf-8",
    ) as f:
        state = json.load(f)

    state.setdefault(
        "project",
        project,
    )
    state.setdefault(
        "uploaded",
        {},
    )
    state.setdefault(
        "patient_ids",
        [],
    )
    state.setdefault(
        "decisions",
        {},
    )

    return state


def save_state(
    project: str,
    state: dict[str, Any],
) -> None:
    path = state_path(project)

    temporary = path.with_suffix(
        ".json.tmp"
    )

    with temporary.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            state,
            f,
            indent=2,
            ensure_ascii=False,
        )

    temporary.replace(path)


# ============================================================
# Key generation
# ============================================================

def blob_key(
    project: str,
    record: dict[str, Any],
) -> str:
    """
    Azure key preserves the RawData-relative path.

    Example:
        project = V-RAPS
        relative = 19/ExtractedData/NOL/PMD_LOG.csv

    becomes:
        V-RAPS/19/ExtractedData/NOL/PMD_LOG.csv
    """
    relative = record["relative"].replace(
        "\\",
        "/",
    )

    return f"{project}/{relative}"


# ============================================================
# Verification
# ============================================================

def verify_state(
    project: str,
    state: dict[str, Any],
    target: Target,
    verify_hash: bool = False,
) -> dict[str, Any]:
    """
    Verify synchronized objects.

    Default:
        existence only

    verify_hash=True:
        compare stored SHA-256 against target SHA-256
    """
    uploaded = state.get(
        "uploaded",
        {},
    )

    result = {
        "checked": 0,
        "missing": 0,
        "hash_mismatch": 0,
        "errors": 0,
    }

    for key, info in uploaded.items():
        result["checked"] += 1

        try:
            if not target.exists(key):
                result["missing"] += 1
                continue

            if verify_hash:
                if isinstance(info, str):
                    # Support old state format:
                    # "key": "sha256..."
                    expected = info
                elif isinstance(info, dict):
                    # Support current state format:
                    # "key": {"sha256": "...", ...}
                    expected = info.get("sha256")
                else:
                    expected = None

                if not expected:
                    result["errors"] += 1
                    continue

                actual = target.sha256(key)

                if actual != expected:
                    result["hash_mismatch"] += 1

        except Exception as exc:
            result["errors"] += 1

            print(
                f"VERIFY ERROR: {key}: {exc}"
            )

    return result


# ============================================================
# Filtering
# ============================================================

def normalize_only_pattern(
    value: str,
) -> str:
    return value.replace(
        "\\",
        "/",
    ).lstrip("/")


def filter_records(
    records: list[dict[str, Any]],
    only_patterns: list[str] | None,
) -> list[dict[str, Any]]:
    """
    Apply --only filters.

    Example:
        --only "19/ExtractedData/NOL/PMD_LOG.csv"

    Multiple --only arguments are supported.
    """
    if not only_patterns:
        return records

    normalized = [
        normalize_only_pattern(pattern)
        for pattern in only_patterns
    ]

    selected = []

    for record in records:
        relative = record["relative"]

        if any(
            fnmatch.fnmatch(
                relative,
                pattern,
            )
            for pattern in normalized
        ):
            selected.append(record)

    return selected


# ============================================================
# Synchronization
# ============================================================

def sync_project(
    project: str,
    dropbox_root: Path,
    rules: dict[str, Any],
    target: Target,
    dry_run: bool = False,
    only_patterns: list[str] | None = None,
    verify: bool = False,
    verify_hash: bool = False,
) -> dict[str, Any]:
    """
    Synchronize one project's RawData folder.

    Raw files are never overwritten.
    """
    project_config = project_rules(
        rules,
        project,
    )

    database_relative = project_config.get(
        "dropbox_path"
    )

    if not database_relative:
        raise RuntimeError(
            f"No dropbox_path configured for {project}"
        )

    database_root = (
        dropbox_root / database_relative
    )

    rawdata_root = database_root / "RawData"

    print()
    print("=" * 72)
    print(f"PROJECT: {project}")
    print("=" * 72)
    print(f"Database: {database_root}")
    print(f"RawData:  {rawdata_root}")

    # --------------------------------------------------------
    # Discovery
    # --------------------------------------------------------

    scan = scan_database_folder(
        database_root,
        project_config,
    )

    for directory in scan[
        "excluded_directories"
    ]:
        print(
            f"REFUSED BY WHITELIST: "
            f"{directory}"
        )

    for directory in scan[
        "unknown_directories"
    ]:
        print(
            f"UNKNOWN TOP-LEVEL DIRECTORY: "
            f"{directory}"
        )

    records = collect_files(
        rawdata_root,
        project_config,
    )

    print(
        f"Discovered {len(records)} files."
    )

    selected = filter_records(
        records,
        only_patterns,
    )

    print(
        f"Selected {len(selected)} files."
    )

    if only_patterns:
        print(
            "ONLY FILTERS:"
        )

        for pattern in only_patterns:
            print(
                f"  {pattern}"
            )

    # --------------------------------------------------------
    # State
    # --------------------------------------------------------

    state = load_state(project)

    patient_ids = {
        record["patient_id"]
        for record in records
        if record.get("patient_id")
    }

    state["patient_ids"] = sorted(
        patient_ids
    )

    # --------------------------------------------------------
    # Counters
    # --------------------------------------------------------

    summary = {
        "project": project,
        "dry_run": dry_run,
        "discovered": len(records),
        "selected": len(selected),
        "transferred": 0,
        "already_present": 0,
        "versioned": 0,
        "failed": 0,
        "oversize": 0,
        "unparsed": 0,
        "errors": [],
    }

    duplicate_groups: dict[
        str,
        list[dict[str, Any]],
    ] = defaultdict(list)

    # --------------------------------------------------------
    # Hash local files
    # --------------------------------------------------------

    print()
    print(
        "Calculating SHA-256 for selected files..."
    )

    for index, record in enumerate(
        selected,
        start=1,
    ):
        path = Path(
            record["path"]
        )

        try:
            digest = sha256_file(path)
            record["sha256"] = digest

            duplicate_groups[
                digest
            ].append(record)

            print(
                f"[{index}/{len(selected)}] "
                f"HASHED: {record['relative']}"
            )

        except Exception as exc:
            record["sha256"] = None

            summary["failed"] += 1

            error = {
                "path": record["path"],
                "relative": record["relative"],
                "stage": "hash",
                "error": str(exc),
            }

            summary["errors"].append(
                error
            )

            print(
                f"HASH FAILED: "
                f"{record['relative']}: "
                f"{exc}"
            )

    # --------------------------------------------------------
    # Duplicate information
    # --------------------------------------------------------

    duplicates = {
        digest: [
            item["relative"]
            for item in group
        ]
        for digest, group in duplicate_groups.items()
        if len(group) > 1
    }

    # --------------------------------------------------------
    # Upload
    # --------------------------------------------------------

    print()
    print(
        "Synchronizing files..."
    )

    for index, record in enumerate(
        selected,
        start=1,
    ):
        path = Path(
            record["path"]
        )

        relative = record[
            "relative"
        ]

        digest = record.get(
            "sha256"
        )

        print()
        print(
            f"[{index}/{len(selected)}] "
            f"{relative}"
        )

        if digest is None:
            print(
                "SKIP: no SHA-256 available."
            )
            continue

        if record.get("oversize"):
            summary["oversize"] += 1

            print(
                "WARNING: file exceeds configured "
                "max_file_mb."
            )

        if not record.get("parseable"):
            summary["unparsed"] += 1

            if project_config.get(
                "report_unparsed_files",
                True,
            ):
                print(
                    "INFO: file is not currently "
                    "parseable by CSV parser."
                )

        key = blob_key(
            project,
            record,
        )

        # ----------------------------------------------------
        # State check
        # ----------------------------------------------------

        previous = state[
            "uploaded"
        ].get(key)

        if previous:
            # Support both the old state format:
            #
            # "path": "sha256..."
            #
            # and the new format:
            #
            # "path": {
            #     "sha256": "...",
            #     "size": ...
            # }
            if isinstance(previous, str):
                previous_digest = previous
            elif isinstance(previous, dict):
                previous_digest = previous.get("sha256")
            else:
                previous_digest = None

            if previous_digest == digest:
                print(
                    f"ALREADY IN STATE: {key}"
                )

                summary[
                    "already_present"
                ] += 1

                continue

        # ----------------------------------------------------
        # Target check
        # ----------------------------------------------------

        try:
            target_exists = target.exists(
                key
            )

        except Exception as exc:
            summary["failed"] += 1

            error = {
                "path": record["path"],
                "relative": relative,
                "key": key,
                "stage": "target_exists",
                "error": str(exc),
            }

            summary["errors"].append(
                error
            )

            print(
                f"TARGET CHECK FAILED: "
                f"{exc}"
            )

            # Important:
            # Do NOT assume the target is absent.
            continue

        if target_exists:
            print(
                f"TARGET EXISTS: {key}"
            )

            try:
                target_digest = target.sha256(
                    key
                )

            except Exception as exc:
                summary["failed"] += 1

                error = {
                    "path": record["path"],
                    "relative": relative,
                    "key": key,
                    "stage": "target_hash",
                    "error": str(exc),
                }

                summary["errors"].append(
                    error
                )

                print(
                    f"TARGET HASH FAILED: "
                    f"{exc}"
                )

                continue

            if target_digest == digest:
                print(
                    "TARGET SHA-256 MATCH: "
                    "treating as already synchronized."
                )

                state[
                    "uploaded"
                ][key] = {
                    "sha256": digest,
                    "size": record["size"],
                    "relative": relative,
                    "patient_id": record.get(
                        "patient_id"
                    ),
                    "device": record.get(
                        "device"
                    ),
                }

                state[
                    "decisions"
                ][relative] = (
                    "already_present"
                )

                summary[
                    "already_present"
                ] += 1

                # Save immediately.
                if not dry_run:
                    save_state(
                        project,
                        state,
                    )

                continue

            # ------------------------------------------------
            # Same key, different content.
            # NEVER overwrite.
            # ------------------------------------------------

            print(
                "TARGET SHA-256 DIFFERENT."
            )

            try:
                key = target.versioned_key(
                    key,
                    digest,
                )

            except Exception as exc:
                summary["failed"] += 1

                error = {
                    "path": record["path"],
                    "relative": relative,
                    "key": key,
                    "stage": "version_key",
                    "error": str(exc),
                }

                summary["errors"].append(
                    error
                )

                print(
                    f"VERSION KEY FAILED: "
                    f"{exc}"
                )

                continue

            summary[
                "versioned"
            ] += 1

            print(
                f"VERSIONED TARGET: {key}"
            )

        # ----------------------------------------------------
        # Dry run
        # ----------------------------------------------------

        if dry_run:
            print(
                f"WOULD UPLOAD: {key}"
            )

            continue

        # ----------------------------------------------------
        # Actual upload
        # ----------------------------------------------------

        try:
            print(
                f"UPLOADING: {key}"
            )

            target.upload(
                path,
                key,
            )

            print(
                f"UPLOADED: {key}"
            )

            state[
                "uploaded"
            ][key] = {
                "sha256": digest,
                "size": record["size"],
                "relative": relative,
                "patient_id": record.get(
                    "patient_id"
                ),
                "device": record.get(
                    "device"
                ),
            }

            state[
                "decisions"
            ][relative] = (
                "versioned"
                if target_exists
                else "uploaded"
            )

            summary[
                "transferred"
            ] += 1

            # ------------------------------------------------
            # IMPORTANT:
            # Save after every successful upload.
            #
            # If the process is interrupted, previous
            # successful uploads are already represented in
            # sync_state.
            # ------------------------------------------------

            save_state(
                project,
                state,
            )

        except ResourceExistsError:
            # Another process may have created the blob between
            # our exists() check and upload().
            #
            # We do NOT overwrite it.
            print(
                "UPLOAD SKIPPED: target was created "
                "concurrently."
            )

            try:
                target_digest = target.sha256(
                    key
                )

                if target_digest == digest:
                    state[
                        "uploaded"
                    ][key] = {
                        "sha256": digest,
                        "size": record["size"],
                        "relative": relative,
                        "patient_id": record.get(
                            "patient_id"
                        ),
                        "device": record.get(
                            "device"
                        ),
                    }

                    state[
                        "decisions"
                    ][relative] = (
                        "already_present"
                    )

                    summary[
                        "already_present"
                    ] += 1

                    save_state(
                        project,
                        state,
                    )

                else:
                    summary[
                        "failed"
                    ] += 1

                    summary[
                        "errors"
                    ].append(
                        {
                            "path": record["path"],
                            "relative": relative,
                            "key": key,
                            "stage": (
                                "upload_resource_exists"
                            ),
                            "error": (
                                "Target exists with "
                                "different content."
                            ),
                        }
                    )

            except Exception as exc:
                summary["failed"] += 1

                summary[
                    "errors"
                ].append(
                    {
                        "path": record["path"],
                        "relative": relative,
                        "key": key,
                        "stage": (
                            "resource_exists_check"
                        ),
                        "error": str(exc),
                    }
                )

        except (
            ServiceRequestError,
            ServiceResponseError,
            TimeoutError,
            ConnectionError,
            AzureError,
        ) as exc:
            # Network/Azure failure:
            # mark the file failed and continue.
            summary["failed"] += 1

            error = {
                "path": record["path"],
                "relative": relative,
                "key": key,
                "stage": "upload",
                "error": str(exc),
                "error_type": type(
                    exc
                ).__name__,
            }

            summary[
                "errors"
            ].append(error)

            print(
                f"UPLOAD FAILED: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            print(
                "Continuing with next file."
            )

        except Exception as exc:
            # Final safety net:
            # no single file should terminate the complete
            # synchronization.
            summary["failed"] += 1

            error = {
                "path": record["path"],
                "relative": relative,
                "key": key,
                "stage": "upload",
                "error": str(exc),
                "error_type": type(
                    exc
                ).__name__,
            }

            summary[
                "errors"
            ].append(error)

            print(
                f"UPLOAD FAILED: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            print(
                "Continuing with next file."
            )

    # --------------------------------------------------------
    # Save state
    # --------------------------------------------------------

    if not dry_run:
        save_state(
            project,
            state,
        )

    # --------------------------------------------------------
    # Optional verification
    # --------------------------------------------------------

    if verify and not dry_run:
        print()
        print(
            "Verifying synchronized state..."
        )

        verification = verify_state(
            project,
            state,
            target,
            verify_hash=verify_hash,
        )

        summary[
            "verification"
        ] = verification

        print(
            f"Verified: "
            f"{verification['checked']}"
        )

        print(
            f"Missing: "
            f"{verification['missing']}"
        )

        print(
            f"Hash mismatches: "
            f"{verification['hash_mismatch']}"
        )

        print(
            f"Verification errors: "
            f"{verification['errors']}"
        )

    # --------------------------------------------------------
    # Report
    # --------------------------------------------------------

    report_directory = Path(
        "sync_reports"
    )

    report_directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    report_path = (
        report_directory
        / f"{project}_sync_report.json"
    )

    report = {
        **summary,
        "duplicates": duplicates,
        "state_file": str(
            state_path(project)
        ),
    }

    with report_path.open(
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(
            report,
            f,
            indent=2,
            ensure_ascii=False,
        )

    # --------------------------------------------------------
    # Final summary
    # --------------------------------------------------------

    print()
    print("=" * 72)
    print(f"SYNC COMPLETE: {project}")
    print("=" * 72)

    print(
        f"Discovered:       {summary['discovered']}"
    )
    print(
        f"Selected:         {summary['selected']}"
    )
    print(
        f"Transferred:      {summary['transferred']}"
    )
    print(
        f"Already present:  {summary['already_present']}"
    )
    print(
        f"Versioned:        {summary['versioned']}"
    )
    print(
        f"Failed:           {summary['failed']}"
    )
    print(
        f"Oversize:         {summary['oversize']}"
    )
    print(
        f"Unparsed:         {summary['unparsed']}"
    )
    print(
        f"Report:           {report_path}"
    )
    print(
        f"State:            {state_path(project)}"
    )

    return report


# ============================================================
# CLI
# ============================================================

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Synchronize project RawData folders "
            "from Dropbox to Azure Blob Storage."
        )
    )

    parser.add_argument(
        "--project",
        required=True,
        help=(
            "Project name, e.g. V-RAPS, IPAMS, "
            "DEXREM, SILVR, ESMONOL, PROMISES."
        ),
    )

    parser.add_argument(
        "--dropbox",
        default=r"C:\Users\katia\Dropbox",
        help=(
            "Dropbox root directory."
        ),
    )

    parser.add_argument(
        "--target",
        choices=[
            "azure",
            "local",
        ],
        default="azure",
        help=(
            "Synchronization target. "
            "Default: azure."
        ),
    )

    parser.add_argument(
        "--local-root",
        default="rehearsal_target",
        help=(
            "Root folder for --target local."
        ),
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do not upload files. "
            "Show what would happen."
        ),
    )

    parser.add_argument(
        "--only",
        action="append",
        help=(
            "Only synchronize matching RawData-relative "
            "paths. Can be supplied multiple times."
        ),
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help=(
            "Verify that state entries exist after sync."
        ),
    )

    parser.add_argument(
        "--verify-hash",
        action="store_true",
        help=(
            "Verify SHA-256 of synchronized objects. "
            "Implies --verify."
        ),
    )

    return parser


def main() -> int:
    parser = build_parser()

    args = parser.parse_args()

    if args.verify_hash:
        args.verify = True

    project = args.project

    dropbox_root = Path(
        args.dropbox
    )

    if not dropbox_root.exists():
        parser.error(
            f"Dropbox root does not exist: "
            f"{dropbox_root}"
        )

    rules = load_rules()

    target = Target(
        target=args.target,
        local_root=args.local_root,
    )

    try:
        sync_project(
            project=project,
            dropbox_root=dropbox_root,
            rules=rules,
            target=target,
            dry_run=args.dry_run,
            only_patterns=args.only,
            verify=args.verify,
            verify_hash=args.verify_hash,
        )

    except KeyboardInterrupt:
        print()
        print(
            "Interrupted by user."
        )
        return 130

    except Exception as exc:
        print()
        print(
            f"FATAL ERROR: "
            f"{type(exc).__name__}: {exc}"
        )
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(
        main()
    )