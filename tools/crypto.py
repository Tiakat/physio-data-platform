"""Encryption helpers for the physio data pipeline.

Every byte stored in Azure data containers (``rawdata``, ``processed``) is
encrypted with Fernet (AES-128-CBC + HMAC-SHA256, via the ``cryptography``
package).  The key lives ONLY in:

* the ``PIPELINE_DATA_KEY`` GitHub Actions secret (CI), and
* the local ``.env`` file (local runs).

Azure never sees plaintext.  Only the pipeline scripts can read the data back.

The public website feed (``reports/``) stays plaintext — it contains only
de-identified aggregates (k=5 suppressed).

Key generation (run locally, keep the output secret)::

    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

Production note: for funded production, move the key into Azure Key Vault and
consider AES-256-GCM envelope encryption.  Fernet is the pragmatic choice for
the free prototype.
"""

from __future__ import annotations

import os

from cryptography.fernet import Fernet, InvalidToken


class MissingDataKeyError(RuntimeError):
    """Raised when PIPELINE_DATA_KEY is not set."""


def get_fernet() -> Fernet:
    """Return a Fernet instance built from the PIPELINE_DATA_KEY env var."""
    raw = os.environ.get("PIPELINE_DATA_KEY", "").strip()
    if not raw:
        raise MissingDataKeyError(
            "PIPELINE_DATA_KEY is not set. Generate one with "
            "`python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\"` and add it as a GitHub "
            "Actions secret (and to your local .env for local runs)."
        )
    try:
        return Fernet(raw.encode("ascii"))
    except Exception as exc:  # invalid key material
        raise MissingDataKeyError(f"PIPELINE_DATA_KEY is not a valid Fernet key: {exc}") from exc


def encrypt_bytes(data: bytes) -> bytes:
    """Encrypt bytes; returns the Fernet token."""
    return get_fernet().encrypt(data)


def decrypt_bytes(token: bytes) -> bytes:
    """Decrypt a Fernet token; raises InvalidToken on tampering/wrong key."""
    return get_fernet().decrypt(token)


def is_encrypted_blob(name: str) -> bool:
    return name.endswith(".enc")
