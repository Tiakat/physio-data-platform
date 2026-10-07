#!/usr/bin/env python3
"""Inventory the real per-project directory layouts under dropbox_enc/.

Walks the encrypted tree, strips the .enc suffix, and prints each project's
directory structure (dirs only, max depth 4) with file counts. Used to write
correct template-fix rules from real data instead of guesses.

Usage (on the cluster):
    ~/cryptenv/bin/python ~/physio-data-platform/alliance/inventory_layout.py > ~/layout.txt
Then paste ~/layout.txt into the chat.
"""
import os
from collections import defaultdict

ENC = "/project/def-molo/katia5/dropbox_enc"
MAX_DEPTH = 4


def main() -> None:
    projects = sorted(d for d in os.listdir(ENC)
                      if os.path.isdir(os.path.join(ENC, d)))
    for proj in projects:
        print(f"=== {proj} ===")
        base = os.path.join(ENC, proj)
        # dir path -> file count (files directly inside)
        counts: dict[str, int] = defaultdict(int)
        dirs: set[str] = set()
        for root, _ds, files in os.walk(base):
            rel = os.path.relpath(root, base)
            if rel == ".":
                rel = ""
            depth = 0 if not rel else rel.count(os.sep) + 1
            if depth <= MAX_DEPTH:
                dirs.add(rel)
                counts[rel] += len(files)
        for d in sorted(dirs):
            depth = 0 if not d else d.count(os.sep) + 1
            if depth > MAX_DEPTH:
                continue
            indent = "  " * depth
            name = "(root)" if not d else os.path.basename(d)
            print(f"{indent}{name}/  [{counts[d]} files]")
        print()


if __name__ == "__main__":
    main()
