"""Validate a release tag and print its checked-in changelog entry."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RELEASE_TAG = re.compile(r"v((?:0|[1-9][0-9]{0,2})\.(?:0|[1-9][0-9]{0,2})\.(?:0|[1-9][0-9]{0,2}))")


def release_notes(tag: str, version: str, changelog: str) -> str:
    match = RELEASE_TAG.fullmatch(tag)
    if not match or match[1] != version or version == "0.0.0":
        raise ValueError(f"Expected a stable release tag matching the source version v{version}")
    pattern = rf"^## \[{re.escape(version)}\][^\n]*\n(.*?)(?=^## |\Z)"
    entries = re.findall(pattern, changelog, re.MULTILINE | re.DOTALL)
    if len(entries) != 1 or not entries[0].strip():
        raise ValueError(f"Expected one non-empty CHANGELOG.md entry for {version}")
    return entries[0].strip()


if __name__ == "__main__":
    sys.path.insert(0, str(ROOT / "src"))
    from localplaud import __version__

    try:
        print(release_notes(sys.argv[1], __version__, (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")))
    except (IndexError, ValueError) as exc:
        sys.exit(str(exc))
