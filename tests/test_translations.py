"""The translation files (ADR 0019, ADR 0027) follow the code: `make translations` writes them in
one canonical form, and these tests fail until it has run (like `make interface`)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()
PROJECT = tool.Project(ROOT)


def test_templates_are_current() -> None:
    assert tool.outdated_template(PROJECT) == []


def test_catalogs_follow_their_templates() -> None:
    assert tool.unmerged(PROJECT) == []


def test_catalogs_are_canonical() -> None:
    files = [
        PROJECT.template,
        *PROJECT.catalogs(),
        PROJECT.installer_template,
        *PROJECT.installer_catalogs(),
    ]
    findings = [
        finding
        for path in files
        for finding in tool.canonical(tool.label(PROJECT, path), path.read_text(encoding="utf-8"))
    ]
    assert findings == []


def test_packs_follow_their_catalogs() -> None:
    """The installer's language packs are written from its catalogs."""
    assert tool.stale_packs(PROJECT) == []


def test_the_installer_pins_its_packs() -> None:
    """The installer offers the languages of i18n.LANGUAGES and checks each pack it loads against
    the digest that `make translations` wrote into it."""
    assert tool.stale_pins(PROJECT, tool.i18n.LANGUAGES) == []


def test_the_template_holds_argparses_texts() -> None:
    """The template exists before any catalog: argparse's own words are its first texts."""
    _, entries = tool.read_po(PROJECT.template.read_text(encoding="utf-8"))
    assert ("usage: ", None) in {(entry.msgid, entry.msgid_plural) for entry in entries}


def test_the_installer_template_holds_its_dialogs_and_errors() -> None:
    _, entries = tool.read_po(PROJECT.installer_template.read_text(encoding="utf-8"))
    msgids = {entry.msgid for entry in entries}
    assert {"Error: {message}", "Container ID", "invalid settings: {reason}"} <= msgids
    assert all(not entry.references for entry in entries)  # one file: none


def test_the_template_holds_the_dashboards_texts() -> None:
    """Every text of the dashboard's template, in the context of the dashboard (ADR 0019: one
    catalog per language)."""
    _, entries = tool.read_po(PROJECT.template.read_text(encoding="utf-8"))
    texts = {entry.msgid: entry for entry in entries if entry.msgctxt == "dashboard"}
    assert {"Mesh", "Overview", "{n} min ago", "request refused"} <= set(texts)
    assert {entry.references for entry in texts.values()} == {
        ("src/meshcore_home_gateway/dashboard.yaml",)
    }
