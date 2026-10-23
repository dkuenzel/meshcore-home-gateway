"""Documentation: every relative link points to an existing file and heading.

External links are not fetched (the tests run offline); the documents cite them as sources.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_OUTPUT = {"build", "dist"}  # generated files, e.g. dist/release-notes.md of `make release`
DOCUMENTS = sorted(
    p
    for p in ROOT.rglob("*.md")
    if not any(part.startswith(".") or part in BUILD_OUTPUT for part in p.relative_to(ROOT).parts)
)
CODE_BLOCK = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
LINK = re.compile(r"\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6} (.+)$", re.MULTILINE)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation removed, spaces to hyphens."""
    text = heading.strip().lower().replace("`", "")
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def headings(path: Path) -> set[str]:
    text = CODE_BLOCK.sub("", path.read_text(encoding="utf-8"))
    return {slug(match) for match in HEADING.findall(text)}


def test_the_documentation_set_is_found() -> None:
    names = {p.relative_to(ROOT).as_posix() for p in DOCUMENTS}
    assert {"README.md", "docs/design.md", "docs/decisions/README.md"} <= names


@pytest.mark.parametrize("document", DOCUMENTS, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_relative_links_resolve(document: Path) -> None:
    text = CODE_BLOCK.sub("", document.read_text(encoding="utf-8"))
    broken = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        path, _, anchor = target.partition("#")
        destination = (document.parent / path).resolve() if path else document
        if not destination.exists():
            broken.append(f"{target}: no such file")
        elif anchor and destination.suffix == ".md" and anchor not in headings(destination):
            broken.append(f"{target}: no such heading")
    assert not broken, broken


def test_attestation_checks_name_the_release_workflow_and_the_tag() -> None:
    """`--repo` alone accepts an attestation from any workflow and ref of the repository."""
    commands = [
        line
        for path in DOCUMENTS
        for line in re.sub(r"\\\n\s*", " ", path.read_text(encoding="utf-8")).splitlines()
        if "gh attestation verify" in line and "--repo" in line
    ]
    assert commands
    for command in commands:
        assert "--signer-workflow" in command, command
        assert "--source-ref" in command, command
