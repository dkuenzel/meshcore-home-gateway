"""Message catalogs (ADR 0027): the PO files translators edit, read as GNU gettext, Poedit and
Weblate write them, and the MO form that Python's gettext loads, written in memory. Pure: the
callers read and write the files."""

from __future__ import annotations

import gettext
import re
import struct
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

_MAGIC = 0x950412DE
_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\",
    '"': '"',
}  # fmt: skip
_KEYWORD = re.compile(r"(msgctxt|msgid_plural|msgid|msgstr(?:\[(\d+)\])?)[ \t]+(.*)")
_PLURAL_FORMS = re.compile(r"\s*nplurals\s*=\s*(\d+)\s*;\s*plural\s*=\s*([^;]+?)\s*;?\s*")
GERMANIC = "nplurals=2; plural=(n != 1);"  # gettext's rule when a catalog names none


@dataclass(frozen=True, slots=True)
class Entry:
    """One message of a catalog."""

    msgid: str
    msgstr: tuple[str, ...] = ()  # one form, or one per plural form; () untranslated
    msgctxt: str | None = None
    msgid_plural: str | None = None
    flags: tuple[str, ...] = ()  # fuzzy, python-brace-format, python-format
    comments: tuple[str, ...] = ()  # "#." extracted comments
    references: tuple[str, ...] = ()  # "#:" file names
    notes: tuple[str, ...] = ()  # "# " the translator's own comments, kept by a merge

    @property
    def key(self) -> str:
        """What gettext looks it up by: the msgid, after its context and EOT."""
        return self.msgid if self.msgctxt is None else f"{self.msgctxt}\x04{self.msgid}"

    @property
    def translated(self) -> bool:
        """Every form filled and not fuzzy: what an MO file holds (D5)."""
        return bool(self.msgstr) and all(self.msgstr) and "fuzzy" not in self.flags


class PoError(ValueError):
    """A catalog that is not well formed; the message names the file and line."""


@dataclass
class _Pending:
    """The entry being read: its fields as they come, the line of its first keyword and the line
    of its first comment or keyword."""

    line: int = 0
    start: int = 0
    fields: dict[str, list[str]] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def described(self) -> bool:
        return bool(self.flags or self.comments or self.references or self.notes)

    @property
    def complete(self) -> bool:
        return any(name.startswith("msgstr") for name in self.fields)


class _Reader:
    """A PO file, line by line: comments, then msgctxt, msgid, msgid_plural and msgstr."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.number = 0
        self.entry = _Pending()
        self.current: list[str] | None = None  # the string a continuation line extends
        self.header: dict[str, str] | None = None
        self.entries: list[Entry] = []
        self.keys: set[str] = set()

    def error(self, reason: str, line: int | None = None) -> PoError:
        return PoError(f"{self.name}:{line or self.number}: {reason}")

    def line(self, text: str) -> None:
        self.number += 1
        if not text.strip():
            self.current = None
        elif text.startswith("#~"):  # an obsolete entry, and the comments before it
            if self.entry.fields:
                self.finish_entry()
            self.entry, self.current = _Pending(), None
        elif text.startswith("#"):
            self.comment(text)
        elif match := _KEYWORD.fullmatch(text):
            self.keyword(match[1], match[2], self.unquote(match[3]))
        elif text.startswith('"'):
            if self.current is None:
                raise self.error("a string without a keyword")
            self.current.append(self.unquote(text))
        else:
            raise self.error(f"unexpected {text!r}")

    def comment(self, text: str) -> None:
        if self.entry.complete:
            self.finish_entry()
        self.current = None
        self.entry.start = self.entry.start or self.number
        kind, rest = text[:2], text[2:].removeprefix(" ")
        if kind == "#,":
            self.entry.flags += [flag.strip() for flag in rest.split(",") if flag.strip()]
        elif kind == "#.":
            self.entry.comments.append(rest)
        elif kind == "#:":
            self.entry.references += rest.split()
        elif kind != "#|":  # a previous msgid is left out
            self.entry.notes.append(text[1:].removeprefix(" "))

    def keyword(self, name: str, index: str | None, value: str) -> None:
        fields = self.entry.fields
        if name in {"msgctxt", "msgid"} and self.entry.complete:
            self.finish_entry()
            fields = self.entry.fields
        if name == "msgctxt" and fields:
            raise self.error("msgctxt after msgid")
        if name == "msgid" and "msgid" in fields:
            raise self.error("msgid without msgstr", self.entry.line)
        if name not in {"msgctxt", "msgid"} and "msgid" not in fields:
            raise self.error(f"{name} without msgid")
        if name == "msgid_plural" and (len(fields) > 1 + ("msgctxt" in fields)):
            raise self.error("msgid_plural after msgstr")
        if name.startswith("msgstr"):
            self.check_msgstr(name, index)
        if not fields:
            self.entry.line = self.number
            self.entry.start = self.entry.start or self.number
        self.current = fields[name] = [value]

    def check_msgstr(self, name: str, index: str | None) -> None:
        fields = self.entry.fields
        if "msgid_plural" in fields:
            forms = sum(1 for key in fields if key.startswith("msgstr["))
            if index is None or int(index) != forms:
                raise self.error(f"{name} where msgstr[{forms}] belongs")
        elif index is not None or "msgstr" in fields:
            raise self.error(f"{name} in an entry without msgid_plural")

    def unquote(self, text: str) -> str:
        if len(text) < 2 or text[0] != '"' or text[-1] != '"':
            raise self.error(f"expected a quoted string, got {text!r}")
        out: list[str] = []
        chars = iter(text[1:-1])
        for char in chars:
            if char == '"':
                raise self.error("an unescaped quote")
            if char != "\\":
                out.append(char)
                continue
            escape = next(chars, "")
            if escape not in _ESCAPES:
                raise self.error(f"an unknown escape \\{escape}")
            out.append(_ESCAPES[escape])
        return "".join(out)

    def finish_entry(self) -> None:
        pending, self.entry, self.current = self.entry, _Pending(), None
        fields = {name: "".join(parts) for name, parts in pending.fields.items()}
        if not fields:
            if pending.described:
                raise self.error("comments without an entry", pending.start)
            return
        if "msgid" not in fields:
            raise self.error("msgctxt without msgid", pending.line)
        if not pending.complete:
            raise self.error("msgid without msgstr", pending.line)
        entry = Entry(
            fields["msgid"],
            tuple(text for name, text in fields.items() if name.startswith("msgstr")),
            fields.get("msgctxt"),
            fields.get("msgid_plural"),
            tuple(pending.flags),
            tuple(pending.comments),
            tuple(pending.references),
            tuple(pending.notes),
        )
        if entry.msgid == "":
            if entry.msgctxt is not None or self.entries or self.header is not None:
                raise self.error("an empty msgid", pending.line)
            self.header = self.read_header(entry.msgstr[0], pending.line)
            return
        if entry.key in self.keys:
            raise self.error(f"a repeated msgid {entry.msgid!r}", pending.line)
        self.keys.add(entry.key)
        self.entries.append(entry)

    def read_header(self, text: str, line: int) -> dict[str, str]:
        header: dict[str, str] = {}
        for item in filter(None, text.split("\n")):
            key, colon, value = item.partition(":")
            if not colon:
                raise self.error(f"a header line without a colon: {item!r}", line)
            header[key.strip()] = value.strip()
        return header


def read_po(text: str, name: str = "<po>") -> tuple[dict[str, str], list[Entry]]:
    """The header's fields, in file order, and the entries in file order (D5). Obsolete entries
    (#~) and previous msgids (#|) are left out; anything GNU's tools would refuse is refused."""
    reader = _Reader(name)
    if text.startswith("﻿"):
        raise reader.error("a byte order mark", 1)
    for line in text.split("\n"):  # not splitlines(): U+2028 and others are text in a msgstr
        reader.line(line)
    reader.finish_entry()
    return reader.header or {}, reader.entries


def plural_rule(header: Mapping[str, str]) -> tuple[int, Callable[[int], int]]:
    """nplurals and the rule compiled by gettext.c2py, which refuses anything outside C's
    expression subset."""
    forms = header.get("Plural-Forms", GERMANIC)
    match = _PLURAL_FORMS.fullmatch(forms)
    if match is None:
        raise PoError(f"Plural-Forms {forms!r}: not 'nplurals=N; plural=EXPRESSION;'")
    try:
        rule = gettext.c2py(match[2])
    except ValueError as exc:
        raise PoError(f"Plural-Forms {forms!r}: {exc}") from exc
    return int(match[1]), rule


def write_mo(header: Mapping[str, str], entries: Iterable[Entry]) -> bytes:
    """GNU `msgfmt --no-hash` bytes: little-endian, keys sorted by their UTF-8 bytes, no hash
    table; the header first (empty msgid), written as "{field}: {value}\\n" in the mapping's
    order; fuzzy and untranslated entries left out, a plural entry with any empty form
    included (D5)."""
    messages = {b"": "".join(f"{key}: {value}\n" for key, value in header.items()).encode()}
    for entry in entries:
        if entry.translated:
            msgid = (
                entry.key if entry.msgid_plural is None else f"{entry.key}\0{entry.msgid_plural}"
            )
            messages[msgid.encode()] = "\0".join(entry.msgstr).encode()
    keys = sorted(messages)
    count = len(keys)
    ids_at = 28 + 16 * count  # after the header and the two tables; no hash table
    strs_at = ids_at + sum(len(key) + 1 for key in keys)
    originals, translations = bytearray(), bytearray()
    ids, strs = bytearray(), bytearray()
    for key in keys:
        originals += struct.pack("<2I", len(key), ids_at + len(ids))
        translations += struct.pack("<2I", len(messages[key]), strs_at + len(strs))
        ids += key + b"\0"
        strs += messages[key] + b"\0"
    head = struct.pack("<7I", _MAGIC, 0, count, 28, 28 + 8 * count, 0, ids_at)
    return head + bytes(originals + translations + ids + strs)
