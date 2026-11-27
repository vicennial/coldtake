#!/usr/bin/env python3
"""Extract release notes for version X.Y.Z from CHANGELOG.md."""

from __future__ import annotations

import re
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: release_notes.py <version>", file=sys.stderr)
        return 2
    version = sys.argv[1]
    text = Path("CHANGELOG.md").read_text(encoding="utf-8")
    pat = re.compile(rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## \[|\Z)", re.M | re.S)
    m = pat.search(text)
    if not m or not m.group(1).strip():
        print(f"No CHANGELOG.md section found for version {version}", file=sys.stderr)
        return 1
    print(m.group(1).strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
