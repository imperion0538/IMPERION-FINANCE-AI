#!/usr/bin/env python3
"""Release metadata consistency: VERSION file, package version and README must agree (fails closed)."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    config = (ROOT / "imperion" / "config.py").read_text(encoding="utf-8")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    checks = [
        ("VERSION is semver", bool(re.fullmatch(r"\d+\.\d+\.\d+", version))),
        ("config.VERSION == VERSION", bool(re.search(rf'^VERSION\s*=\s*"{re.escape(version)}"', config, re.M))),
        ("README mentions the version", version in readme),
        ("CHANGELOG has an entry", f"## {version}" in changelog),
    ]
    for name, ok in checks:
        print(("PASS " if ok else "FAIL ") + name)
    failed = [n for n, ok in checks if not ok]
    print("RELEASE_CHECK=" + ("PASS" if not failed else f"FAIL ({len(failed)})"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
