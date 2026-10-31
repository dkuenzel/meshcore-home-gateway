"""Translations (ADR 0019, ADR 0027): gettext catalogs compiled in memory from the PO files in
locale/ when a language is first used. The command line follows the environment, the names sent
to Home Assistant follow [homeassistant] language. Logs, machine output and every MQTT value stay
English (docs/design.md §6.12).

Callers look `LANGUAGES`, `translator` and `choose` up through this module when they need them
(`i18n.translator(…)`), never by importing the names: the tests offer languages this way."""

from __future__ import annotations

import collections
import functools
import gettext
import io
import logging
import re
import string
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from meshcore_home_gateway import catalog
from meshcore_home_gateway.texts import Text, fill

if TYPE_CHECKING:
    from importlib.resources.abc import Traversable

log = logging.getLogger(__name__)

# en is the source; every other language has complete catalogs (tests/test_translations.py)
LANGUAGES: Final[Mapping[str, str]] = MappingProxyType({"en": "English"})
HOME_ASSISTANT: Final = "home assistant"  # msgctxt of every name sent to Home Assistant
DASHBOARD: Final = "dashboard"  # msgctxt reserved for the dashboards (ADR 0021)
CATALOGS: Traversable = resources.files(__package__ or __name__) / "locale"

_VARIABLES: Final = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG")  # gettext.find's order
# a %-directive as argparse reads it: %s, %(name)s, %-5.2f, %%; and a lone %
_PERCENT: Final = re.compile(r"%(?:\([^)]*\))?[-#0 +]*(?:\*|\d+)?(?:\.(?:\*|\d+))?[a-zA-Z%]|%")
# what str.format_map raises for a field the values do not fit
_FORMAT_ERRORS: Final = (KeyError, IndexError, ValueError, AttributeError, TypeError)


def N_(msgid: str) -> str:  # noqa: N802 - gettext's name for a text translated where it is shown
    """Mark a text for the template; it is translated where it is shown."""
    return msgid


def N_ha(msgid: str) -> str:  # noqa: N802
    """N_ for a Home Assistant name (msgctxt "home assistant")."""
    return msgid


def requested(environ: Mapping[str, str]) -> list[str]:
    """The languages asked for, in the order of Python's gettext.find: the first of LANGUAGE,
    LC_ALL, LC_MESSAGES and LANG that is set and not empty; LANGUAGE may list several, separated
    by ':'. 'de_AT.UTF-8@euro' asks for 'de'; C, POSIX and C.<codeset> end the list. No
    locale.normalize: the same on every Python."""
    value = next((environ[name] for name in _VARIABLES if environ.get(name)), "")
    languages = []
    for item in value.split(":"):
        code = re.split(r"[.@]", item, maxsplit=1)[0]
        if code in {"C", "POSIX"}:
            break
        if language := code.partition("_")[0].lower():
            languages.append(language)
    return languages


def choose(environ: Mapping[str, str]) -> str:
    """The first requested language that LANGUAGES lists (en included), else 'en'."""
    return next((code for code in requested(environ) if code in LANGUAGES), "en")


def directives(text: str) -> collections.Counter[str]:
    """The %-directives of a text that argparse %-formats, as a multiset."""
    return collections.Counter(_PERCENT.findall(text))


@dataclass(frozen=True, slots=True)
class Translator:
    """One language's catalog; English where it has no usable translation (D7)."""

    language: str
    catalog: gettext.NullTranslations

    def text(self, msgid: str, /, **values: object) -> str:
        return self._show(None, msgid, None, 1, values)

    def plural(self, singular: str, plural: str, n: int, /, **values: object) -> str:
        """The form for n; {count} is n unless given."""
        return self._show(None, singular, plural, n, {"count": n, **values})

    def pgettext(self, context: str, msgid: str, /, **values: object) -> str:
        return self._show(context, msgid, None, 1, values)

    def ha_name(self, msgid: str, /, **values: object) -> str:
        """A name sent to Home Assistant."""
        return self.pgettext(HOME_ASSISTANT, msgid, **values)

    def render(self, text: object) -> str:
        """A Text in this language, nested Texts too; anything else as str() gives it."""
        if isinstance(text, Text):
            return self.text(text.msgid, **text.values)
        return str(text)

    def argparse_text(self, msgid: str, /) -> str:
        """A text argparse %-formats (help, description, argparse's own words): the translation
        only when its %-directives equal the msgid's; never filled (D7)."""
        return _guarded(msgid, self._lookup(None, msgid, None, 1))

    def argparse_words(self) -> tuple[Callable[[str], str], Callable[[str, str, int], str]]:
        """The guarded gettext and ngettext for argparse's module attributes (no filling)."""

        def ngettext(singular: str, plural: str, n: int) -> str:
            english = singular if n == 1 else plural
            return _guarded(english, self._lookup(None, singular, plural, n))

        return self.argparse_text, ngettext

    def _lookup(self, context: str | None, msgid: str, plural: str | None, n: int) -> str:
        """The catalog's text, or the English form: every lookup passes here (the tests' strict
        catalog replaces it, tests/conftest.py)."""
        if plural is None:
            if context is None:
                return self.catalog.gettext(msgid)
            return self.catalog.pgettext(context, msgid)
        if context is None:
            return self.catalog.ngettext(msgid, plural, n)
        return self.catalog.npgettext(context, msgid, plural, n)

    def _show(
        self,
        context: str | None,
        msgid: str,
        plural: str | None,
        n: int,
        values: Mapping[str, object],
    ) -> str:
        """The translation filled with the values, if it fits them (_fits); else the English.
        Without values the text is returned unfilled."""
        english = msgid if plural is None or n == 1 else plural
        chosen = self._lookup(context, msgid, plural, n)
        if chosen != english and not self._fits(chosen, (msgid, plural or ""), values):
            chosen = english
        return fill(chosen, values, show=self.render) if values else chosen

    def _fits(self, text: str, english: tuple[str, ...], values: Mapping[str, object]) -> bool:
        """A translation names only fields of its English texts (no {alias.upper}) and formats
        with the given values (D7)."""
        try:
            fill(text, values, show=self.render)
            names = _field_names(text)
        except _FORMAT_ERRORS:
            return False
        return names <= set().union(*map(_field_names, english))


def _guarded(english: str, chosen: str) -> str:
    return chosen if directives(chosen) == directives(english) else english


def _field_names(text: str) -> set[str]:
    """The fields of a format string as it names them: value, alias.upper, roles[0]."""
    return {name for _, name, _, _ in string.Formatter().parse(text) if name is not None}


ENGLISH: Final = Translator("en", gettext.NullTranslations())


@functools.cache
def translator(language: str) -> Translator:
    """ENGLISH for 'en'; otherwise the catalog compiled from locale/<language>.po, once per
    process. A language not in LANGUAGES, a missing or unreadable file: ENGLISH and one
    warning in the log."""
    if language == "en":
        return ENGLISH
    if language not in LANGUAGES:
        log.warning("no catalog for %s: not one of %s; English", language, ", ".join(LANGUAGES))
        return ENGLISH
    name = f"{language}.po"
    try:
        header, entries = catalog.read_po((CATALOGS / name).read_text(encoding="utf-8"), name)
        mo = catalog.write_mo(header, entries)
        compiled = gettext.GNUTranslations(io.BytesIO(mo))
    except (OSError, ValueError) as exc:  # ValueError: UnicodeError, PoError, Plural-Forms
        log.warning("no catalog for %s (%s): English", language, exc)
        return ENGLISH
    return Translator(language, compiled)
