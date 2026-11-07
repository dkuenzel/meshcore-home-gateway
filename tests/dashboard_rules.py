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
from meshcore_home_gateway.core.model import DeliveryState
from meshcore_home_gateway.mqtt.discovery import DOCUMENTATION_URL
from meshcore_home_gateway.views import _TELEMETRY_MODES

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "home-assistant.md"
# the guide of a release: the guide the gateway's device page links to, at the release's tag
GUIDE_URL = DOCUMENTATION_URL.replace("/blob/main/", "/blob/v{version}/")
VERSION = "2.1.0"
NODE = "radio1"
GW = "meshcore_home_gateway_radio1"
ALICE, BOB = "a1" * 32, "b2" * 32
TABS = ("overview", "messages", "contacts")
TAB_LETTERS = 22  # the three titles together, or the tab bar scrolls at 360 px
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
)
# rule 16: a help text that quotes a label of the dashboard, by msgid: help -> label
QUOTES: dict[str, str] = {}
# rule 19: what m() removes: C0 controls but tab, LF and CR; C1 controls; the invisible
# characters the gateway drops before it parses a contact link; never U+200C and U+200D
INVISIBLE = frozenset(
    [
        0x00AD,
        0x061C,
        0x200B,
        0x200E,
        0x200F,
        0xFEFF,
        *range(0x202A, 0x202F),
        *range(0x2060, 0x2065),
        *range(0x2066, 0x206A),
    ]
)
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
    contact target's, a hidden target, an alias pair; Alice's switch and remove button."""

    chats: Sequence[Mapping[str, str]]
    switches: Sequence[Mapping[str, str]]
    removes: Sequence[Mapping[str, str]]
    trackers: Sequence[str]
    controls: Sequence[str]


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
    removes=[{"entity": "button.mesh_alice_remove", "title": "Alice", "key": ALICE}],
    trackers=["device_tracker.mesh_alice"],
    controls=["switch.mesh_alice_telemetry", "button.mesh_alice_remove"],
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
        controls=household.controls,
        chats=household.chats,
        switches=household.switches,
        removes=household.removes,
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
    """Every mapping, list and string, with the nodes above it."""
    yield node, above
    if isinstance(node, dict):
        for value in node.values():
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
        if match := re.search(r"\{%-?\s*macro (\w+)\((\w+)\)\s*-?%\}", line):
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
        "mon": [str(n) for n in range(1, 13)],
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
    contact |= set(household.controls)
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
            needs = ["is not none", "'unavailable'"]
            if entity not in gateway:
                needs += ["via_device_id", "serial_number"]
            if not any(
                entity in g
                and all(n in g for n in needs)
                and (entity not in gateway or _OWN.search(g))
                for g in guards
            ):
                found.append(f"rule 3: {entity} without its guard")
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


# a gateway's own field: its device is the gateway's, not a device below it
_OWN = re.compile(r"(?:device_id\('[^']*'\)|(?<![\w)'])\b\w+) == gw\b")


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
        if re.fullmatch(r"\./\{\{ \w+\.path \}\}", href) and listed in content:
            continue
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
            if card.get("type") == "heading" or _helped(card) or (after and _about(after)):
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
            if name in MAPPINGS:
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
    """Three tabs with their titles alone; the pages without a tab one tap away."""
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
    outside = " ".join(
        line
        for text in texts(by_path.get("overview", {}))
        for line in re.sub(r"<details>.*?</details>", "", text).split("\n")
    )
    for target in ("map", "gateway", "settings", "help"):
        if f'href="./{target}"' not in outside and f"./{target}" not in _navigations(
            by_path.get("overview", {})
        ):
            found.append(f"rule 12: no link to {target} on overview")
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


def _navigations(view: Any) -> list[str]:
    return [
        str(n["navigation_path"])
        for n, _ in nodes(view)
        if isinstance(n, dict) and "navigation_path" in n
    ]


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
    """Quoted labels: a help that quotes a label is in QUOTES, and quotes it as written."""
    return quote_findings(template, QUOTES, [i18n.translator(code) for code in i18n.LANGUAGES])


def quote_findings(
    template: str, quotes: Mapping[str, str], translators: Sequence[i18n.Translator]
) -> list[str]:
    labels = {
        json.loads(f'"{m.group(2)}"')
        for line in template.split("\n")
        if (m := dashboard.VALUE.match(line))
    }
    labels |= {
        json.loads(f'"{m}"')
        for m in re.findall(r'<a href="[^"]*">_\("((?:[^"\\]|\\.)*)"\)</a>', template)
    }
    found = []
    for card in cards(template):
        for help_ in (m for line in card.lines for m in msgids(line)):
            for quoted in re.findall(r'"([^"]+)"|„([^“]+)“|“([^”]+)”', help_):
                label = next(q for q in quoted if q)
                if label in labels and quotes.get(help_) != label:
                    found.append(f"rule 16: {help_!r} quotes {label!r}, not in QUOTES")
    for help_, label in quotes.items():
        for translator in translators:
            said = translator.pgettext(i18n.DASHBOARD, help_)
            named = translator.pgettext(i18n.DASHBOARD, label)
            if named not in said:
                found.append(f"rule 16: {translator.language}: {said!r} does not say {named!r}")
    return found


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
    """The footer: the refused line, the draft's row, the Send tile, in this order."""
    found = []
    for view in loaded["views"]:
        if "footer" not in view:
            continue
        stack = (view["footer"] or {}).get("card") or {}
        parts = stack.get("cards") or []
        kinds = [part.get("type") for part in parts]
        if stack.get("type") != "vertical-stack" or kinds != ["markdown", "entities", "tile"]:
            found.append(f"rule 18: {view['path']}: the footer is {stack.get('type')} of {kinds}")
            continue
        refused, field, tile = parts
        rows = field.get("entities") or []
        actions = ("tap_action", "hold_action", "double_tap_action")
        if refused.get("show_empty") is not False:
            found.append(f"rule 18: {view['path']}: the refused line shows when empty")
        if (
            len(rows) != 1
            or not str(rows[0].get("entity")).startswith("text.")
            or any((rows[0].get(a) or {}).get("action") != "none" for a in actions)
        ):
            found.append(f"rule 18: {view['path']}: the field is not one quiet text row")
        if {"type": "button"} not in [{"type": f.get("type")} for f in tile.get("features") or []]:
            found.append(f"rule 18: {view['path']}: the tile has no button")
    return found


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
]


def fields(msgid: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(msgid) if name is not None]
