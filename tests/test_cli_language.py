"""The command line's language (ADR 0019): argparse's own words are in the catalog's template."""

from __future__ import annotations

import argparse
import ast
import inspect
import json
from pathlib import Path

from meshcore_home_gateway.catalog import read_po

ROOT = Path(__file__).resolve().parent.parent
LISTED = ROOT / "tools" / "argparse_messages.json"
TEMPLATE = ROOT / "src" / "meshcore_home_gateway" / "locale" / "meshcore-home-gateway.pot"


def argparse_texts() -> set[tuple[str, str | None]]:
    """This Python's argparse texts: the literals it passes to _() and ngettext()."""
    found: set[tuple[str, str | None]] = set()
    for node in ast.walk(ast.parse(inspect.getsource(argparse))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            count = {"_": 1, "ngettext": 2}.get(node.func.id, 0)
            texts = [
                arg.value
                for arg in node.args[:count]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if count and len(texts) == count:
                found.add((texts[0], texts[1] if count == 2 else None))
    return found


def test_every_argparse_message_is_listed() -> None:
    """A new Python may bring new words: `make translations` under it adds them (D10)."""
    listed = {(msgid, plural) for msgid, plural in json.loads(LISTED.read_text(encoding="utf-8"))}
    assert argparse_texts() <= listed, "run make translations with this Python"
    _, template = read_po(TEMPLATE.read_text(encoding="utf-8"), TEMPLATE.name)
    assert listed <= {(entry.msgid, entry.msgid_plural) for entry in template}
    assert {"usage: ", "positional arguments", "show this help message and exit"} <= {
        msgid for msgid, _ in listed
    }
