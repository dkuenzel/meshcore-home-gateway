"""The numbered rules of the dashboard's template (docs/development.md) that need its structure, for
tests/test_dashboard.py; and a reader of the YAML the template and the generator write.

The reader takes the subset of YAML the template is written in, and refuses anything else: block
mappings and sequences, literal and folded block scalars, JSON strings, single-quoted and plain
scalars, flow sequences of those on one line. Comment lines are skipped outside block scalars.
Home Assistant reads the same files with a full YAML parser (the contract tests, make ha-test).

`findings(template)` renders the template for HOUSEHOLD, a household that has one of every kind
of chat, control and tracker, and reads it; every finding starts with the number of its rule.
"""

from __future__ import annotations

import json
import re
import string
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from meshcore_home_gateway import dashboard, i18n
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.mesh import NODE_TYPES
from meshcore_home_gateway.core.model import DeliveryState
from meshcore_home_gateway.mqtt.discovery import DOCUMENTATION_URL, PROFILE_STATES
from meshcore_home_gateway.protocol.links import _INVISIBLE
from meshcore_home_gateway.views import _TELEMETRY_MODES

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "home-assistant.md"
# the guide of a release: the guide the gateway's device page links to, at the release's tag
GUIDE_URL = DOCUMENTATION_URL.replace("/blob/main/", "/blob/v{version}/")
VERSION = "2.1.1"
NODE = "radio1"
GW = "meshcore_home_gateway_radio1"
# rule 18: a chat page's thread, and its refused line, which alone reads the delivery event
THREAD = "macro row(e)"
REFUSED = f"'event.{GW}_delivery'"
ALICE, BOB = "a1" * 32, "b2" * 32
TABS = ("overview", "messages", "contacts")
# rule 12: the row of links under Overview's status line, to the pages without a tab
LINK_ROW = '<div><a href="./'
ROW_PAGES = ("map", "gateway", "settings", "help")
TAB_LETTERS = 22  # the three titles together, or the tab bar scrolls at 360 px
# the name of the tile beside a chat's Send button: 122 px at 360 px, which 17 letters of
# "Return, then Send" fill to 111 px; a longer name is cut (docs/testing.md, the browser check)
SEND_NAME_LETTERS = 17
# rule 2: the mapping macros, each over a closed list; the gateway's list where one exists
MAPPINGS = (
    "said",
    "mark",
    "why",
    "what",
    "telemetry",
    "status",
    "profile",
    "part",
    "place",
    "problem",
    "mon",
    "kind",
    "audience",
    "node",
)
# rule 8: mappings whose words need no explanation: the names of the months
SELF_EVIDENT = frozenset({"mon"})
# rule 16: a text that quotes or names labels of the dashboard, by msgid: text -> labels. The
# name beside a chat's Send button names the button's label (design §6.2); the chat's help and
# the lines for an administrator name Send too (D-63)
QUOTES: dict[str, tuple[str, ...]] = {
    "Return, then Send": ("Send",),
    # the heading above the remove rows and the question before a remove name its button
    "Remove a contact": ("Remove",),
    "Remove {name} from the radio?": ("Remove",),
    (
        "Type in the field “Write to {name}” at the bottom, tap Return (↵), then tap Send. "
        "Return only keeps the text; Send sends it."
    ): ("Write to {name}", "Send"),
    (
        "Nothing was sent: the gateway had no text. Tap Return (↵) in the field (on a phone this "
        "also closes the keyboard), then Send."
    ): ("Send",),
    # the texts that name a label of NAMED by its words (D-63)
    "Gateway: Health": ("Gateway",),
    (
        "The household's map does not need it: the gateway asks for positions privately (Ask for "
        "position and battery)."
    ): ("Ask for position and battery",),
    "Gateway: Radio settings": ("Gateway",),
    (
        "Nobody shares a position yet. Turn on Ask for position and battery for a person who "
        "agrees (Contacts)."
    ): ("Ask for position and battery", "Contacts"),
    ("GPS off, location not allowed in the app, out of range, or asking is off (Contacts)."): (
        "Contacts",
    ),
    (
        "Positions come from the person's device when the gateway asks it (Ask for position and "
        "battery), or from its adverts."
    ): ("Ask for position and battery",),
    (
        "No one to write to yet. The administrator sets up channels, and makes a chat page for "
        "each contact you add under Contacts."
    ): ("Contacts",),
    (
        "Long keys and links are shown in pieces here. Copied into Contacts, they still work; "
        "pasted elsewhere, they may not."
    ): ("Contacts",),
    "Help explains the words under Asking.": ("Asking",),
    (
        "Your administrator can turn the contacts' controls off for everyone who uses Home "
        "Assistant; Settings shows it."
    ): ("Settings",),
    (
        "Asking for position and battery is off for everyone; Settings shows it. Each switch keeps "
        "whether its person would be asked."
    ): ("Settings",),
    "To write back from here, add that person under Add a contact.": ("Add a contact",),
    "Remove: the contact leaves the radio; to talk again, add it again.": ("Remove",),
    "Show again brings back all hidden contacts.": ("Show again",),
    (
        "Other people's radios that this home's radio heard in the last 7 days. Add one under Add "
        "a contact."
    ): ("Add a contact",),
    "Tap a marker or a dot for its name. Where everyone is, below, says it in words.": (
        "Where everyone is",
    ),
    "Where everyone is, below, lists everyone with a position now.": ("Where everyone is",),
    "Someone missing: see Where everyone is.": ("Where everyone is",),
    ("Ask for position and battery on Contacts turns it on or off for each person."): (
        "Ask for position and battery",
        "Contacts",
    ),
    "The switches and buttons on Contacts.": ("Contacts",),
    (
        "At the bottom of a chat's page: type, tap Return (↵), then tap Send. Return only keeps "
        "the text at the gateway; Send sends it, once."
    ): ("Send",),
    (
        "On Contacts, a switch per person: on, the gateway asks that person's device now and then "
        "for its position and battery. Only for people who agreed."
    ): ("Contacts",),
    "On Contacts: adds the node you pick to the radio at once.": ("Contacts",),
    ("On Contacts: paste a key or a contact link from the MeshCore app, then tap Return (↵)."): (
        "Contacts",
    ),
    "On Contacts: takes a contact off the radio. It asks first.": ("Contacts",),
    ("On Contacts: brings back every contact whose device was deleted in Home Assistant."): (
        "Contacts",
    ),
    "On Gateway: tap one for its history.": ("Gateway",),
    "On Gateway: the radio's airtime over the last 24 hours.": ("Gateway",),
    (
        "The links, and the badges Gateway, Settings and Help, open a page of this dashboard; the "
        "back arrow returns to where you came from."
    ): ("Settings", "Gateway"),
    (
        "This dashboard writes by draft and send: the field keeps your text at the gateway, and "
        "Send sends it, once."
    ): ("Send",),
    ("Someone deleted the contact's device in Home Assistant, or tapped Show again."): (
        "Show again",
    ),
    ("Someone turned Ask for position and battery off or on for this person."): (
        "Ask for position and battery",
    ),
    ("Adding and removing do not work now; the line above the controls on Contacts says why."): (
        "Contacts",
    ),
    ("To write again, add the contact again, or show hidden contacts again (Contacts)."): (
        "Contacts",
    ),
    (
        "Everyone in this home who uses Home Assistant shares this field. What someone else leaves "
        "in it is what Send sends, and their text can replace yours while you type. Read the field "
        "before you tap Send."
    ): ("Send",),
    (
        "If the connection drops, the field can show an older text again. Read it before you tap "
        "Send."
    ): ("Send",),
    (
        "For your administrator: this chat's field or its Send button is disabled, unavailable or "
        "on another device. Check both in Home Assistant's Settings › Devices & services › "
        "Entities."
    ): ("Send",),
    "Change on Contacts": ("Contacts",),
    (
        "Ask for position and battery: on means the gateway asks this device for both now and "
        "then. Change it on Contacts. The words after on or off say how the device answers:"
    ): ("Ask for position and battery", "Contacts"),
}
# rule 16 (D-63): the labels a text may name without quote marks, by their English words: D-63's
# and the controls, headings and column of Contacts and the map. A text that names one, as a
# whole word, is in QUOTES with it, or in PLAIN where the word means no label
NAMED: tuple[str, ...] = (
    "Return, then Send",
    "Send",
    "Contacts",
    "Gateway",
    "Settings",
    "Ask for position and battery",
    "Asking",
    "Add a contact",
    "Add by key or contact link",
    "Remove",
    "Hidden contacts",
    "Show again",
    "Where everyone is",
)
# rule 16: the texts that say a word of NAMED in its plain sense: text -> those words
PLAIN: dict[str, tuple[str, ...]] = {
    "Gateway not found.": ("Gateway",),
    "Gateway offline.": ("Gateway",),
    "Asking for position and battery is paused until the file is fixed or deleted.": ("Asking",),
    (
        "Contacts are told apart by the first bytes of their key. A node with the same first bytes "
        "may be chance or someone imitating that contact. The gateway does not act on commands "
        "from it and does not ask it for its position. If it stays, tell your administrator."
    ): ("Contacts",),
    ("Asking for position and battery is paused: the list of people not to ask cannot be read."): (
        "Asking",
    ),
    "Asking for position and battery is paused: the radio adds contacts by itself.": ("Asking",),
    "Asking for position and battery starts within the hour.": ("Asking",),
    ("Asking for position and battery is paused: the radio sent a lot in the last hour."): (
        "Asking",
    ),
    "Asking for position and battery is paused.": ("Asking",),
    "Remove contacts you no longer need.": ("Remove",),
    (
        "Nobody shares a position yet. Asking for position and battery is off in this home's "
        "settings (your administrator)."
    ): ("Asking",),
    (
        "✗ not delivered and ✗ not sent: waited too long show in yellow, with the reason. Send it "
        "again or ask in another way."
    ): ("Send",),
    "Contacts can be added and removed again once it is back.": ("Contacts",),
    ("The radio is not connected. Contacts can be added and removed again once it is."): (
        "Contacts",
    ),
    "Contacts cannot be added or removed now. Tell your administrator this code:": ("Contacts",),
    (
        "Asking for position and battery is off for everyone; Settings shows it. Each switch keeps "
        "whether its person would be asked."
    ): ("Asking",),
    "In the MeshCore app: Contacts, Add, Scan QR code.": ("Contacts",),
    "Asking for position and battery": ("Asking",),
    "Contacts added only by hand": ("Contacts",),
    (
        "A switch here would work for everyone who uses Home Assistant, children and guests "
        "included. Settings that protect people, the radio or the law stay with the administrator."
    ): ("Settings",),
    "Contacts managed by the gateway": ("Contacts",),
    "Contacts with a device": ("Contacts",),
    "Contacts that may send commands to this home's automations.": ("Contacts",),
    (
        "The radio may send only so much per hour, and many messages were waiting. Send it again "
        "later, or shorter."
    ): ("Send",),
    "The gateway was shutting down or restarting. Send it again once it is back.": ("Send",),
    "Asking for position and battery: the words": ("Asking",),
    "The radio's contact list is full. Remove contacts you no longer need.": ("Remove",),
    "Contacts and Home Assistant": ("Contacts",),
}
# rule 16: the keyboard's key is not a label: each text that asks for it says it before ↵ in
# the word of the name beside the Send button (D-65)
KEY = "(↵)"
KEY_NAME = "Return, then Send"
# rule 19: what m() removes: C0 controls but tab, LF and CR; C1 controls; the invisible
# characters the gateway drops before it parses a contact link (protocol/links.py, its one
# list); never U+200C and U+200D
INVISIBLE = frozenset(_INVISIBLE)
REMOVED = frozenset([*range(0x09), 0x0B, 0x0C, *range(0x0E, 0x20), *range(0x7F, 0xA0)]) | INVISIBLE
# rule 17: Home Assistant's menus, which only a line for the administrator names
MENUS = ("›", "Devices & services", "Developer tools")
ADMINISTRATOR = "For your administrator:"


def items(**fields: str) -> dict[str, str]:
    chat = {
        "path": "",
        "title": "",
        "key": "",
        "channel": "-1",
        "target": "",
        "aliases": "",
        "pair": "",
        "write": "",
        "send": "",
        "room": "160",
    }
    return chat | fields


@dataclass(frozen=True)
class Household:
    """What the generator puts in: a writable channel, a person, a person whose field is a
    contact target's, a hidden target, an alias pair; Alice's switch and remove button, Bob's
    remove button."""

    chats: Sequence[Mapping[str, str]]
    switches: Sequence[Mapping[str, str]]
    removes: Sequence[Mapping[str, str]]
    trackers: Sequence[str]


HOUSEHOLD = Household(
    chats=[
        items(
            path="chat-family",
            title="Family",
            channel="1",
            target="family",
            aliases="alerts,family",
            write=f"text.{GW}_write_family",
            send=f"button.{GW}_write_family_send",
            room="158",
        ),
        items(path="chat-a-b", title="A b", channel="4", target="a-b", aliases="a-b", pair="a_b"),
        items(path="chat-alarm", title="Alarm", channel="2", target="alarm", aliases="alarm"),
        items(
            path="contact-alice",
            title="Alice",
            key=ALICE,
            write="text.mesh_alice_write",
            send="button.mesh_alice_write_send",
        ),
        items(
            path="contact-bob",
            title="Bob",
            key=BOB,
            target="bob",
            aliases="bob",
            write=f"text.{GW}_write_bob",
            send=f"button.{GW}_write_bob_send",
        ),
    ],
    switches=[{"entity": "switch.mesh_alice_telemetry", "title": "Alice", "key": ALICE}],
    removes=[
        {"entity": "button.mesh_alice_remove", "title": "Alice", "key": ALICE},
        {"entity": "button.mesh_bob_remove", "title": "Bob", "key": BOB},
    ],
    trackers=["device_tracker.mesh_alice"],
)


def english(msgid: str, values: Mapping[str, str]) -> str:
    return i18n.ENGLISH.pgettext(i18n.DASHBOARD, msgid, **values)


def rendered(template: str, household: Household = HOUSEHOLD) -> str:
    return dashboard.render(
        template,
        node_id=NODE,
        version=VERSION,
        trackers=household.trackers,
        text=english,
        chats=household.chats,
        switches=household.switches,
        removes=household.removes,
        models=dashboard.models(i18n.ENGLISH),
    )


# --- the reader -------------------------------------------------------------------------------

_KEY = re.compile(r"([A-Za-z_][\w-]*):(?:[ ]+(.*))?$")
_INTEGER = re.compile(r"-?(?:0|[1-9][0-9]*)")


class _Reader:
    def __init__(self, text: str) -> None:
        self.lines = text.split("\n")
        self.at = 0

    def next(self) -> tuple[int, str] | None:
        """The next line that is neither blank nor a comment: its indentation and text."""
        while self.at < len(self.lines):
            line = self.lines[self.at]
            if line.strip() and not line.lstrip().startswith("#"):
                if "\t" in line[: len(line) - len(line.lstrip())]:
                    raise ValueError(f"line {self.at + 1}: a tab in the indentation")
                return len(line) - len(line.lstrip(" ")), line.strip()
            self.at += 1
        return None

    def node(self, indent: int) -> Any:
        found = self.next()
        assert found is not None
        if found[1] == "-" or found[1].startswith("- "):
            return self.sequence(indent)
        return self.mapping(indent)

    def sequence(self, indent: int) -> list[Any]:
        result = []
        while (found := self.next()) and found[0] == indent and found[1].startswith("- "):
            rest = found[1][2:]
            if _KEY.match(rest):  # a mapping, its first key on the dash's line
                self.lines[self.at] = " " * (indent + 2) + rest
                result.append(self.mapping(indent + 2))
            else:
                self.at += 1
                result.append(scalar(rest))
        return result

    def mapping(self, indent: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        while (found := self.next()) and found[0] == indent and not found[1].startswith("- "):
            match = _KEY.match(found[1])
            if match is None:
                raise ValueError(f"line {self.at + 1}: no key: {found[1]!r}")
            key, value = match.group(1), (match.group(2) or "").strip()
            if key in result:
                raise ValueError(f"line {self.at + 1}: {key} twice")
            self.at += 1
            if value in ("|", "|-", ">", ">-"):
                result[key] = self.block(indent, value)
            elif value:
                result[key] = scalar(value)
            else:
                below = self.next()
                deeper = below is not None and (
                    below[0] > indent or (below[0] == indent and below[1].startswith("- "))
                )
                result[key] = self.node(below[0]) if below is not None and deeper else None
        return result

    def block(self, indent: int, style: str) -> str:
        """A literal (|) or folded (>) block scalar, its lines indented more than its key."""
        lines: list[str] = []
        while self.at < len(self.lines):
            line = self.lines[self.at]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                break
            lines.append(line)
            self.at += 1
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            return ""
        base = min(len(line) - len(line.lstrip(" ")) for line in lines if line.strip())
        cut = [line[base:] for line in lines]
        if style.startswith("|"):
            return "\n".join(cut)
        text, previous, empty = cut[0], cut[0], 0
        for line in cut[1:]:  # a break folds to a space, but next to a line indented more
            if not line:
                empty += 1
                continue
            more = line.startswith(" ") or previous.startswith(" ")
            text += "\n" * (empty + more) if empty or more else " "
            text += line
            previous, empty = line, 0
        return text


_PLAIN: dict[str, Any] = {"true": True, "false": False, "null": None, "~": None}


def scalar(text: str) -> Any:
    if text.startswith('"'):
        return json.loads(text)
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise ValueError(f"an open quote: {text!r}")
        return text[1:-1].replace("''", "'")
    if text.startswith("["):
        if not text.endswith("]"):
            raise ValueError(f"a flow sequence over several lines: {text!r}")
        inner = text[1:-1].strip()
        return [scalar(part.strip()) for part in _split(inner)] if inner else []
    if text.startswith(("{", "&", "*", "!", "%", "@", "`", "|", ">")):
        raise ValueError(f"not in the template's YAML: {text!r}")
    plain = re.sub(r"\s+#.*$", "", text)
    if plain in _PLAIN:
        return _PLAIN[plain]
    return int(plain) if _INTEGER.fullmatch(plain) else plain


def _split(text: str) -> list[str]:
    """A flow sequence's items, split at the commas outside quotes."""
    parts, current, quote = [], "", ""
    for char in text:
        if quote:
            current += char
            if char == quote and not current.endswith("\\" + quote):
                quote = ""
        elif char in "\"'":
            quote, current = char, current + char
        elif char == ",":
            parts.append(current)
            current = ""
        else:
            current += char
    return [*parts, current]


def load(text: str) -> Any:
    reader = _Reader(text)
    found = reader.next()
    if found is None:
        return None
    value = reader.node(found[0])
    if (left := reader.next()) is not None:
        raise ValueError(f"line {reader.at + 1}: not read: {left[1]!r}")
    return value


# --- walking the dashboard --------------------------------------------------------------------


def nodes(node: Any, above: tuple[Any, ...] = ()) -> Iterator[tuple[Any, tuple[Any, ...]]]:
    """Every mapping, list and string, with the nodes above it; not a card's condition of its
    own content, a copy of the content (dashboard._shown_by_condition)."""
    yield node, above
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "visibility" and shown_by_content(node):
                value = value[:-1]  # noqa: PLW2901 - the copy left out
            yield from nodes(value, (*above, node))
    elif isinstance(node, list):
        for value in node:
            yield from nodes(value, (*above, node))


def texts(node: Any) -> Iterator[str]:
    yield from (found for found, _ in nodes(node) if isinstance(found, str))


def conditions(node: dict[str, Any]) -> list[dict[str, Any]]:
    """The visibility conditions of a card or section, or the conditions of a conditional."""
    return [c for key in ("visibility", "conditions") for c in node.get(key) or []]


def all_conditions(node: Any) -> Iterator[dict[str, Any]]:
    """Every condition, nested ones (and, or, not) included."""
    for found, above in nodes(node):
        if isinstance(found, dict) and "condition" in found and above:
            yield found


def guide_anchors() -> set[str]:
    """The guide's anchors as GitHub makes them from its headings."""
    anchors = set()
    for line in GUIDE.read_text(encoding="utf-8").split("\n"):
        if match := re.match(r"#+ (.*)", line):
            slug = re.sub(r"[^\w\- ]", "", match.group(1).lower()).replace(" ", "-")
            anchors.add(slug)
    return anchors


def guide_headings() -> list[str]:
    """The headings of the guide's sections (not its title)."""
    lines = GUIDE.read_text(encoding="utf-8").split("\n")
    return [m.group(1) for line in lines if (m := re.match(r"#{2,} (.*)", line))]


# --- the template's source ----------------------------------------------------------------------


@dataclass(frozen=True)
class Card:
    """A card's content in the template, by line: its first line and its lines."""

    first: int
    lines: list[str]


_BLOCK = re.compile(r"^(\s*(?:- )?)content: \|-?$")


def cards(template: str) -> list[Card]:
    found: list[Card] = []
    lines = template.split("\n")
    indent = -1
    for number, line in enumerate(lines, 1):
        depth = len(line) - len(line.lstrip())
        if indent >= 0 and line.strip() and depth > indent:
            found[-1].lines.append(line.strip())
            continue
        indent = -1
        if match := _BLOCK.match(line):
            indent = len(match.group(1))
            found.append(Card(number, []))
    return found


def msgids(text: str) -> list[str]:
    return [json.loads(f'"{m.group(1)}"') for m in dashboard.MARKER.finditer(text)]


def macros(card: Card) -> dict[str, tuple[str, list[str]]]:
    """The card's macros: their parameter and lines."""
    found: dict[str, tuple[str, list[str]]] = {}
    current = ""
    for line in card.lines:
        if match := re.search(r"\{%-?\s*macro (\w+)\((\w+)(?:, \w+=\w+)*\)\s*-?%\}", line):
            current = match.group(1)
            found[current] = (match.group(2), [])
        if current:
            found[current][1].append(line)
            if "endmacro" in line:
                current = ""
    return found


def _view_ranges(template: str) -> dict[str, str]:
    """The template's text of each view, by its path."""
    lines = template.split("\n")
    starts = [i for i, line in enumerate(lines) if line.startswith("  - title: ")]
    ranges = {}
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        chunk = "\n".join(lines[start:end])
        if path := re.search(r"^    path: \"?([^\"\n]+)\"?$", chunk, re.MULTILINE):
            ranges[path.group(1)] = chunk
    return ranges


# --- the rules ----------------------------------------------------------------------------------


def findings(template: str, household: Household = HOUSEHOLD) -> list[str]:
    """What of the numbered rules the template breaks, rule by rule."""
    source = load(template)
    loaded = load(rendered(template, household))
    found: list[str] = []
    for rule in RULES:
        found += rule(template, source, loaded, household)
    return found


Rule = Callable[[str, Any, Any, Household], list[str]]


def rule_2(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Mapping macros over closed lists: each branch a text, a value added later through m(),
    the gateway's values all there where it has a list."""
    lists: dict[str, list[str]] = {
        "said": [state.value for state in DeliveryState],
        "telemetry": list(telemetry.STATES),
        "audience": list(_TELEMETRY_MODES.values()),
        "profile": list(PROFILE_STATES),
        "mon": [str(n) for n in range(1, 13)],
        "node": list(NODE_TYPES.values()),
    }
    found = []
    for card in cards(template):
        for name, (parameter, lines) in macros(card).items():
            if name not in MAPPINGS:
                continue
            branches = [line for line in lines if re.search(rf"\b{parameter} == ", line)]
            given: list[str] = re.findall(
                rf"\b{parameter} == '?([^' ]*)'? ", "\n".join(branches) + " "
            )
            if any('-%}_("' not in line for line in branches):
                found.append(f"rule 2: {card.first}: {name} has a branch without a text")
            fallback = rf"\{{%- else -%\}}(<code>)?\{{\{{ m\({parameter}\) \}}\}}"
            if not re.search(fallback, "\n".join(lines)):
                found.append(f"rule 2: {card.first}: {name} gives no m({parameter}) at the end")
            if name in lists and sorted(given) != sorted(lists[name]):
                found.append(f"rule 2: {card.first}: {name} has {given}, the list {lists[name]}")
    return found


def rule_3(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Guards: no marker and no m() in a condition; every fixed per-contact entity ID only
    inside a card or row whose template condition pins it; has_value() in no condition; a
    footer's guard names both its entities."""
    found = []
    for condition in all_conditions(source):
        written = str(condition.get("value_template", ""))
        if '_("' in written or re.search(r"\bm\(", written):
            found.append(f"rule 3: a condition with a marker or m(): {written[:60]!r}")
    found.extend(
        "rule 3: has_value() in a condition"
        for condition in all_conditions(loaded)
        if "has_value(" in str(condition.get("value_template", ""))
    )
    return found + _guards(loaded, household) + _footer_guards(loaded)


def _guards(loaded: Any, household: Household) -> list[str]:
    """Every entity ID of a contact, or of a target's field, inside the guard that pins it."""
    found = []
    contact = {item["entity"] for item in [*household.switches, *household.removes]}
    gateway: set[str] = set()
    for chat in household.chats:
        (gateway if chat["target"] else contact).update(
            entity for entity in (chat["write"], chat["send"]) if entity
        )
    for text, above in nodes(loaded):
        parent = above[-1] if above else None
        if not isinstance(text, str) or (isinstance(parent, dict) and "condition" in parent):
            continue
        for entity in (e for e in contact | gateway if re.search(rf"\b{re.escape(e)}\b", text)):
            guards = [
                str(c.get("value_template", ""))
                for node in above
                if isinstance(node, dict)
                for c in conditions(node)
                if c.get("condition") == "template"
            ]
            lacking = [guard_lacks(entity, g, own=entity in gateway) for g in guards]
            named = [lack for lack in lacking if lack is not None]
            if not named:
                found.append(f"rule 3: {entity} without its guard")
            elif all(named):
                best = min(named, key=len)
                found.append(f"rule 3: {entity}: its guard lacks {', '.join(best)}")
    return found


# what a guard binds to a name: an entity ID, or the device of an entity
_SET = re.compile(r"\bset (\w+) = ('[^']*'|device_id\((?:\w+|'[^']*')\))")
# two devices the guard says are the same: device_id(b) == d, d == gw
_DEVICE = r"device_id\((?:\w+|'[^']*')\)|\b\w+\b"
_SAME = re.compile(rf"(?<![\w')])({_DEVICE})\s*==\s*({_DEVICE})(?!\s*\()")


def guard_lacks(entity: str, guard: str, *, own: bool) -> list[str] | None:
    """What a guard lacks for one entity (D-23): that it exists, that it is not unavailable, and
    that its device is the gateway's (`own`, a target's field) or a contact's below the gateway,
    by its serial number; the device of a footer's button is the field's (device_id(b) == d).
    None when the guard does not name the entity."""
    if f"'{entity}'" not in guard:
        return None
    bound = dict(_SET.findall(guard))
    names = [f"'{entity}'", *(name for name, value in bound.items() if value == f"'{entity}'")]
    found = []
    if not any(f"states[{name}] is not none" in guard for name in names):
        found.append("states[…] is not none")
    if not any(f"not is_state({name}, 'unavailable')" in guard for name in names):
        found.append("not is_state(…, 'unavailable')")
    devices = {f"device_id({name})" for name in names}
    devices |= {name for name, value in bound.items() if value in devices}
    same = _SAME.findall(guard)
    grown = True
    while grown:  # until no equality adds a device
        more = {b for a, b in same if a in devices} | {a for a, b in same if b in devices}
        grown = not more <= devices
        devices |= more
    pinned = (
        "gw" in devices
        if own
        else any(
            f"device_attr({device}, 'via_device_id') == gw" in guard
            and f"device_attr({device}, 'serial_number') == '" in guard
            for device in devices
        )
    )
    if not pinned:
        found.append("its device")
    return found


def _footer_guards(loaded: Any) -> list[str]:
    found = []
    for view in loaded["views"]:
        if card := (view.get("footer") or {}).get("card"):
            guard = " ".join(str(c.get("value_template")) for c in conditions(card))
            rows = [
                e for e in texts(card.get("cards", [])) if re.fullmatch(r"(text|button)\.\w+", e)
            ]
            if not rows or not all(row in guard for row in rows):
                found.append(f"rule 3: {view['path']}: the footer's guard names not both")
    return found


def rule_4(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No `condition: state` but the attribute form on the radio; no `state_not`."""
    found = []
    for condition in all_conditions(loaded):
        if "state_not" in condition:
            found.append(f"rule 4: state_not on {condition.get('entity')}")
        elif condition.get("condition") == "state" and (
            condition.get("entity") != f"binary_sensor.{GW}_radio" or "attribute" not in condition
        ):
            found.append(f"rule 4: condition: state on {condition.get('entity')}")
    return found


def rule_5(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No entity row for a remove button; a remove asks first, naming the person."""
    found = []
    for node, above in nodes(loaded):
        if isinstance(node, str) and node.startswith("button.") and node.endswith("_remove"):
            parent = above[-1] if above else None
            in_list = (
                isinstance(parent, list) and len(above) > 1 and above[-2].get("entities") is parent
            )
            row = isinstance(parent, dict) and parent.get("entity") == node and "type" not in parent
            if in_list or row:
                found.append(f"rule 5: {node} as an entity row")
    for node, _ in nodes(source):
        if isinstance(node, dict) and node.get("action") == "perform-action":
            target = json.dumps(node.get("target"))
            if "remove" in target and "{name}" not in str(
                (node.get("confirmation") or {}).get("text")
            ):
                found.append(f"rule 5: a remove of {target} that does not ask naming {{name}}")
    return found


def rule_6(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Links only to a page of this dashboard or to the guide's anchor of this version."""
    found = []
    paths = {view["path"] for view in loaded["views"]}
    guide = GUIDE_URL.format(version=VERSION)
    anchors = guide_anchors()
    listed = json.dumps([dashboard._document(chat) for chat in household.chats])
    links: list[tuple[str, str]] = []
    for node, _ in nodes(loaded):
        if isinstance(node, dict):
            links += [(str(node[k]), "") for k in ("navigation_path", "back_path") if k in node]
        elif isinstance(node, str):
            for anchor in re.findall(r"<a\b[^>]*>", node):
                if not (match := re.fullmatch(r'<a href="([^"]*)">', anchor)):
                    found.append(f"rule 6: a link of another form: {anchor[:60]}")
                    continue
                links.append((match.group(1), node))
    for href, content in links:
        if href.startswith("./") and href[2:] in paths:
            continue
        if href.startswith(guide + "#") and href.removeprefix(guide + "#") in anchors:
            continue
        # its path as it is or through m(): an item of the chats list, never text from the mesh
        page = re.fullmatch(r"\./\{\{ (?:m\((\w+)\.path\)|(\w+)\.path) \}\}", href)
        name = (page.group(1) or page.group(2)) if page else None
        if name and listed in content and re.search(rf"\bfor {name} in\b", content):
            continue  # a page of the chats list, in a card that loops over it
        found.append(f"rule 6: a link nowhere in this dashboard: {href}")
    return found


def rule_7(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No marker inside an HTML attribute's value."""
    return [
        f"rule 7: {number}: a marker in an attribute"
        for number, line in enumerate(template.split("\n"), 1)
        if re.search(r"=\s*\"[^\"<>]*_\(\\?\"|=\s*'[^'<>]*_\(\"", line)
    ]


def rule_8(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Help completeness: help at every card, About at the end of every view, the
    mappings' words explained, every view and control in Help."""
    found = []
    for view in loaded["views"]:
        listed = [
            card for section in view.get("sections") or [] for card in section.get("cards") or []
        ]
        for card, after in zip(listed, [*listed[1:], None], strict=True):
            # a Markdown card holds its own help; the card after a tile, entities, map or
            # history-graph card may hold its help (design D-10); the refused line, right above
            # the thread since #143, has the help of the thread under it (rule 18)
            borrowed = card.get("type") != "markdown" and after is not None and _about(after)
            borrowed |= is_refused_line(card) and after is not None and is_thread(after)
            if card.get("type") == "heading" or _helped(card) or borrowed:
                continue
            found.append(f"rule 8: {view['path']}: a {card.get('type')} card without help")
        if not listed or not _about(listed[-1]):
            found.append(f"rule 8: {view['path']}: no About at the end")
    ranges = _view_ranges(template)
    helped = " ".join(msgids(ranges.get("help", "")))
    for card in cards(template):
        folded = " ".join(
            m for line in card.lines if line.startswith("<details>") for m in msgids(line)
        )
        for name, (_, lines) in macros(card).items():
            if name in MAPPINGS and name not in SELF_EVIDENT:
                found += [
                    f"rule 8: {card.first}: {name} says {word!r}, which no help explains"
                    for line in lines
                    for word in msgids(line)
                    if word not in folded and word not in helped
                ]
    views = [view for path, view in ranges.items() if not path.startswith("@")]
    titles = [
        json.loads(f'"{m.group(1)}"')
        for v in views
        if (m := re.match(r'  - title: _\("(.*)"\)', v))
    ]
    names = [
        json.loads(f'"{m.group(2)}"')
        for line in template.split("\n")
        if (m := re.match(r'\s*(?:- )?(name|action_name): _\("(.*)"\)$', line))
    ]
    for label in [*titles, *names]:
        part = max(re.split(r"\{\w+\}", label), key=len).strip()
        if part not in helped:
            found.append(f"rule 8: Help does not list {label!r}")
    return found


def _helped(card: dict[str, Any]) -> bool:
    lines = str(card.get("content", "")).split("\n")
    return card.get("type") == "markdown" and lines[-1].startswith("<details>")


def _about(card: dict[str, Any]) -> bool:
    return card.get("text_only") is True and _helped(card)


def rule_9(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """The status line and the problem cards list the same classes in the same order:
    the branches of the status card's problem(), and the classes the problem cards name on
    their first line, `{#- problem: <class> -#}`."""
    status = [c for c in cards(template) if "problem" in macros(c)]
    if len(status) != 1:
        return [f"rule 9: {len(status)} status cards with problem()"]
    parameter, lines = macros(status[0])["problem"]
    order = re.findall(rf"\b{parameter} == '([^']*)'", "\n".join(lines))
    named = [
        m.group(1)
        for c in cards(template)
        if c.lines and (m := re.fullmatch(r"\{#- problem: (\w+) -#\}", c.lines[0]))
    ]
    listed = [code for code in named if code in order]
    if listed != order:
        return [f"rule 9: the status line has {order}, the problem cards {listed}"]
    return []


def rule_11(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No GitHub alert in any card: coloured boxes are <div><ha-alert> lines."""
    return [
        f"rule 11: {card.first}: a GitHub alert"
        for card in cards(template)
        if any(line.startswith("> [!") for line in card.lines)
    ]


def rule_12(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Three tabs with their titles alone; the pages without a tab one tap away: from the row
    of links under Overview's status line, and from the badges of a heading of Contacts."""
    found = []
    views = loaded["views"]
    tabs = [view for view in views if not view.get("subview")]
    if [view.get("path") for view in tabs] != list(TABS):
        found.append(f"rule 12: the tabs are {[view.get('path') for view in tabs]}")
    found += [
        f"rule 12: {view.get('path')} has an icon"
        for view in tabs
        if "icon" in view or "show_icon_and_title" in view
    ]
    by_path = {view["path"]: view for view in views}
    # the pages without a tab one tap from the top of Overview, the map first: its other links
    # come below everything on a phone, and the map seemed gone (owner feedback on 2.1.0)
    status = [text for text in texts(by_path.get("overview", {})) if "macro problem(" in text]
    rows = [line for text in status for line in text.split("\n") if line.startswith(LINK_ROW)]
    if len(status) != 1 or len(rows) != 1:
        found.append(f"rule 12: {len(rows)} rows of links under overview's status line")
    elif (targets := re.findall(r'<a href="\./([^"]*)">', rows[0])) != list(ROW_PAGES):
        found.append(f"rule 12: the links under overview's status line are {targets}")
    elif depth := _depth(status[0].split(rows[0])[0]):
        found.append(
            "rule 12: the row of links under overview's status line is inside an {% if %}, a"
            f" {{% for %}}, a <details> or a comment (depth {depth})"
        )
    badges = [
        badge.get("tap_action", {}).get("navigation_path")
        for card, _ in nodes(by_path.get("contacts", {}))
        if isinstance(card, dict) and card.get("type") == "heading"
        for badge in card.get("badges") or []
    ]
    found += [
        f"rule 12: no badge to {target} on contacts"
        for target in ("gateway", "settings", "help")
        if f"./{target}" not in badges
    ]
    return found


# rule 12: what opens and what closes a block that can hide what is in it, and a closed comment,
# also over several lines; HTML ends a comment at --> and also at --!>
_OPENS = re.compile(r"\{%-?\s*(?:if|for|macro)\b|<details\b|\{#|<!--")
_CLOSES = re.compile(r"\{%-?\s*end(?:if|for|macro)\b|</details>|#\}|--!?>")
_COMMENT = re.compile(r"\{#.*?#\}|<!--.*?--!?>", re.DOTALL)


def _depth(text: str) -> int:
    """How many blocks are open at the end of a card's text: Jinja's if, for and macro, HTML's
    <details>, and a comment not yet closed. What a closed comment names opens nothing."""
    plain = _COMMENT.sub("", text)
    return len(_OPENS.findall(plain)) - len(_CLOSES.findall(plain))


def rule_13(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """A card that prints a message's text prints it through mt(); m() and mt() read the same in
    every card."""
    found = []
    for name in ("m", "mt"):
        written = {
            line
            for card in cards(template)
            for line in card.lines
            if line.startswith(f"{{%- macro {name}(")
        }
        if len(written) > 1:
            found.append(f"rule 13: {len(written)} forms of {name}()")
    for card in cards(template):
        body = "\n".join(card.lines)
        if not re.search(r"'text'\)|\.get\('text'\)|\['text'\]", body):
            continue
        if "macro mt(" not in body:
            found.append(f"rule 13: {card.first}: a message text without mt()")
        holding = {m.group(1) for m in re.finditer(r"set (\w+) = [^%]*'text'", body)}
        found.extend(
            f"rule 13: {card.first}: a message text through m(), not mt()"
            for printed in re.findall(r"\{\{ m\((.*?)\) \}\}", body)
            if "'text'" in printed or printed in holding
        )
    return found


# rule 22: a value whose key names a unit is a number, which may have a fraction
UNIT_KEY = re.compile(
    r"\bm\((?:state_attr\([^()]*, |\w+\.get\()'(\w+_(?:s|mb|v|db|dbm|mhz|khz|percent|hour))'\)"
)
# rule 22: the fields of the texts that are numbers, which may have a fraction (the template sets
# each through num(), tests/test_dashboard.py checks that this list names them all)
NUMBERS = frozenset(
    {"volts", "slow", "spare", "hour", "mhz", "khz", "dbm", "cr", "budget", "burst", "hops", "snr"}
)
NUM_MACRO = re.compile(r"\{%-?\s*macro num\(")
_PERCENT_FORMAT = re.compile(r"""['"]%[-+ #0]*\d*\.""")
_WHOLE = re.compile(r"\|\s*(?:int|count)(?:\([^()]*\))?$")
_SET = re.compile(r"set (\w+) = (.*?)\s*-?%\}")


def rule_22(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Numbers in the dashboard's language (#145): a card prints a number that may have a
    fraction through num(), which it defines and which reads the same in every card. A card
    that prints a field of NUMBERS sets it through num(), or as a whole number (from | int or
    | count); m(x | string) prints only a whole number; a value whose key names a unit goes
    through num(), not m(); no number is formatted with '%.'. A number printed some other way,
    such as m() of a value Home Assistant gives, is beyond these patterns: the contract test
    test_numbers_follow_the_dashboards_language prints every number in German."""
    found = []
    written = {line for card in cards(template) for line in card.lines if NUM_MACRO.search(line)}
    if len(written) > 1:
        found.append(f"rule 22: {len(written)} forms of num()")
    for card in cards(template):
        body = "\n".join(card.lines)
        if re.search(r"\bnum\(", body.replace("macro num(", "")) and "macro num(" not in body:
            found.append(f"rule 22: {card.first}: num() without its macro")
        if _PERCENT_FORMAT.search(body):
            found.append(f"rule 22: {card.first}: a number formatted with '%.'")
        sets = _SET.findall(body)
        whole = {name for name, value in sets if _WHOLE.search(value)}
        printed = {name for text in msgids(body) for name in fields(text)}
        printed |= set(re.findall(r"\{\{ (\w+) \}\}", body))
        found += [
            f"rule 22: {card.first}: the number {{{name}}} not set through num()"
            for name in sorted(NUMBERS & printed)
            if not (values := [value for named, value in sets if named == name])
            or not all(value.startswith("num(") or _WHOLE.search(value) for value in values)
        ]
        found += [
            f"rule 22: {card.first}: m({name} | string) of a number"
            for name in re.findall(r"\bm\((\w+) \| string\)", body)
            if name not in whole
        ]
        found += [
            f"rule 22: {card.first}: {key} through m(), not num()" for key in UNIT_KEY.findall(body)
        ]
    return found


def rule_15(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Conditions without `enabled`, numeric bounds no strings: the harness models neither."""
    found = []
    for condition in all_conditions(loaded):
        if "enabled" in condition:
            found.append(f"rule 15: a condition with enabled: {condition}")
        if condition.get("condition") == "numeric_state":
            found += [
                f"rule 15: a numeric bound as a string: {condition}"
                for key in ("above", "below")
                if isinstance(condition.get(key), str)
            ]
    return found


def rule_16(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """Quoted labels: a text that quotes a label, and a name (a whole value) that quotes one or
    names a button's label bare, is in QUOTES; each catalog's text says its labels as written,
    and the keyboard's key as the name beside the Send button says it."""
    translators = [i18n.translator(code) for code in i18n.LANGUAGES]
    found = quote_findings(template, QUOTES, translators, PLAIN)
    return found + key_findings(template, translators)


def said(translator: i18n.Translator, msgid: str) -> str:
    """A catalog's text of the dashboard, its fields left as they are: each filled with itself,
    since a text with fields given no values is the English one."""
    fields = {name: f"{{{name}}}" for _, name, _, _ in string.Formatter().parse(msgid) if name}
    return translator.pgettext(i18n.DASHBOARD, msgid, **fields)


def key_findings(template: str, translators: Sequence[i18n.Translator]) -> list[str]:
    """Each text that asks for the keyboard's key says, in every catalog, a word of the name
    beside the Send button just before the sign ↵ (D-65)."""
    asking = [text for text in dict.fromkeys(msgids(template)) if KEY in text]
    found = []
    for translator in translators:
        name = said(translator, KEY_NAME)
        for text in asking:
            asks = said(translator, text)
            keys = re.findall(rf"(\w+) {re.escape(KEY)}", asks)
            found += [
                f"rule 16: {translator.language}: {asks!r} names the key {key!r}, not as {name!r}"
                for key in keys or [""]
                if not key or not re.search(rf"(?<!\w){re.escape(key)}(?!\w)", name)
            ]
    return found


def quote_findings(
    template: str,
    quotes: Mapping[str, Sequence[str]],
    translators: Sequence[i18n.Translator],
    plain: Mapping[str, Sequence[str]] | None = None,
) -> list[str]:
    """Rule 16: a text that quotes a label, a whole value that names a button's label, and a
    text that names a label of NAMED by its words (`plain`: where the words mean no label) is in
    `quotes` with it; each catalog's text says the catalog's label."""
    labels = {
        json.loads(f'"{m.group(2)}"')
        for line in template.split("\n")
        if (m := dashboard.VALUE.match(line))
    }
    labels |= {
        json.loads(f'"{m}"')
        for m in re.findall(r'<a href="[^"]*">_\("((?:[^"\\]|\\.)*)"\)</a>', template)
    }
    values = [
        json.loads(f'"{m.group(2)}"')
        for line in template.split("\n")
        if (m := dashboard.VALUE.match(line))
    ]
    buttons = {
        json.loads(f'"{m.group(1)}"')
        for m in re.finditer(
            r'^\s*(?:- )?action_name: _\("((?:[^"\\]|\\.)*)"\)$', template, re.MULTILINE
        )
    }
    found = []
    plain = plain or {}
    helps = [m for card in cards(template) for line in card.lines for m in msgids(line)]
    texts = dict.fromkeys([*helps, *values])
    bare = [label for label in sorted(NAMED, key=len, reverse=True) if label in texts]
    for help_ in texts:
        cited: dict[str, str] = {}
        for quoted in re.findall(r'"([^"]+)"|„([^“]+)“|“([^”]+)”', help_):
            if (label := next(q for q in quoted if q)) in labels:
                cited[label] = "quotes"
        if help_ in values:  # a name beside a control that names a button's label, bare
            for label in sorted(buttons - {help_}):
                if re.search(rf"(?<!\w){re.escape(label)}(?!\w)", help_):
                    cited.setdefault(label, "names")
        rest = _menus(help_)  # a label's words, the longest first: Send in Return, then Send
        for label in (label for label in bare if label != help_):
            word = re.compile(rf"(?<![\w']){re.escape(label)}(?![\w'])")
            if word.search(rest) and label not in plain.get(help_, ()):
                cited.setdefault(label, "names")
            rest = word.sub("\0", rest)
        found += [
            f"rule 16: {help_!r} {verb} {label!r}, not in QUOTES"
            for label, verb in cited.items()
            if label not in quotes.get(help_, ())
        ]
    found += [
        f"rule 16: PLAIN has {help_!r} with {label!r}, which it does not say"
        for help_, words in plain.items()
        for label in words
        if help_ not in texts
        or not re.search(rf"(?<![\w']){re.escape(label)}(?![\w'])", _menus(help_))
    ]
    for help_, listed in quotes.items():
        for translator in translators:
            text = said(translator, help_)
            for label in listed:
                named = said(translator, label)
                if not says(text, named):
                    found.append(f"rule 16: {translator.language}: {text!r} does not say {named!r}")
    return found


def says(text: str, label: str) -> bool:
    """Rule 16: whether `text` says `label`, its first letter in either case, as a language's
    grammar may want: a German heading ends in its button's verb, "Kontakt entfernen" for
    "Entfernen"."""
    rest = label[1:]
    return any(first + rest in text for first in {label[:1], label[:1].lower(), label[:1].upper()})


def _menus(text: str) -> str:
    """A text without the words of Home Assistant's menus (Settings › …), which name no label
    of the dashboard (rule 17)."""
    return re.sub(r"\w+(?= ›)", "\0", text)


def rule_17(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No text names a guide's section (a link to its anchor does); only a line for the
    administrator names Home Assistant's menus."""
    found = []
    headings = guide_headings()
    for text in msgids(template):
        named = [
            h
            for h in headings
            if (" " in h and h in text)
            or ("guide" in text and re.search(rf"\b{re.escape(h)}\b", text))
        ]
        if named:
            found.append(f"rule 17: {text[:50]!r} names the guide's {named}")
        if any(menu in text for menu in MENUS) and not text.startswith(ADMINISTRATOR):
            found.append(f"rule 17: {text[:50]!r} names Home Assistant's menus")
    return found


def rule_18(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """A chat page's footer: the draft's row and the Send tile, in this order. The refused line
    is the card right above the thread, not in the footer: the frontend caps the footer at a
    quarter of the screen, and a refused line on top of the field pushed the Send tile below a
    phone's screen (H-27); under the thread, the footer, which sticks to the bottom of the screen,
    covered the line until the page was scrolled (#143, docs/platform-notes.md)."""
    found = []
    for view in loaded["views"]:
        if "footer" not in view:
            continue
        stack = (view["footer"] or {}).get("card") or {}
        parts = stack.get("cards") or []
        kinds = [part.get("type") for part in parts]
        if stack.get("type") != "vertical-stack" or kinds != ["entities", "tile"]:
            found.append(f"rule 18: {view['path']}: the footer is {stack.get('type')} of {kinds}")
        else:
            field, tile = parts
            rows = field.get("entities") or []
            actions = ("tap_action", "hold_action", "double_tap_action")
            if (
                len(rows) != 1
                or not str(rows[0].get("entity")).startswith("text.")
                or any((rows[0].get(a) or {}).get("action") != "none" for a in actions)
            ):
                found.append(f"rule 18: {view['path']}: the field is not one quiet text row")
            features = tile.get("features") or []
            if {"type": "button"} not in [{"type": f.get("type")} for f in features]:
                found.append(f"rule 18: {view['path']}: the tile has no button")
        listed = [
            card for section in view.get("sections") or [] for card in section.get("cards") or []
        ]
        threads = [n for n, card in enumerate(listed) if is_thread(card)]
        above = listed[threads[0] - 1] if len(threads) == 1 and threads[0] > 0 else {}
        if not is_refused_line(above):
            found.append(f"rule 18: {view['path']}: the refused line is not above the thread")
        elif not shown_by_content(above):
            found.append(f"rule 18: {view['path']}: the refused line shows when empty")
    return found


def is_thread(card: Mapping[str, Any]) -> bool:
    """A chat page's thread: the Markdown card of its rows."""
    return card.get("type") == "markdown" and THREAD in str(card.get("content"))


def is_refused_line(card: Mapping[str, Any]) -> bool:
    """A chat page's refused line: the one Markdown card that reads the delivery event (§3.4)."""
    return card.get("type") == "markdown" and REFUSED in str(card.get("content"))


def rule_19(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """m() removes the C0 and C1 controls and the gateway's invisible characters, not the
    joiners."""
    found = []
    for line in {
        line for card in cards(template) for line in card.lines if line.startswith("{%- macro m(")
    }:
        match = re.search(r"regex_replace\('\[(.*?)\]', ''\)", line)
        if match is None:
            found.append("rule 19: m() removes no characters")
            continue
        removed = removed_by(match.group(1))
        if removed != REMOVED:
            more = sorted(f"U+{c:04X}" for c in removed - REMOVED)
            less = sorted(f"U+{c:04X}" for c in REMOVED - removed)
            found.append(f"rule 19: m() removes also {more} and not {less}")
    return found


def removed_by(written: str) -> set[int]:
    """The code points of a character class written with \\u escapes and ranges."""
    points = [int(m, 16) for m in re.findall(r"\\u([0-9a-fA-F]{4})", written)]
    shape = re.sub(r"\\u[0-9a-fA-F]{4}", "u", written)
    if set(shape) - {"u", "-"}:
        raise ValueError(f"not a class of \\u escapes: {written!r}")
    found: set[int] = set()
    at = 0
    for part in re.findall(r"u-u|u", shape):
        if part == "u":
            found.add(points[at])
            at += 1
        else:
            found.update(range(points[at], points[at + 1] + 1))
            at += 2
    return found


def rule_20(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No view has a back_path: the back arrow goes back in the history."""
    return [
        f"rule 20: {view['path']} has a back_path"
        for view in loaded["views"]
        if "back_path" in view
    ]


def _cards_of(found: Any) -> Iterator[dict[str, Any]]:
    """Every card of the made dashboard, nested ones and the footers' included."""
    if isinstance(found, dict):
        if "type" in found and ("content" in found or "cards" in found or "entities" in found):
            yield found
        for value in found.values():
            yield from _cards_of(value)
    elif isinstance(found, list):
        for value in found:
            yield from _cards_of(value)


def shown_by_content(card: Mapping[str, Any]) -> bool:
    """The card shows by a condition of its own content, as the generator makes a card that may
    be empty (dashboard._shown_by_condition): it hides while its content renders nothing."""
    conditions = card.get("visibility") or []
    body = "{%- set body -%}\n" + str(card.get("content")) + "\n{%- endset -%}\n"
    return bool(conditions) and conditions[-1] == {
        "condition": "template",
        "value_template": body + "{{ body | trim != '' }}",
    }


def rule_21(template: str, source: Any, loaded: Any, household: Household) -> list[str]:
    """No card of the made dashboard hides itself: Home Assistant's card wrapper would take it
    out of the page for good (dashboard._shown_by_condition, the stage-5 browser check)."""
    return [
        f"rule 21: a card hides itself: {str(card.get('content', ''))[:60]!r}"
        for card in _cards_of(loaded)
        if "show_empty" in card
    ]


def tab_letters(template: str, translator: i18n.Translator) -> int:
    """Rule 14: the letters of the three tabs' titles in a language."""
    source = load(template)
    titles = [
        re.fullmatch(r'_\("(.*)"\)', view["title"]).group(1)  # type: ignore[union-attr]
        for view in source["views"]
        if not view.get("subview")
    ]
    return sum(len(translator.pgettext(i18n.DASHBOARD, json.loads(f'"{t}"'))) for t in titles)


RULES: list[Rule] = [
    rule_2,
    rule_3,
    rule_4,
    rule_5,
    rule_6,
    rule_7,
    rule_8,
    rule_9,
    rule_11,
    rule_12,
    rule_13,
    rule_15,
    rule_16,
    rule_17,
    rule_18,
    rule_19,
    rule_20,
    rule_21,
    rule_22,
]


def fields(msgid: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(msgid) if name is not None]
