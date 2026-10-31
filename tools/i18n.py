"""Translations (ADR 0019, ADR 0027): the template of the texts, the catalogs and their checks.

    python3 tools/i18n.py update   # what `make translations` runs: the template, the catalogs
    python3 tools/i18n.py check    # the checks of the tests, without pytest; exit 1 on a finding

`update` extracts every text marked in the package (docs/development.md, Translations) and
argparse's own words into src/meshcore_home_gateway/locale/meshcore-home-gateway.pot, adds the
running Python's argparse texts to tools/argparse_messages.json (it never removes one: the list
serves every Python of the matrix) and merges each catalog with the template. The files are
written in one canonical form, without dates or names, so the tests can compare them with what
the code says. Standard library only; it puts src/ on the path itself.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import io
import json
import re
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

from meshcore_home_gateway import i18n  # noqa: E402 - once src/ is on the path
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
    """`# Translators:` comments, and the comment lines after them, by the line below them."""
    lines = {
        token.start[0]: token.string[1:].strip()
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type == tokenize.COMMENT and token.line.lstrip().startswith("#")
    }
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


def sources(project: Project) -> list[tuple[Callable[[Path, Path], list[Entry]], Path]]:
    """What the template is made of; the dashboards' template will join it (ADR 0021)."""
    return [(extract_python, project.package), (extract_argparse, project.argparse_list)]


def template(project: Project) -> list[Entry]:
    """The template's entries, sorted by their first file, then context, then msgid."""
    found: dict[str, Entry] = {}
    for extract, path in sources(project):
        for entry in extract(path, project.root):
            _add(found, entry, entry.references[0])
    return sorted(
        found.values(),
        key=lambda e: (e.references[0], e.msgctxt is not None, e.msgctxt or "", e.msgid),
    )


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


def _write(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def update(project: Project) -> list[Path]:
    """Write what changed: argparse's texts of this Python, the template, the catalogs."""
    written = []
    listed = _read_list(project.argparse_list) | argparse_messages()
    if _write(project.argparse_list, _list_text(listed)):
        written.append(project.argparse_list)
    entries = template(project)
    if _write(project.template, write_po(TEMPLATE_HEADER, entries)):
        written.append(project.template)
    for path in project.catalogs():
        header, old = read_po(path.read_text(encoding="utf-8"), label(project, path))
        if _write(path, write_po(*merge(entries, header, old, path.stem))):
            written.append(path)
    return written


# --- checks: each returns its findings; the tests run them on the committed files -----------


def outdated_template(project: Project) -> list[str]:
    name = label(project, project.template)
    try:
        expected = write_po(TEMPLATE_HEADER, template(project))
    except ExtractError as exc:
        return [str(exc)]
    if not project.template.exists():
        return [f"{name}: missing: run make translations"]
    if project.template.read_text(encoding="utf-8") != expected:
        return [f"{name}: not current: run make translations"]
    return []


def unmerged(project: Project) -> list[str]:
    """Catalogs that `make translations` would change."""
    try:
        entries = template(project)
    except ExtractError:
        return []  # outdated_template() names it
    findings = []
    for path in project.catalogs():
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


CATALOG_CHECKS: Final = (
    untranslated, fuzzy, placeholders, percent, plural_forms, tokens, control_characters,
)  # fmt: skip


def check(project: Project, languages: Mapping[str, str] | None = None) -> list[str]:
    """Every finding of the checks above on the project's files."""
    offered = i18n.LANGUAGES if languages is None else languages
    findings = [
        *outdated_template(project), *unmerged(project), *languages_agree(project, offered),
    ]  # fmt: skip
    for path in [project.template, *project.catalogs()]:
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
    commands.add_parser("update", help="write the template and merge the catalogs")
    commands.add_parser("check", help="the checks of the tests; exit 1 on a finding")
    args = parser.parse_args(argv)
    project = Project(ROOT)
    if args.command == "update":
        try:
            written = update(project)
        except (ExtractError, PoError) as exc:
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
