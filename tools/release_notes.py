"""Print the CHANGELOG.md section of one version: the notes of its GitHub release.

    python3 tools/release_notes.py 0.1.0 [--changelog CHANGELOG.md]

The section must exist with a date, as Keep a Changelog writes it (`## [0.1.0] - 2026-11-02`), so
a release cannot go out without its changelog entry. `make release` uses it.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"
_NEXT_SECTION = re.compile(r"^## ", re.MULTILINE)
_LINK_DEFINITIONS = re.compile(r"(?:^\[[^\]]+\]: \S+\s*$\n?)+\Z", re.MULTILINE)


def section(text: str, version: str) -> str:
    """The body of the version's section, without its heading and trailing link definitions."""
    heading = rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}[ \t]*$"
    match = re.search(heading, text, re.MULTILINE)
    if match is None:
        raise ValueError(f"CHANGELOG.md has no section '## [{version}] - YYYY-MM-DD'")
    following = _NEXT_SECTION.search(text, match.end())
    body = text[match.end() : following.start() if following else len(text)]
    body = _LINK_DEFINITIONS.sub("", body.strip() + "\n").strip()
    if not body:
        raise ValueError(f"the CHANGELOG.md section of {version} is empty")
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("version", help="version without the leading v, e.g. 0.1.0")
    parser.add_argument("--changelog", type=Path, default=CHANGELOG)
    args = parser.parse_args(argv)
    try:
        print(section(args.changelog.read_text(encoding="utf-8"), args.version))
    except ValueError as exc:
        raise SystemExit(f"release_notes: {exc}") from None
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
