"""The translation tool (tools/i18n.py): extraction, the canonical files, merging, the installer's
language packs and the checks. Runs on temporary copies, never on the checkout's files."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any

import pytest

from meshcore_home_gateway import dashboard
from meshcore_home_gateway.catalog import Entry, PoError, read_po

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
MODULE = "src/pkg/module.py"
INSTALLER = "deploy/proxmox/meshcore-home-gateway-lxc.sh"
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


ERRORS = """\
def check(key, kind, problem):
    # Translators: key is a setting, such as mqtt.port.
    raise ConfigError("{key}: required", key=key)
    raise config.ConfigError("{key}: {detail}", key=key, detail=problem)
    raise ConfigError(problem.msgid, **problem.values)
    raise LinkError("invalid_link", "the contact link is malformed")
    raise links.LinkError("unsupported_type", "the contact link has type {kind!r}", kind=kind)
"""


def test_configuration_and_link_errors_are_extracted(tmp_path: Path) -> None:
    """ConfigError takes its text first, LinkError second, after the reason, which is a code."""
    found = extract(tmp_path, ERRORS)
    brace = ("python-brace-format",)
    reference = (MODULE,)
    assert found == {
        "{key}: required": Entry(
            "{key}: required",
            ("",),
            flags=brace,
            comments=("key is a setting, such as mqtt.port.",),
            references=reference,
        ),
        "{key}: {detail}": Entry("{key}: {detail}", ("",), flags=brace, references=reference),
        "the contact link is malformed": Entry(
            "the contact link is malformed", ("",), references=reference
        ),
        "the contact link has type {kind!r}": Entry(
            "the contact link has type {kind!r}", ("",), flags=brace, references=reference
        ),
    }


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ('ConfigError(f"{key}: required")', "string literal"),
        ('config.ConfigError("{key}: " + "required", key=key)', "string literal"),
        ('ConfigError("{key}: required")', "no value for key"),
        ('LinkError("unsupported_type", f"the contact link has type {kind!r}")', "string literal"),
        ('LinkError("unsupported_type", "the link has type {kind!r}")', "no value for kind"),
        ('LinkError("invalid_link")', "no text"),
        ('LinkError("invalid_link", "")', "empty"),
    ],
)
def test_configuration_and_link_errors_that_cannot_be_extracted_are_refused(
    tmp_path: Path, call: str, reason: str
) -> None:
    with pytest.raises(tool.ExtractError, match=f"^{MODULE}:2: .*{reason}"):
        extract(tmp_path, f"def check(key, kind):\n    raise {call}\n")


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


DASHBOARD = "src/meshcore_home_gateway/dashboard.yaml"
DASHBOARD_TEMPLATE = """\
# meshcore-home-gateway dashboard for node @node@
# Translators: the dashboard's title
# in the sidebar.
title: _("Mesh")
views:
  - title: _("Overview")
    cards:
      - type: markdown
        content: |-
          {%- macro ago(value) -%}
          {#- Translators: how long ago; n is a whole number -#}
          {%- set n = 5 -%}_("{n} min ago") _("{n} h ago")
          {%- endmacro -%}
          > _("Mesh")
          _("Node \\"{node}\\"")
"""


def test_the_dashboards_texts_are_extracted(tmp_path: Path) -> None:
    """Every marker of the template, in the context of the dashboard, with the translator's note
    of the line above it: a YAML comment above a whole value, a Jinja one inside a card."""
    path = tmp_path / DASHBOARD
    path.parent.mkdir(parents=True)
    path.write_text(DASHBOARD_TEMPLATE, encoding="utf-8")
    found = {entry.key: entry for entry in tool.extract_dashboard(path, tmp_path)}
    reference, brace, ago = (
        (DASHBOARD,),
        ("python-brace-format",),
        ("how long ago; n is a whole number",),
    )
    assert found == {
        "dashboard\x04Mesh": Entry(
            "Mesh", ("",), "dashboard", comments=("the dashboard's title", "in the sidebar."),
            references=reference,
        ),
        "dashboard\x04Overview": Entry("Overview", ("",), "dashboard", references=reference),
        "dashboard\x04{n} min ago": Entry(
            "{n} min ago", ("",), "dashboard", flags=brace, comments=ago, references=reference
        ),
        "dashboard\x04{n} h ago": Entry(
            "{n} h ago", ("",), "dashboard", flags=brace, comments=ago, references=reference
        ),
        'dashboard\x04Node "{node}"': Entry(
            'Node "{node}"', ("",), "dashboard", flags=brace, references=reference
        ),
    }  # fmt: skip


def test_a_dashboard_text_that_is_no_format_string_is_refused(tmp_path: Path) -> None:
    path = tmp_path / DASHBOARD
    path.parent.mkdir(parents=True)
    path.write_text('content: |-\n  _("{0} min")\n', encoding="utf-8")
    with pytest.raises(tool.ExtractError, match=r"dashboard\.yaml:2: .* needs named fields"):
        tool.extract_dashboard(path, tmp_path)


def test_the_template_is_made_of_the_code_argparse_and_the_dashboard() -> None:
    project = tool.Project(ROOT)
    assert [(extract.__name__, path) for extract, path in tool.sources(project)] == [
        ("extract_python", project.package),
        ("extract_dashboard", project.package / "dashboard.yaml"),
        ("extract_argparse", project.argparse_list),
    ]


def extract_sh(tmp_path: Path, source: str) -> dict[str, Entry]:
    script = tmp_path / INSTALLER
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(source, encoding="utf-8")
    return {entry.key: entry for entry in tool.extract_shell(script, tmp_path)}


SHELL = r"""#!/usr/bin/env bash
# --- messages (docs/development.md, Translations): tests cut this block out and run it on its own
t() { local msgid=$1; shift; _fill "${L10N[$msgid]-$msgid}" "$@"; }
tp() { local key=$1$'\x04'$2; printf '%s' "${L10N[$key]-$2}"; }
# --- end of messages
# --- language packs: written by `make translations` from deploy/proxmox/locale/*.po; do not edit
L10N_ORDER=(en)
t 'not a call: the generated block'
# --- end of language packs

die() {  # die MSGID [NAME VALUE]...: its own text is passed on
  log "error: $(_fill "$@")"
  whiptail --msgbox -- "$(t 'Error: {message}' message "$(t "$@")")" 12 72 >&2 || true
}

ask_container() {
  # die 'a commented call'
  value=$(show_dialog input "$(t 'Container ID')" "${CT[id]:-$(echo 100)}")
  # Translators: the user's name on the broker
  # is a value.
  show_dialog password "$(t "Password of MQTT user '{user}' (empty: none)" user "$user")"
  [[ -n $x ]] || { die 'container {ctid} has no network' ctid "$ctid"; }
  # Translators: one statement over two lines.
  CT[ip]=$(ask_advanced ct.ip input \
    "$(t 'IPv4 address with prefix, or dhcp')" "dhcp")
  # Translators: the second command of a list.
  pvesh get /cluster/nextid --vmid "$id" >/dev/null \
    || die 'ID {id} is already used' id "$id"
  BUTTONS=(--yes-button "$(tp button Yes)" --no-button "$(tp button 'No')")
  cat <<EOF
t 'not a call: the text of a here-document'
EOF
  x='die "not a call either"' y="t 'nor this' $(printf '%s' "#") ${x#t 'no'}"
  case $x in  # patterns are no calls
    t|tp) die cancelled ;;
    (die)
      x=$(case $y in t) echo ;; esac) ;;
  esac
}

plan_text() {
  local say=$1
  sets+="  $($say 'The service leaves alone:')"$'\n'
  $say "The radio's firmware is not tested:
(radio.profile.manage = always sets it anyway):"
  $say 'At its next connect
the service sets the radio:'
}
"""


def test_the_installers_texts_are_extracted(tmp_path: Path) -> None:
    """Single-, double-quoted and multi-line literals after t, die and $say; tp's context; no
    references (one file)."""
    brace = ("python-brace-format",)
    assert extract_sh(tmp_path, SHELL) == {
        "Error: {message}": Entry("Error: {message}", ("",), flags=brace),
        "Container ID": Entry("Container ID", ("",)),
        "Password of MQTT user '{user}' (empty: none)": Entry(
            "Password of MQTT user '{user}' (empty: none)",
            ("",),
            flags=brace,
            comments=("the user's name on the broker", "is a value."),
        ),
        "container {ctid} has no network": Entry(
            "container {ctid} has no network", ("",), flags=brace
        ),
        "IPv4 address with prefix, or dhcp": Entry(
            "IPv4 address with prefix, or dhcp", ("",), comments=("one statement over two lines.",)
        ),
        "ID {id} is already used": Entry(
            "ID {id} is already used",
            ("",),
            flags=brace,
            comments=("the second command of a list.",),
        ),
        "button\x04Yes": Entry("Yes", ("",), "button"),
        "button\x04No": Entry("No", ("",), "button"),
        "cancelled": Entry("cancelled", ("",)),
        "The service leaves alone:": Entry("The service leaves alone:", ("",)),
        "The radio's firmware is not tested:\n(radio.profile.manage = always sets it anyway):": (
            Entry(
                "The radio's firmware is not tested:\n"
                "(radio.profile.manage = always sets it anyway):",
                ("",),
            )
        ),
        "At its next connect\nthe service sets the radio:": Entry(
            "At its next connect\nthe service sets the radio:", ("",)
        ),
    }


def test_the_installer_template_is_sorted_by_context_and_text(tmp_path: Path) -> None:
    extract_sh(tmp_path, SHELL)
    entries = tool.installer_template(tool.Project(tmp_path))
    plain = sorted(entry.key for entry in entries if entry.msgctxt is None)
    assert [entry.key for entry in entries] == [*plain, "button\x04No", "button\x04Yes"]


@pytest.mark.parametrize(
    ("call", "reason"),
    [
        ('t "Container $id"', "literal"),
        ('t "Container `id`"', "substitution"),
        (r't "Container \"x\""', "literal"),
        ('t "Container $(id)"', "literal"),
        (r"t $'Container\nID'", "literal"),
        ("t 'Container '\"ID\"", "literal"),
        ('t "$msgid"', "literal"),
        ('die "$@"', "literal"),
        ('$say "$text"', "literal"),
        ('tp button "$label"', "literal"),
        ('tp "$context" Yes', "literal"),
        ("die", "no text"),
        ("tp button", "no text"),
        ("t ''", "empty"),
        ("t 'meshcore-home-gateway: cannot read'", "prefix"),
        ("t 'container {ctid} has no network'", "no value for ctid"),
        ("die 'container {ctid} has no network' ctdi 105", "no value for ctid"),
        ("t 'got {value!r}' value 1", "{name}"),
        ("t 'a lone { brace'", "{name}"),
        ("t 'a {{brace}}' brace 1", "{name}"),
        ("tp button 'Yes {x}' x 1", "no values"),
        ("echo `t 'Container ID'`", "substitution"),
        ("echo \"`t 'Container ID'`\"", "substitution"),
    ],
)
def test_what_the_installer_cannot_extract_is_refused(
    tmp_path: Path, call: str, reason: str
) -> None:
    """A message call takes a literal: single-quoted, double-quoted without $, ` and \\, or plain
    letters; its fields are plain {name}s with their values. Only die passes its own on."""
    with pytest.raises(tool.ExtractError, match=f"^{INSTALLER}:3: .*{reason}"):
        extract_sh(tmp_path, f"#!/usr/bin/env bash\nask() {{\n  x=$({call}) || true\n}}\n")


@pytest.mark.parametrize(
    ("source", "what"),
    [
        ("echo 'open", "a quote"),
        ('echo "open', "a quote"),
        ("echo $'open", "a quote"),
        ("x=$(echo", "$("),
        ("x=${y", "${"),
        ('x="${y:-\'open}"', "a quote"),
        ("cat <<EOF\nt 'not a call'", "the here-document EOF"),
    ],
)
def test_what_is_not_closed_is_named(tmp_path: Path, source: str, what: str) -> None:
    with pytest.raises(
        tool.ExtractError, match=f"^{INSTALLER}:[23]: {re.escape(what)} is not closed"
    ):
        extract_sh(tmp_path, f"#!/usr/bin/env bash\n{source}\n")


def test_here_documents_hold_no_calls(tmp_path: Path) -> None:
    """<<WORD, << WORD, <<-WORD (its lines may start with tabs) and a quoted WORD."""
    source = (
        "#!/usr/bin/env bash\n"
        "cat << EOF\nt 'one'\nEOF\n"
        "cat <<-'END'\n\tt 'two'\n\tEND\n"
        "cat <<\"EOF\" | t 'three'\nt 'four'\nEOF\n"
        "t 'five'\n"
    )
    assert set(extract_sh(tmp_path, source)) == {"three", "five"}


def test_hazards_in_the_installer_are_found(tmp_path: Path) -> None:
    """A language pack is data: nothing may run text as code, and bash's own $"…" translation
    would read catalogs the installer does not control."""
    script = tmp_path / INSTALLER
    script.parent.mkdir(parents=True)
    script.write_text(
        "#!/usr/bin/env bash\n"
        'eval "$x"\n'
        "source ./messages\n"
        "if true; then . ./messages; fi\n"
        'echo $"Hello"\n'
        "x=$(command eval y) || builtin source z\n"
        "# eval in a comment, 'eval' in quotes, a word: echo eval source .\n"
        "x=eval y='source z' z=\"$(echo eval)\"\n",
        encoding="utf-8",
    )
    findings = tool.shell_hazards(script, tmp_path)
    assert [finding.split(": ", 1)[0] for finding in findings] == [
        f"{INSTALLER}:{line}" for line in (2, 3, 4, 5, 6, 6)
    ]
    assert "eval" in findings[0]
    assert "source" in findings[1]
    assert '$"' in findings[3]


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


@pytest.mark.parametrize(
    "bad", [*dashboard.FORBIDDEN, "\r", "\n", "\u2028", "\u2029", "\u200e", "\x7f"], ids=repr
)
def test_a_dashboard_translation_holds_nothing_a_card_reads_as_code(bad: str) -> None:
    """The rule the command applies (dashboard.unsafe): Jinja, HTML, Markdown links and
    addresses, a marker, line breaks; the English text would show instead."""
    entry = Entry("{n} min ago", (f"vor {{n}} {bad} Min.",), "dashboard")
    (found,) = tool.dashboard_texts("de.po", *catalog(entry))
    assert found.startswith("de.po: '{n} min ago' (dashboard): ")


def test_a_dashboard_translation_without_them_passes() -> None:
    entries = [
        Entry("{n} min ago", ("vor {n} Min.",), "dashboard"),
        Entry("Mesh", ("Netz ‹Funk›: ein Gerät",), "dashboard"),
        Entry("Removed", ("<Entfernt> @ www.example.org",)),  # not a dashboard text
        Entry("Radio", ("<Funk>",), "home assistant"),
    ]
    assert tool.dashboard_texts("de.po", *catalog(*entries)) == []


def test_a_button_label_has_at_most_twelve_characters() -> None:
    """whiptail draws its buttons for its own labels (docs/translations.md)."""
    entries = [
        Entry("Cancel", ("Abbrechen",), "button"),
        Entry("Ok", ("x" * tool.BUTTON_LABEL,), "button"),
        Entry("Removed", ("Entfernt, und noch viel mehr",)),  # not a button
    ]
    assert tool.buttons("de.po", *catalog(*entries)) == []
    longer = Entry("Cancel", ("Abbrechen und zurück",), "button")
    assert tool.buttons("de.po", *catalog(longer, Entry("No", ("x" * 13,), "button"))) == [
        "de.po: 'Cancel' (button): 'Abbrechen und zurück' has 20 characters, a button at most 12",
        "de.po: 'No' (button): 'xxxxxxxxxxxxx' has 13 characters, a button at most 12",
    ]
    assert tool.buttons in tool.CATALOG_CHECKS


def test_the_installer_tells_translators_how_long_a_button_may_be() -> None:
    _, entries = read_po((ROOT / "deploy/proxmox/locale/installer.pot").read_text(encoding="utf-8"))
    buttons = [entry for entry in entries if entry.msgctxt == "button"]
    assert {entry.msgid for entry in buttons} == {"Yes", "No", "Ok", "Cancel"}
    for entry in buttons:
        assert any(f"at most {tool.BUTTON_LABEL} characters" in c for c in entry.comments), entry


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


PACK_HEAD = (
    "# meshcore-home-gateway installer messages, de: written by make translations from de.po; "
    "do not edit\n"
)


def test_a_pack_holds_the_translated_texts_escaped() -> None:
    """One line per translated text, sorted by key, a context joined to its text with an
    escaped EOT; backslash, tab and line break escaped as printf %b reads them."""
    entries = [
        Entry("Yes", ("Ja",), "button"),
        Entry("b {x}\tc", ("B\t{x}\nzwei \\c",), flags=("python-brace-format",)),
        Entry("a", ("A",)),
        Entry("untranslated", ("",)),
        Entry("unsure", ("unsicher",), flags=("fuzzy",)),
    ]
    assert tool.pack("de", entries) == (
        PACK_HEAD
        + "msg\ta\tA\n"
        + "msg\tb {x}\\tc\tB\\t{x}\\nzwei \\\\c\n"
        + "msg\tbutton\\x04Yes\tJa\n"
    )
    assert tool.pack("de", []) == PACK_HEAD


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        (Entry("a", ("x\x1fy",)), "U+001F"),
        (Entry("a", ("x\x00y",)), "U+0000"),
        (Entry("a", ("x\ry",)), "U+000D"),
        (Entry("a\x04b", ("x",)), "U+0004"),
        (Entry("{count} file", ("{count} Datei", "{count} Dateien"), None, "{count} files"),
         "plural"),
    ],
)  # fmt: skip
def test_a_pack_refuses_what_it_cannot_hold(entry: Entry, reason: str) -> None:
    """No installer text counts anything; a control character would end or split a line."""
    with pytest.raises(ValueError, match=re.escape(reason)):
        tool.pack("de", [entry])


def test_the_pin_block_reads_back_in_bash(tmp_path: Path) -> None:
    """What `make translations` writes into the script is what bash reads: the languages in
    order, their names, the digest of each pack; a name runs nothing."""
    languages = {"en": "English", "de": "Deutsch", "fr": "Français", "xx": "$(touch ran) 'x'"}
    packs = {"de": PACK_HEAD, "fr": "# fr\n", "xx": "# xx\nmsg\ta\tb\n"}
    block = tool.pin_block(languages, packs)
    assert block.startswith(tool.PINS_START + "\n")
    assert block.endswith(tool.PINS_END + "\n")
    program = (
        block
        + 'for code in "${L10N_ORDER[@]}"; do\n'
        + '  printf "%s\\0%s\\0%s\\0" "$code" "${L10N_NAME[$code]}" "${L10N_SHA256[$code]-}"\n'
        + "done\n"
    )
    shown = subprocess.run(  # noqa: S603 - the block just written
        [BASH, "-c", program], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    fields = shown.stdout.split("\0")[:-1]
    digest = {code: hashlib.sha256(text.encode()).hexdigest() for code, text in packs.items()}
    assert fields == [
        part for code, name in languages.items() for part in (code, name, digest.get(code, ""))
    ]
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize(
    ("languages", "packs", "reason"),
    [
        ({"en": "English", "de": "Deutsch"}, {}, "no pack for de"),
        ({"en": "English", "pt_BR": "Português"}, {"pt_BR": "#\n"}, "pt_BR"),
        ({"de": "Deutsch"}, {"de": "#\n"}, "English first"),
    ],
)
def test_the_pin_block_refuses_what_the_installer_cannot_offer(
    languages: dict[str, str], packs: dict[str, str], reason: str
) -> None:
    """The installer offers English first and checks a code against ^[a-z]{2}$ before it uses
    one (docs/development.md, Translations)."""
    with pytest.raises(ValueError, match=reason):
        tool.pin_block(languages, packs)


INSTALLER_SCRIPT = f"""#!/usr/bin/env bash
{tool.PINS_START}
L10N_ORDER=(en)
declare -A L10N_NAME=([en]=English)
declare -A L10N_SHA256=()
{tool.PINS_END}
die 'cannot read {{file}}' file "$1"
"""


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """A checkout with one module, a dashboard, a short argparse list, an installer and no
    catalog, of a version that offers English only (whatever this one ships)."""
    monkeypatch.setattr(tool.i18n, "LANGUAGES", MappingProxyType({"en": "English"}))
    package = tmp_path / "src" / "meshcore_home_gateway"
    (package / "locale").mkdir(parents=True)
    (package / "cli.py").write_text('tr("Removed")\n', encoding="utf-8")
    (package / "dashboard.yaml").write_text('title: _("Mesh")\n', encoding="utf-8")
    (tmp_path / "tools").mkdir()
    listed = [["usage: ", None], ["an argparse text of an older Python", None]]
    (tmp_path / "tools" / "argparse_messages.json").write_text(json.dumps(listed), encoding="utf-8")
    installer = tmp_path / INSTALLER
    (installer.parent / "locale").mkdir(parents=True)
    installer.write_text(INSTALLER_SCRIPT, encoding="utf-8")
    return tool.Project(tmp_path)


def test_update_writes_the_template_and_merges_the_catalogs(project: Any) -> None:
    written = tool.update(project)
    assert written == [project.argparse_list, project.template, project.installer_template]
    listed = json.loads(project.argparse_list.read_text(encoding="utf-8"))
    assert ["an argparse text of an older Python", None] in listed  # never removed
    assert ["show this help message and exit", None] in listed  # this Python's
    assert listed == sorted(listed, key=lambda pair: (pair[0], pair[1] or ""))
    assert tool.check(project) == []
    _, template = read_po(project.template.read_text(encoding="utf-8"))
    assert template[0] == Entry("Removed", ("",), references=("src/meshcore_home_gateway/cli.py",))
    assert template[-1].references == ("tools/argparse_messages.json",)
    mesh = Entry(
        "Mesh", ("",), "dashboard", references=("src/meshcore_home_gateway/dashboard.yaml",)
    )
    assert template[1] == mesh
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
    pins, *findings = tool.check(project, {"en": "English", "de": "Deutsch"})
    assert pins == f"{INSTALLER}: no pack for de"  # the installer has no catalog of it
    assert len(findings) == len(entries)
    assert findings[0] == "src/meshcore_home_gateway/locale/de.po: untranslated: 'Removed'"
    assert all(": untranslated: " in finding for finding in findings)


def test_check_finds_a_dashboard_text_a_card_would_read_as_code(project: Any) -> None:
    tool.update(project)
    german = project.locale / "de.po"
    entries = [Entry("Mesh", ("<b>Netz</b>",), "dashboard"), Entry("Removed", ("Entfernt",))]
    german.write_text(tool.write_po({"Language": "de"}, entries), encoding="utf-8")
    tool.update(project)
    findings = tool.check(project, {"en": "English", "de": "Deutsch"})
    name = "src/meshcore_home_gateway/locale/de.po"
    assert f"{name}: 'Mesh' (dashboard): '<' cannot be in a dashboard text" in findings


def test_check_finds_a_template_behind_the_code(project: Any) -> None:
    tool.update(project)
    (project.package / "cli.py").write_text('tr("Removed")\ntr("Added")\n', encoding="utf-8")
    name = "src/meshcore_home_gateway/locale/meshcore-home-gateway.pot"
    assert tool.check(project) == [f"{name}: not current: run make translations"]
    project.template.unlink()
    assert tool.check(project) == [f"{name}: missing: run make translations"]


def test_the_installer_has_its_own_template_catalogs_and_packs(project: Any) -> None:
    """deploy/proxmox/locale: the installer's template, a catalog per language merged with it, and
    the language pack the installer loads, written from the catalog."""
    tool.update(project)
    template = "deploy/proxmox/locale/installer.pot"
    _, entries = read_po(project.installer_template.read_text(encoding="utf-8"))
    assert entries == [Entry("cannot read {file}", ("",), flags=("python-brace-format",))]

    german = project.installer_locale / "de.po"
    german.write_text(
        tool.write_po({"Language": "de"}, [Entry("cannot read {file}", ("{file} unlesbar",))]),
        encoding="utf-8",
    )
    findings = tool.check(project)
    assert "deploy/proxmox/locale/de.po: does not follow the template: run make translations" in (
        findings
    )
    assert "deploy/proxmox/locale/de.messages: missing: run make translations" in findings
    assert tool.update(project) == [german, project.pack("de")]
    assert project.pack("de").read_text(encoding="utf-8") == (
        PACK_HEAD + "msg\tcannot read {file}\t{file} unlesbar\n"
    )
    assert tool.check(project) == []
    assert tool.update(project) == []

    project.pack("de").write_text(PACK_HEAD, encoding="utf-8")
    (project.installer_locale / "fr.messages").write_text("# fr\n", encoding="utf-8")
    assert tool.check(project) == [
        "deploy/proxmox/locale/de.messages: not current: run make translations",
        "deploy/proxmox/locale/fr.messages: no catalog fr.po: remove it",
    ]
    (project.installer_locale / "fr.messages").unlink()
    tool.update(project)

    project.installer.write_text(INSTALLER_SCRIPT + "die 'gone'\n", encoding="utf-8")
    assert tool.check(project) == [
        f"{template}: not current: run make translations",
        "deploy/proxmox/locale/de.po: does not follow the template: run make translations",
    ]
    project.installer.write_text(INSTALLER_SCRIPT + 'die "gone $x"\n', encoding="utf-8")
    (finding,) = tool.check(project)
    assert finding.startswith(f"{INSTALLER}:8: ")
    with pytest.raises(tool.ExtractError):
        tool.update(project)


def test_an_installer_catalog_that_no_pack_can_hold_is_found(project: Any) -> None:
    """A catalog with a plural, as a translation tool may write one: the pack check names it."""
    tool.update(project)
    german = project.installer_locale / "de.po"
    counted = Entry("{count} file", ("{count} Datei", "{count} Dateien"), None, "{count} files")
    german.write_text(
        tool.write_po(tool.catalog_header("de", GERMANIC), [counted]), encoding="utf-8"
    )
    (finding,) = tool.stale_packs(project)
    assert finding.startswith("deploy/proxmox/locale/de.po: '{count} file': a plural")
    german.write_text('msgid "a"\nmsgstr "\\q"\n', encoding="utf-8")
    assert tool.stale_packs(project) == []  # canonical() names a catalog it cannot read
    findings = tool.check(project)
    assert any(f.startswith("deploy/proxmox/locale/de.po:2: ") for f in findings)
    assert not [f for f in findings if f.startswith(INSTALLER)]  # its pins need no German pack


def test_a_wrong_installer_translation_is_found(project: Any) -> None:
    """The catalog checks hold for the installer's catalogs too."""
    tool.update(project)
    german = project.installer_locale / "de.po"
    entry = Entry("cannot read {file}", ("{datei} unlesbar",), flags=("python-brace-format",))
    german.write_text(tool.write_po(tool.catalog_header("de", GERMANIC), [entry]), encoding="utf-8")
    tool.update(project)
    (finding,) = tool.check(project)
    assert finding.startswith("deploy/proxmox/locale/de.po: 'cannot read {file}': ")
    assert "{datei}" in finding


GERMAN = {"en": "English", "de": "Deutsch"}


def test_update_pins_each_pack_in_the_installer(project: Any) -> None:
    """The installer's block of language packs: the languages of i18n.LANGUAGES, their names and
    the digest of each pack, written by update and checked by check."""
    tool.update(project)
    assert tool.stale_pins(project, {"en": "English"}) == []
    german = project.installer_locale / "de.po"
    german.write_text(
        tool.write_po(tool.catalog_header("de", GERMANIC),
                      [Entry("cannot read {file}", ("{file} unlesbar",))]),
        encoding="utf-8",
    )  # fmt: skip
    tool.update(project)
    assert tool.stale_pins(project, GERMAN) == [
        f"{INSTALLER}: the block of language packs is not current: run make translations"
    ]
    assert tool.update(project, GERMAN) == [project.installer]
    pack = project.pack("de").read_text(encoding="utf-8")
    assert tool.pin_block(GERMAN, {"de": pack}) in project.installer.read_text(encoding="utf-8")
    assert project.installer.read_text(encoding="utf-8").endswith(
        "\ndie 'cannot read {file}' file \"$1\"\n"
    )
    assert tool.stale_pins(project, GERMAN) == []
    assert tool.update(project, GERMAN) == []

    german.write_text(
        german.read_text(encoding="utf-8").replace("unlesbar", "nicht lesbar"), encoding="utf-8"
    )
    assert tool.stale_pins(project, GERMAN) == [
        f"{INSTALLER}: the block of language packs is not current: run make translations"
    ]
    assert tool.update(project, GERMAN) == [project.pack("de"), project.installer]
    assert tool.check(project, GERMAN) == [
        "src/meshcore_home_gateway/locale/de.po: missing: i18n.LANGUAGES offers de"
    ]


def test_a_release_publishes_each_pack_its_installer_pins(project: Any, tmp_path: Path) -> None:
    """make release: beside the installer, by the name the installer fetches from its release,
    and only the pack whose digest the installer carries."""
    release = tmp_path / "release"
    release.mkdir()
    published = release / project.installer.name
    tool.update(project)
    shutil.copy(project.installer, published)
    assert tool.release_packs(project, release) == []  # English needs none
    german = project.installer_locale / "de.po"
    german.write_text(
        tool.write_po(tool.catalog_header("de", GERMANIC),
                      [Entry("cannot read {file}", ("{file} unlesbar",))]),
        encoding="utf-8",
    )  # fmt: skip
    tool.update(project, GERMAN)
    shutil.copy(project.installer, published)
    (pack,) = tool.release_packs(project, release)
    assert pack == release / "meshcore-home-gateway-lxc.de.messages"
    assert pack.read_bytes() == project.pack("de").read_bytes()
    assert tool.RELEASE_PACK.format(language="de") == pack.name

    project.pack("de").write_text("# another\n", encoding="utf-8")
    with pytest.raises(
        ValueError, match=r"de\.messages: not the pack the installer pins: run make"
    ):
        tool.release_packs(project, release)
    project.pack("de").unlink()
    with pytest.raises(ValueError, match=r"de\.messages: missing: run make translations"):
        tool.release_packs(project, release)


@pytest.mark.parametrize(
    ("block", "reason"),
    [
        ("L10N_ORDER=(en de)\ndeclare -A L10N_SHA256=()\n", "offers de without a pin"),
        (f"L10N_ORDER=(en)\ndeclare -A L10N_SHA256=([de]={'0' * 64})\n", "pins de, not offered"),
        ("L10N_ORDER=(en de)\n", "no L10N_SHA256"),
    ],
)
def test_a_release_refuses_an_installer_whose_pins_do_not_hold(
    project: Any, tmp_path: Path, block: str, reason: str
) -> None:
    text = project.installer.read_text(encoding="utf-8")
    old = tool.pin_block({"en": "English"}, {})
    (tmp_path / project.installer.name).write_text(
        text.replace(old, f"{tool.PINS_START}\n{block}{tool.PINS_END}\n"), encoding="utf-8"
    )
    with pytest.raises(ValueError, match=reason):
        tool.release_packs(project, tmp_path)


@pytest.mark.parametrize("blocks", [0, 2])
def test_the_installer_needs_one_block_of_language_packs(project: Any, blocks: int) -> None:
    text = project.installer.read_text(encoding="utf-8")
    block = tool.pin_block({"en": "English"}, {})
    project.installer.write_text(text.replace(block, block * blocks), encoding="utf-8")
    (finding,) = tool.stale_pins(project, {"en": "English"})
    assert finding == (
        f"{INSTALLER}: the script needs one block of language packs, between "
        f"{tool.PINS_START!r} and {tool.PINS_END!r}"
    )
    with pytest.raises(ValueError, match="one block of language packs"):
        tool.update(project)


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


def test_main_publishes_the_packs_of_a_release(
    project: Any, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:  # fmt: skip
    monkeypatch.setattr(tool, "ROOT", project.root)
    release = tmp_path / "release"
    release.mkdir()
    (release / project.installer.name).write_text(
        f"{tool.PINS_START}\nL10N_ORDER=(en de)\ndeclare -A L10N_SHA256=([de]={'0' * 64})\n"
        f"{tool.PINS_END}\n",
        encoding="utf-8",
    )
    assert tool.main(["release-packs", str(release)]) == 1
    assert (
        capsys.readouterr().err
        == "deploy/proxmox/locale/de.messages: missing: run make translations\n"
    )
    pack = "#\n"
    project.pack("de").write_text(pack, encoding="utf-8")
    digest = hashlib.sha256(pack.encode()).hexdigest()
    (release / project.installer.name).write_text(
        f"{tool.PINS_START}\nL10N_ORDER=(en de)\ndeclare -A L10N_SHA256=([de]={digest})\n"
        f"{tool.PINS_END}\n",
        encoding="utf-8",
    )
    assert tool.main(["release-packs", str(release)]) == 0
    assert capsys.readouterr().out == f"wrote {release / 'meshcore-home-gateway-lxc.de.messages'}\n"
