"""What the commands share: the exit codes, the default paths, the language of the texts
(tr, tr_help, trn) and texts made safe for a terminal."""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import json
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from meshcore_home_gateway import i18n

EX_OK, EX_FAILURE, EX_USAGE, EX_CONFIG = 0, 1, 2, 78


DEFAULT_CONFIG = Path("/etc/meshcore-home-gateway/config.toml")


DEFAULT_STATE_DIR = Path("/var/lib/meshcore-home-gateway")


PREFIX = "meshcore-home-gateway: "  # before a message, never inside one: the installer strips it


@dataclasses.dataclass
class _Language:
    """The language of the command line's texts: one per process, so that texts made in paho's
    network thread follow it too."""

    translator: i18n.Translator


_LANGUAGE = _Language(i18n.ENGLISH)  # main() sets it for one call; English outside main()


_ARGPARSE_WORDS = ("_", "ngettext")  # argparse's gettext functions, which it reads as globals


@contextlib.contextmanager
def _speaking(translator: i18n.Translator) -> Iterator[None]:
    """tr(), tr_help(), trn() and argparse's own words in this language until the block ends,
    also when argparse ends it with SystemExit. A Python whose argparse lacks the module
    attributes keeps argparse's words English (ADR 0019)."""
    names = vars(argparse)
    saved = {name: names[name] for name in _ARGPARSE_WORDS if name in names}
    before, _LANGUAGE.translator = _LANGUAGE.translator, translator
    if len(saved) == len(_ARGPARSE_WORDS):
        words, plurals = translator.argparse_words()
        names.update(_=words, ngettext=plurals)
    try:
        yield
    finally:
        _LANGUAGE.translator = before
        names.update(saved)


def tr(msgid: str, /, **values: object) -> str:
    """A text for people in the command line's language, with its values; English where the
    catalog has no usable translation."""
    return _LANGUAGE.translator.text(msgid, **values)


def tr_help(msgid: str, /) -> str:
    """A help or description text: argparse %-formats it, so it is never filled, and its
    translation is used only with the %-directives of the English text."""
    return _LANGUAGE.translator.argparse_text(msgid)


def trn(singular: str, plural: str, n: int, /, **values: object) -> str:
    """The form of a text for the count n, which fills {count}."""
    return _LANGUAGE.translator.plural(singular, plural, n, **values)


def tr_text(text: object, /) -> str:
    """A Text that code outside the command line made (a warning) in the command line's language."""
    return _LANGUAGE.translator.render(text)


def _safe_text(text: str) -> str:
    """For tab-separated lists and dialogs: control characters (tabs, line breaks, terminal
    escapes), format characters (direction overrides) and Unicode line separators become
    spaces."""
    return "".join(" " if unicodedata.category(ch) in _UNSAFE else ch for ch in text)


def _json_text(value: Any, indent: int | None = None) -> str:
    """JSON for a terminal: readable letters stay, but the characters `_safe_text` replaces are
    written as \\u escapes, so texts from the mesh cannot steer the terminal. Still valid JSON:
    json.dumps escapes C0 controls itself, so a raw line break is indentation, and the rest
    only occurs inside strings."""
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    return "".join(
        _escaped(ch) if ch != "\n" and unicodedata.category(ch) in _UNSAFE else ch for ch in text
    )


def _escaped(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000  # outside the basic plane: a UTF-16 surrogate pair, as JSON requires
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


_UNSAFE = frozenset({"Cc", "Cf", "Zl", "Zp"})
