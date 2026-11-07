"""The Home Assistant dashboard of one gateway (ADR 0021; the part this version builds:
docs/design.md): the template dashboard.yaml of this package, filled in for the gateway's node,
this version, its chats and the contacts of its state, its texts in [homeassistant] language.
Text in, text out: no YAML parser at run time (ADR 0005).

The template is YAML as it stands, every branch of its conditionals included:

* `@gw@` (the gateway's part of its entity IDs), `@contact@` (how its contacts' entity IDs
  begin), `@node@`, `@version@`, `@major@` and `@minor@` (its first two numbers), `@guide@`
  (the address of the guide, docs/home-assistant.md, of this version),
  `["@trackers@"]` (the map's trackers, a JSON list) and `["@chats@"]` (the chat pages, a list
  of JSON objects, which Jinja reads) are filled in.
* `# @each <list>` … `# @end` repeats its lines for each item of `chats`, `switches` or
  `removes`, with `@<list>.<field>@` the item's field; no @each inside another. Each item value
  matches its pattern (FIELDS) before it goes in: none is text from the mesh.
* `# @if <flag>` … `# @else` … `# @end` keeps the first or the second part: `trackers` and a
  list are true while not empty, `<list>.<field>` inside that list's @each while the field is
  not empty. A directive may stand inside a card's content, indented as its lines: there it
  repeats or keeps lines of the content.
* `_("…")`, a JSON string inside, marks a text (msgctxt "dashboard"; tools/i18n.py extracts it).
  As a whole YAML value it becomes a quoted scalar, whose only field is {name}; inside a card's
  content (a literal block) it becomes the text, {node} and {version} filled in, and any other
  field the Jinja variable of that name, which the card sets before it. {name} is the title of
  the item of an @each, and refused outside one.
* A translation is used only when it holds nothing of FORBIDDEN and no character of UNSAFE, the
  rule the catalog check of tools/i18n.py applies too; else the English text.

tests/test_dashboard.py checks the rest of the template's rules on the shipped one: a value read
from Home Assistant reaches a card only through the card's macro m(), on a line that is one HTML
element.
"""

from __future__ import annotations

import json
import re
import string
import unicodedata
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from typing import TYPE_CHECKING, Final

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.core.model import MessageKind, Target
from meshcore_home_gateway.mqtt.discovery import DOCUMENTATION_URL, text_room
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES
from meshcore_home_gateway.state import ContactDeviceRecords, DeviceRecord, JsonStateFile

if TYPE_CHECKING:
    from pathlib import Path

    from meshcore_home_gateway.config import Config

TEMPLATE: Final = "dashboard.yaml"
# the Home Assistant a dashboard of this template needs: its guards are template conditions,
# which the frontend has since 2026.10; the gateway's own floor stays 2026.8
HOME_ASSISTANT: Final = "2026.10"
MARKER: Final = re.compile(r'_\("((?:[^"\\]|\\.)*)"\)')
VALUE: Final = re.compile(r'^(\s*(?:- )?[A-Za-z_][\w-]*: )_\("((?:[^"\\]|\\.)*)"\)\s*$')
# what a card would read as code: Jinja, HTML, Markdown links and images, the autolinks of web
# and mail addresses (the alerts are Markdown), a marker
FORBIDDEN: Final = ("{{", "{%", "{#", "<", ">", "@", "](", "://", "www.", '_("')
# control and format characters, line and paragraph separators: CR and LF end a card's HTML
# block, U+2028 and U+2029 the YAML block that holds the card
UNSAFE: Final = frozenset({"Cc", "Cf", "Zl", "Zp"})
# filled in here; any other field is a Jinja variable. {name}: the item's title, in an @each only
FIXED: Final = ("node", "version", "name")
LISTS: Final = ("chats", "switches", "removes")
# people with a chat page, at most, besides the contact targets': 30 load as fast as a small
# dashboard (H-9)
CHAT_PAGES: Final = 30
_ALIAS: Final = r"[a-z0-9](?:[a-z0-9]|[-_](?=[a-z0-9])){0,31}"  # config.ID_RULE
_ENTITY: Final = r"(?:text|switch|button)\.[a-z0-9_]+"
TITLE_CHARS: Final = 32  # a page's title, at most: one fits a phone's header (H-24)
_TITLE: Final = re.compile(rf"[A-Za-z0-9][A-Za-z0-9 ]{{0,{TITLE_CHARS - 1}}}")
# what the gateway adds to a slug another contact has: '_' and four digits of the key
# (mqtt/contact_devices.py), after a slug of up to 32 characters
_TAKEN: Final = 5
_KEY: Final = r"[0-9a-f]{64}"
_CONTROL: Final = {
    "entity": re.compile(_ENTITY),
    "title": _TITLE,
    "key": re.compile(_KEY),
}
# each list's fields, and what a value must match: never text from the mesh
FIELDS: Final[Mapping[str, Mapping[str, re.Pattern[str]]]] = {
    "chats": {
        "path": re.compile(r"(?:chat|contact)-[a-z0-9_-]{1,40}"),
        "title": _TITLE,
        "key": re.compile(f"(?:{_KEY})?"),
        "channel": re.compile(r"-1|0|[1-9][0-9]{0,2}"),  # a channel's index; -1: a person
        "target": re.compile(f"(?:{_ALIAS})?"),
        "aliases": re.compile(f"(?:{_ALIAS}(?:,{_ALIAS})*)?"),
        "pair": re.compile(f"(?:{_ALIAS})?"),
        "write": re.compile(f"(?:{_ENTITY})?"),
        "send": re.compile(f"(?:{_ENTITY})?"),
        "room": re.compile(r"[1-9][0-9]?|1[0-5][0-9]|160"),
    },
    "switches": _CONTROL,
    "removes": _CONTROL,
}
_PLACEHOLDER: Final = re.compile(r"@[\w.]+@")
_ITEM: Final = re.compile(r"@(\w+)\.(\w+)@")
_SLUG: Final = re.compile(r"[a-z0-9_]+")  # as the gateway gives them: a part of an entity ID
_NAME: Final = re.compile(r"[A-Za-z_]\w*")  # a field's name: a Jinja variable
# stands for a Jinja variable while a text is checked: private use characters, in no text
_STAND_IN: Final = chr(0xE000) + "{}" + chr(0xE001)

Item = Mapping[str, str]


class TemplateError(ValueError):
    """The template breaks one of its rules (tests/test_dashboard.py finds it before a release)."""


def unsafe(text: str) -> str | None:
    """The first thing of FORBIDDEN or UNSAFE the text holds, as a finding names it; else None."""
    for item in FORBIDDEN:
        if item in text:
            return repr(item)
    for char in text:
        if unicodedata.category(char) in UNSAFE:
            return f"U+{ord(char):04X}"
    return None


def refused(name: str, item: Item) -> tuple[str, str] | None:
    """The first field of an item of the list `name` whose value fails its pattern, missing or
    not one of the list's fields, with the value; None for an item that may go in."""
    fields = FIELDS[name]
    for key, pattern in fields.items():
        value = item.get(key)
        if value is None:
            return key, ""
        if not pattern.fullmatch(value):
            return key, value
    return next(((key, value) for key, value in item.items() if key not in fields), None)


# --- the chat pages -----------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Chat:
    """A chat page: a channel, a contact target without a device, or a person. `write` and
    `send`: its draft field and Send button, none for a hidden target or a pair; `pair`:
    the other alias of such a pair; `room`: the gateway's limit of a text, in bytes."""

    path: str
    title: str
    key: str = ""
    channel: int = -1
    target: str = ""
    aliases: tuple[str, ...] = ()
    pair: str = ""
    write: str = ""
    send: str = ""
    room: int = MAX_TEXT_BYTES

    def item(self) -> dict[str, str]:
        return {
            "path": self.path,
            "title": self.title,
            "key": self.key,
            "channel": str(self.channel),
            "target": self.target,
            "aliases": ",".join(self.aliases),
            "pair": self.pair,
            "write": self.write,
            "send": self.send,
            "room": str(self.room),
        }


@dataclass(frozen=True, slots=True)
class Chats:
    """The pages, and what the command line reports: the pages skipped (field and value), the
    alias pairs, the people beyond CHAT_PAGES."""

    pages: tuple[Chat, ...] = ()
    skipped: tuple[tuple[str, str], ...] = ()
    pairs: tuple[tuple[str, str], ...] = ()
    without_page: int = 0

    @property
    def channels(self) -> int:
        return sum(1 for chat in self.pages if chat.channel >= 0)

    @property
    def people(self) -> int:
        return len(self.pages) - self.channels


def chats(config: Config, records: ContactDeviceRecords) -> Chats:
    """The targets' pages by alias, those with a field first, then the people's, by slug: the
    contacts with a device that takes texts, at most CHAT_PAGES besides the contact targets'. A
    target's conversation is its channel or its contact: two aliases on one share a page, led by
    the first visible one of the configuration; a contact target's contact with a device has the
    person's page, with the target's field, and keeps it whatever the cap, as every target
    does. Two visible aliases that differ in '-' and '_' only get one entity ID in Home
    Assistant: their pages have no field. A page whose values fail their patterns is skipped,
    and the command line says so."""
    gateway = _gateway(config)
    hidden = config.homeassistant.hidden_targets
    merged: dict[str, list[str]] = {}
    for target in config.targets:
        if target.alias not in hidden:
            merged.setdefault(_part(target.alias), []).append(target.alias)
    groups = [sorted(group) for group in merged.values() if len(group) > 1]
    partner = {alias: next(b for b in group if b != alias) for group in groups for alias in group}
    conversations: dict[tuple[str, object], list[Target]] = {}
    for target in config.targets:
        conversations.setdefault(_conversation(target), []).append(target)
    people = {k: r for k, r in records.announced.items() if r.components.get("send") == "notify"}

    def fields(lead: Target) -> tuple[str, str, str]:
        """The pair, the draft field and the Send button of a target's page."""
        if lead.alias in hidden:
            return "", "", ""
        if lead.alias in partner:
            return partner[lead.alias], "", ""
        part = _part(lead.alias)
        return "", f"text.{gateway}_write_{part}", f"button.{gateway}_write_{part}_send"

    led: dict[str, tuple[Target, tuple[str, ...]]] = {}  # a person's key: the target's field
    shown: list[Chat] = []
    quiet: list[Chat] = []
    for group in conversations.values():
        lead = next((t for t in group if t.alias not in hidden), group[0])
        aliases = tuple(sorted(t.alias for t in group))
        if lead.public_key is not None and lead.public_key in people:
            led[lead.public_key] = (lead, aliases)
            continue
        pair, write, send = fields(lead)
        chat = Chat(
            f"chat-{lead.alias}",
            _title(lead.alias),
            key=lead.public_key or "",
            channel=lead.channel_index if lead.channel_index is not None else -1,
            target=lead.alias,
            aliases=aliases,
            pair=pair,
            write=write,
            send=send,
            room=room(config, lead),
        )
        (quiet if lead.alias in hidden else shown).append(chat)
    persons = []
    for key, record in sorted(people.items(), key=lambda kv: (kv[1].slug, kv[0])):
        persons.append(_person(config, key, record, led.get(key), fields))
    skipped: list[tuple[str, str]] = []
    paths: set[str] = set()

    def checked(chat: Chat) -> bool:
        bad = refused("chats", chat.item())
        if bad is None and chat.path in paths:
            bad = ("path", chat.path)
        if bad is not None:
            skipped.append(bad)
            return False
        paths.add(chat.path)
        return True

    by_alias = sorted(shown, key=lambda c: c.target) + sorted(quiet, key=lambda c: c.target)
    targets = [chat for chat in by_alias if checked(chat)]
    valid = [chat for chat in persons if checked(chat)]
    pairs = tuple((group[0], other) for group in sorted(groups) for other in group[1:])
    capped = {chat.path for chat in [chat for chat in valid if not chat.target][CHAT_PAGES:]}
    kept = [chat for chat in valid if chat.path not in capped]
    return Chats(tuple(targets + kept), tuple(skipped), pairs, len(capped))


def room(config: Config, target: Target | None) -> int:
    """The bytes a text to the target, or to a person (None), may have beside the target's prefix
    and suffix: the `max` of the chat's draft before the gateway knows the radio's name, which
    only the radio knows (discovery.text_room)."""
    return text_room(config, target, None)


def _person(
    config: Config,
    key: str,
    record: DeviceRecord,
    led: tuple[Target, tuple[str, ...]] | None,
    fields: Callable[[Target], tuple[str, str, str]],
) -> Chat:
    path, title = f"contact-{record.slug}", _title(record.slug)
    if led is not None:  # a contact target: its field, prefix and suffix
        lead, aliases = led
        pair, write, send = fields(lead)
        return Chat(
            path,
            title,
            key=key,
            target=lead.alias,
            aliases=aliases,
            pair=pair,
            write=write,
            send=send,
            room=room(config, lead),
        )
    prefix = f"{config.homeassistant.contact_entity_prefix}_{record.slug}"
    return Chat(
        path,
        title,
        key=key,
        write=f"text.{prefix}_write",
        send=f"button.{prefix}_write_send",
        room=room(config, None),
    )


def _conversation(target: Target) -> tuple[str, object]:
    if target.kind is MessageKind.CHANNEL:
        return "channel", target.channel_index
    return "contact", target.public_key


def _gateway(config: Config) -> str:
    """The gateway's part of its entity IDs: Home Assistant turns '-' into '_'."""
    return "meshcore_home_gateway_" + _part(config.mqtt.node_id)


def _part(name: str) -> str:
    return name.replace("-", "_")


def _title(name: str) -> str:
    """A page's title from an alias or a slug: words, the first letter capital. A longer one is
    cut before its last five characters, which keep the digits the gateway adds to a slug that
    another contact has."""
    words = re.sub(r"[-_]", " ", name)
    if len(words) > TITLE_CHARS:
        words = words[: TITLE_CHARS - _TAKEN].rstrip() + words[-_TAKEN:]
    return words[:1].upper() + words[1:]


# --- the contacts' controls -------------------------------------------------------------------


def trackers(records: ContactDeviceRecords, prefix: str = "mesh") -> list[str]:
    """The trackers of the contacts announced with a position, sorted; `prefix`: how the
    contacts' entity IDs begin (`[homeassistant] contact_entity_prefix`). A contact deleted in
    Home Assistant is hidden, not announced; a slug that is no part of an entity ID never came
    from the gateway."""
    return sorted(
        f"device_tracker.{prefix}_{record.slug}"
        for record in records.announced.values()
        if record.components.get("position") == "device_tracker" and _SLUG.fullmatch(record.slug)
    )


def switches(records: ContactDeviceRecords, prefix: str = "mesh") -> list[dict[str, str]]:
    """The items of `switches`: each contact's switch *Telemetry*, by slug."""
    return _controls("switches", records, prefix)[0]


def removes(records: ContactDeviceRecords, prefix: str = "mesh") -> list[dict[str, str]]:
    """The items of `removes`: each contact's button *Remove from radio*, by slug."""
    return _controls("removes", records, prefix)[0]


def left_out(records: ContactDeviceRecords, prefix: str = "mesh") -> tuple[tuple[str, str], ...]:
    """The switches and buttons whose values fail their patterns, which switches() and removes()
    leave out: the field and its value, for the command line (D-39)."""
    return _controls("switches", records, prefix)[1] + _controls("removes", records, prefix)[1]


# each list of controls: the component of a contact's device, and its entity ID
_CONTROLS: Final = {
    "switches": ("telemetry", "switch.{prefix}_{slug}_telemetry"),
    "removes": ("remove", "button.{prefix}_{slug}_remove"),
}


def _controls(
    name: str, records: ContactDeviceRecords, prefix: str
) -> tuple[list[dict[str, str]], tuple[tuple[str, str], ...]]:
    component, entity = _CONTROLS[name]
    platform = entity.partition(".")[0]
    found, skipped = [], []
    for key, record in sorted(records.announced.items(), key=lambda kv: (kv[1].slug, kv[0])):
        if record.components.get(component) != platform:
            continue
        slug = record.slug
        item = {
            "entity": entity.format(prefix=prefix, slug=slug),
            "title": _title(slug),
            "key": key,
        }
        if (bad := refused(name, item)) is None:
            found.append(item)
        else:  # a slug the gateway never gives
            skipped.append(bad)
    return found, tuple(skipped)


# --- filling in the template --------------------------------------------------------------------


def render(
    template: str,
    *,
    node_id: str,
    version: str,
    trackers: Sequence[str],
    text: Callable[[str, Mapping[str, str]], str],
    contact_prefix: str = "mesh",
    chats: Sequence[Item] = (),
    switches: Sequence[Item] = (),
    removes: Sequence[Item] = (),
) -> str:
    """The template filled in; `text(msgid, values)` gives a text in the dashboard's language,
    filled with the values; `contact_prefix` begins the contacts' entity IDs; `chats`,
    `switches`, `removes`: the items of the lists, each checked against its pattern."""
    lists = {"chats": list(chats), "switches": list(switches), "removes": list(removes)}
    for name, items in lists.items():
        for each in items:
            if (bad := refused(name, each)) is not None:
                raise TemplateError(f"an item of {name}: {bad[0]} {bad[1]!r} is not allowed")
    numbers = re.match(r"(\d+)\.(\d+)", version)
    if numbers is None:
        raise TemplateError(f"the version {version!r} has no major and minor number")
    flags = {"trackers": bool(trackers)}
    tree = _parsed(template.split("\n"), flags)
    gw = "meshcore_home_gateway_" + _part(node_id)
    pages = json.dumps([_document(item) for item in lists["chats"]])
    filled = []
    for kept, name, item in _expanded(tree, flags, lists):
        line = kept if item is None else _with_item(kept, name, item)
        line = (
            line.replace('["@trackers@"]', json.dumps(sorted(trackers)))
            .replace('["@chats@"]', pages)
            .replace("@gw@", gw)
            .replace("@node@", node_id)
            .replace("@version@", version)
            .replace("@major@", str(int(numbers.group(1))))
            .replace("@minor@", str(int(numbers.group(2))))
            .replace("@contact@", contact_prefix)
            .replace("@guide@", DOCUMENTATION_URL.replace("/blob/main/", f"/blob/v{version}/"))
        )
        # before any text goes in: no translation can trip it
        if '_("' in MARKER.sub("", line) or _PLACEHOLDER.search(line):
            raise TemplateError(f"left over: {line.strip()}")
        filled.append((line, item))
    out = []
    for line, item in filled:
        fixed = {"node": node_id, "version": version}
        if item is not None:
            fixed["name"] = item["title"]
        if match := VALUE.match(line):
            msgid = _msgid(match.group(2))
            if extra := [f for f in _fields(msgid) if f != "name"]:
                raise TemplateError(f"a whole value takes no field other than {{name}}: {extra}")
            chosen = _text(msgid, {k: v for k, v in fixed.items() if k == "name"}, text)
            out.append(match.group(1) + json.dumps(chosen, ensure_ascii=False))
        else:
            out.append(_with_texts(line, fixed, text))
    return "\n".join(out)


@dataclass(frozen=True, slots=True)
class Built:
    """A dashboard and what the command line says about it: the trackers on its map, its chat
    pages, when the service wrote the state it read (None: no state, or none it could read),
    and the switches and buttons left out (left_out())."""

    yaml: str
    trackers: int
    chats: Chats = field(default_factory=Chats)
    written_at: float | None = None
    left_out: tuple[tuple[str, str], ...] = ()


def build(config: Config, state_dir: Path) -> Built:
    """The dashboard of this gateway, from the service's state in state_dir, read only: without
    it, no contacts."""
    path = state_dir / "state.json"
    records = ContactDeviceRecords(JsonStateFile(path, read_only=True))
    prefix = config.homeassistant.contact_entity_prefix
    listed = trackers(records, prefix)
    pages = chats(config, records)
    translator = i18n.translator(config.homeassistant.language)

    def text(msgid: str, values: Mapping[str, str]) -> str:
        return translator.pgettext(i18n.DASHBOARD, msgid, **values)

    template = (resources.files(__package__ or __name__) / TEMPLATE).read_text(encoding="utf-8")
    yaml = render(
        template,
        node_id=config.mqtt.node_id,
        version=__version__,
        trackers=listed,
        text=text,
        contact_prefix=prefix,
        chats=[chat.item() for chat in pages.pages],
        switches=switches(records, prefix),
        removes=removes(records, prefix),
    )
    return Built(yaml, len(listed), pages, _written_at(path), left_out(records, prefix))


def _written_at(path: Path) -> float | None:
    """When the service wrote its state: None without a file, or for one that JsonStateFile
    reads as empty because it is no JSON object, whose contacts the dashboard does not have."""
    try:
        written = path.stat().st_mtime
        document = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, ValueError):  # UnicodeDecodeError and JSONDecodeError are ValueErrors
        return None
    return written if isinstance(document, dict) else None


def dashboard(config: Config, state_dir: Path) -> tuple[str, int]:
    """The YAML for this gateway, and how many contacts its map lists (build())."""
    built = build(config, state_dir)
    return built.yaml, built.trackers


# --- the template's own language ----------------------------------------------------------------


@dataclass(slots=True)
class _If:
    condition: str
    then: list[_Node] = field(default_factory=list)
    otherwise: list[_Node] = field(default_factory=list)
    past_else: bool = False


@dataclass(slots=True)
class _Each:
    name: str
    body: list[_Node] = field(default_factory=list)


_Node = str | _If | _Each


def _parsed(lines: Sequence[str], flags: Collection[str]) -> list[_Node]:
    """The lines as a tree of @if and @each, without the directives and without the
    translators' notes, which are YAML comments for the catalog, not for the household's file."""
    root: list[_Node] = []
    opened: list[_If | _Each] = []
    note = False  # in the comment lines that a `# Translators:` line begins
    for line in lines:
        directive = line.strip()
        note = directive.startswith("# Translators:") or (
            note and directive.startswith("#") and not directive.startswith("# @")
        )
        if note:
            continue
        into = _into(root, opened)
        if not directive.startswith("# @"):
            into.append(line)
            continue
        word, _, argument = directive.removeprefix("# @").partition(" ")
        each = next((node.name for node in opened if isinstance(node, _Each)), None)
        node: _If | _Each
        if word in {"else", "end"} and not argument:
            _close(word, opened, directive)
            continue
        if word == "if":
            if argument not in flags and argument not in LISTS and not _is_field(argument, each):
                raise TemplateError(f"unknown flag in {directive!r}")
            node = _If(argument)
        elif word == "each":
            if argument not in LISTS:
                raise TemplateError(f"unknown list in {directive!r}")
            if each is not None:
                raise TemplateError(f"a nested @each in {directive!r}")
            node = _Each(argument)
        else:
            raise TemplateError(f"unknown line {directive!r}")
        into.append(node)
        opened.append(node)
    if opened:
        raise TemplateError(f"an @{'each' if isinstance(opened[-1], _Each) else 'if'} not closed")
    return root


def _close(word: str, opened: list[_If | _Each], directive: str) -> None:
    """An @end closes the last @if or @each, an @else turns to the second part of an @if."""
    if not opened:
        raise TemplateError(f"{directive!r} with no @if or @each")
    last = opened[-1]
    if word == "end":
        opened.pop()
    elif isinstance(last, _Each):
        raise TemplateError(f"@else in an @each: {directive!r}")
    elif last.past_else:
        raise TemplateError("a second @else")
    else:
        last.past_else = True


def _into(root: list[_Node], opened: Sequence[_If | _Each]) -> list[_Node]:
    if not opened:
        return root
    last = opened[-1]
    if isinstance(last, _Each):
        return last.body
    return last.otherwise if last.past_else else last.then


def _is_field(condition: str, each: str | None) -> bool:
    name, dot, key = condition.partition(".")
    return bool(dot) and name == each and key in FIELDS[name]


def _expanded(
    nodes: Sequence[_Node],
    flags: Mapping[str, bool],
    lists: Mapping[str, Sequence[Item]],
    name: str = "",
    item: Item | None = None,
) -> Iterator[tuple[str, str, Item | None]]:
    """The kept lines, each with the list and the item of its @each."""
    for node in nodes:
        if isinstance(node, str):
            yield node, name, item
        elif isinstance(node, _If):
            if node.condition in flags:
                holds = flags[node.condition]
            elif node.condition in lists:
                holds = bool(lists[node.condition])
            else:
                holds = item is not None and bool(item[node.condition.partition(".")[2]])
            yield from _expanded(node.then if holds else node.otherwise, flags, lists, name, item)
        else:
            for each in lists[node.name]:
                yield from _expanded(node.body, flags, lists, node.name, each)


def _with_item(line: str, name: str, item: Item) -> str:
    """The item placeholders of this @each's list filled in; any other stays, a left-over."""

    def value(match: re.Match[str]) -> str:
        if match.group(1) == name and match.group(2) in item:
            return item[match.group(2)]
        return match.group(0)

    return _ITEM.sub(value, line)


def _with_texts(
    line: str, fixed: Mapping[str, str], text: Callable[[str, Mapping[str, str]], str]
) -> str:
    return MARKER.sub(lambda m: _text(_msgid(m.group(1)), fixed, text), line)


def _document(item: Item) -> dict[str, object]:
    """A chat page for the cards that list them: numbers as numbers, the aliases as a list, and
    `write` whether the page has a field. The field's and the button's entity IDs stay out: they
    stand only inside the guard of the page's footer (rule 3 of the template)."""
    return {
        key: int(value)
        if key in ("channel", "room")
        else (value.split(",") if value else [])
        if key == "aliases"
        else bool(value)
        if key == "write"
        else value
        for key, value in item.items()
        if key != "send"
    }


def _msgid(body: str) -> str:
    try:
        return str(json.loads(f'"{body}"'))
    except ValueError as exc:
        raise TemplateError(f'_("{body}") is no JSON string: {exc}') from exc


def _fields(msgid: str) -> list[str]:
    try:
        names = [name for _, name, _, _ in string.Formatter().parse(msgid) if name is not None]
    except ValueError as exc:
        raise TemplateError(f"{msgid!r} is no format string: {exc}") from exc
    for name in names:
        if not _NAME.fullmatch(name):
            raise TemplateError(f"{msgid!r}: a field is a plain name, such as {{n}}")
    return names


def _text(
    msgid: str, fixed: Mapping[str, str], text: Callable[[str, Mapping[str, str]], str]
) -> str:
    """The text in the dashboard's language, or English if a card would read it as code."""
    names = _fields(msgid)
    if "name" in names and "name" not in fixed:
        raise TemplateError(f"{msgid!r}: {{name}} is an item's title, only inside an @each")
    values = {name: fixed.get(name, _STAND_IN.format(name)) for name in names}
    chosen = text(msgid, values)
    if _refused(chosen):
        chosen = i18n.ENGLISH.pgettext(i18n.DASHBOARD, msgid, **values)
        if _refused(chosen):
            raise TemplateError(f"{msgid!r}: its English text breaks the rule of the translations")
    for name in names:
        if name not in fixed:
            chosen = chosen.replace(_STAND_IN.format(name), "{{ " + name + " }}")
    return chosen


def _refused(text: str) -> bool:
    """unsafe(); or a '{', which is what a '{{' of the translation leaves once filled in."""
    return unsafe(text) is not None or "{" in text
