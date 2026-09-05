"""
Persistent SHA-256 index for processed files.

Development implementation.
The production version will eventually use PostgreSQL.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


INDEX_PATH = Path("sync_state") / "hash_index.json"


class HashIndex:
    """Simple persistent SHA-256 index."""

    def __init__(self, path: Path = INDEX_PATH):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        self._load()

    def _load(self) -> None:
        """Load the index if it already exists."""

        if not self.path.exists():
            return

        with self.path.open(
            "r",
            encoding="utf-8",
        ) as f:
            self.data = json.load(f)

    def _save(self) -> None:
        """Save the index to disk."""

        self.path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        with self.path.open(
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(
                self.data,
                f,
                indent=2,
                ensure_ascii=False,
            )

    def find(
        self,
        sha256: str,
    ) -> Optional[dict]:
        """Return the record associated with a SHA-256 hash."""

        return self.data.get(sha256)

    def contains(
        self,
        sha256: str,
    ) -> bool:
        """Return True if the hash is already indexed."""

        return sha256 in self.data

    def register(
        self,
        sha256: str,
        blob_key: str,
        size_bytes: int,
    ) -> dict:
        """Register a newly processed file."""

        record = {
            "sha256": sha256,
            "blob_key": blob_key,
            "size_bytes": size_bytes,
        }

        self.data[sha256] = record
        self._save()

        return record