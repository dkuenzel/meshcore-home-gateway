"""The runtime of the translations (ADR 0019, ADR 0027): the language asked for, the catalogs
compiled from the package's PO files, and the guard that keeps a broken translation harmless."""

from __future__ import annotations

import importlib
import logging
import os
import sys
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from types import MappingProxyType

import pytest

from meshcore_home_gateway import i18n
from meshcore_home_gateway.catalog import Entry
from meshcore_home_gateway.texts import Text, TextError
from tests.i18n_helpers import compiled, hostile, pseudo, speaking

FRENCH = "nplurals=2; plural=(n > 1);"
POLISH = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"


@pytest.mark.parametrize(
    ("environ", "languages"),
    [
        ({"LANGUAGE": "pl:de", "LANG": "fr_FR.UTF-8"}, ["pl", "de"]),
        ({"LANGUAGE": "", "LC_ALL": "fr_FR.UTF-8", "LANG": "de_DE.UTF-8"}, ["fr"]),
        ({"LC_ALL": "C.UTF-8", "LANGUAGE": "de"}, ["de"]),  # Python reads LANGUAGE first
        ({"LC_ALL": "nl_NL.UTF-8", "LC_MESSAGES": "fr_FR", "LANG": "de_DE"}, ["nl"]),
        ({"LC_MESSAGES": "fr_FR", "LANG": "de_DE"}, ["fr"]),
        ({"LANG": "de_AT.UTF-8@euro"}, ["de"]),
        ({"LANG": "DE_de"}, ["de"]),
        ({"LANG": "C"}, []),
        ({"LANG": "POSIX"}, []),
        ({"LANG": "C.UTF-8"}, []),
        ({"LC_ALL": "C.UTF-8", "LANG": "de_DE.UTF-8"}, []),
        ({"LANGUAGE": "de:C:fr"}, ["de"]),
        ({"LANGUAGE": ":pt_BR::de"}, ["pt", "de"]),
        ({}, []),
    ],
)
def test_requested_follows_pythons_order(environ: dict[str, str], languages: list[str]) -> None:
    """The order of Python's gettext.find, without locale.normalize: alike on 3.11 to 3.14."""
    assert i18n.requested(environ) == languages


@pytest.mark.parametrize(
    ("language", "chosen"),
    [("pt_BR:de", "de"), ("pt_BR", "en"), ("en:de", "en"), ("fr_FR.UTF-8:de", "fr"), ("C", "en")],
)
def test_choose_takes_the_first_listed(
    monkeypatch: pytest.MonkeyPatch, language: str, chosen: str
) -> None:
    """LANGUAGES is read at call time: the tests offer languages this version does not ship."""
    offered = {"en": "English", "de": "Deutsch", "fr": "Français"}
    monkeypatch.setattr(i18n, "LANGUAGES", MappingProxyType(offered))
    assert i18n.choose({"LANGUAGE": language}) == chosen


def test_the_tests_run_in_an_english_environment() -> None:
    """tests/conftest.py: a developer's German shell changes nothing the tests read."""
    assert os.environ["LC_ALL"] == "C.UTF-8"
    assert not {"LANGUAGE", "LC_MESSAGES", "LANG"} & set(os.environ)
    assert i18n.requested(os.environ) == []


def test_english_comes_first_and_stands_in_for_any_other_language() -> None:
    """Whatever this version ships (docs/translations.md, Adding a language)."""
    assert next(iter(i18n.LANGUAGES.items())) == ("en", "English")
    assert "zz" not in i18n.LANGUAGES  # no language has this code
    assert i18n.choose({"LANG": "zz_ZZ.UTF-8"}) == "en"
    assert i18n.translator("en") is i18n.ENGLISH


@pytest.fixture
def package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temporary package whose locale/ the translators read, with xx offered."""
    name = f"translated_{tmp_path.name}"
    (tmp_path / name / "locale").mkdir(parents=True)
    (tmp_path / name / "__init__.py").write_text("", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module(name)
    monkeypatch.setattr(i18n, "CATALOGS", resources.files(module) / "locale")
    monkeypatch.setattr(i18n, "LANGUAGES", MappingProxyType({"en": "English", "xx": "Pseudo"}))
    i18n.translator.cache_clear()
    yield tmp_path / name / "locale"
    i18n.translator.cache_clear()
    sys.modules.pop(name, None)


XX_PO = """msgid ""
msgstr ""
"Language: xx\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"

msgid "Removed"
msgstr "Entfernt"

msgctxt "home assistant"
msgid "Removed"
msgstr "Entfernt (Home Assistant)"
"""


@pytest.mark.real_catalogs
def test_translator_reads_the_package_po(package: Path, caplog: pytest.LogCaptureFixture) -> None:
    (package / "xx.po").write_text(XX_PO, encoding="utf-8")
    (package / "en.po").write_text('msgid "Removed"\nmsgstr "Entfernt"\n', encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        xx = i18n.translator("xx")
        assert xx.language == "xx"
        assert xx.text("Removed") == "Entfernt"
        assert xx.ha_name("Removed") == "Entfernt (Home Assistant)"
        (package / "xx.po").unlink()
        assert i18n.translator("xx") is xx  # compiled once per process
        assert i18n.translator("en") is i18n.ENGLISH  # English reads no file
        assert i18n.ENGLISH.text("Removed") == "Removed"
    assert caplog.records == []


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (None, "xx.po"),
        (b'msgid "a"\nmsgstr "\\q"\n', "escape"),
        (b'msgid "a"\nmsgstr "\xff"\n', "utf-8"),
        (b'msgid ""\nmsgstr "Plural-Forms: nplurals=2; plural=os.system(1);\\n"\n', "plural"),
    ],
)
def test_a_missing_or_broken_catalog_gives_english(
    package: Path, caplog: pytest.LogCaptureFixture, content: bytes | None, reason: str
) -> None:
    if content is not None:
        (package / "xx.po").write_bytes(content)
    with caplog.at_level(logging.WARNING):
        assert i18n.translator("xx") is i18n.ENGLISH
        assert i18n.translator("xx") is i18n.ENGLISH
    (record,) = caplog.records
    assert record.getMessage().startswith("no catalog for xx (")
    assert reason in record.getMessage().lower()
    assert record.getMessage().endswith("): English")


@pytest.mark.real_catalogs
def test_a_language_this_version_lacks_gives_english(
    package: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (package / "it.po").write_text(XX_PO, encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        assert i18n.translator("it") is i18n.ENGLISH
    (record,) = caplog.records
    assert record.getMessage() == "no catalog for it: not one of en, xx; English"


GERMAN = [
    Entry("no host in {text!r}", ("kein Host in {text!r}",)),
    Entry(
        "{count} sender", ("{count} Absender", "{count} Absender"), msgid_plural="{count} senders"
    ),
    Entry("Send to {alias}", ("An {alias} senden",), "home assistant"),
    Entry("… at most {duty:.3g} %", ("… höchstens {duty:.3g} % der Zeit",)),
    Entry("show this help message and exit", ("diese Hilfe zeigen und beenden",)),
    Entry("usage: ", ("Aufruf: ",)),
    Entry("%(prog)s: error: %(message)s\n", ("%(prog)s: Fehler: %(message)s\n",)),
    Entry(
        "expected %s argument",
        ("%s Argument erwartet", "%s Argumente erwartet"),
        None,
        "expected %s arguments",
    ),
]


@pytest.mark.real_catalogs
def test_a_translation_is_used_and_filled() -> None:
    german = compiled("de", GERMAN)
    assert german.text("no host in {text!r}", text="gate") == "kein Host in 'gate'"
    assert german.plural("{count} sender", "{count} senders", 2) == "2 Absender"
    assert german.ha_name("Send to {alias}", alias="Tür") == "An Tür senden"
    assert german.pgettext("home assistant", "Send to {alias}", alias="Tür") == "An Tür senden"
    assert german.text("Send to {alias}", alias="Tür") == "Send to Tür"  # another context
    assert german.text("show this help message and exit") == "diese Hilfe zeigen und beenden"
    assert german.text("not in the catalog") == "not in the catalog"
    gettext, ngettext = german.argparse_words()
    assert gettext("usage: ") == "Aufruf: "
    assert gettext("%(prog)s: error: %(message)s\n") == "%(prog)s: Fehler: %(message)s\n"
    assert ngettext("expected %s argument", "expected %s arguments", 2) == "%s Argumente erwartet"
    assert (
        german.argparse_text("show this help message and exit") == "diese Hilfe zeigen und beenden"
    )


BROKEN = [
    Entry("no host in {text!r}", ("kein Host in {txet!r}",)),
    Entry("{count} sender", ("{0} Absender", "{count} Absender {"), msgid_plural="{count} senders"),
    Entry("Send to {alias}", ("An {alias senden",), "home assistant"),
    Entry("Removed", ("Entfernt {was}",), "home assistant"),
    Entry("Send to {alias}", ("An {alias.upper} senden",)),
    Entry("{key} ({roles})", ("{key} ({roles[0]})",)),
    Entry("at most {duty:.3g} %", ("höchstens {duty:d} %",)),
    Entry("show this help message and exit", ("Hilfe zu 100 % zeigen",)),
    Entry("%(prog)s: error: %(message)s\n", ("Fehler: %(message)s\n",)),
    Entry("usage: ", ("Aufruf: %s",)),
    Entry(
        "expected %s argument",
        ("%s Argument erwartet", "Argumente erwartet"),
        None,
        "expected %s arguments",
    ),
]


@pytest.mark.real_catalogs
def test_a_broken_translation_gives_english() -> None:
    """Nothing a translator writes can break a command (D7)."""
    broken = compiled("de", BROKEN)
    assert broken.text("no host in {text!r}", text="gate") == "no host in 'gate'"
    assert broken.plural("{count} sender", "{count} senders", 1) == "1 sender"
    assert broken.plural("{count} sender", "{count} senders", 2) == "2 senders"
    assert broken.ha_name("Send to {alias}", alias="Tür") == "Send to Tür"
    assert broken.ha_name("Removed") == "Removed"  # a field without values
    assert broken.text("Send to {alias}", alias="Tür") == "Send to Tür"
    assert broken.text("{key} ({roles})", key="AB12", roles=1) == "AB12 (1)"
    assert broken.text("at most {duty:.3g} %", duty=12.5) == "at most 12.5 %"
    assert (
        broken.argparse_text("show this help message and exit") == "show this help message and exit"
    )
    gettext, ngettext = broken.argparse_words()
    assert gettext("%(prog)s: error: %(message)s\n") == "%(prog)s: error: %(message)s\n"
    assert gettext("usage: ") == "usage: "
    assert ngettext("expected %s argument", "expected %s arguments", 1) == "%s Argument erwartet"
    assert ngettext("expected %s argument", "expected %s arguments", 2) == "expected %s arguments"


@pytest.mark.real_catalogs
def test_the_one_lookup_keeps_contexts_apart() -> None:
    """Every method looks up through _lookup, which tests/conftest.py wraps: plurals with a
    context too."""
    forms = ("{count} Zähler", "{count} Zähler (viele)")
    entries = [
        Entry("{count} meter", forms, "home assistant", "{count} meters"),
        Entry(
            "{count} meter", ("{count} Messer", "{count} Messer (viele)"), None, "{count} meters"
        ),
    ]
    german = compiled("de", entries)
    assert german._lookup("home assistant", "{count} meter", "{count} meters", 2) == forms[1]
    assert german._lookup(None, "{count} meter", "{count} meters", 1) == "{count} Messer"
    assert german._lookup("dashboard", "{count} meter", "{count} meters", 2) == "{count} meters"


@pytest.mark.real_catalogs
def test_a_percent_is_text_outside_argparse() -> None:
    """`% d` of `% der Zeit` reads as a directive: only texts argparse formats are held to it."""
    german = compiled("de", GERMAN)
    assert german.text("… at most {duty:.3g} %", duty=12.5) == "… höchstens 12.5 % der Zeit"


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("language", "forms", "plural_forms", "expected"),
    [
        ("de", ("{count} Absender", "{count} Absender"), "nplurals=2; plural=(n != 1);",
         {0: "0 Absender", 1: "1 Absender", 2: "2 Absender"}),
        ("fr", ("{count} expéditeur", "{count} expéditeurs"), FRENCH,
         {0: "0 expéditeur", 1: "1 expéditeur", 2: "2 expéditeurs"}),
        ("pl", ("{count} nadawca", "{count} nadawców (2-4)", "{count} nadawców"), POLISH,
         {1: "1 nadawca", 2: "2 nadawców (2-4)", 5: "5 nadawców", 12: "12 nadawców",
          22: "22 nadawców (2-4)", 25: "25 nadawców", 101: "101 nadawców", 111: "111 nadawców"}),
    ],
)  # fmt: skip
def test_plural_fills_count(
    language: str, forms: tuple[str, ...], plural_forms: str, expected: dict[int, str]
) -> None:
    entry = Entry("{count} sender", forms, msgid_plural="{count} senders")
    translator = compiled(language, [entry], plural_forms)
    for n, text in expected.items():
        assert translator.plural("{count} sender", "{count} senders", n) == text
    assert translator.plural("{count} sender", "{count} senders", 2, count="two") == forms[
        1
    ].format(count="two")


@pytest.mark.real_catalogs
def test_render_translates_nested_texts() -> None:
    entries = [
        Entry("{key}: {detail}", ("{key}: {detail}",)),
        Entry("public key must be 64 hex digits, got {key!r}", ("Schlüssel ungültig: {key!r}",)),
        Entry("{key} ({roles})", ("{key} ({roles})",)),
        Entry("target {alias}", ("Ziel {alias}",)),
        Entry("command sender", ("Befehlsgeber",)),
    ]
    german = compiled("de", entries)
    inner = TextError("public key must be 64 hex digits, got {key!r}", key="xyz")
    error = TextError("{key}: {detail}", key="commands.senders", detail=inner.text)
    assert str(error) == "commands.senders: public key must be 64 hex digits, got 'xyz'"
    assert german.render(error.text) == "commands.senders: Schlüssel ungültig: 'xyz'"
    roles = (Text("target {alias}", alias="gate"), Text("command sender"))
    look_alike = Text("{key} ({roles})", key="AB12", roles=roles)
    assert german.render(look_alike) == "AB12 (Ziel gate, Befehlsgeber)"
    assert str(look_alike) == "AB12 (target gate, command sender)"
    assert german.render("not a text") == "not a text"
    assert german.render(3) == "3"


def test_the_pseudo_language_marks_every_text(monkeypatch: pytest.MonkeyPatch) -> None:
    offered = speaking(monkeypatch)
    assert i18n.choose({"LANGUAGE": "xx"}) == "xx"
    assert i18n.translator("xx") is offered
    assert i18n.translator("en") is i18n.ENGLISH
    gettext, ngettext = offered.argparse_words()
    assert gettext("usage: ") == "‹usage: ›"
    assert gettext("%(prog)s: error: %(message)s\n") == "‹%(prog)s: error: %(message)s›\n"
    assert ngettext("expected %s argument", "expected %s arguments", 3) == "‹expected %s arguments›"
    assert offered.argparse_text("options") == "‹options›"


def test_the_hostile_catalog_shows_english(monkeypatch: pytest.MonkeyPatch) -> None:
    offered = speaking(monkeypatch, hostile())
    assert i18n.translator("xx") is offered
    gettext, ngettext = offered.argparse_words()
    assert gettext("usage: ") == "usage: "
    assert gettext("%(prog)s: error: %(message)s\n") == "%(prog)s: error: %(message)s\n"
    assert ngettext("expected %s argument", "expected %s arguments", 1) == "expected %s argument"
    assert offered.text("options") == "options"  # a lone { does not format
    assert pseudo().text("options") == "‹options›"


def test_a_text_missing_from_the_template_fails_the_test() -> None:
    """tests/conftest.py's strict catalog: a text nobody marked cannot pass the tests (D13)."""
    with pytest.raises(pytest.fail.Exception, match="not in the template"):
        i18n.ENGLISH.text("a text nobody marked")
    with pytest.raises(pytest.fail.Exception, match=r"home assistant.*not in the template"):
        i18n.ENGLISH.ha_name("options")  # in the template, but not as a name
    with pytest.raises(pytest.fail.Exception, match="not in the template"):
        i18n.ENGLISH.render(Text("a text nobody marked"))
    assert i18n.ENGLISH.text("options") == "options"
    assert i18n.ENGLISH.argparse_text("show this help message and exit") == (
        "show this help message and exit"
    )
    assert i18n.ENGLISH.argparse_words()[1]("expected %s argument", "expected %s arguments", 2) == (
        "expected %s arguments"
    )


def test_markers_return_the_text() -> None:
    assert i18n.N_("Removed") == "Removed"
    assert i18n.N_ha("Removed") == "Removed"
    assert (i18n.HOME_ASSISTANT, i18n.DASHBOARD) == ("home assistant", "dashboard")
