#!/usr/bin/env python3
"""Pretty-print files discovered by RepoWalker for a given repo root.
Usage: python scripts/pretty_walk.py [path/to/repo]
"""
from pathlib import Path
import sys
import json
from pprint import pprint

sys.path.insert(0, "src")
from deplar.scanner.walker import RepoWalker

import argparse

parser = argparse.ArgumentParser(description="Pretty-print RepoWalker output")
parser.add_argument("root", nargs="?", default="tests/fixtures/sample_repo", help="Repository root to scan")
args = parser.parse_args()

root = Path(args.root)
print(f"Scanning: {root.resolve()}")

rw = RepoWalker(root)
fm = rw.walk()

# Build a serializable structure: language -> list of relative paths
out = {}
for lang, files in fm.files.items():
    out[lang] = [str(p.relative_to(fm.root)) for p in files]

pprint(out)

# Also print totals
print("Totals:")
print("  languages:", len(out))
print("  files:", fm.total())
