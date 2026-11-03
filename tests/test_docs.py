"""Documentation: every relative link points to an existing file and heading.

External links are not fetched (the tests run offline); the documents cite them as sources.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType

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


# CODE-006: the code names the document that holds a rule, as `design §6.6` (or
# `docs/design.md §6.6`) and `ADR 0025`; the numbered decisions of a working document that is
# not in the repository leave a reader nothing to open.
CODE = sorted(
    path
    for folder in ("src", "tests", "tools")
    for path in (ROOT / folder).rglob("*.py")
    if "firmware" not in path.relative_to(ROOT).parts  # the C++ sources' citations are other
)
DESIGN_SECTION = re.compile(r"design(?:\.md)? ?§ ?(\d+(?:\.\d+)*)")
DECISION = re.compile(r"\bADR (\d{4})\b")
# a working document's own numbering: a design or slice named by a number and a letter, its
# open points, and its decisions, findings and goals (D, F or G and a number) as a reference
WORKING_DOCUMENT = re.compile(
    r"\bdesign \d+[a-z]\b|\bslice \d+[a-z]\b|\b\d+[a-z]'s open point"
    r"|(?:\(|, |; |design |# |\"\"\")[DFG]\d{1,2}\b(?![ \t][0-9A-F]{2}\b)"
)


@pytest.mark.parametrize("module", CODE, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_design_references_in_the_code_resolve(module: Path) -> None:
    text = module.read_text(encoding="utf-8")
    sections = {
        heading.split(" ", 1)[0]
        for heading in HEADING.findall((ROOT / "docs" / "design.md").read_text(encoding="utf-8"))
    }
    decisions = {path.name[:4] for path in (ROOT / "docs" / "decisions").glob("[0-9]*.md")}
    unresolved = [f"design §{number}" for number in DESIGN_SECTION.findall(text)
                  if number not in sections]  # fmt: skip
    unresolved += [f"ADR {number}" for number in DECISION.findall(text) if number not in decisions]
    unresolved += WORKING_DOCUMENT.findall(text)
    assert not unresolved, unresolved


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


def _load_index_tool() -> ModuleType:
    path = ROOT / "tools" / "platform_notes_index.py"
    spec = importlib.util.spec_from_file_location("platform_notes_index", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["platform_notes_index"] = module
    spec.loader.exec_module(module)
    return module


def test_the_index_of_the_platform_notes_lists_every_fact() -> None:
    """Run `python3 tools/platform_notes_index.py` after adding or changing a fact."""
    tool = _load_index_tool()
    text = tool.NOTES.read_text(encoding="utf-8")
    assert tool.with_index(text) == text
    leads = [lead for _, section in tool.facts(text) for lead in section]
    assert len(leads) > 100
    assert all(f"\n* {lead}\n" in text for lead in leads)
