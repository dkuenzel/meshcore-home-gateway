"""The translation tool (tools/i18n.py): extraction, the canonical files, merging and the checks.
Runs on temporary copies, never on the checkout's files."""

from __future__ import annotations

import importlib.util
import json
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from meshcore_home_gateway.catalog import Entry, PoError, read_po

ROOT = Path(__file__).resolve().parent.parent
MODULE = "src/pkg/module.py"
GERMANIC = "nplurals=2; plural=(n != 1);"
FRENCH = "nplurals=2; plural=(n > 1);"
POLISH = "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def extract(tmp_path: Path, source: str) -> dict[str, Entry]:
    package = tmp_path / "src" / "pkg"
    package.mkdir(parents=True, exist_ok=True)
    (package / "module.py").write_text(textwrap.dedent(source), encoding="utf-8")
    return {entry.key: entry for entry in tool.extract_python(package, tmp_path)}


MARKED = """\
from meshcore_home_gateway import i18n
from meshcore_home_gateway.texts import Text, TextError


def show(cli, translator, name, table, key, n):
    cli.tr("plain text")
    tr("no host in {text!r}", text=name)
    # Translators: a help text; argparse formats it.
    tr_help("TOML file {not a field} at 100%% of $STATE_DIRECTORY")
    trn("{count} sender", "{count} senders", n)
    i18n.N_("doing {reported}")
    # an ordinary comment
    # Translators: Binary sensor on the gateway's device:
    # its link to its own radio.
    translator.ha_name("Radio link")
    print(
        # Translators: a reading type.
        i18n.N_ha("Temperature {channel}"),
    )
    translator.pgettext("dashboard", "Title")
    # Translators: a whole statement.
    shown = [
        tr("plain text"),
        tr("{a} and {b}", **table),
    ]
    raise TextError("bad word {word!r}", word=name, extra=Text("command sender"))
    return shown, tr(name), tr(cli.text), tr(table[key]), i18n.N_(name)
"""


def test_marked_texts_are_extracted(tmp_path: Path) -> None:
    found = extract(tmp_path, MARKED)
    reference = (MODULE,)
    brace = ("python-brace-format",)
    assert found == {
        # the statement's comment reaches the second use
        "plain text": Entry(
            "plain text", ("",), comments=("a whole statement.",), references=reference
        ),
        "no host in {text!r}": Entry(
            "no host in {text!r}", ("",), flags=brace, references=reference
        ),
        "TOML file {not a field} at 100%% of $STATE_DIRECTORY": Entry(
            "TOML file {not a field} at 100%% of $STATE_DIRECTORY",
            ("",),
            flags=("python-format",),
            comments=("a help text; argparse formats it.",),
            references=reference,
        ),
        "{count} sender": Entry(
            "{count} sender", ("", ""), None, "{count} senders", brace, references=reference
        ),
        "doing {reported}": Entry("doing {reported}", ("",), flags=brace, references=reference),
        "home assistant\x04Radio link": Entry(
            "Radio link",
            ("",),
            "home assistant",
            comments=("Binary sensor on the gateway's device:", "its link to its own radio."),
            references=reference,
        ),
        "home assistant\x04Temperature {channel}": Entry(
            "Temperature {channel}",
            ("",),
            "home assistant",
            flags=brace,
            comments=("a reading type.",),
            references=reference,
        ),
        "dashboard\x04Title": Entry("Title", ("",), "dashboard", references=reference),
        "{a} and {b}": Entry(
            "{a} and {b}",
            ("",),
            flags=brace,
            comments=("a whole statement.",),
            references=reference,
        ),
        "bad word {word!r}": Entry("bad word {word!r}", ("",), flags=brace, references=reference),
        "command sender": Entry("command sender", ("",), references=reference),
    }


def test_a_text_used_twice_is_one_entry(tmp_path: Path) -> None:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "a.py").write_text(
        '# Translators: first.\ntr("Removed")\ntr("Removed")\n', encoding="utf-8"
    )
    (tmp_path / "src" / "pkg" / "b.py").write_text(
        '# Translators: second.\ntr("Removed")\n', encoding="utf-8"
    )
    (entry,) = tool.extract_python(tmp_path / "src" / "pkg", tmp_path)
    assert entry.references == ("src/pkg/a.py", "src/pkg/b.py")
    assert entry.comments == ("first.", "second.")


def test_two_plurals_for_one_text_are_refused(tmp_path: Path) -> None:
    with pytest.raises(tool.ExtractError, match=f"{MODULE}:2: .*plural"):
        extract(
            tmp_path,
            'trn("{count} file", "{count} files", 1)\ntrn("{count} file", "{count} filez", 1)\n',
        )


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ('tr(f"no host in {name}")', "string literal"),
        ('i18n.N_(f"doing {name}")', "string literal"),
        ('tr("no host in " + name)', "string literal"),
        ('tr("no host in {text}".format(text=name))', "string literal"),
        ('tr("no host in %s" % name)', "string literal"),
        ('tr("open" if name else "closed")', "string literal"),
        ('trn("{count} file", name, n)', "string literal"),
        ('translator.pgettext(name, "Title")', "string literal"),
        ("tr(*names)", "string literal"),
        ("tr()", "no text"),
        ('tr("meshcore-home-gateway: configuration error")', "prefix"),
        ('trn("{count} file", "meshcore-home-gateway: {count} files", n)', "prefix"),
        ('tr("")', "empty"),
        ('tr("no host in {text!r}")', "no value for text"),
        ('tr("{a} and {b}", a=1)', "no value for b"),
        ('trn("{count} file in {path}", "{count} files in {path}", n)', "no value for path"),
        ('tr("{0} senders", count=1)', "named fields"),
        ('tr("{} senders", count=1)', "named fields"),
        ('tr("a lone { brace", x=1)', "format"),
        ('i18n.N_("a lone { brace")', "format"),
    ],
)
def test_what_cannot_be_extracted_is_refused(tmp_path: Path, call: str, reason: str) -> None:
    with pytest.raises(tool.ExtractError, match=f"^{MODULE}:2: .*{reason}"):
        extract(tmp_path, f"def show(name, names, n, translator):\n    {call}\n")


@pytest.mark.parametrize(
    "call",
    [
        'i18n.N_("doing {reported}")',
        'N_ha("Temperature {channel}")',
        'tr_help("a {brace} is text here")',
        'tr("{a} and {b}", **values)',
        'trn("{count} file", "{count} files", n)',
        'Text("{key} ({roles})", key=1, roles=())',
    ],
)
def test_texts_whose_values_come_later_are_extracted(tmp_path: Path, call: str) -> None:
    assert len(extract(tmp_path, f"def show(n, values):\n    {call}\n")) == 1


def test_argparse_texts_are_listed_with_their_flag(tmp_path: Path) -> None:
    listed = tmp_path / "tools" / "argparse_messages.json"
    listed.parent.mkdir()
    pairs = [
        ["%(prog)s: error: %(message)s\n", None],
        ["expected %s argument", "expected %s arguments"],
    ]
    listed.write_text(json.dumps(pairs), encoding="utf-8")
    error, plural = tool.extract_argparse(listed, tmp_path)
    comment = ("Python's argparse: usage, help and errors",)
    reference = ("tools/argparse_messages.json",)
    assert error == Entry(
        "%(prog)s: error: %(message)s\n", ("",), None, None, ("python-format",), comment, reference
    )
    assert plural.msgid_plural == "expected %s arguments"
    assert plural.msgstr == ("", "")


def test_this_pythons_argparse_texts_are_found() -> None:
    found = tool.argparse_messages()
    assert ("usage: ", None) in found
    assert ("expected %s argument", "expected %s arguments") in found
    assert all(msgid for msgid, _ in found)


CANONICAL = r"""msgid ""
msgstr ""
"Project-Id-Version: meshcore-home-gateway\n"
"Language: de\n"
"MIME-Version: 1.0\n"
"Content-Type: text/plain; charset=UTF-8\n"
"Content-Transfer-Encoding: 8bit\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\n"

# a note of the translator
#
#. Translators: keep the values in the column of the other summary lines.
#.
#: src/meshcore_home_gateway/cli.py src/meshcore_home_gateway/config.py
#, fuzzy, python-brace-format
msgid "  radio      {transport} {where}"
msgstr "  Funk       {transport} {where}"

#: src/meshcore_home_gateway/mqtt/discovery.py
msgctxt "home assistant"
msgid "A name long enough for GNU's tools to wrap it at 79 columns, unless told not to"
msgstr "Ein Name, lang genug, dass GNUs Werkzeuge ihn bei 79 Spalten umbrechen würden"

msgid "{count} sender"
msgid_plural "{count} senders"
msgstr[0] "{count} Absender"
msgstr[1] ""

msgid ""
"first line\n"
"second line"
msgstr "quote \" backslash \\ tab \t return \r line\n"

#: tools/argparse_messages.json
#, python-format
msgid "%(prog)s: error: %(message)s\n"
msgstr ""
"%(prog)s: Fehler:\n"
"%(message)s\n"
"""


def test_write_po_gives_back_what_it_wrote() -> None:
    header, entries = read_po(CANONICAL, "de.po")
    assert tool.write_po(header, entries) == CANONICAL
    assert tool.canonical("de.po", CANONICAL) == []


def test_write_po_writes_untranslated_entries_empty() -> None:
    entries = [Entry("Removed"), Entry("{count} file", msgid_plural="{count} files")]
    assert tool.write_po({"Language": "de"}, entries) == (
        'msgid ""\nmsgstr "Language: de\\n"\n\n'
        'msgid "Removed"\nmsgstr ""\n\n'
        'msgid "{count} file"\nmsgid_plural "{count} files"\nmsgstr[0] ""\nmsgstr[1] ""\n'
    )


TEMPLATE = [
    Entry("b first", ("",), references=("src/a.py",), comments=("new comment",)),
    Entry("{count} file", ("", ""), None, "{count} files", ("python-brace-format",)),
    Entry("{count} item", ("", ""), None, "{count} items", ("python-brace-format",)),
    Entry("a new text", ("",)),
]
OLD = [
    Entry("obsolete", ("veraltet",)),
    Entry("{count} item", ("{count} Ding", "{count} Dinge"), None, "{count} itemz"),
    Entry("{count} file", ("{count} Datei", "{count} Dateien"), None, "{count} files",
          ("python-brace-format", "fuzzy"), notes=("check the plural",)),
    Entry("b first", ("B zuerst",), references=("src/old.py",), comments=("old",), notes=("ok",)),
]  # fmt: skip


def test_merge_follows_the_template_and_keeps_translations() -> None:
    old_header = {"Language": "de", "Last-Translator": "Someone <someone@example.org>"}
    header, merged = tool.merge(TEMPLATE, old_header, OLD, "de")
    assert header == tool.catalog_header("de", GERMANIC)
    assert "Last-Translator" not in tool.write_po(header, merged)
    assert merged == [
        Entry("b first", ("B zuerst",), references=("src/a.py",), comments=("new comment",),
              notes=("ok",)),
        Entry("{count} file", ("{count} Datei", "{count} Dateien"), None, "{count} files",
              ("fuzzy", "python-brace-format"), notes=("check the plural",)),
        Entry("{count} item", ("", ""), None, "{count} items", ("python-brace-format",)),
        Entry("a new text", ("",)),
    ]  # fmt: skip


def test_merge_takes_the_plural_count_from_the_language() -> None:
    header, merged = tool.merge(TEMPLATE, {}, OLD, "pl")
    assert header["Plural-Forms"] == POLISH
    assert [len(entry.msgstr) for entry in merged] == [1, 3, 3, 1]
    assert merged[1].msgstr == ("{count} Datei", "{count} Dateien", "")
    header, _ = tool.merge(TEMPLATE, {"Plural-Forms": FRENCH}, OLD, "xx")  # a test's language
    assert header == tool.catalog_header("xx", FRENCH)
    with pytest.raises(PoError, match="Plural-Forms"):
        tool.merge(TEMPLATE, {}, OLD, "xx")


def catalog(*entries: Entry, plural_forms: str = GERMANIC) -> tuple[dict[str, str], list[Entry]]:
    return tool.catalog_header("de", plural_forms), list(entries)


@pytest.mark.parametrize(
    ("entry", "finding"),
    [
        (Entry("Removed", ("",)), "untranslated: 'Removed'"),
        (Entry("{count} file", ("{count} Datei", ""), None, "{count} files"), "untranslated"),
        (Entry("Removed", ("Entfernt",), flags=("fuzzy",)), "fuzzy: 'Removed'"),
    ],
)
def test_shipped_catalogs_are_complete(entry: Entry, finding: str) -> None:
    findings = tool.untranslated("de.po", *catalog(entry)) + tool.fuzzy("de.po", *catalog(entry))
    assert len(findings) == 1
    assert finding in findings[0]
    assert tool.untranslated("de.po", *catalog(Entry("Removed", ("Entfernt",)))) == []


@pytest.mark.parametrize(
    ("entry", "plural_forms", "finding"),
    [
        (Entry("cannot read {path}", ("{pfad} nicht lesbar",)), GERMANIC, "{pfad}"),
        (Entry("got {value!r}", ("erhielt {wert!r}",)), GERMANIC, "{wert!r}"),
        (Entry("got {value!r}", ("erhielt {value}",)), GERMANIC, "{value}"),
        (Entry("at most {duty:.3g} %", ("höchstens {duty} %",)), GERMANIC, "{duty}"),
        (Entry("got {value}", ("erhielt {value",)), GERMANIC, "format"),
        (Entry("{count} file", ("{count} Datei", "Dateien"), None, "{count} files"),
         GERMANIC, "Dateien"),
        (Entry("{count} file", ("{count} plik", "{count} pliki {path}", "{count} plików"), None,
               "{count} files"), POLISH, "{path}"),
        (Entry("{count} file", ("un fichier", "{count} fichiers"), None, "{count} files"),
         FRENCH, "un fichier"),  # n = 0 shows it too
    ],
)  # fmt: skip
def test_placeholders_match(entry: Entry, plural_forms: str, finding: str) -> None:
    findings = tool.placeholders("de.po", *catalog(entry, plural_forms=plural_forms))
    assert len(findings) == 1, findings
    assert finding in findings[0]


@pytest.mark.parametrize(
    ("entry", "plural_forms"),
    [
        (Entry("got {value!r}", ("erhielt {value!r}",)), GERMANIC),
        (Entry("{count} file", ("eine Datei", "{count} Dateien"), None, "{count} files"), GERMANIC),
        (Entry("{count} file", ("jeden plik", "{count} pliki", "{count} plików"), None,
               "{count} files"), POLISH),
        (Entry("… at most {duty:.3g} %", ("… höchstens {duty:.3g} % der Zeit",)), GERMANIC),
        (Entry("Removed", ("",)), GERMANIC),
    ],
)  # fmt: skip
def test_placeholders_that_match_pass(entry: Entry, plural_forms: str) -> None:
    """A form only n == 1 selects may leave {count} out; a % is text outside argparse."""
    assert tool.placeholders("de.po", *catalog(entry, plural_forms=plural_forms)) == []
    assert tool.percent("de.po", *catalog(entry, plural_forms=plural_forms)) == []


@pytest.mark.parametrize(
    ("entry", "finding"),
    [
        (Entry("show this help message and exit", ("afficher cette aide à 20 %",),
               flags=("python-format",)), "%"),
        (Entry("%(prog)s: error: %(message)s\n", ("erreur : %(message)s\n",),
               flags=("python-format",)), "%(prog)s"),
        (Entry("expected %s argument", ("%s argument attendu", "arguments attendus"), None,
               "expected %s arguments", ("python-format",)), "arguments attendus"),
    ],
)  # fmt: skip
def test_percent_directives_match_where_argparse_formats(entry: Entry, finding: str) -> None:
    findings = tool.percent("fr.po", *catalog(entry, plural_forms=FRENCH))
    assert len(findings) == 1, findings
    assert finding in findings[0]


def test_plural_headers_are_the_languages() -> None:
    plural = Entry("{count} file", ("{count} Datei", "{count} Dateien"), None, "{count} files")
    assert tool.plural_forms("de.po", *catalog(plural)) == []
    (wrong,) = tool.plural_forms("de.po", *catalog(plural, plural_forms=FRENCH))
    assert "Plural-Forms" in wrong
    (count,) = tool.plural_forms("pl.po", tool.catalog_header("pl", POLISH), [plural])
    assert "2 forms" in count
    (refused,) = tool.plural_forms(
        "xx.po", tool.catalog_header("xx", "nplurals=2; plural=os.system(1);"), []
    )
    assert "Plural-Forms" in refused
    assert tool.plural_forms("xx.po", tool.catalog_header("xx", FRENCH), []) == []


@pytest.mark.parametrize(
    ("msgid", "msgstr"),
    [
        ("see --answers", "siehe --antworten"),
        ("set $STATE_DIRECTORY", "STATE_DIRECTORY setzen"),
        ("cannot write /var/lib/meshcore-home-gateway", "/var/lib/gateway nicht schreibbar"),
        ("check homeassistant.device_name.", "prüfe homeassistant.gerätename."),
        ("between KEY12…KEY8", "zwischen KEY12...KEY8"),
        ("ADR 0019 says so", "ADR 19 sagt es"),
        ("see docs/deployment.md", "siehe docs/installation.md"),
        ("see https://example.org/a/b", "siehe https://example.org/a"),
        ("run pct exec 100", "führe pct 100 aus"),
        ("see journalctl", "siehe das Journal"),
    ],
)
def test_tokens_are_kept(msgid: str, msgstr: str) -> None:
    (finding,) = tool.tokens("de.po", *catalog(Entry(msgid, (msgstr,))))
    assert repr(msgid) in finding
    kept = msgid.replace("see", "siehe").replace("cannot write", "nicht schreibbar:")
    assert tool.tokens("de.po", *catalog(Entry(msgid, (kept,)))) == []


def test_tokens_at_the_end_of_a_sentence_are_kept() -> None:
    entry = Entry(
        "check radio.transport and /etc/x.toml.", ("radio.transport in /etc/x.toml prüfen",)
    )
    assert tool.tokens("de.po", *catalog(entry)) == []


@pytest.mark.parametrize(
    ("entry", "finding"),
    [
        (
            Entry("configuration error: {detail}", ("erreur de configuration\u202f: {detail}",)),
            "U+202F",
        ),
        (Entry("configuration error: {detail}", ("erreur\u00a0: {detail}",)), "U+00A0"),
        (Entry("Removed", ("Ent\u00adfernt",)), "U+00AD"),
        (Entry("Removed", ("Ent\tfernt",)), "U+0009"),
    ],
)
def test_no_control_characters(entry: Entry, finding: str) -> None:
    (found,) = tool.control_characters("fr.po", *catalog(entry))
    assert finding in found


def test_home_assistant_names_may_hold_narrow_spaces() -> None:
    entries = [
        Entry("Duty cycle", ("Rapport cyclique\u202f%",), "home assistant"),
        Entry("first\nsecond", ("erste\nzweite",)),
    ]
    assert tool.control_characters("fr.po", *catalog(*entries)) == []


def test_catalogs_are_canonical() -> None:
    header_line = '"Content-Transfer-Encoding: 8bit\\n"\n'
    translator = '"Last-Translator: Someone\\n"\n'
    (finding,) = tool.canonical("de.po", CANONICAL.replace(header_line, header_line + translator))
    assert finding.startswith("de.po: the header must be exactly ")
    assert finding.endswith(": run make translations")
    obsolete = CANONICAL + '\n#~ msgid "old"\n#~ msgstr "alt"\n'
    assert any("#~" in f for f in tool.canonical("de.po", obsolete))
    wrapped = CANONICAL.replace('msgid "A name long enough', 'msgid ""\n"A name long enough')
    assert tool.canonical("de.po", wrapped) != []
    (broken,) = tool.canonical("de.po", 'msgid "a"\nmsgstr "\\q"\n')
    assert broken.startswith("de.po:2: ")


def test_headers_have_exactly_the_fixed_fields() -> None:
    assert tool.fixed_header("de.po", tool.catalog_header("de", GERMANIC)) == []
    assert tool.fixed_header("x/meshcore-home-gateway.pot", tool.TEMPLATE_HEADER) == []
    (other,) = tool.fixed_header("fr.po", tool.catalog_header("de", GERMANIC))
    assert "Language: fr" in other
    extra = {**tool.catalog_header("de", GERMANIC), "Last-Translator": "Someone"}
    assert tool.fixed_header("de.po", extra) != []
    assert tool.fixed_header("x.pot", tool.catalog_header("de", GERMANIC)) != []
    assert list(tool.TEMPLATE_HEADER) == [
        "Project-Id-Version", "MIME-Version", "Content-Type", "Content-Transfer-Encoding",
    ]  # fmt: skip


@pytest.fixture
def project(tmp_path: Path) -> Any:
    """A checkout with one module, a short argparse list and no catalog."""
    package = tmp_path / "src" / "meshcore_home_gateway"
    (package / "locale").mkdir(parents=True)
    (package / "cli.py").write_text('tr("Removed")\n', encoding="utf-8")
    (tmp_path / "tools").mkdir()
    listed = [["usage: ", None], ["an argparse text of an older Python", None]]
    (tmp_path / "tools" / "argparse_messages.json").write_text(json.dumps(listed), encoding="utf-8")
    return tool.Project(tmp_path)


def test_update_writes_the_template_and_merges_the_catalogs(project: Any) -> None:
    written = tool.update(project)
    assert written == [project.argparse_list, project.template]
    listed = json.loads(project.argparse_list.read_text(encoding="utf-8"))
    assert ["an argparse text of an older Python", None] in listed  # never removed
    assert ["show this help message and exit", None] in listed  # this Python's
    assert listed == sorted(listed, key=lambda pair: (pair[0], pair[1] or ""))
    assert tool.check(project) == []
    _, template = read_po(project.template.read_text(encoding="utf-8"))
    assert template[0] == Entry("Removed", ("",), references=("src/meshcore_home_gateway/cli.py",))
    assert template[-1].references == ("tools/argparse_messages.json",)
    assert tool.update(project) == []

    german = project.locale / "de.po"
    german.write_text(
        tool.write_po({"Language": "de"}, [Entry("gone", ("weg",))]), encoding="utf-8"
    )
    assert any("de.po: does not follow the template" in f for f in tool.check(project))
    assert any("not one of the languages" in f for f in tool.check(project))
    assert tool.update(project) == [german]
    header, entries = read_po(german.read_text(encoding="utf-8"))
    assert header == tool.catalog_header("de", GERMANIC)
    assert [e.msgid for e in entries] == [e.msgid for e in template]
    findings = tool.check(project, {"en": "English", "de": "Deutsch"})
    assert len(findings) == len(entries)
    assert findings[0] == "src/meshcore_home_gateway/locale/de.po: untranslated: 'Removed'"
    assert all(": untranslated: " in finding for finding in findings)


def test_check_finds_a_template_behind_the_code(project: Any) -> None:
    tool.update(project)
    (project.package / "cli.py").write_text('tr("Removed")\ntr("Added")\n', encoding="utf-8")
    name = "src/meshcore_home_gateway/locale/meshcore-home-gateway.pot"
    assert tool.check(project) == [f"{name}: not current: run make translations"]
    project.template.unlink()
    assert tool.check(project) == [f"{name}: missing: run make translations"]


def test_main_runs_the_commands(
    project: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(tool, "ROOT", project.root)
    assert tool.main(["update"]) == 0
    assert (
        "wrote src/meshcore_home_gateway/locale/meshcore-home-gateway.pot"
        in capsys.readouterr().out
    )
    assert tool.main(["check"]) == 0
    assert capsys.readouterr().out == ""
    (project.package / "cli.py").write_text('tr(f"{x}")\n', encoding="utf-8")
    assert tool.main(["check"]) == 1
    assert "src/meshcore_home_gateway/cli.py:1: " in capsys.readouterr().out
    assert tool.main(["update"]) == 1
    assert "src/meshcore_home_gateway/cli.py:1: " in capsys.readouterr().err
