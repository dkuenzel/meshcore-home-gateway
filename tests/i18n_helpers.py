"""Catalogs for tests: real ones compiled in memory, the pseudo-language `xx` that marks every text
of the template, and a hostile one that breaks every text a translator can break.

`speaking(monkeypatch, translator)` offers a test catalog as if it were shipped: `LANGUAGE=xx` and
`[homeassistant] language = "xx"` then use it. It reaches only this process (the code under test
looks `i18n.LANGUAGES` and `i18n.translator` up at call time, docs/development.md).
"""

from __future__ import annotations

import gettext
import io
from collections.abc import Callable, Iterable
from pathlib import Path
from types import MappingProxyType

import pytest

from meshcore_home_gateway import i18n
from meshcore_home_gateway.catalog import Entry, read_po, write_mo

TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "meshcore_home_gateway"
    / "locale"
    / "meshcore-home-gateway.pot"
)
GERMANIC = "nplurals=2; plural=(n != 1);"


def compiled(
    language: str, entries: Iterable[Entry], plural_forms: str = GERMANIC
) -> i18n.Translator:
    """A translator of these entries, compiled as the package's catalogs are (ADR 0027)."""
    header = {
        "Language": language,
        "Content-Type": "text/plain; charset=UTF-8",
        "Plural-Forms": plural_forms,
    }
    mo = write_mo(header, entries)
    return i18n.Translator(language, gettext.GNUTranslations(io.BytesIO(mo)))


def template() -> list[Entry]:
    return read_po(TEMPLATE.read_text(encoding="utf-8"), TEMPLATE.name)[1]


def _marked(text: str) -> str:
    """‹text›, a final line break outside: fields and % directives stay as they are."""
    body, newline = (text[:-1], "\n") if text.endswith("\n") else (text, "")
    return f"‹{body}›{newline}"


def pseudo(language: str = "xx") -> i18n.Translator:
    """Every text of the template as ‹text›, plural forms alike: a text shown without the marks
    did not pass through the catalog."""
    return compiled(language, _translated(_marked))


def hostile(language: str = "xx") -> i18n.Translator:
    """Every text of the template with ' 50 %' and a lone '{' added: the guard must show English
    (D7) wherever argparse formats a text or values are filled in."""
    return compiled(language, _translated(lambda text: f"{text} 50 % {{"))


def _translated(translate: Callable[[str], str]) -> list[Entry]:
    return [
        Entry(
            entry.msgid, tuple(map(translate, _english(entry))), entry.msgctxt, entry.msgid_plural
        )
        for entry in template()
    ]


def _english(entry: Entry) -> tuple[str, ...]:
    return (entry.msgid,) if entry.msgid_plural is None else (entry.msgid, entry.msgid_plural)


def speaking(
    monkeypatch: pytest.MonkeyPatch, translator: i18n.Translator | None = None
) -> i18n.Translator:
    """Offer `translator` (the pseudo-language by default) as a language of this version."""
    offered = translator or pseudo()
    original = i18n.translator
    monkeypatch.setattr(
        i18n,
        "LANGUAGES",
        MappingProxyType({**i18n.LANGUAGES, offered.language: "Pseudo"}),
    )

    def translator_of(language: str) -> i18n.Translator:
        return offered if language == offered.language else original(language)

    monkeypatch.setattr(i18n, "translator", translator_of)
    return offered
