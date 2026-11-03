"""The index of the facts at the top of docs/platform-notes.md: one line per fact, the bold lead
of each bullet, by section. `python3 tools/platform_notes_index.py` writes it again after a fact
was added or changed; tests/test_docs.py fails while it is out of date."""

from __future__ import annotations

import re
import sys
from pathlib import Path

NOTES = Path(__file__).resolve().parent.parent / "docs" / "platform-notes.md"
START, END = "<!-- index of the facts -->\n", "<!-- end of the index -->\n"
BEFORE = "\n## How we verify"


def _anchor(title: str) -> str:
    return re.sub(r"[^\w\- ]", "", title.strip().lower()).replace(" ", "-")


def _without_index(text: str) -> str:
    if START not in text:
        return text
    before, _, rest = text.partition(START)
    return before + rest.partition(END)[2]


def facts(text: str) -> list[tuple[str, list[str]]]:
    """Each section's title and the bold leads of its facts, in order."""
    sections: list[tuple[str, list[str]]] = []
    lines = _without_index(text).split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("## "):
            sections.append((line[3:].strip(), []))
        elif line.startswith("* **") and sections:
            lead = line[4:]
            while "**" not in lead and i + 1 < len(lines):
                i += 1
                lead += " " + lines[i].strip()
            sections[-1][1].append(re.sub(r"\s+", " ", lead.split("**")[0]).strip())
        i += 1
    return sections


def index(text: str) -> str:
    out = [START, "## Index of the facts\n", "\nOne line per fact, by section.\n"]
    for title, leads in facts(text):
        if leads:
            out.append(f"\n**[{title}](#{_anchor(title)})**\n\n")
            out += [f"* {lead}\n" for lead in leads]
    return "".join([*out, "\n", END])


def with_index(text: str) -> str:
    """The notes with their index written before the section on how facts are verified."""
    plain = _without_index(text)
    head, marker, tail = plain.partition(BEFORE)
    if not marker:
        raise ValueError("docs/platform-notes.md has no section 'How we verify'")
    return head.rstrip("\n") + "\n\n" + index(plain) + marker + tail


def main() -> int:
    text = NOTES.read_text(encoding="utf-8")
    updated = with_index(text)
    if updated != text:
        NOTES.write_text(updated, encoding="utf-8")
        print(f"{NOTES.name}: index written", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
