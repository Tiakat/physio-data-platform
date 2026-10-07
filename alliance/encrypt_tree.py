#!/usr/bin/env python3
"""Encrypt every file under SRC into DST, preserving relative paths.

Each file becomes <relative path>.enc (Fernet, via tools/crypto.py).
Skips files already encrypted (resume-safe). Needs PIPELINE_DATA_KEY set.

Usage (from the repo root on the cluster):
    export PIPELINE_DATA_KEY="<key from your .env — paste here, never in chat>"
    python3 alliance/encrypt_tree.py /project/def-molo/katia5/dropbox \\
        /project/def-molo/katia5/dropbox_enc
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from tools.crypto import encrypt_bytes, get_fernet  # noqa: E402


def main() -> None:
    if len(sys.argv) != 3:
        sys.exit("usage: encrypt_tree.py SRC DST")
    src, dst = sys.argv[1], sys.argv[2]
    get_fernet()  # fails fast if PIPELINE_DATA_KEY is missing/invalid
    n = skipped = 0
    for root, _dirs, files in os.walk(src):
        for f in files:
            s = os.path.join(root, f)
            d = os.path.join(dst, os.path.relpath(s, src) + ".enc")
            if os.path.exists(d):
                skipped += 1
                continue
            os.makedirs(os.path.dirname(d), exist_ok=True)
            with open(s, "rb") as fh:
                data = fh.read()
            with open(d, "wb") as fh:
                fh.write(encrypt_bytes(data))
            n += 1
            if n % 500 == 0:
                print(f"encrypted {n} files...", flush=True)
    print(f"done: {n} files encrypted, {skipped} already done")


if __name__ == "__main__":
    main()
