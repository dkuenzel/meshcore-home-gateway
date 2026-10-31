"""Translations (ADR 0019, ADR 0027): the templates of the texts, the catalogs, the installer's
language packs and their checks.

    python3 tools/i18n.py update   # what `make translations` runs: templates, catalogs, packs, pins
    python3 tools/i18n.py check    # the checks of the tests, without pytest; exit 1 on a finding
    python3 tools/i18n.py release-packs dist/release   # make release: the packs of the installer

`update` extracts every text marked in the package (docs/development.md, Translations), the
texts of the dashboard's template and argparse's own words into
src/meshcore_home_gateway/locale/meshcore-home-gateway.pot, adds the
running Python's argparse texts to tools/argparse_messages.json (it never removes one: the list
serves every Python of the matrix) and merges each catalog with the template. The installer's
texts go into deploy/proxmox/locale/installer.pot the same way; each of its catalogs also gives
the language pack the installer loads (<language>.messages), whose SHA-256 the installer carries
in its block of language packs, with the languages of i18n.LANGUAGES. The files are written in one
canonical form, without dates or names, so the tests can compare them with what the code says.
Standard library only; it puts src/ on the path itself.
"""

from __future__ import annotations

import argparse
import ast
import bisect
import dataclasses
import hashlib
import inspect
import io
import json
import re
import shlex
import string
import sys
import tokenize
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from meshcore_home_gateway import dashboard, i18n  # noqa: E402 - once src/ is on the path
from meshcore_home_gateway.catalog import Entry, PoError, plural_rule, read_po  # noqa: E402

PROJECT_NAME: Final = "meshcore-home-gateway"
PREFIX: Final = "meshcore-home-gateway: "  # printed before a text, never part of one
ARGPARSE_COMMENT: Final = "Python's argparse: usage, help and errors"
PLURAL_FORMS: Final = {  # each language's rule, as GNU gettext and CLDR give it
    "de": "nplurals=2; plural=(n != 1);",
    "nl": "nplurals=2; plural=(n != 1);",
    "fr": "nplurals=2; plural=(n > 1);",
    "pl": "nplurals=3; plural=(n==1 ? 0 : n%10>=2 && n%10<=4 && (n%100<10 || n%100>=20) ? 1 : 2);",
}
# Words a translation keeps as they are, as often as the English text has them.
TOKENS: Final = re.compile(
    "|".join([
        r"(?<![\w-])--[a-z][\w-]*",  # options
        r"\$[A-Z_]+",  # environment variables
        r"(?<![\w/.])/[\w-]+(?:[./][\w-]+)*",  # absolute paths
        (
            r"\b(?:radio|mqtt|homeassistant|messages|airtime|commands|targets|history|contacts"
            r"|telemetry|diagnostics)(?:\.[a-z_]+)+"
        ),  # configuration keys
        r"KEY12…KEY8",
        r"\bADR \d{4}\b",
        r"\bdocs/[\w/-]*\.md\b",
        r"https?://[\w.~:/?#@!$&'()*+,;=%-]*[\w/#=-]",  # addresses
        r"\bpct exec\b",
        r"\bjournalctl\b",
    ])
)  # fmt: skip


@dataclass(frozen=True)
class Project:
    """Where the texts and the catalogs are: the checkout, or a test's copy of one."""

    root: Path

    @property
    def package(self) -> Path:
        return self.root / "src" / "meshcore_home_gateway"

    @property
    def locale(self) -> Path:
        return self.package / "locale"

    @property
    def template(self) -> Path:
        return self.locale / "meshcore-home-gateway.pot"

    @property
    def argparse_list(self) -> Path:
        return self.root / "tools" / "argparse_messages.json"

    def catalogs(self) -> list[Path]:
        return sorted(self.locale.glob("*.po"))

    @property
    def installer(self) -> Path:
        return self.root / "deploy" / "proxmox" / "meshcore-home-gateway-lxc.sh"

    @property
    def installer_locale(self) -> Path:
        return self.installer.parent / "locale"

    @property
    def installer_template(self) -> Path:
        return self.installer_locale / "installer.pot"

    def installer_catalogs(self) -> list[Path]:
        return sorted(self.installer_locale.glob("*.po"))

    def pack(self, language: str) -> Path:
        """The installer's language pack of a catalog."""
        return self.installer_locale / f"{language}.messages"


def label(project: Project, path: Path) -> str:
    """A file as the findings name it: relative to the checkout."""
    return path.relative_to(project.root).as_posix()


def catalog_header(language: str, plural_forms: str) -> dict[str, str]:
    """A catalog's whole header: no dates, no names, the credit is in git's history."""
    return {
        "Project-Id-Version": PROJECT_NAME,
        "Language": language,
        "MIME-Version": "1.0",
        "Content-Type": "text/plain; charset=UTF-8",
        "Content-Transfer-Encoding": "8bit",
        "Plural-Forms": plural_forms,
    }


TEMPLATE_HEADER: Final = {
    key: value
    for key, value in catalog_header("", "").items()
    if key not in {"Language", "Plural-Forms"}
}


# --- extraction ------------------------------------------------------------------------------


class ExtractError(ValueError):
    """A marked text the template cannot hold; the message names the file and line."""


@dataclass(frozen=True)
class Keyword:
    """Where a function that marks texts takes them (positions of its arguments)."""

    msgid: int = 0
    plural: int | None = None
    context: int | str | None = None  # the argument that names the context, or the context
    argparse: bool = False  # argparse %-formats the text: braces are text, never filled
    later: bool = False  # the values come where the text is shown


KEYWORDS: Final = {
    "tr": Keyword(),
    "tr_help": Keyword(argparse=True),
    "trn": Keyword(plural=1),
    "N_": Keyword(later=True),
    "ha_name": Keyword(context=i18n.HOME_ASSISTANT),
    "N_ha": Keyword(context=i18n.HOME_ASSISTANT, later=True),
    "pgettext": Keyword(msgid=1, context=0),
    "Text": Keyword(),
    "TextError": Keyword(),
    "ConfigError": Keyword(),
    "LinkError": Keyword(msgid=1),  # after the reason, a code of the contact event
}


def extract_python(root: Path, top: Path) -> list[Entry]:
    """The texts marked in the modules below root, each with the files that use it. A name, an
    attribute or a subscript where a text belongs is a text marked elsewhere (N_, N_ha)."""
    found: dict[str, Entry] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        name = path.relative_to(top).as_posix()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, name)
        comments = _translator_comments(source)
        statements = {
            node: statement
            for statement in ast.walk(tree)
            if isinstance(statement, ast.stmt)
            for node in ast.walk(statement)
        }  # each node's innermost statement: ast.walk reaches the inner ones later
        calls = sorted(
            (node for node in ast.walk(tree) if isinstance(node, ast.Call)),
            key=lambda node: (node.lineno, node.col_offset),
        )
        for call in calls:
            keyword = KEYWORDS.get(_callee(call))
            entry = None if keyword is None else _message(call, keyword, name)
            if keyword is None or entry is None:
                continue
            lines = (call.args[keyword.msgid].lineno, call.lineno, statements[call].lineno)
            notes = next((comments[line] for line in lines if line in comments), ())
            _add(found, replace(entry, comments=notes), f"{name}:{call.lineno}")
    return list(found.values())


def _callee(call: ast.Call) -> str:
    if isinstance(call.func, ast.Name):
        return call.func.id
    return call.func.attr if isinstance(call.func, ast.Attribute) else ""


def _message(call: ast.Call, keyword: Keyword, name: str) -> Entry | None:
    msgid = _literal(call, keyword.msgid, name, deferred=True)
    if msgid is None:
        return None
    plural = None if keyword.plural is None else _literal(call, keyword.plural, name)
    context = keyword.context
    if isinstance(context, int):
        context = _literal(call, context, name)
    where = f"{name}:{call.args[keyword.msgid].lineno}"
    texts = [msgid] if plural is None else [msgid, plural]
    for text in texts:
        if not text:
            raise ExtractError(f"{where}: an empty msgid")
        if text.startswith(PREFIX):
            raise ExtractError(f"{where}: the prefix {PREFIX!r} is printed before a text")
    flags: tuple[str, ...] = ("python-format",)
    if not keyword.argparse:
        fields = set().union(*(_field_names(text, where) for text in texts))
        if not keyword.later:
            _check_values(call, fields, plural is not None, where)
        flags = ("python-brace-format",) if fields else ()
    return Entry(msgid, ("",) * len(texts), context, plural, flags, references=(name,))


def _literal(call: ast.Call, index: int, name: str, *, deferred: bool = False) -> str | None:
    if index >= len(call.args):
        raise ExtractError(f"{name}:{call.lineno}: {_callee(call)}() has no text")
    argument = call.args[index]
    if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
        return argument.value
    if deferred and isinstance(argument, ast.Name | ast.Attribute | ast.Subscript):
        return None
    raise ExtractError(
        f"{name}:{argument.lineno}: {_callee(call)}() takes a string literal here, not "
        f"{ast.unparse(argument)}: put the values in as named fields"
    )


def _field_names(text: str, where: str) -> set[str]:
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise ExtractError(f"{where}: {text!r} is not a format string: {exc}") from exc
    names = set()
    for _, field, _, _ in parsed:
        if field is None:
            continue
        name = field.split(".", 1)[0].split("[", 1)[0]
        if not name or name.isdigit():
            raise ExtractError(f"{where}: {text!r} needs named fields, such as {{name}}")
        names.add(name)
    return names


def _check_values(call: ast.Call, fields: set[str], plural: bool, where: str) -> None:
    given = {keyword.arg for keyword in call.keywords}
    if None in given:  # **values: known only when the code runs
        return
    missing = sorted(fields - given - ({"count"} if plural else set()))
    if missing:
        raise ExtractError(f"{where}: no value for {', '.join(missing)}")


def _translator_comments(source: str) -> dict[int, tuple[str, ...]]:
    """`# Translators:` comments of a module, by the line below them."""
    return _comment_blocks({
        token.start[0]: token.string[1:].strip()
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and token.line.lstrip().startswith("#")
    })  # fmt: skip


def _comment_blocks(lines: Mapping[int, str]) -> dict[int, tuple[str, ...]]:
    """`# Translators:` comments, and the comment lines after them, by the line below them; from
    the comment lines (their text after the #) by their numbers."""
    runs: list[list[int]] = []
    for number in sorted(lines):
        if runs and runs[-1][-1] == number - 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    blocks = {}
    for run in runs:
        starts = [number for number in run if lines[number].startswith("Translators:")]
        if starts:
            texts = [lines[number] for number in run if number >= starts[-1]]
            texts[0] = texts[0].removeprefix("Translators:").strip()
            blocks[run[-1] + 1] = tuple(texts)
    return blocks


def _add(found: dict[str, Entry], entry: Entry, where: str) -> None:
    known = found.get(entry.key)
    if known is None:
        found[entry.key] = entry
        return
    if known.msgid_plural != entry.msgid_plural:
        raise ExtractError(
            f"{where}: {entry.msgid!r} with the plural {entry.msgid_plural!r}, "
            f"elsewhere {known.msgid_plural!r}"
        )
    found[entry.key] = replace(
        known,
        flags=tuple(sorted({*known.flags, *entry.flags})),
        comments=tuple(dict.fromkeys([*known.comments, *entry.comments])),
        references=tuple(dict.fromkeys([*known.references, *entry.references])),
    )


# a translator's note in a card's content, where a `#` line would be the card's own text
_JINJA_NOTE: Final = re.compile(r"\{#-?\s*Translators:\s*(.*?)\s*-?#\}")


def extract_dashboard(path: Path, top: Path) -> list[Entry]:
    """The texts of the dashboard's template, msgctxt "dashboard": every _("…") (dashboard.py),
    with the translator's note of the line above it, a `# Translators:` comment above a whole
    value and a `{#- Translators: … -#}` line in a card's content."""
    name = path.relative_to(top).as_posix()
    lines = path.read_text(encoding="utf-8").split("\n")
    comments = _comment_blocks({
        number: line.strip()[1:].strip()
        for number, line in enumerate(lines, 1)
        if line.strip().startswith("#")
    })  # fmt: skip
    notes = {
        number + 1: (match.group(1),)
        for number, line in enumerate(lines, 1)
        if (match := _JINJA_NOTE.fullmatch(line.strip()))
    }
    found: dict[str, Entry] = {}
    for number, line in enumerate(lines, 1):
        where = f"{name}:{number}"
        note = comments if dashboard.VALUE.match(line) else notes
        for marker in dashboard.MARKER.finditer(line):
            try:
                msgid = json.loads(f'"{marker.group(1)}"')
            except ValueError as exc:
                raise ExtractError(
                    f"{where}: {marker.group()} holds no JSON string: {exc}"
                ) from exc
            if not msgid:
                raise ExtractError(f"{where}: an empty msgid")
            flags = ("python-brace-format",) if _field_names(msgid, where) else ()
            entry = Entry(msgid, ("",), i18n.DASHBOARD, None, flags, note.get(number, ()), (name,))
            _add(found, entry, where)
    return list(found.values())


def extract_argparse(path: Path, top: Path) -> list[Entry]:
    """argparse's own texts, as the list names them: argparse %-formats them."""
    reference = (path.relative_to(top).as_posix(),)
    return [
        Entry(
            msgid,
            ("",) if plural is None else ("", ""),
            None,
            plural,
            ("python-format",),
            (ARGPARSE_COMMENT,),
            reference,
        )
        for msgid, plural in json.loads(path.read_text(encoding="utf-8"))
    ]


def argparse_messages() -> set[tuple[str, str | None]]:
    """The texts this Python's argparse passes to _() and ngettext(), with their plurals."""
    found: set[tuple[str, str | None]] = set()
    for node in ast.walk(ast.parse(inspect.getsource(argparse))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            count = {"_": 1, "ngettext": 2}.get(node.func.id, 0)
            texts = [
                argument.value
                for argument in node.args[:count]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
            ]
            if count and len(texts) == count:
                found.add((texts[0], texts[1] if count == 2 else None))
    return found


# --- the installer's texts: t, tp, die and $say with a literal (docs/development.md, Translations)

SHELL_KEYWORDS: Final = frozenset({"t", "tp", "die", "$say"})
SHELL_PASSING: Final = frozenset({"die"})  # passes its own text on: t "$@"
# the blocks whose texts are no calls: the message functions themselves, and the pins
SHELL_BLOCKS: Final = (
    ("# --- messages ", "# --- end of messages"),
    ("# --- language packs", "# --- end of language packs"),
)
_PLAIN: Final = re.compile(r"[A-Za-z0-9_.,:/@%+=-]+")  # a word that is a literal unquoted
_SHELL_FIELD: Final = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")  # what _fill fills in
_ASSIGNMENT: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^]]*\])?\+?=")
_FUNCTION: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
# words before a command's name
_PREFIXES: Final = frozenset({"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"})
_OPERATORS: Final = (";;&", ";;", ";&", "&&", "||", "|&", ";", "&", "|", "(", ")", "\n")
_ENDS_STATEMENT: Final = frozenset({";;&", ";;", ";&", ";", "&", "(", ")", "\n"})
_EMPTY_PARENTHESES: Final = re.compile(r"[ \t]*\)")
_NAME: Final = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9#?@*!$-]?)")  # $NAME, $1, $#, …


@dataclass(frozen=True)
class _Word:
    text: str  # as written
    line: int
    literal: str | None  # what it stands for: one quoted string, or plain characters


@dataclass
class _Scope:
    """What a scan keeps between its words."""

    statement: int  # the first line of the outermost command
    nested: bool  # in a $( or <(
    words: list[_Word] = dataclasses.field(default_factory=list)  # the command so far
    depth: int = 0  # ( open
    cases: int = 0  # case statements open
    patterns: bool = False  # in a case statement, before a pattern's )
    fresh: bool = True  # the next word starts a statement


@dataclass(frozen=True)
class _Command:
    words: tuple[_Word, ...]
    statement: int  # the first line of the outermost command around it
    function: str  # the function whose body holds it; "" outside one

    def head(self) -> int | None:
        """Where its name is: after assignments and words such as if, then, !."""
        for index, word in enumerate(self.words):
            if word.text not in _PREFIXES and not _ASSIGNMENT.match(word.text):
                return index
        return None


class _Shell:
    """As much of bash's grammar as finding the commands takes: their words, quotes, $(…), ${…},
    here-documents, case patterns and functions; and the comment lines. A case statement inside
    $(…) is beyond it, and it refuses `…` (the installer has neither)."""

    def __init__(self, text: str, name: str) -> None:
        self.text = text
        self.name = name
        self.at = 0
        self.starts = [0] + [match.end() for match in re.finditer("\n", text)]
        self.commands: list[_Command] = []
        self.comments: dict[int, str] = {}  # the comment lines, by their numbers
        self.translated: list[int] = []  # lines with bash's own $"…"
        self.here_documents: list[tuple[str, bool]] = []  # delimiters, and if tabs go first
        self.function = ""
        self.defining = ""  # a function's name, before its body
        self.braces = 0  # { … } open in the function's body
        self._scan(statement=0, nested=False)

    def line(self, at: int) -> int:
        return bisect.bisect_right(self.starts, at)

    def _error(self, at: int, what: str) -> ExtractError:
        return ExtractError(f"{self.name}:{self.line(at)}: {what} is not closed")

    def _scan(self, statement: int, nested: bool) -> None:
        """Commands up to the end, or (nested) up to the ) that closes a $( or <(."""
        text = self.text
        opened = self.at
        scope = _Scope(statement, nested)
        while self.at < len(text):
            char = text[self.at]
            if char in " \t":
                self.at += 1
            elif text.startswith("\\\n", self.at):
                self.at += 2
            elif char == "#":  # at a word's start: a comment
                self._comment()
            elif char in "\n;|()" or (char == "&" and not text.startswith("&>", self.at)):
                if self._operator(scope):
                    return
            else:
                self._add_word(scope)
        if nested:
            raise self._error(opened, "$(")
        self._end(scope, "")

    def _operator(self, scope: _Scope) -> bool:
        """An operator ends the command so far; True when it closes the scan's $(."""
        operator = next(o for o in _OPERATORS if self.text.startswith(o, self.at))
        self.at += len(operator)
        if operator == "\n":
            self._skip_here_documents()
        if scope.patterns:  # a pattern's |, ( and line breaks; its ) ends it
            scope.patterns = operator != ")"
            return False
        closing = operator == ")" and scope.nested and scope.depth == 0
        scope.depth = max(scope.depth + {"(": 1, ")": -1}.get(operator, 0), 0)
        self._end(scope, operator)
        scope.patterns = scope.cases > 0 and operator in {";;", ";&", ";;&"}
        scope.fresh = operator in _ENDS_STATEMENT
        return closing

    def _add_word(self, scope: _Scope) -> None:
        if scope.fresh and not scope.nested:
            scope.statement, scope.fresh = self.line(self.at), False
        word = self._word(scope.statement)
        if scope.patterns and word.text != "esac":
            return
        if word.text == "esac":
            scope.patterns, scope.cases = False, max(scope.cases - 1, 0)
        if not scope.nested and all(known.text in _PREFIXES for known in scope.words):
            self._braces_of_functions(word.text)
        scope.words.append(word)
        self._here_document(scope.words)
        if len(scope.words) == 3 and scope.words[0].text == "case" and word.text == "in":
            self._end(scope, "")
            scope.patterns, scope.cases = True, scope.cases + 1

    def _end(self, scope: _Scope, operator: str) -> None:
        """The command so far is complete, or it names a function whose definition starts."""
        words, scope.words = scope.words, []
        if (
            len(words) == 1
            and operator == "("
            and _FUNCTION.fullmatch(words[0].text)
            and _EMPTY_PARENTHESES.match(self.text, self.at)
        ):  # NAME() { …: a definition, not a call
            if not scope.nested:
                self.defining = words[0].text
            return
        if words:
            self.commands.append(_Command(tuple(words), scope.statement, self.function))

    def _braces_of_functions(self, first: str) -> None:
        if first == "{":
            self.braces += 1
            if self.defining:
                self.function, self.defining = self.defining, ""
        elif first == "}":
            self.braces = max(self.braces - 1, 0)
            if not self.braces:
                self.function = ""

    def _comment(self) -> None:
        end = self.text.find("\n", self.at)
        end = len(self.text) if end < 0 else end
        start = self.starts[self.line(self.at) - 1]
        if not self.text[start : self.at].strip():
            self.comments[self.line(self.at)] = self.text[self.at + 1 : end].strip()
        self.at = end

    def _here_document(self, words: list[_Word]) -> None:
        """<<WORD, << WORD: the lines after this one up to WORD are text, not commands."""
        word = words[-1].text
        before = words[-2].text if len(words) > 1 else ""
        if word.startswith("<<") and not word.startswith("<<<") and word not in {"<<", "<<-"}:
            delimiter, tabs = word[2:], False
        elif before in {"<<", "<<-"}:
            delimiter, tabs = word, before == "<<-"
        else:
            return
        if delimiter.startswith("-"):
            delimiter, tabs = delimiter[1:], True
        self.here_documents.append((re.sub(r"[\"'\\]", "", delimiter), tabs))

    def _skip_here_documents(self) -> None:
        for delimiter, tabs in self.here_documents:
            opened = self.at
            while True:
                if self.at >= len(self.text):
                    raise self._error(opened, f"the here-document {delimiter}")
                end = self.text.find("\n", self.at)
                end = len(self.text) if end < 0 else end
                line = self.text[self.at : end]
                self.at = end + 1
                if (line.lstrip("\t") if tabs else line) == delimiter:
                    break
        self.here_documents = []

    def _word(self, statement: int) -> _Word:
        text = self.text
        start = self.at
        parts: list[str | None] = []  # each part's literal text, None where it is none
        plain = ""
        while self.at < len(text):
            char = text[self.at]
            if text.startswith(("<(", ">("), self.at):
                self.at += 2
                self._scan(statement, nested=True)
                parts.append(None)
                continue
            if char in " \t\n;|()" or (
                char == "&" and text[self.at - 1] not in "<>" and not text.startswith("&>", self.at)
            ):  # &> and >& redirect: they are words
                break
            if char not in "'\"$`\\":
                plain += char
                self.at += 1
                continue
            if plain:
                parts.append(plain if _PLAIN.fullmatch(plain) else None)
                plain = ""
            if char == "'":
                end = text.find("'", self.at + 1)
                if end < 0:
                    raise self._error(self.at, "a quote")
                parts.append(text[self.at + 1 : end])
                self.at = end + 1
            elif char == '"':
                parts.append(self._double(statement))
            elif char == "$":
                self._expansion(statement)
                parts.append(None)
            elif char == "`":
                self._backticks()
                parts.append(None)
            else:  # a backslash and the character it escapes
                self.at += 2
                parts.append(None)
        if plain:
            parts.append(plain if _PLAIN.fullmatch(plain) else None)
        literal = parts[0] if len(parts) == 1 else None
        return _Word(text[start : self.at], self.line(start), literal)

    def _double(self, statement: int) -> str | None:
        """A double-quoted string from its quote: its text, None if it holds $, ` or \\."""
        text = self.text
        opened = self.at
        self.at += 1
        literal = True
        while self.at < len(text) and text[self.at] != '"':
            char = text[self.at]
            if char in "$`\\":
                literal = False
            if char == "$":
                self._expansion(statement)
            elif char == "`":
                self._backticks()
            else:
                self.at += 2 if char == "\\" else 1
        if self.at >= len(text):
            raise self._error(opened, "a quote")
        self.at += 1
        return text[opened + 1 : self.at - 1] if literal else None

    def _expansion(self, statement: int) -> None:
        """$(…), $((…)), ${…}, $'…', $"…" or $NAME from its $."""
        text = self.text
        following = text[self.at + 1 : self.at + 2]
        if following == "(":
            self.at += 2
            self._scan(statement, nested=True)
        elif following == "{":
            self._parameter(statement)
        elif following == "'":
            opened = self.at
            self.at += 2
            while self.at < len(text) and text[self.at] != "'":
                self.at += 2 if text[self.at] == "\\" else 1
            if self.at >= len(text):
                raise self._error(opened, "a quote")
            self.at += 1
        elif following == '"':
            self.translated.append(self.line(self.at))
            self.at += 1
            self._double(statement)
        else:
            name = _NAME.match(text, self.at)
            self.at = name.end() if name else self.at + 1

    def _parameter(self, statement: int) -> None:
        """${…} from its $, with what it holds: quotes, $(…) and further ${…}."""
        text = self.text
        opened = self.at
        self.at += 2
        depth = 1
        while depth:
            if self.at >= len(text):
                raise self._error(opened, "${")
            char = text[self.at]
            if char == "'":
                end = text.find("'", self.at + 1)
                if end < 0:
                    raise self._error(self.at, "a quote")
                self.at = end + 1
            elif char == '"':
                self._double(statement)
            elif char == "$" and text[self.at + 1 : self.at + 2] in {"(", "'"}:
                self._expansion(statement)
            elif text.startswith("${", self.at):
                self.at += 2
                depth += 1
            else:
                if char == "}":
                    depth -= 1
                self.at += 2 if char == "\\" else 1

    def _backticks(self) -> None:
        """`…`: a message call inside would escape the extraction, so the script uses $(…)."""
        raise ExtractError(f"{self.name}:{self.line(self.at)}: a `…` substitution: write $(…)")


def _without_blocks(text: str) -> str:
    """The script without the message functions' block and the pins' block: their texts are no
    calls. The line numbers stay."""
    lines = text.split("\n")
    end = ""
    for number, line in enumerate(lines):
        if not end:
            end = next((last for first, last in SHELL_BLOCKS if line.startswith(first)), "")
        if end:
            lines[number] = ""
            end = "" if line == end else end
    return "\n".join(lines)


def extract_shell(path: Path, top: Path) -> list[Entry]:
    """The installer's texts: what t, die and $say take first, and tp's context and text. A
    literal is single-quoted, double-quoted without $, ` and \\, or plain characters; its fields
    are {name}s with their values. Only die passes its own text on (t "$@"). No references: the
    template has one file."""
    name = path.relative_to(top).as_posix()
    shell = _Shell(_without_blocks(path.read_text(encoding="utf-8")), name)
    comments = _comment_blocks(shell.comments)
    found: dict[str, Entry] = {}
    for command in shell.commands:
        head = command.head()
        if head is None or command.words[head].text not in SHELL_KEYWORDS:
            continue
        keyword, arguments = command.words[head], command.words[head + 1 :]
        entry = _shell_message(keyword, arguments, command.function, name)
        if entry is None:
            continue
        lines = (arguments[_texts(keyword) - 1].line, keyword.line, command.statement)
        notes = next((comments[line] for line in lines if line in comments), ())
        _add(found, replace(entry, comments=notes), f"{name}:{keyword.line}")
    return list(found.values())


def _texts(keyword: _Word) -> int:
    """How many literals a message call takes: tp CONTEXT MSGID, the others MSGID."""
    return 2 if keyword.text == "tp" else 1


def _shell_message(
    keyword: _Word, arguments: Sequence[_Word], function: str, name: str
) -> Entry | None:
    where = f"{name}:{keyword.line}"
    count = _texts(keyword)
    if len(arguments) < count:
        raise ExtractError(f"{where}: {keyword.text} has no text")
    texts = [word.literal for word in arguments[:count]]
    if None in texts:
        if function in SHELL_PASSING:
            return None
        bad = next(word.text for word in arguments[:count] if word.literal is None)
        raise ExtractError(
            f"{where}: {keyword.text} takes a literal here, not {bad}: single-quoted, or "
            "double-quoted without $, ` and \\; put the values in as {name} fields"
        )
    *context, msgid = [text for text in texts if text is not None]
    if not msgid:
        raise ExtractError(f"{where}: an empty msgid")
    if msgid.startswith(PREFIX):
        raise ExtractError(f"{where}: the prefix {PREFIX!r} is printed before a text")
    if set(_SHELL_FIELD.sub("", msgid)) & {"{", "}"}:
        raise ExtractError(f"{where}: {msgid!r}: the fields of a text are plain {{name}}s")
    fields = {field[1:-1] for field in _SHELL_FIELD.findall(msgid)}
    values = arguments[count:]
    if keyword.text == "tp" and (fields or values):
        raise ExtractError(f"{where}: tp fills no values: {msgid!r}")
    missing = sorted(fields - {word.literal for word in values[::2]})
    if missing:
        raise ExtractError(f"{where}: no value for {', '.join(missing)}")
    flags = ("python-brace-format",) if fields else ()
    return Entry(msgid, ("",), context[0] if context else None, None, flags)


def shell_hazards(path: Path, top: Path) -> list[str]:
    """What the installer must not do: run a text as code (eval, source, .: a language pack is
    data), or translate with bash's own $"…", which reads catalogs the installer does not ship."""
    name = path.relative_to(top).as_posix()
    shell = _Shell(path.read_text(encoding="utf-8"), name)
    found = [(line, f"{name}:{line}: $\"…\" translates with bash's catalogs: use t") for line in
             shell.translated]  # fmt: skip
    for command in shell.commands:
        head = command.head()
        if head is None:
            continue
        names = [word.text for word in command.words[head : head + 2]]
        if names[0] not in {"command", "builtin", "exec"}:
            names = names[:1]
        for word in names:
            if word in {"eval", "source", "."}:
                line = command.words[head].line
                found.append((line, f"{name}:{line}: {word} runs a text as code"))
    return [finding for _, finding in sorted(found, key=lambda item: item[0])]


def sources(project: Project) -> list[tuple[Callable[[Path, Path], list[Entry]], Path]]:
    """What the template is made of: the package's code, the dashboard's template (ADR 0021) and
    argparse's words."""
    return [
        (extract_python, project.package),
        (extract_dashboard, project.package / dashboard.TEMPLATE),
        (extract_argparse, project.argparse_list),
    ]


def template(project: Project) -> list[Entry]:
    """The template's entries, sorted by their first file, then context, then msgid."""
    found: dict[str, Entry] = {}
    for extract, path in sources(project):
        for entry in extract(path, project.root):
            _add(found, entry, entry.references[0])
    return _sorted(found.values())


def installer_template(project: Project) -> list[Entry]:
    """The installer template's entries: one file, so sorted by context, then msgid."""
    return _sorted(extract_shell(project.installer, project.root))


def _sorted(entries: Iterable[Entry]) -> list[Entry]:
    return sorted(
        entries, key=lambda e: (e.references[:1], e.msgctxt is not None, e.msgctxt or "", e.msgid)
    )


def templates(project: Project) -> list[tuple[Path, list[Path], Callable[[Project], list[Entry]]]]:
    """Each template, its catalogs and what makes its entries: the program's texts and the
    installer's."""
    return [
        (project.template, project.catalogs(), template),
        (project.installer_template, project.installer_catalogs(), installer_template),
    ]


# --- files -----------------------------------------------------------------------------------


def write_po(header: Mapping[str, str], entries: Iterable[Entry]) -> str:
    """The canonical form: GNU's layout without line wrapping (a string breaks only after
    \\n); the translator's notes, comments, files and flags in GNU's order."""
    fields = "".join(f"{key}: {value}\n" for key, value in header.items())
    lines = ['msgid ""', *_string("msgstr", fields)]
    for entry in entries:
        lines.append("")
        lines += [f"# {note}" if note else "#" for note in entry.notes]
        lines += [f"#. {comment}" if comment else "#." for comment in entry.comments]
        if entry.references:
            lines.append("#: " + " ".join(entry.references))
        if entry.flags:
            lines.append("#, " + ", ".join(entry.flags))
        if entry.msgctxt is not None:
            lines += _string("msgctxt", entry.msgctxt)
        lines += _string("msgid", entry.msgid)
        if entry.msgid_plural is None:
            lines += _string("msgstr", entry.msgstr[0] if entry.msgstr else "")
            continue
        lines += _string("msgid_plural", entry.msgid_plural)
        for index, form in enumerate(entry.msgstr or ("", "")):
            lines += _string(f"msgstr[{index}]", form)
    return "\n".join(lines) + "\n"


def _string(keyword: str, text: str) -> list[str]:
    escaped = (
        text.replace("\\", "\\\\").replace('"', '\\"').replace("\t", "\\t").replace("\r", "\\r")
    )
    parts = escaped.split("\n")
    pieces = [f"{part}\\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])
    if len(pieces) <= 1:
        return [f'{keyword} "{"".join(pieces)}"']
    return [f'{keyword} ""', *(f'"{piece}"' for piece in pieces)]


def merge(
    template: Iterable[Entry], header: Mapping[str, str], entries: Iterable[Entry], language: str
) -> tuple[dict[str, str], list[Entry]]:
    """The catalog of `language` in the template's order, with its fixed header: the translations
    and the translator's notes of unchanged texts kept, new texts empty, texts the template lost
    dropped; no fuzzy matching (a reworded text is a new text)."""
    forms = PLURAL_FORMS.get(language) or header.get("Plural-Forms")
    if not forms:
        raise PoError(f"{language}.po: no Plural-Forms for {language}")
    fixed = catalog_header(language, forms)
    count, _ = plural_rule(fixed)
    known = {entry.key: entry for entry in entries}
    merged = []
    for wanted in template:
        size = 1 if wanted.msgid_plural is None else count
        old = known.get(wanted.key)
        if old is None or old.msgid_plural != wanted.msgid_plural:
            merged.append(replace(wanted, msgstr=("",) * size, notes=()))
            continue
        fuzzy = ("fuzzy",) if "fuzzy" in old.flags else ()
        merged.append(
            replace(
                wanted,
                msgstr=(*old.msgstr, *("",) * size)[:size],
                flags=fuzzy + tuple(flag for flag in wanted.flags if flag != "fuzzy"),
                notes=old.notes,
            )
        )
    return fixed, merged


def _read_list(path: Path) -> set[tuple[str, str | None]]:
    if not path.exists():
        return set()
    return {(msgid, plural) for msgid, plural in json.loads(path.read_text(encoding="utf-8"))}


def _list_text(pairs: Iterable[tuple[str, str | None]]) -> str:
    ordered = sorted(set(pairs), key=lambda pair: (pair[0], pair[1] or ""))
    lines = [json.dumps(list(pair), ensure_ascii=False) for pair in ordered]
    return "[\n" + ",\n".join(f"  {line}" for line in lines) + "\n]\n"


# --- the installer's language packs ---------------------------------------------------------

PINS_START: Final = (
    "# --- language packs: written by `make translations` from deploy/proxmox/locale/*.po; "
    "do not edit"
)
PINS_END: Final = "# --- end of language packs"
_PINS: Final = re.compile(
    rf"^{re.escape(PINS_START)}\n.*?^{re.escape(PINS_END)}\n", re.MULTILINE | re.DOTALL
)
ONE_BLOCK: Final = (
    f"the script needs one block of language packs, between {PINS_START!r} and {PINS_END!r}"
)


def pack(language: str, entries: Iterable[Entry]) -> str:
    """The installer's language pack: a comment line, then `msg<TAB>KEY<TAB>TEXT` for each
    translated text, sorted by key; KEY is the msgid, or the context, \\x04 and the msgid. The
    escapes \\\\, \\t, \\n and \\x04 are printf %b's; no other control character fits a line."""
    lines = []
    for entry in entries:
        if entry.msgid_plural is not None:
            raise ValueError(f"{_label(entry)}: a plural, but no installer text counts anything")
        if "fuzzy" in entry.flags or not entry.msgstr or not entry.msgstr[0]:
            continue
        key = _packed(entry.msgid, entry)
        if entry.msgctxt is not None:
            key = _packed(entry.msgctxt, entry) + "\\x04" + key
        lines.append((entry.key, f"msg\t{key}\t{_packed(entry.msgstr[0], entry)}"))
    head = (
        f"# meshcore-home-gateway installer messages, {language}: written by make translations "
        f"from {language}.po; do not edit"
    )
    return "\n".join([head, *(line for _, line in sorted(lines))]) + "\n"


def _packed(text: str, entry: Entry) -> str:
    bad = sorted({char for char in text if unicodedata.category(char) == "Cc"} - {"\t", "\n"})
    if bad:
        raise ValueError(f"{_label(entry)}: U+{ord(bad[0]):04X} cannot be in a language pack")
    return text.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n")


def pin_block(languages: Mapping[str, str], packs: Mapping[str, str]) -> str:
    """The installer's block of language packs: the languages in their order, their names, and
    each pack's SHA-256, which the installer checks before it reads the pack. English comes
    first and has no pack; a code is two letters, as the installer checks it."""
    codes = list(languages)
    if codes[:1] != ["en"]:
        raise ValueError(f"the installer offers English first, not {', '.join(codes)}")
    for code in codes:
        if not re.fullmatch("[a-z]{2}", code):
            raise ValueError(f"{code}: the installer takes two lower-case letters as a language")
    missing = [code for code in codes[1:] if code not in packs]
    if missing:
        raise ValueError(f"no pack for {', '.join(missing)}")
    names = " ".join(f"[{code}]={shlex.quote(name)}" for code, name in languages.items())
    digests = " ".join(
        f"[{code}]={hashlib.sha256(packs[code].encode('utf-8')).hexdigest()}" for code in codes[1:]
    )
    return "\n".join([
        PINS_START,
        f"L10N_ORDER=({' '.join(codes)})",
        f"declare -A L10N_NAME=({names})",
        f"declare -A L10N_SHA256=({digests})",
        PINS_END,
        "",
    ])  # fmt: skip


def with_pins(script: str, block: str) -> str:
    """The installer's text with its block of language packs replaced by `block`."""
    if len(_PINS.findall(script)) != 1:
        raise ValueError(ONE_BLOCK)
    return _PINS.sub(lambda _: block, script)


RELEASE_PACK: Final = "meshcore-home-gateway-lxc.{language}.messages"  # lxc.sh pack_text
_PIN: Final = re.compile(r"\[([a-z]{2})\]=([0-9a-f]{64})")


def pinned(script: str) -> dict[str, str]:
    """Each language the installer's block of language packs offers but English, with the
    SHA-256 it pins."""
    blocks = _PINS.findall(script)
    if len(blocks) != 1:
        raise ValueError(ONE_BLOCK)
    (block,) = blocks
    order = re.search(r"^L10N_ORDER=\((.*)\)$", block, re.MULTILINE)
    digests = re.search(r"^declare -A L10N_SHA256=\((.*)\)$", block, re.MULTILINE)
    if order is None or digests is None:
        raise ValueError("no L10N_ORDER or no L10N_SHA256 in the block of language packs")
    offered = order.group(1).split()[1:]
    pins = dict(_PIN.findall(digests.group(1)))
    for code in offered:
        if code not in pins:
            raise ValueError(f"the installer offers {code} without a pin")
    for code in pins:
        if code not in offered:
            raise ValueError(f"the installer pins {code}, not offered")
    return {code: pins[code] for code in offered}


def release_packs(project: Project, folder: Path) -> list[Path]:
    """Copy the pack of each language that the installer published in `folder` offers into
    `folder`, by the name the installer fetches from its release (make release). A pack that is
    missing, or not the one whose digest the installer carries, is refused: the installer would
    refuse it."""
    script = (folder / project.installer.name).read_text(encoding="utf-8")
    written = []
    for language, digest in pinned(script).items():
        source = project.pack(language)
        name = label(project, source)
        if not source.exists():
            raise ValueError(f"{name}: missing: run make translations")
        text = source.read_bytes()
        if hashlib.sha256(text).hexdigest() != digest:
            raise ValueError(f"{name}: not the pack the installer pins: run make translations")
        target = folder / RELEASE_PACK.format(language=language)
        target.write_bytes(text)
        written.append(target)
    return written


def _packs(project: Project) -> dict[str, str]:
    """Each installer catalog's language pack, as update() writes it; a catalog that cannot be
    read or packed has none (canonical() and stale_packs() name it)."""
    packs = {}
    for path in project.installer_catalogs():
        try:
            _, entries = read_po(path.read_text(encoding="utf-8"), label(project, path))
            packs[path.stem] = pack(path.stem, entries)
        except ValueError:  # PoError too
            continue
    return packs


def _pinned(project: Project, languages: Mapping[str, str], packs: Mapping[str, str]) -> str:
    """The installer's text with the block that offers `languages` and pins their packs."""
    script = project.installer.read_text(encoding="utf-8")
    return with_pins(script, pin_block(languages, packs))


def _write(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return True


def update(project: Project, languages: Mapping[str, str] | None = None) -> list[Path]:
    """Write what changed: argparse's texts of this Python, the templates, the catalogs, the
    installer's language packs and its block that offers the languages (i18n.LANGUAGES) and pins
    their packs."""
    written = []
    listed = _read_list(project.argparse_list) | argparse_messages()
    if _write(project.argparse_list, _list_text(listed)):
        written.append(project.argparse_list)
    for path, catalogs, make in templates(project):
        entries = make(project)
        if _write(path, write_po(TEMPLATE_HEADER, entries)):
            written.append(path)
        for catalog in catalogs:
            header, old = read_po(catalog.read_text(encoding="utf-8"), label(project, catalog))
            if _write(catalog, write_po(*merge(entries, header, old, catalog.stem))):
                written.append(catalog)
    packs = {}
    for catalog in project.installer_catalogs():
        _, entries = read_po(catalog.read_text(encoding="utf-8"), label(project, catalog))
        packs[catalog.stem] = pack(catalog.stem, entries)
        if _write(project.pack(catalog.stem), packs[catalog.stem]):
            written.append(project.pack(catalog.stem))
    offered = i18n.LANGUAGES if languages is None else languages
    if _write(project.installer, _pinned(project, offered, packs)):
        written.append(project.installer)
    return written


# --- checks: each returns its findings; the tests run them on the committed files -----------


def outdated_template(project: Project) -> list[str]:
    """Templates that `make translations` would change."""
    findings = []
    for path, _, make in templates(project):
        name = label(project, path)
        try:
            expected = write_po(TEMPLATE_HEADER, make(project))
        except ExtractError as exc:
            findings.append(str(exc))
            continue
        if not path.exists():
            findings.append(f"{name}: missing: run make translations")
        elif path.read_text(encoding="utf-8") != expected:
            findings.append(f"{name}: not current: run make translations")
    return findings


def unmerged(project: Project) -> list[str]:
    """Catalogs that `make translations` would change."""
    findings = []
    for _, catalogs, make in templates(project):
        try:
            entries = make(project)
        except ExtractError:
            continue  # outdated_template() names it
        for path in catalogs:
            name = label(project, path)
            text = path.read_text(encoding="utf-8")
            try:
                header, old = read_po(text, name)
                merged = write_po(*merge(entries, header, old, path.stem))
            except PoError as exc:
                findings.append(str(exc))
                continue
            if merged != text:
                findings.append(f"{name}: does not follow the template: run make translations")
    return findings


def stale_packs(project: Project) -> list[str]:
    """Each installer catalog's language pack as `make translations` writes it, and no other."""
    findings = []
    catalogs = {path.stem: path for path in project.installer_catalogs()}
    for language, path in catalogs.items():
        try:
            _, entries = read_po(path.read_text(encoding="utf-8"), label(project, path))
        except PoError:
            continue  # canonical() names it
        try:
            expected = pack(language, entries)
        except ValueError as exc:
            findings.append(f"{label(project, path)}: {exc}")
            continue
        target = project.pack(language)
        if not target.exists():
            findings.append(f"{label(project, target)}: missing: run make translations")
        elif target.read_bytes() != expected.encode("utf-8"):
            findings.append(f"{label(project, target)}: not current: run make translations")
    findings.extend(
        f"{label(project, target)}: no catalog {target.stem}.po: remove it"
        for target in sorted(project.installer_locale.glob("*.messages"))
        if target.stem not in catalogs
    )
    return findings


def stale_pins(project: Project, languages: Mapping[str, str]) -> list[str]:
    """The installer's block of language packs as `make translations` writes it."""
    name = label(project, project.installer)
    try:
        expected = _pinned(project, languages, _packs(project))
    except ValueError as exc:
        return [f"{name}: {exc}"]
    if project.installer.read_text(encoding="utf-8") != expected:
        return [f"{name}: the block of language packs is not current: run make translations"]
    return []


def languages_agree(project: Project, languages: Mapping[str, str]) -> list[str]:
    """A catalog for every language of i18n.LANGUAGES but English, and for no other."""
    have = {path.stem for path in project.catalogs()}
    want = set(languages) - {"en"}
    where = label(project, project.locale)
    return [
        f"{where}/{code}.po: missing: i18n.LANGUAGES offers {code}" for code in sorted(want - have)
    ] + [
        f"{where}/{code}.po: not one of the languages ({', '.join(languages)}): add it to "
        "i18n.LANGUAGES or remove it"
        for code in sorted(have - want)
    ]


def canonical(name: str, text: str) -> list[str]:
    """The file as write_po writes it, with the fixed header and no obsolete entries."""
    try:
        header, entries = read_po(text, name)
    except PoError as exc:
        return [str(exc)]
    findings = fixed_header(name, header)
    if re.search(r"^#~", text, re.MULTILINE):
        findings.append(f"{name}: obsolete entries (#~): run make translations")
    elif write_po(header, entries) != text:
        findings.append(f"{name}: not in the canonical form: run make translations")
    return findings


def fixed_header(name: str, header: Mapping[str, str]) -> list[str]:
    """Exactly these fields; a catalog's Language is its file's name."""
    if name.endswith(".pot"):
        expected = TEMPLATE_HEADER
    else:
        expected = catalog_header(Path(name).stem, header.get("Plural-Forms", ""))
    if list(header.items()) == list(expected.items()):
        return []
    fields = "; ".join(f"{key}: {value}" for key, value in expected.items())
    return [f"{name}: the header must be exactly {fields}: run make translations"]


def _label(entry: Entry) -> str:
    return repr(entry.msgid) if entry.msgctxt is None else f"{entry.msgid!r} ({entry.msgctxt})"


def untranslated(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """A shipped language translates every text."""
    return [
        f"{name}: untranslated: {_label(e)}" for e in entries if not e.msgstr or not all(e.msgstr)
    ]


def fuzzy(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    return [f"{name}: fuzzy: {_label(entry)}" for entry in entries if "fuzzy" in entry.flags]


def _selected(header: Mapping[str, str]) -> dict[int, list[int]]:
    """For each plural form, the counts from 0 to 999 that select it."""
    try:
        count, rule = plural_rule(header)
    except PoError:  # plural_forms() names it
        count, rule = plural_rule({})
    selected: dict[int, list[int]] = {index: [] for index in range(count)}
    for n in range(1000):
        selected.setdefault(rule(n), []).append(n)
    return selected


def _forms(entry: Entry, selected: Mapping[int, list[int]]) -> Iterator[tuple[str, list[str]]]:
    """Each filled form with the English texts it stands for."""
    if entry.msgid_plural is None:
        if entry.msgstr and entry.msgstr[0]:
            yield entry.msgstr[0], [entry.msgid]
        return
    for index, form in enumerate(entry.msgstr):
        counts = selected.get(index, [])
        english = [entry.msgid] if 1 in counts else []
        if any(n != 1 for n in counts):
            english.append(entry.msgid_plural)
        if form:
            yield form, english


def _whole_fields(text: str) -> set[str]:
    """A format string's fields as written: {value!r}, {duty:.3g}."""
    return {
        "{" + name + (f"!{conversion}" if conversion else "") + (f":{spec}" if spec else "") + "}"
        for _, name, spec, conversion in string.Formatter().parse(text)
        if name is not None
    }


def placeholders(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """The fields of a translation are its English text's, compared whole; a plural form uses
    all of them, but a form only n == 1 selects may leave {count} out. Texts argparse formats
    have no fields (percent())."""
    findings = []
    selected = _selected(header)
    for entry in entries:
        if "python-format" in entry.flags:
            continue
        wanted = _whole_fields(entry.msgid) | _whole_fields(entry.msgid_plural or "")
        for form, english in _forms(entry, selected):
            try:
                got = _whole_fields(form)
            except ValueError as exc:
                findings.append(f"{name}: {_label(entry)}: {form!r} is not a format string: {exc}")
                continue
            needed = wanted
            if entry.msgid_plural is not None and english == [entry.msgid]:
                needed = {field for field in wanted if not re.match(r"\{count\W", field)}
            if not needed <= got <= wanted:
                findings.append(
                    f"{name}: {_label(entry)}: {form!r} has the fields {sorted(got)}, "
                    f"the English {sorted(wanted)}"
                )
    return findings


def percent(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """Where argparse %-formats a text, its %-directives are its English text's (D7)."""
    findings = []
    selected = _selected(header)
    for entry in entries:
        if "python-format" not in entry.flags:
            continue
        for form, english in _forms(entry, selected):
            got = i18n.directives(form)
            if any(got != i18n.directives(text) for text in english):
                wanted = sorted(i18n.directives(english[0]).elements())
                findings.append(
                    f"{name}: {_label(entry)}: {form!r} has the %-directives "
                    f"{sorted(got.elements())}, the English {wanted}"
                )
    return findings


def plural_forms(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """The language's rule, and as many forms in every plural entry as it has."""
    expected = PLURAL_FORMS.get(Path(name).stem)
    if expected is not None and header.get("Plural-Forms") != expected:
        return [f"{name}: Plural-Forms must be {expected!r}"]
    try:
        count, _ = plural_rule(header)
    except PoError as exc:
        return [f"{name}: {exc}"]
    return [
        f"{name}: {_label(entry)}: {len(entry.msgstr)} forms, Plural-Forms has {count}"
        for entry in entries
        if entry.msgid_plural is not None and len(entry.msgstr) != count
    ]


def tokens(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """Options, variables, paths, keys, documents and commands kept as they are."""
    findings = []
    selected = _selected(header)
    for entry in entries:
        for form, english in _forms(entry, selected):
            got = sorted(TOKENS.findall(form))
            if any(got != sorted(TOKENS.findall(text)) for text in english):
                wanted = sorted(TOKENS.findall(english[0]))
                findings.append(f"{name}: {_label(entry)}: {form!r} keeps {got}, not {wanted}")
    return findings


def control_characters(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """No control or format character but \\n: the command line shows them as spaces. No
    no-break space in the command line's texts, which the installer's boxes show too, until
    newt is known to render it (docs/testing.md)."""
    findings = []
    for entry in entries:
        for form in entry.msgstr:
            bad = sorted({
                char
                for char in form
                if (char != "\n" and unicodedata.category(char) in {"Cc", "Cf"})
                or (entry.msgctxt is None and char in "\u00a0\u202f")
            })  # fmt: skip
            if bad:
                found = ", ".join(f"U+{ord(char):04X}" for char in bad)
                findings.append(f"{name}: {_label(entry)}: {found}")
    return findings


BUTTON_LABEL: Final = 12  # characters at most: whiptail draws its buttons for its own labels


def buttons(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """An installer button's label (msgctxt "button") fits its button."""
    return [
        f"{name}: {_label(entry)}: {form!r} has {len(form)} characters, a button at most "
        f"{BUTTON_LABEL}"
        for entry in entries
        if entry.msgctxt == "button"
        for form in entry.msgstr
        if len(form) > BUTTON_LABEL
    ]


def dashboard_texts(name: str, header: Mapping[str, str], entries: Sequence[Entry]) -> list[str]:
    """A text of the dashboard holds nothing a card reads as code, as the command checks it
    (dashboard.unsafe): no Jinja, HTML, Markdown link or address, marker or line break."""
    return [
        f"{name}: {_label(entry)}: {found} cannot be in a dashboard text"
        for entry in entries
        if entry.msgctxt == i18n.DASHBOARD
        for form in entry.msgstr
        if (found := dashboard.unsafe(form)) is not None
    ]


CATALOG_CHECKS: Final = (
    untranslated, fuzzy, placeholders, percent, plural_forms, tokens, control_characters,
    buttons, dashboard_texts,
)  # fmt: skip


def check(project: Project, languages: Mapping[str, str] | None = None) -> list[str]:
    """Every finding of the checks above on the project's files."""
    offered = i18n.LANGUAGES if languages is None else languages
    findings = [
        *outdated_template(project), *unmerged(project), *stale_packs(project),
        *stale_pins(project, offered), *languages_agree(project, offered),
    ]  # fmt: skip
    files = [
        project.template, *project.catalogs(),
        project.installer_template, *project.installer_catalogs(),
    ]  # fmt: skip
    for path in files:
        if not path.exists():
            continue
        name = label(project, path)
        text = path.read_text(encoding="utf-8")
        findings += canonical(name, text)
        if path.suffix == ".po":
            try:
                header, entries = read_po(text, name)
            except PoError:
                continue  # canonical() names it
            for rule in CATALOG_CHECKS:
                findings += rule(name, header, entries)
    return findings


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True, metavar="command")
    commands.add_parser("update", help="write the templates, the catalogs, the packs and pins")
    commands.add_parser("check", help="the checks of the tests; exit 1 on a finding")
    packs = commands.add_parser(
        "release-packs", help="copy the packs the installer in FOLDER pins into FOLDER"
    )
    packs.add_argument("folder", type=Path, metavar="FOLDER")
    args = parser.parse_args(argv)
    project = Project(ROOT)
    if args.command == "release-packs":
        try:
            published = release_packs(project, args.folder)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 1
        for path in published:
            print(f"wrote {path}")
        return 0
    if args.command == "update":
        try:
            written = update(project)
        except ValueError as exc:  # ExtractError, PoError, a text no pack can hold
            print(exc, file=sys.stderr)
            return 1
        for path in written:
            print(f"wrote {label(project, path)}")
        return 0
    findings = check(project)
    for finding in findings:
        print(finding)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
