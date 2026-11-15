"""The installer's dialogs and how whiptail shows their texts (docs/platform-notes.md, whiptail):
a model of newt's line wrapping, the lines each kind of box shows, and every dialog of
deploy/proxmox/meshcore-home-gateway-lxc.sh with its box and its text in every language.

`tests/test_dialog_texts.py` checks that every text fits its box; `tests/manual/dialog_texts.py`
compares the model with the real whiptail. The model follows newt 0.52.24 (tag `r0-52-24`):
`whiptail.c` cleanNewlines() and the box less 2 lines and columns (`:542`, `:555-556`),
`textbox.c` expandTabs() and doReflow() (`:141-276`), and the text area of each kind of box in
`dialogboxes.c` (`:90-113`, `:203`, `:318`, `:464`, `:561`). The installer runs whiptail under
`LC_ALL=C.UTF-8`, whose wcwidth() and iswspace() it copies.

A dialog's text is read from the script with the parser of tools/i18n.py: the msgid of each
`t` call, and what fills its {name} fields. A field's value is a text where the script gives
one (a literal, another `t` call, a variable set from texts in the same function); otherwise it
is the worst case named in FIELD_VALUES, or in EMBEDDED for a value inside a longer word. The
final message and the plan for the radio are put together as the script does (COMPOSED). Every
combination of the alternatives is a candidate: the box must hold the longest.
"""

from __future__ import annotations

import importlib.util
import itertools
import re
import sys
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final, NamedTuple

from meshcore_home_gateway.catalog import read_po

ROOT: Final = Path(__file__).resolve().parent.parent
SCRIPT: Final = ROOT / "deploy" / "proxmox" / "meshcore-home-gateway-lxc.sh"


def _load_tool() -> ModuleType:
    known = sys.modules.get("i18n_tool")
    if known is not None:
        return known
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


# --- how whiptail lays out a text -------------------------------------------------------------

# iswspace() of glibc's C.UTF-8 (the "space" class of its i18n file): not the no-break spaces
# U+00A0, U+2007 and U+202F, which Python's str.isspace() counts
SPACES: Final = frozenset(
    "\t\n\v\f\r "
    + "".join(map(chr, (0x1680, *range(0x2000, 0x2007), 0x2008, 0x2009, 0x200A, 0x2028)))
    + "".join(map(chr, (0x2029, 0x205F, 0x3000)))
)


def cell_width(char: str) -> int:
    """wcwidth() of glibc's C.UTF-8: -1 for a control character, 0 for a combining or format
    character (the soft hyphen takes a cell), 2 for an East Asian wide or full-width one."""
    code = ord(char)
    if code < 0x20 or 0x7F <= code < 0xA0:
        return -1
    if char == "\N{SOFT HYPHEN}":
        return 1
    if unicodedata.category(char) in {"Mn", "Me", "Cf"} or 0x1160 <= code <= 0x11FF:
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def text_width(text: str) -> int:
    """newt's wstrlen(): the cells a text takes, a control character none."""
    return sum(max(cell_width(char), 0) for char in text)


def clean_newlines(text: str) -> str:
    """whiptail's cleanNewlines(): the two characters \\n become a line break."""
    return text.replace("\\n", "\n")


def expand_tabs(text: str) -> str:
    """newt's expandTabs(), as written: a tab becomes 8 spaces, or none when the byte count of the
    line so far has the bit 8 set (`8 - (linePos & 8)`)."""
    out: list[str] = []
    column = 0
    for char in text:
        if char == "\t":
            step = 8 - (column & 8)
            out.append(" " * step)
            column += step
        else:
            column = 0 if char == "\n" else column + len(char.encode("utf-8"))
            out.append(char)
    return "".join(out)


def reflow(text: str, width: int) -> list[str]:
    """newt's doReflow(): the lines of `text` in a text area `width` cells wide. A paragraph ends
    at a line break; a line that does not fit breaks at the start of the last run of spaces
    that begins within the width, or, without one, after the last character that fits; the
    spaces after a break are dropped."""
    lines: list[str] = []
    at, size = 0, len(text)
    while at < size:
        end = text.find("\n", at)
        if end < 0:
            end = size
        while at < size and at <= end:
            if text_width(text[at:end]) <= width:
                lines.append(text[at:end])
                at = end + 1 if end < size else end
                continue
            space: int | None = None
            in_space, used, cut = False, 0, at
            while cut < end:
                char = text[cut]
                if in_space and char not in SPACES:
                    in_space = False
                elif not in_space and char in SPACES:
                    in_space, space = True, cut
                cells = max(cell_width(char), 0)
                if used and cells + used > width:
                    break
                cut += 1
                used += cells
            if space is not None:
                cut = space
            lines.append(text[at:cut])
            at = cut
            while at < end and text[at] in SPACES:
                at += 1
    return lines


KINDS: Final = frozenset({"msgbox", "yesno", "infobox", "inputbox", "passwordbox", "menu"}) | {
    "checklist",
    "radiolist",
}
# the kinds whose text goes through dialogboxes.c textbox(), which cleans its line breaks again
_TEXTBOX_HELPER: Final = frozenset({"inputbox", "passwordbox", "menu", "checklist", "radiolist"})


@dataclass(frozen=True)
class Box:
    """A whiptail box as the installer calls it: its kind (the option without --), its height and
    width, the height of its list, and whether its text scrolls or its buttons are full."""

    kind: str
    height: int
    width: int
    list_height: int = 0
    scroll: bool = False
    full_buttons: bool = False

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"no such kind of box: {self.kind}")

    @property
    def text_width(self) -> int:
        """The cells of a line of text: the window is the box less 2, the text area 2 less."""
        return self.width - 4

    @property
    def lines(self) -> int:
        """The lines of text the box shows: its window (the box less 2) less the button row
        (1, or 3 with --fb) and the rows around them; a menu's and a checklist's also less the
        list."""
        window = self.height - 2
        buttons = 3 if self.full_buttons else 1
        if self.kind == "menu":
            return window - 4 - buttons - self.list_height
        if self.kind in {"checklist", "radiolist"}:
            return window - 3 - buttons - self.list_height
        return window - 3 - buttons

    def layout(self, text: str) -> list[str]:
        """Every line of `text` as this box wraps it, also those it cannot show."""
        cleaned = clean_newlines(text)
        if self.kind in _TEXTBOX_HELPER:
            cleaned = clean_newlines(cleaned)
        return reflow(expand_tabs(cleaned), self.text_width)

    def describe(self) -> str:
        extra = f" {self.list_height}" if self.kind in {"menu", "checklist", "radiolist"} else ""
        flags = (" --scrolltext" if self.scroll else "") + (" --fb" if self.full_buttons else "")
        return f"--{self.kind}{flags} {self.height} {self.width}{extra}"


# --- texts ------------------------------------------------------------------------------------


class Expr:
    """What a dialog shows: rendered into every candidate text, in one language."""

    def render(self, translate: Callable[[str], str]) -> list[str]:
        raise NotImplementedError

    def msgids(self) -> Iterator[str]:
        yield from ()


@dataclass(frozen=True)
class Lit(Expr):
    """A text as it is in every language: written in the script, or a worst case."""

    text: str

    def render(self, translate: Callable[[str], str]) -> list[str]:
        return [self.text]


@dataclass(frozen=True)
class Msg(Expr):
    """`t MSGID NAME VALUE ...`: the msgid in the language, its {name}s filled in one pass."""

    msgid: str
    values: tuple[tuple[str, Expr], ...] = ()

    def render(self, translate: Callable[[str], str]) -> list[str]:
        template = translate(self.msgid)
        names = [name for name, _ in self.values]
        choices = [value.render(translate) for _, value in self.values]
        return _unique(
            [_fill(template, dict(zip(names, values, strict=True))) for values in _product(choices)]
        )

    def msgids(self) -> Iterator[str]:
        yield self.msgid
        for _, value in self.values:
            yield from value.msgids()


@dataclass(frozen=True)
class Cat(Expr):
    """Texts one after the other."""

    parts: tuple[Expr, ...]

    def render(self, translate: Callable[[str], str]) -> list[str]:
        return _unique(["".join(p) for p in _product([x.render(translate) for x in self.parts])])

    def msgids(self) -> Iterator[str]:
        for part in self.parts:
            yield from part.msgids()


@dataclass(frozen=True)
class Alt(Expr):
    """One of several texts, as the script's branches choose."""

    options: tuple[Expr, ...]

    def render(self, translate: Callable[[str], str]) -> list[str]:
        return _unique([text for option in self.options for text in option.render(translate)])

    def msgids(self) -> Iterator[str]:
        for option in self.options:
            yield from option.msgids()


@dataclass(frozen=True)
class Output(Expr):
    """What $(...) gives: the command's output without its final line breaks."""

    inner: Expr

    def render(self, translate: Callable[[str], str]) -> list[str]:
        return _unique([text.rstrip("\n") for text in self.inner.render(translate)])

    def msgids(self) -> Iterator[str]:
        yield from self.inner.msgids()


_FIELD: Final = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")  # what the installer's _fill fills


def _fill(template: str, values: Mapping[str, str]) -> str:
    """The installer's _fill: each {name} with a value replaced, in one pass."""
    return _FIELD.sub(lambda match: values.get(match[1], match[0]), template)


MAX_CANDIDATES: Final = 5000


def _product(choices: Sequence[Sequence[str]]) -> Iterator[tuple[str, ...]]:
    count = 1
    for options in choices:
        count *= len(options)
    if count > MAX_CANDIDATES:
        raise DialogError(f"{count} combinations of values: more than {MAX_CANDIDATES}")
    return itertools.product(*choices)


def _unique(texts: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(texts))


def one_of(options: Sequence[Expr]) -> Expr:
    unique = list(dict.fromkeys(options))
    return unique[0] if len(unique) == 1 else Alt(tuple(unique))


Catalog = Mapping[str, str]  # msgid -> its translation; English is the empty catalog


def catalogs() -> dict[str, Catalog]:
    """English and every language of the installer, each catalog as `make translations` writes it:
    a language added later is in it as soon as its catalog is."""
    project = tool.Project(ROOT)
    found: dict[str, Catalog] = {"en": {}}
    for path in project.installer_catalogs():
        header, entries = read_po(path.read_text(encoding="utf-8"), path.name)
        found[str(header.get("Language") or path.stem)] = {
            entry.msgid: entry.msgstr[0]
            for entry in entries
            if entry.msgctxt is None and entry.msgstr and entry.msgstr[0]
        }
    return found


def translator(catalog: Catalog) -> Callable[[str], str]:
    """The installer's t: the translation, else the msgid (`${L10N[$msgid]-$msgid}`)."""
    return lambda msgid: catalog.get(msgid, msgid)


# --- worst cases ------------------------------------------------------------------------------


class Worst(NamedTuple):
    value: str
    why: str


# A field's value where the script does not give a text: the longest realistic value, or a
# documented worst case. The same name means the same in every text; where it does not, the
# longest meaning is taken.
FIELD_VALUES: Final[Mapping[str, Worst]] = {
    "address": Worst(
        "[2001:db8:1234:5678::1]:5000", "an IPv6 address and the port, as probe takes it"
    ),
    "bridge": Worst("vmbr0", "Proxmox VE's default bridge"),
    "ctid": Worst("1000", "a container ID of four digits (Proxmox VE offers the next from 100)"),
    "ctids": Worst("1000, 1001", "two containers that pass the radio through"),
    "channel": Worst("999", "the most digits targets.channels takes"),
    "device": Worst(
        "/dev/serial/by-id/usb-Example_Vendors_Example_LoRa_Radio_0123456789AB-if00",
        "a by-id path as long as udev gives an ESP32 board's USB serial port",
    ),
    "disk": Worst("1000", "a disk of four digits in GB"),
    "file": Worst("/root/meshcore-home-gateway-answers", "the answers file the installer writes"),
    "gateway": Worst("192.0.2.254", "an IPv4 address"),
    "host": Worst("mqtt.home.example.org", "a broker's host name"),
    "hostname": Worst("meshcore-home-gateway", "the container host name the installer suggests"),
    "id": Worst("1000", "as ctid"),
    "ip": Worst("192.0.2.200/24", "an IPv4 address with its prefix"),
    "key": Worst("homeassistant.device_name", "the longest answer key"),
    "kind": Worst("checklist", "the longest kind of dialog"),
    "line": Worst("1000", "a line number of four digits"),
    "memory": Worst("10000", "memory of five digits in MB"),
    "name": Worst("x" * 32, "a radio name one byte too long (the limit is 31 bytes)"),
    "option": Worst("--reconfigure=1000", "an option with a value"),
    "path": Worst("/etc/meshcore-home-gateway/config.toml", "the configuration's path"),
    "port": Worst("65535", "a port of five digits"),
    "radio": Worst(
        "/dev/serial/by-id/usb-Example_Vendors_Example_LoRa_Radio_0123456789AB-if00",
        "as device",
    ),
    "reason": Worst(
        "cannot connect to the broker mqtt.home.example.org:8883: [SSL: "
        "CERTIFICATE_VERIFY_FAILED] certificate verify failed: unable to get local issuer "
        "certificate (_ssl.c:1032)",
        "the program's message when TLS fails, with the host name of the broker",
    ),
    "ref": Worst("feature/a-branch-with-a-long-name", "a branch name of 32 characters"),
    "storage": Worst("local-zfs-storage", "a storage name"),
    "template": Worst("debian-13-standard_13.6-1_amd64.tar.zst", "Debian 13's template"),
    "text": Worst(
        "MQTT broker: host name or IP address (no mqtt://)", "the longest required input's text"
    ),
    "tls": Worst("false", "true or false"),
    "tool": Worst("dpkg-query", "the longest tool preflight looks for"),
    "user": Worst("meshcore-home-gateway", "the user name the installer suggests"),
    "version": Worst("2.10.10rc10", "a program version (longer than Proxmox VE's)"),
    "was": Worst("meshcore-home-gateway", "as user"),
}

# the installer's releases, from its own constant (REPOSITORY=owner/name)
_REPOSITORY = re.search(r"^REPOSITORY=(\S+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE)
RELEASES: Final = f"https://github.com/{_REPOSITORY[1] if _REPOSITORY else ''}/releases"

# An expansion inside a longer word (`"pct exec $ctid -- ..."`), by its text in the script.
EMBEDDED: Final[Mapping[str, Worst]] = {
    "$ctid": FIELD_VALUES["ctid"],
    "${CT[bridge]}": Worst("vmbr0", "Proxmox VE's default bridge"),
    "${CT[ip]}": Worst("192.0.2.200/24", "an IPv4 address with its prefix"),
    "${L10N_NAME[$lang]}": Worst("N" * 24, "a language's name of 24 letters"),
    "$(invocation)": Worst(
        f'bash -c "$(wget -qO- {RELEASES}/download/v2.10.10rc10/meshcore-home-gateway-lxc.sh)" --',
        "a pre-release's installer, the longest a release gives (invocation(); --ref is longer)",
    ),
}


# --- the dialogs of the script ----------------------------------------------------------------


class DialogError(ValueError):
    """A dialog whose box or text the model cannot read: the message names the line."""


@dataclass(frozen=True)
class Dialog:
    line: int
    function: str
    box: Box
    text: Expr

    def where(self) -> str:
        return f"line {self.line} ({self.function or 'top level'})"


# The installer's own dialog functions: where they take the kind (its index, or the kind itself)
# and the text. show_dialog's kinds are whiptail's (KIND_OPTIONS); its sizes are its own calls.
DIALOG_FUNCTIONS: Final[Mapping[str, tuple[int | str, int]]] = {
    "show_dialog": (0, 1),
    "ask": (1, 2),
    "ask_advanced": (1, 2),
    "ask_required": ("input", 1),
}
KIND_OPTIONS: Final = {
    "input": "inputbox",
    "password": "passwordbox",
    "yesno": "yesno",
    "menu": "menu",
    "checklist": "checklist",
}
NO_BOX: Final = "cancelled"  # die shows no box for these (`[[ $1 != cancelled* ]]`)
_WITH_VALUE: Final = frozenset(
    {"--title", "--backtitle", "--default-item", "--output-fd"}
    | {"--yes-button", "--no-button", "--ok-button", "--cancel-button"}
)
_FLAGS: Final = frozenset(
    {"--defaultno", "--separate-output", "--nocancel", "--noitem", "--notags", "--topleft"}
)
_ASSIGNED: Final = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[[^]]*\])?(\+?)=(.*)", re.DOTALL)
_DECLARING: Final = frozenset({"local", "declare", "readonly", "export"})


@dataclass(frozen=True)
class _Context:
    function: str
    caller: tuple[Sequence[Any], _Context] | None = None  # die's: the call's words and context


@dataclass(frozen=True)
class _Assignment:
    function: str
    name: str
    append: bool
    value: Any  # a word of the parser in tools/i18n.py


@dataclass
class Script:
    """The script's commands as tools/i18n.py parses them, and its dialogs."""

    text: str
    name: str = "deploy/proxmox/meshcore-home-gateway-lxc.sh"
    commands: list[Any] = field(init=False)
    functions: dict[str, list[Any]] = field(init=False)
    assignments: list[_Assignment] = field(init=False)

    def __post_init__(self) -> None:
        shell = tool._Shell(tool._without_blocks(self.text), self.name)
        self.commands = list(shell.commands)
        self.functions = {}
        for command in self.commands:
            self.functions.setdefault(command.function, []).append(command)
        self.assignments = [a for command in self.commands for a in _assignments(command)]
        self._resolving: set[tuple[str, str]] = set()

    # the boxes ---------------------------------------------------------------------------------

    def _whiptail(self, command: Any) -> tuple[Box, Any]:
        """A whiptail call's box and the word of its text."""
        head = command.head()
        words = command.words[head + 1 :]
        where = f"{self.name}:{command.words[head].line}"
        kind, scroll, full, index = "", False, False, 0
        while index < len(words):
            text = words[index].text
            if text == "--":
                break
            if text in _WITH_VALUE:
                index += 1
            elif text == "--scrolltext":
                scroll = True
            elif text in {"--fb", "--fullbuttons"}:
                full = True
            elif text.removeprefix("--") in KINDS:
                kind = text.removeprefix("--")
            elif text not in _FLAGS and not text.startswith('"${'):
                raise DialogError(f"{where}: whiptail option {text} is not known to the model")
            index += 1
        if not kind or index >= len(words):
            raise DialogError(f"{where}: whiptail without a kind of box or without --")
        listed = kind in {"menu", "checklist", "radiolist"}
        sizes = words[index + 2 : index + (5 if listed else 4)]
        if len(sizes) < (3 if listed else 2) or any(not w.text.isdigit() for w in sizes):
            raise DialogError(f"{where}: the size of the box is not written as numbers")
        numbers = [int(word.text) for word in sizes]
        box = Box(kind, numbers[0], numbers[1], numbers[2] if listed else 0, scroll, full)
        return box, words[index + 1]

    def show_dialog_boxes(self) -> dict[str, Box]:
        """show_dialog's box for each of its kinds."""
        boxes = {}
        for command in self._calls("whiptail", "show_dialog"):
            box, _ = self._whiptail(command)
            boxes[box.kind] = box
        return boxes

    def _calls(self, name: str, function: str | None = None) -> Iterator[Any]:
        for command in self.commands:
            head = command.head()
            if head is None or command.words[head].text != name:
                continue
            if function is None or command.function == function:
                yield command

    # the dialogs -------------------------------------------------------------------------------

    def dialogs(self) -> list[Dialog]:
        """Every dialog of the script with its box and its text, in the order of the script."""
        found: list[Dialog] = []
        boxes = self.show_dialog_boxes()
        error_box, error_text = self._whiptail(next(self._calls("whiptail", "die")))
        for command in self.commands:
            head = command.head()
            if head is None:
                continue
            name, line = command.words[head].text, command.words[head].line
            arguments = command.words[head + 1 :]
            context = _Context(command.function)
            if name == "whiptail" and command.function not in {"show_dialog", "die"}:
                box, word = self._whiptail(command)
                found.append(Dialog(line, command.function, box, self.text_of(word, context)))
            elif name == "die" and command.function != "die":
                message = arguments[0].literal if arguments else None
                if message is None:
                    raise DialogError(f"{self.name}:{line}: die without a literal text")
                if message.startswith(NO_BOX):
                    continue
                inner = _Context("die", (arguments, context))
                found.append(
                    Dialog(line, command.function, error_box, self.text_of(error_text, inner))
                )
            elif name in DIALOG_FUNCTIONS and command.function not in DIALOG_FUNCTIONS:
                where, at = DIALOG_FUNCTIONS[name]
                kind = where if isinstance(where, str) else _literal(arguments, where)
                if kind not in KIND_OPTIONS or KIND_OPTIONS[kind] not in boxes:
                    raise DialogError(f"{self.name}:{line}: {name} with the kind {kind!r}")
                if len(arguments) <= at:
                    raise DialogError(f"{self.name}:{line}: {name} without a text")
                text = self.text_of(arguments[at], context)
                found.append(Dialog(line, command.function, boxes[KIND_OPTIONS[kind]], text))
        return found

    def text_of(self, word: Any, context: _Context) -> Expr:
        """What a word that is a dialog's text shows."""
        expr = self._word(word.text, context, None)
        if expr is None:
            raise DialogError(f"{self.name}:{word.line}: cannot read the text {word.text[:60]}")
        return expr

    # reading a word ----------------------------------------------------------------------------

    def _word(self, text: str, context: _Context, fallback: Worst | None) -> Expr | None:
        """A word as text: its literal parts, and what its expansions give; None when it holds
        an expansion that is no text and has no worst case."""
        parts = _parts(text)
        if len(parts) == 1 and parts[0][0] != "lit":
            kind, inner = parts[0]
            expr = (
                self._command(inner, context) if kind == "cmd" else self._variable(inner, context)
            )
            if expr is not None:
                return expr
            return None if fallback is None else Lit(fallback.value)
        found: list[Expr] = []
        for kind, inner in parts:
            if kind == "lit":
                found.append(Lit(inner))
                continue
            expr = (
                self._command(inner, context) if kind == "cmd" else self._variable(inner, context)
            )
            if expr is None:
                key = f"$({inner})" if kind == "cmd" else inner
                if key not in EMBEDDED:
                    raise DialogError(f"{self.name}: no worst case for {key} in {text[:60]}")
                expr = Lit(EMBEDDED[key].value)
            found.append(expr)
        if all(isinstance(part, Lit) for part in found):
            return Lit("".join(part.text for part in found if isinstance(part, Lit)))
        return found[0] if len(found) == 1 else Cat(tuple(found))

    def _command(self, inner: str, context: _Context) -> Expr | None:
        """$(...): a `t` or `$say` call, or a function of the script that gives texts."""
        commands = tool._Shell(inner, self.name).commands
        if not commands:
            return None
        command = commands[-1]  # the outermost: a $(...) inside it is parsed before it ends
        head = command.head()
        if head is None:
            return None
        name, arguments = command.words[head].text, command.words[head + 1 :]
        if name in {"t", "$say"}:
            return Output(self._message(arguments, context))
        if name in COMPOSED:
            return Output(COMPOSED[name](self))
        texts = [c for c in self.functions.get(name, []) if _head(c) in {"t", "$say"}]
        if texts:  # its texts are its output, one of them
            inner_context = _Context(name)
            return Output(
                one_of([self._message(c.words[c.head() + 1 :], inner_context) for c in texts])
            )
        return None

    def _message(self, arguments: Sequence[Any], context: _Context) -> Expr:
        if arguments and arguments[0].text == '"$@"' and context.caller is not None:
            arguments, context = context.caller
        msgid = _literal(arguments, 0)
        values = []
        for index in range(1, len(arguments) - 1, 2):
            name = _literal(arguments, index)
            value = self._word(arguments[index + 1].text, context, FIELD_VALUES.get(name))
            if value is None:
                raise DialogError(f"{self.name}: no worst case for {{{name}}} in {msgid[:60]!r}")
            values.append((name, value))
        return Msg(msgid, tuple(values))

    def _variable(self, expansion: str, context: _Context) -> Expr | None:
        """$NAME: one of the texts the function (or, for a constant, the script) sets it to."""
        match = re.fullmatch(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?", expansion)
        if match is None:
            return None
        name = match[1]
        assigned = [a for a in self.assignments if a.name == name]
        local = [a for a in assigned if a.function == context.function and not a.append]
        if not local and len(assigned) == 1 and assigned[0].function == "":
            local = assigned  # a constant: set once, at the top of the script
        key = (context.function, name)
        if not local or key in self._resolving:
            return None
        self._resolving.add(key)
        try:
            options = [self._word(a.value.text, _Context(a.function), None) for a in local]
        finally:
            self._resolving.discard(key)
        if any(option is None for option in options):
            return None
        return one_of([option for option in options if option is not None])

    def appended(self, function: str, name: str) -> Cat:
        """A variable as text that is set once and then appended to once: one of its texts,
        then one of its appends (the branches of an if)."""
        context = _Context(function)
        sets = [a for a in self.assignments if a.function == function and a.name == name]
        parts = []
        for append in (False, True):
            options = [self._word(a.value.text, context, None) for a in sets if a.append is append]
            if not options or any(option is None for option in options):
                raise DialogError(f"{self.name}: cannot read {name} in {function}")
            parts.append(one_of([option for option in options if option is not None]))
        return Cat(tuple(parts))

    def messages_in(self, function: str) -> set[str]:
        """The msgids of the `t` and `$say` calls in a function."""
        return {
            c.words[c.head() + 1].literal
            for c in self.functions.get(function, [])
            if _head(c) in {"t", "$say"} and c.words[c.head() + 1].literal
        }


def _head(command: Any) -> str | None:
    head = command.head()
    return None if head is None else str(command.words[head].text)


def _literal(words: Sequence[Any], index: int) -> str:
    if index >= len(words) or words[index].literal is None:
        shown = words[index].text if index < len(words) else "nothing"
        raise DialogError(f"a literal expected, not {shown}")
    return str(words[index].literal)


def _assignments(command: Any) -> Iterator[_Assignment]:
    head = command.head()
    words = command.words
    if head is None:
        candidates = list(words)
    elif words[head].text in _DECLARING:
        candidates = [*words[:head], *words[head + 1 :]]
    else:
        candidates = list(words[:head])
    for word in candidates:
        match = _ASSIGNED.fullmatch(word.text)
        if match and not match[2]:
            value = type(word)(match[4], word.line, None)
            yield _Assignment(command.function, match[1], bool(match[3]), value)


# --- the parts of a word ----------------------------------------------------------------------

_ESCAPES: Final = {"n": "\n", "t": "\t", "\\": "\\", "'": "'", '"': '"'}
_NAME: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?$!-]")


def _parts(text: str) -> list[tuple[str, str]]:
    """A word as bash reads it: ("lit", text), ("cmd", what $(...) holds) and ("var", an
    expansion as written), quotes removed."""
    parts: list[tuple[str, str]] = []
    at = 0
    while at < len(text):
        char = text[at]
        if char == "'":
            end = text.index("'", at + 1)
            _add_literal(parts, text[at + 1 : end])
            at = end + 1
        elif text.startswith("$'", at):
            at = _ansi(text, at + 2, parts)
        elif char == '"':
            at = _double(text, at + 1, parts)
        elif char == "$":
            at = _dollar(text, at, parts)
        elif char == "\\":
            _add_literal(parts, "" if text[at + 1 : at + 2] == "\n" else text[at + 1 : at + 2])
            at += 2
        else:
            _add_literal(parts, char)
            at += 1
    return parts


def _add_literal(parts: list[tuple[str, str]], text: str) -> None:
    if parts and parts[-1][0] == "lit":
        parts[-1] = ("lit", parts[-1][1] + text)
    else:
        parts.append(("lit", text))


def _ansi(text: str, at: int, parts: list[tuple[str, str]]) -> int:
    out = []
    while text[at] != "'":
        if text[at] == "\\":
            out.append(_ESCAPES[text[at + 1]])
            at += 2
        else:
            out.append(text[at])
            at += 1
    _add_literal(parts, "".join(out))
    return at + 1


def _double(text: str, at: int, parts: list[tuple[str, str]]) -> int:
    """From after the opening quote; the index after the closing one."""
    while text[at] != '"':
        char = text[at]
        if char == "\\" and text[at + 1] in '$`"\\\n':
            _add_literal(parts, "" if text[at + 1] == "\n" else text[at + 1])
            at += 2
        elif char == "$" and (text[at + 1] in "({" or _NAME.match(text, at + 1)):
            at = _dollar(text, at, parts)
        else:
            _add_literal(parts, char)
            at += 1
    return at + 1


def _dollar(text: str, at: int, parts: list[tuple[str, str]]) -> int:
    if text.startswith("$(", at):
        end = _closing(text, at + 2, ")")
        parts.append(("cmd", text[at + 2 : end - 1]))
        return end
    if text.startswith("${", at):
        end = _closing(text, at + 2, "}")
        parts.append(("var", text[at:end]))
        return end
    name = _NAME.match(text, at + 1)
    if name is None:
        _add_literal(parts, "$")
        return at + 1
    parts.append(("var", text[at : name.end()]))
    return name.end()


def _closing(text: str, at: int, closer: str) -> int:
    """The index after the `closer` that ends what starts at `at` (after `$(` or `${`)."""
    depth = 0
    while at < len(text):
        char = text[at]
        if char == "\\":
            at += 2
        elif char == "'":
            at = text.index("'", at + 1) + 1
        elif char == '"':
            at = _double(text, at + 1, [])
        elif text.startswith(("$(", "${"), at):
            at = _closing(text, at + 2, ")" if text[at + 1] == "(" else "}")
        else:
            if char == closer and depth == 0:
                return at + 1
            if closer == ")" and char in "()":
                depth += 1 if char == "(" else -1
            at += 1
    raise DialogError(f"{closer} missing in {text[:60]}")


# --- texts made of several calls --------------------------------------------------------------


# the results of check_broker and self_test that come together in summary_text: a failed login
# skips the self-test, and a version without the login check (before 1.0.0rc1) has no doctor
# (2.0.0a1) either. "login not checked" is the fallback of its case statement, which no box
# shows: both modes run check_broker, which sets a result or ends the installer, before summary
SUMMARY_RESULTS: Final[Mapping[str, frozenset[str]]] = {
    "login accepted": frozenset(
        {
            "passed",
            "problems found (see above): {command}",
            "not run (version {version} has no doctor command)",
        }
    ),
    "not checked (version {version} cannot; check after --update)": frozenset(
        {"not run (version {version} has no doctor command)"}
    ),
    "login failed (see above), the service keeps trying": frozenset(
        {"not run (the broker login failed)"}
    ),
    "login not checked": frozenset(),
}


def _summary(script: Script) -> Expr:
    """summary_text: its text, with the login and the self-test as they come together
    (SUMMARY_RESULTS), then the line about the radio (serial or TCP)."""
    text, radio = script.appended("summary_text", "text").parts
    message = text.inner if isinstance(text, Output) else text
    if not isinstance(message, Msg):
        raise DialogError("summary_text: its text is no t call")
    values = dict(message.values)
    logins, selftests = _options(values["login"]), _options(values["selftest"])
    pairs = []
    for login, selftest in itertools.product(logins, selftests):
        login_id, selftest_id = next(login.msgids()), next(selftest.msgids())
        if login_id not in SUMMARY_RESULTS:
            raise DialogError(f"summary_text: the login result {login_id!r} is not paired")
        if selftest_id in SUMMARY_RESULTS[login_id]:
            joint = {**values, "login": login, "selftest": selftest}
            pairs.append(Msg(message.msgid, tuple(joint.items())))
    return Cat((Alt(tuple(pairs)), radio))


def _options(expr: Expr) -> tuple[Expr, ...]:
    return expr.options if isinstance(expr, Alt) else (expr,)


# the radio's settings as `probe --list profile` writes them: name, value on the radio, wanted
PLAN_SETTINGS: Final = (
    ("manual_add", "false", "true"),
    ("autoadd", "30", "0"),
    ("telemetry", "3", "0"),
    ("advert_location", "1", "0"),
    ("airtime_factor_milli", "0", "9000"),
    ("name", '"' + "r" * 31 + '"', '"' + "g" * 31 + '"'),  # the most bytes a name takes
)
PLAN_CONTACTS: Final = 3  # e.g. two targets and a command sender
PLAN_CONTACT: Final = "0123456789ab Handheld of a neighbour"  # the start of its key, its name


def _plan(script: Script) -> Expr:
    """plan_text, for the worst of four realistic plans: on a new radio with tested firmware
    every setting is set and the contacts are added; with untested firmware (auto) or with
    radio.profile.manage = observe they are only reported; on a radio the service has managed
    before, two settings are set and four are left alone, one of each kind."""
    names = script.messages_in("plan_text")

    def say(msgid: str, **values: str) -> Expr:
        if msgid not in names:
            raise DialogError(f"plan_text has no text {msgid!r}")
        return Msg(msgid, tuple((name, Lit(value)) for name, value in values.items()))

    def setting(name: str, live: str, wanted: str) -> Expr:
        return Lit(f"  {name}: {live} -> {wanted}\n")

    def contact(msgid: str) -> list[Expr]:
        return [Cat((Lit("  "), say(msgid, contact=PLAN_CONTACT), Lit("\n")))] * PLAN_CONTACTS

    def section(header: Expr, lines: list[Expr]) -> list[Expr]:
        return [header, Lit("\n"), *lines]

    settings = [setting(*s) for s in PLAN_SETTINGS]
    sets = say("At its next connect the service sets the radio:")
    untested = say(
        "The radio's firmware is not tested: the service only reports what differs\n"
        "(radio.profile.manage = always sets it anyway):"
    )
    observe = say("With radio.profile.manage = observe the service only reports what differs:")
    left_alone = [
        say("{setting}: {live} (changed on the radio; kept)", setting="telemetry", live="3"),
        say(
            "{setting}: {live}, not {wanted} (did not take effect; no longer set)",
            setting="airtime_factor_milli",
            live="0",
            wanted="9000",
        ),
        say(
            "{setting}: {live}, not {wanted} (the firmware does not support it)",
            setting="advert_location",
            live="1",
            wanted="0",
        ),
        say(
            "{setting}: {live}, not {wanted} (compiled out of the radio's firmware)",
            setting="autoadd",
            live="30",
            wanted="0",
        ),
    ]
    plans = [
        section(sets, [*settings, *contact("contact {contact}: added as a favourite")]),
        section(untested, [*settings, *contact("contact {contact}: not on the radio")]),
        section(observe, [*settings, *contact("contact {contact}: no favourite")]),
        [
            *section(sets, [*settings[:2], *contact("contact {contact}: made a favourite")[:1]]),
            *section(
                say("The service leaves alone:"),
                [Cat((Lit("  "), line, Lit("\n"))) for line in left_alone],
            ),
        ],
    ]
    return Alt(tuple(Cat(tuple(plan)) for plan in plans))


COMPOSED: Final[Mapping[str, Callable[[Script], Expr]]] = {
    "summary_text": _summary,
    "plan_text": _plan,
}


def dialogs(path: Path = SCRIPT) -> list[Dialog]:
    return Script(path.read_text(encoding="utf-8")).dialogs()


# --- the check --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Overflow:
    """A text that needs more lines than its box shows."""

    language: str
    dialog: Dialog
    lines: tuple[str, ...]

    def __str__(self) -> str:
        box = self.dialog.box
        shown = self.lines[box.lines - 1] if box.lines > 0 else ""
        return (
            f"{self.language}: {self.dialog.where()}: whiptail {box.describe()} shows "
            f"{box.lines} lines, the text needs {len(self.lines)}; the last shown is "
            f"{shown!r}, cut: {' '.join(self.lines[box.lines :])!r}"
        )


def overflows(found: Sequence[Dialog], languages: Mapping[str, Catalog]) -> list[Overflow]:
    """Every dialog whose text, in a language, needs more lines than its box shows: the longest
    of its candidates. A text that scrolls (--scrolltext) is exempt."""
    result = []
    for language, catalog in languages.items():
        translate = translator(catalog)
        for dialog in found:
            if dialog.box.scroll:
                continue
            laid_out = [dialog.box.layout(text) for text in dialog.text.render(translate)]
            longest = max(laid_out, key=len)
            if len(longest) > dialog.box.lines:
                result.append(Overflow(language, dialog, tuple(longest)))
    return result
