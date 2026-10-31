"""Message catalogs (ADR 0027): the PO reader and the MO writer, checked against GNU gettext."""

from __future__ import annotations

import gettext
import io
import re
import subprocess
from pathlib import Path

import pytest

from meshcore_home_gateway.catalog import Entry, PoError, plural_rule, read_po, write_mo

LOCALE = Path(__file__).resolve().parent.parent / "src" / "meshcore_home_gateway" / "locale"
POLISH = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"

# A context, Polish plurals, an empty and a fuzzy entry; no partly translated plural entry, where
# GNU's output differs from ours on purpose (D5).
GOLDEN_PO = r"""msgid ""
msgstr ""
"Project-Id-Version: meshcore-home-gateway\n"
"Language: pl\n"
"MIME-Version: 1.0\n"
"Content-Type: text/plain; charset=UTF-8\n"
"Content-Transfer-Encoding: 8bit\n"
"Plural-Forms: nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && "
"(n%100<10 || n%100>=20) ? 1 : 2);\n"

#. The gateway's link to its own radio.
#: src/meshcore_home_gateway/mqtt/discovery.py
msgctxt "home assistant"
msgid "Radio link"
msgstr "Połączenie z radiem"

#: src/meshcore_home_gateway/cli.py
#, python-brace-format
msgid "  commands   {words} from {count} sender"
msgid_plural "  commands   {words} from {count} senders"
msgstr[0] "  polecenia  {words} od {count} nadawcy"
msgstr[1] "  polecenia  {words} od {count} nadawców"
msgstr[2] "  polecenia  {words} od {count} nadawców"

#: src/meshcore_home_gateway/mqtt/discovery.py
msgctxt "home assistant"
msgid "Removed"
msgstr ""

#: tools/argparse_messages.json
#, fuzzy, python-format
msgid "options"
msgstr "opcje"

#: src/meshcore_home_gateway/cli.py
#, python-brace-format
msgid "configuration error: {detail}"
msgstr "błąd konfiguracji: {detail}"

#: tools/argparse_messages.json
#, python-format
msgid "%(prog)s: error: %(message)s\n"
msgstr "%(prog)s: błąd: %(message)s\n"
"""
# Made once from GOLDEN_PO with GNU gettext 0.21: msgfmt --no-hash -o golden.mo golden.po
GOLDEN_MO = bytes.fromhex(
    "de12049500000000050000001c00000044000000000000006c000000000000006c000000520000006d0000001d000000"
    "c00000001d000000de00000019000000fc000000f6000000160100007b0000000d0200001e000000890200001d000000"
    "a802000015000000c6020000002020636f6d6d616e64732020207b776f7264737d2066726f6d207b636f756e747d2073"
    "656e646572002020636f6d6d616e64732020207b776f7264737d2066726f6d207b636f756e747d2073656e6465727300"
    "252870726f6729733a206572726f723a2025286d65737361676529730a00636f6e66696775726174696f6e206572726f"
    "723a207b64657461696c7d00686f6d6520617373697374616e7404526164696f206c696e6b0050726f6a6563742d4964"
    "2d56657273696f6e3a206d657368636f72652d686f6d652d676174657761790a4c616e67756167653a20706c0a4d494d"
    "452d56657273696f6e3a20312e300a436f6e74656e742d547970653a20746578742f706c61696e3b2063686172736574"
    "3d5554462d380a436f6e74656e742d5472616e736665722d456e636f64696e673a20386269740a506c7572616c2d466f"
    "726d733a206e706c7572616c733d333b20706c7572616c3d286e3d3d31203f2030203a206e2531303e3d32202626206e"
    "2531303c3d3420262620286e253130303c3130207c7c206e253130303e3d323029203f2031203a2032293b0a00202070"
    "6f6c6563656e696120207b776f7264737d206f64207b636f756e747d206e616461776379002020706f6c6563656e6961"
    "20207b776f7264737d206f64207b636f756e747d206e6164617763c3b377002020706f6c6563656e696120207b776f72"
    "64737d206f64207b636f756e747d206e6164617763c3b37700252870726f6729733a2062c582c485643a2025286d6573"
    "7361676529730a0062c582c48564206b6f6e666967757261636a693a207b64657461696c7d00506fc582c485637a656e"
    "6965207a2072616469656d00"
)

# As GNU's tools, Poedit and Weblate write a catalog: a header with more fields, wrapped strings,
# references with lines, translators' notes, a previous msgid and an obsolete entry.
GNU_PO = r"""# Translation of meshcore-home-gateway.
#
msgid ""
msgstr ""
"Project-Id-Version: meshcore-home-gateway\n"
"POT-Creation-Date: 2026-01-01 00:00+0000\n"
"Language: de\n"
"Content-Type: text/plain; charset=UTF-8\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\n"

# a note of the translator
#. Translators: keep the column.
#. Second line.
#: src/meshcore_home_gateway/cli.py:12 src/meshcore_home_gateway/cli.py:30
#: src/meshcore_home_gateway/config.py:7
#, fuzzy, python-brace-format
#| msgid "  radio {transport}"
msgid "  radio      {transport} {where}"
msgstr "  Funk       {transport} {where}"

msgctxt "home assistant"
msgid "Send to {alias}"
msgstr ""
"An {alias} "
"senden"

msgid ""
"first line\n"
"second line"
msgstr ""
"erste Zeile\n"
"zweite Zeile"

msgid "{count} sender"
msgid_plural "{count} senders"
msgstr[0] "{count} Absender"
msgstr[1] "{count} Absender"

msgid "quote \" backslash \\ tab \t"
msgstr "Zeile\u2028Trenner"

#~ msgid "old text"
#~ msgstr "alter Text"
"""


def test_read_po_reads_what_gnu_writes() -> None:
    po = GNU_PO.replace(r"\u2028", "\u2028")  # a line separator inside a msgstr, as typed
    header, entries = read_po(po, "de.po")
    assert list(header) == [
        "Project-Id-Version", "POT-Creation-Date", "Language", "Content-Type", "Plural-Forms",
    ]  # fmt: skip
    assert header["Plural-Forms"] == "nplurals=2; plural=(n != 1);"
    radio, send, lines, plural, escapes = entries
    assert radio == Entry(
        "  radio      {transport} {where}",
        ("  Funk       {transport} {where}",),
        flags=("fuzzy", "python-brace-format"),
        comments=("Translators: keep the column.", "Second line."),
        references=(
            "src/meshcore_home_gateway/cli.py:12",
            "src/meshcore_home_gateway/cli.py:30",
            "src/meshcore_home_gateway/config.py:7",
        ),
        notes=("a note of the translator",),
    )
    assert (send.msgctxt, send.msgid, send.msgstr) == (
        "home assistant", "Send to {alias}", ("An {alias} senden",),
    )  # fmt: skip
    assert send.key == "home assistant\x04Send to {alias}"
    assert (lines.msgid, lines.msgstr) == (
        "first line\nsecond line",
        ("erste Zeile\nzweite Zeile",),
    )
    assert (plural.msgid_plural, plural.msgstr) == ("{count} senders", ("{count} Absender",) * 2)
    assert escapes.msgid == 'quote " backslash \\ tab \t'
    assert escapes.msgstr == ("Zeile\u2028Trenner",)
    assert [e.msgid for e in entries if "old" in e.msgid] == []  # obsolete entries skipped


@pytest.mark.parametrize(
    ("po", "line", "reason"),
    [
        ('\ufeffmsgid ""\nmsgstr ""\n', 1, "byte order mark"),
        ('msgid ""\nmsgstr ""\n\nmsgid "a"\nmsgstr "\\q"\n', 5, "escape"),
        ('msgid "a"\nmsgstr "x"\n\nmsgid "a"\nmsgstr "y"\n', 4, "repeated"),
        (
            'msgctxt "c"\nmsgid "a"\nmsgstr "x"\n\nmsgctxt "c"\nmsgid "a"\nmsgstr "y"\n',
            5,
            "repeated",
        ),
        ('msgid "a"\nmsgid_plural "b"\nmsgstr[1] "y"\nmsgstr[0] "x"\n', 3, "msgstr[1]"),
        ('msgid "a"\nmsgid_plural "b"\nmsgstr "x"\n', 3, "msgstr"),
        ('msgid "a"\nmsgstr[0] "x"\n', 2, "msgstr[0]"),
        ('msgid "a"\nmsgstr "x\n', 2, "quoted"),
        ('msgid "a"\nmsgstr "x"y"\n', 2, "quote"),
        ('msgid "a"\n\nmsgid "b"\nmsgstr "y"\n', 1, "without msgstr"),
        ('msgid "a"\nmsgstr "x"\n\nmsgid "b"\n', 4, "without msgstr"),
        ('msgid "a"\nmsgstr "x"\n\n"more"\n', 4, "keyword"),
        ('msgid "a"\nmsgstr "x"\nmsgctxt "c"\n', 3, "without msgid"),
        ('msgid "a"\nmsgctxt "c"\nmsgstr "x"\n', 2, "msgctxt"),
        ('msgid "a"\nmsgstr "x"\nmsgid_plural "b"\n', 3, "msgid_plural"),
        ('msgstr "x"\n', 1, "msgstr"),
        ('msgid "a"\nmsgstr "x"\n\nmsgid ""\nmsgstr "y"\n', 4, "empty msgid"),
        ('msgid ""\nmsgstr "Language de\\n"\n', 1, "header"),
        ('msgid "a"\nmsgstr "x"\n#. comment of nothing\n', 3, "comment"),
        ('msgid "a"\nmsgstr "x"\nmsgfoo "b"\n', 3, "msgfoo"),
    ],
)
def test_read_po_refuses_what_is_not_well_formed(po: str, line: int, reason: str) -> None:
    with pytest.raises(PoError, match=rf"^xx\.po:{line}: .*{re.escape(reason)}"):
        read_po(po, "xx.po")


def test_the_first_of_two_entries_is_not_kept_silently() -> None:
    """GNU's msgfmt refuses a repeated msgid; a reader that kept the second would hide it."""
    with pytest.raises(PoError, match="repeated"):
        read_po('msgid "a"\nmsgstr "first"\n\nmsgid "a"\nmsgstr "second"\n')
    _, entries = read_po('msgid "a"\nmsgstr "x"\n\nmsgctxt "c"\nmsgid "a"\nmsgstr "y"\n')
    assert [e.key for e in entries] == ["a", "c\x04a"]


def test_write_mo_matches_the_golden_catalog() -> None:
    header, entries = read_po(GOLDEN_PO)
    assert write_mo(header, entries) == GOLDEN_MO


def test_fuzzy_and_untranslated_are_left_out() -> None:
    header, entries = read_po(GOLDEN_PO)
    partly = Entry(
        "{count} node", ("{count} węzeł", "", "{count} węzłów"), msgid_plural="{count} nodes"
    )
    loaded = gettext.GNUTranslations(io.BytesIO(write_mo(header, [*entries, partly])))
    assert loaded.gettext("options") == "options"  # fuzzy
    assert loaded.pgettext("home assistant", "Removed") == "Removed"  # untranslated
    assert loaded.pgettext("home assistant", "Radio link") == "Połączenie z radiem"
    # a plural entry with one empty form: English for every count, never an empty text
    assert [loaded.ngettext("{count} node", "{count} nodes", n) for n in (1, 2, 5)] == [
        "{count} node", "{count} nodes", "{count} nodes",
    ]  # fmt: skip
    assert not partly.translated
    assert Entry("a", ("x",)).translated
    assert not Entry("a").translated
    assert not Entry("a", ("x",), flags=("fuzzy",)).translated


def test_compiler_matches_gnu_msgfmt(msgfmt: str, tmp_path: Path) -> None:
    """Every shipped catalog and the golden fixture's source, byte for byte (D5, D22)."""
    golden = tmp_path / "golden.po"
    golden.write_text(GOLDEN_PO, encoding="utf-8")
    for path in [golden, *sorted(LOCALE.glob("*.po"))]:
        gnu = subprocess.run(  # noqa: S603 - GNU msgfmt on our own files
            [msgfmt, "--no-hash", "-o", "-", str(path)], capture_output=True, check=True
        ).stdout
        header, entries = read_po(path.read_text(encoding="utf-8"), path.name)
        assert write_mo(header, entries) == gnu, path.name


@pytest.mark.parametrize(
    "forms",
    [
        "nplurals=2; plural=__import__('os').system('true');",
        "nplurals=2; plural=n.real;",
        "nplurals=two; plural=(n != 1);",
        "plural=(n != 1);",
    ],
)
def test_plural_rules_outside_the_c_subset_are_refused(forms: str) -> None:
    with pytest.raises(PoError, match="Plural-Forms"):
        plural_rule({"Plural-Forms": forms})


def test_plural_rules_read_the_header() -> None:
    count, rule = plural_rule({"Plural-Forms": POLISH})
    assert count == 3
    assert [rule(n) for n in (1, 2, 5, 22, 25, 112)] == [0, 1, 2, 1, 2, 2]
    count, rule = plural_rule({"Plural-Forms": "nplurals=2; plural=(n > 1)"})
    assert (count, rule(0), rule(1), rule(2)) == (2, 0, 0, 1)
    count, rule = plural_rule({})  # a template: gettext's default
    assert (count, rule(1), rule(2)) == (2, 0, 1)


def polish_form(n: int) -> int:
    """The Polish rule written out, not compiled from the header."""
    if n == 1:
        return 0
    return 1 if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else 2


def test_every_entry_reads_back_through_gettext() -> None:
    header = {"Language": "pl", "Content-Type": "text/plain; charset=UTF-8", "Plural-Forms": POLISH}
    forms = ("{count} węzeł", "{count} węzły", "{count} węzłów")
    entries = [
        Entry("{count} node", forms, msgid_plural="{count} nodes"),
        Entry("{count} node", tuple(f"ctx {f}" for f in forms), "home assistant", "{count} nodes"),
        Entry("Radio link", ("Połączenie z radiem",), "home assistant"),
        Entry("Radio link", ("Łącze radiowe",)),
        Entry("zażółć", ("gęślą jaźń",)),
    ]
    loaded = gettext.GNUTranslations(io.BytesIO(write_mo(header, entries)))
    for n in range(251):
        assert loaded.ngettext("{count} node", "{count} nodes", n) == forms[polish_form(n)], n
        assert loaded.npgettext("home assistant", "{count} node", "{count} nodes", n) == (
            f"ctx {forms[polish_form(n)]}"
        )
    assert loaded.pgettext("home assistant", "Radio link") == "Połączenie z radiem"
    assert loaded.gettext("Radio link") == "Łącze radiowe"
    assert loaded.gettext("zażółć") == "gęślą jaźń"
    assert loaded.info()["language"] == "pl"
