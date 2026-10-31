"""The dashboard generator (dashboard.py): the template of the package filled in for one gateway,
its texts in [homeassistant] language and guarded, the map's trackers from the service's state;
and the template's own rules, checked here on the shipped template. How Home Assistant loads the
dashboard and renders its cards is for the contract tests (make ha-test)."""

from __future__ import annotations

import json
import re
import string
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from meshcore_home_gateway import __version__, dashboard, i18n
from meshcore_home_gateway.catalog import Entry
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.model import DeliveryState, Position
from meshcore_home_gateway.dashboard import TemplateError
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from tests.i18n_helpers import compiled, pseudo, speaking
from tests.test_contact_devices import ALICE, BOB, CAROL, HILL, Clock, devices, mesh_of, node, topic

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "src" / "meshcore_home_gateway" / "dashboard.yaml"
DAVE = "e5" * 32
HERE = Position(52.1, 9.2)
# what every Markdown card that prints a value from Home Assistant defines first
MACRO = (
    r"{%- macro m(x) -%}{{ (x or '') | string | replace('\r', ' ') | replace('\n', ' ') | e }}"
    r"{%- endmacro -%}"
)
Speaker = Callable[[str, Mapping[str, str]], str]


def shipped() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def speaker(translator: i18n.Translator) -> Speaker:
    """The `text` of dashboard.render, as dashboard.dashboard makes it."""
    return lambda msgid, values: translator.pgettext(i18n.DASHBOARD, msgid, **values)


ENGLISH = speaker(i18n.ENGLISH)


def translated(texts: Mapping[str, str]) -> Speaker:
    entries = [Entry(msgid, (msgstr,), i18n.DASHBOARD) for msgid, msgstr in texts.items()]
    return speaker(compiled("xx", entries))


def config(node_id: str = "radio1", more: str = "") -> Config:
    return parse_config(
        '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\n'
        f'[mqtt]\nhost = "broker.example.org"\nnode_id = "{node_id}"\n{more}'
    )


def render(template: str, text: Speaker = ENGLISH, trackers: list[str] | None = None) -> str:
    return dashboard.render(
        template, node_id="radio-1", version="1.2.3", trackers=trackers or [], text=text
    )


# --- filling in the gateway -----------------------------------------------------------------------


@pytest.mark.parametrize(("node_id", "gateway"), [("radio1", "radio1"), ("radio-1", "radio_1")])
def test_the_dashboard_names_the_gateways_entities(
    node_id: str, gateway: str, tmp_path: Path
) -> None:
    """Home Assistant turns '-' into '_' in entity IDs only; the texts keep the node ID."""
    text, count = dashboard.dashboard(config(node_id), tmp_path)
    assert count == 0
    lines = text.split("\n")
    assert lines[0].startswith(
        f"# meshcore-home-gateway dashboard for node {node_id}, made by version {__version__}"
    )
    assert f"entity: binary_sensor.meshcore_home_gateway_{gateway}_radio\n" in text
    assert f"'sensor.meshcore_home_gateway_{gateway}_queue'" in text
    assert f'state_not: ["{__version__}", unknown]' in text
    assert f"meshcore_home_gateway_{node_id}_" not in text or node_id == gateway
    assert f"Home Assistant has no entity of node {node_id}: " in text
    assert not re.search(r"@\w+@", text)
    assert '_("' not in text


def test_a_whole_value_becomes_one_quoted_scalar() -> None:
    """A text that is a YAML value is written as a JSON string, which YAML reads as it is, also
    with quotes, backslashes and ': #' in it."""
    template = 'title: _("Mesh")\nviews:\n  - title: _("Overview")\n'
    wild = 'Le "réseau": #1 \\ fin'
    out = render(template, translated({"Mesh": wild, "Overview": "‹Overview›"}))
    first, second, third = out.split("\n")[:3]
    assert first == 'title: "Le \\"réseau\\": #1 \\\\ fin"'
    assert json.loads(first.removeprefix("title: ")) == wild
    assert second == "views:"
    assert third == '  - title: "‹Overview›"'


@pytest.mark.real_catalogs
def test_a_whole_value_takes_no_field() -> None:
    with pytest.raises(TemplateError, match="no field"):
        render('title: _("Mesh of {node}")\n')


@pytest.mark.real_catalogs
def test_a_text_in_a_card_becomes_the_text_its_fields_jinja_variables() -> None:
    """{node} and {version} are filled in; every other field is a variable the card sets."""
    template = 'content: |-\n  {%- set n = 3 -%}_("{n} min ago, node {node}, version {version}")\n'
    assert render(template) == (
        "content: |-\n  {%- set n = 3 -%}{{ n }} min ago, node radio-1, version 1.2.3\n"
    )
    german = translated({"{n} min ago, node {node}, version {version}": "vor {n} Min. ({node})"})
    assert render(template, german).endswith("{%- set n = 3 -%}vor {{ n }} Min. (radio-1)\n")


UNSAFE = [
    "{{", "{%", "{#", "<", ">", "@", "](", "://", "www.", '_("', "\r", "\n", "\u2028", "\u2029",
    "\u200e", "\x7f",
]  # fmt: skip


@pytest.mark.parametrize("bad", UNSAFE, ids=repr)
def test_a_translation_a_card_would_read_as_code_gives_the_english_text(bad: str) -> None:
    """Jinja, HTML, Markdown links and web or mail addresses, a marker, a line break or another
    control or format character: the English text instead. U+2028 and U+2029 would end the
    YAML block, CR and LF the card's HTML block."""
    template = 'title: _("Mesh")\ncontent: |-\n  {%- set n = 3 -%}_("{n} min ago")\n'
    texts = {"Mesh": f"Netz {bad} x", "{n} min ago": f"vor {{n}} {bad} Min."}
    assert render(template, translated(texts)) == (
        'title: "Mesh"\ncontent: |-\n  {%- set n = 3 -%}{{ n }} min ago\n'
    )


def test_a_translation_with_a_field_of_its_own_gives_the_english_text() -> None:
    template = 'content: |-\n  {%- set n = 3 -%}_("{n} min ago")\n'
    for wrong in ("vor {m} Min.", "vor {n.real} Min.", "vor {0} Min.", "vor {n Min."):
        assert render(template, translated({"{n} min ago": wrong})).endswith("{{ n }} min ago\n")
    assert render(template, translated({"{n} min ago": "vor {n} Min."})).endswith(
        "vor {{ n }} Min.\n"
    )


def test_the_pseudo_language_passes_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every text of the shipped template goes through the catalog, fields kept."""
    speaking(monkeypatch, pseudo())
    text, _ = dashboard.dashboard(config(more='[homeassistant]\nlanguage = "xx"\n'), tmp_path)
    assert '\ntitle: "‹Mesh›"\n' in text
    assert "‹{{ n }} min ago›" in text
    assert "‹Home Assistant has no entity of node radio1: " in text
    english, _ = dashboard.dashboard(config(), tmp_path)
    assert "‹" not in english
    assert re.sub(r"[‹›]", "", text) == english


@pytest.mark.real_catalogs
def test_no_translation_makes_a_marker_or_a_placeholder() -> None:
    """The left-overs are looked for in the template, before any text goes in: a translation,
    even one the guard lets through, cannot make the command fail."""
    template = 'content: |-\n  @_("gw")@ _("Mesh")("x") _("Overview")\n'
    texts = {"gw": "gw", "Mesh": "_", "Overview": '@gw@ _("x")'}
    out = render(template, translated(texts))
    assert out == 'content: |-\n  @gw@ _("x") Overview\n'


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    "template",
    [
        'title: _("Mesh"\n',
        'content: |-\n  _("open\n',
        "title: @gx@\n",
    ],
)
def test_a_left_over_marker_or_placeholder_is_refused(template: str) -> None:
    with pytest.raises(TemplateError, match="left over"):
        render(template)


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("marker", "error"),
    [
        ('_("a \\q")', "no JSON string"),
        ('_("{")', "no format string"),
        ('_("{a.b}")', "a field is a plain name"),
        ('_("{0}")', "a field is a plain name"),
    ],
)
def test_a_marker_is_a_json_string_with_named_fields(marker: str, error: str) -> None:
    with pytest.raises(TemplateError, match=error):
        render(f"content: |-\n  {marker}\n")


@pytest.mark.real_catalogs
def test_an_english_text_the_guard_refuses_is_an_error_of_the_template() -> None:
    with pytest.raises(TemplateError, match="its English text"):
        render('content: |-\n  _("see <b>this</b>")\n')


# --- the map's trackers -------------------------------------------------------------------------


def test_the_map_lists_the_contacts_announced_with_a_position(tmp_path: Path) -> None:
    """From the service's state: a contact deleted in Home Assistant (hidden) or no longer on the
    radio has no tracker there."""
    contacts, _ = devices(tmp_path, clock=Clock())
    nodes = [
        node(HILL, "Zed hill", NodeType.REPEATER, position=HERE),
        node(ALICE, "Alice", position=HERE), node(BOB, "Bob"),
        node(CAROL, "Carol", position=HERE), node(DAVE, "Dave", position=HERE),
    ]  # fmt: skip
    mesh = mesh_of(*nodes)
    contacts.update(mesh)
    contacts.discovery_message(topic(CAROL), b"", retained=False)  # the user deleted it
    contacts.update(mesh_of(*nodes[:4], mesh=mesh))  # Dave left the radio
    records = ContactDeviceRecords(JsonStateFile(tmp_path / "state.json", read_only=True))
    assert set(records.announced) == {HILL, ALICE, BOB}
    assert dashboard.trackers(records) == [
        "device_tracker.mesh_alice",
        "device_tracker.mesh_zed_hill",
    ]
    text, count = dashboard.dashboard(config(), tmp_path)
    assert count == 2
    assert 'entities: ["device_tracker.mesh_alice", "device_tracker.mesh_zed_hill"]' in text


def test_the_trackers_are_sorted_and_only_valid_entity_ids(tmp_path: Path) -> None:
    def record(slug: str, **components: str) -> dict[str, object]:
        return {"slug": slug, "cmps": components}

    announced = {
        "k1": record("zed", position="device_tracker"),
        "k2": record("alice", position="device_tracker", last_heard="sensor"),
        "k3": record("bob", last_heard="sensor"),
        "k4": record('x"]\n- y', position="device_tracker"),  # never a slug the gateway gives
        "k5": record("Upper", position="device_tracker"),
        "k6": record("carol", position="sensor"),
    }
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ha_contact_devices": {"announced": announced}}), encoding="utf-8")
    records = ContactDeviceRecords(JsonStateFile(state, read_only=True))
    assert dashboard.trackers(records) == ["device_tracker.mesh_alice", "device_tracker.mesh_zed"]


@pytest.mark.parametrize("state", ["missing", "a directory", "not json"])
def test_without_a_readable_state_the_map_is_the_note(state: str, tmp_path: Path) -> None:
    if state == "a directory":
        (tmp_path / "state.json").mkdir()
    elif state == "not json":
        (tmp_path / "state.json").write_text("{", encoding="utf-8")
    text, count = dashboard.dashboard(config(), tmp_path)
    assert count == 0
    assert "No contact had a position when this dashboard was made." in text
    assert "type: map" not in text
    assert "{%- set listed = [] -%}" in text
    assert not (tmp_path / "state.json.corrupt").exists()  # read only


def test_with_trackers_the_map_without_them_the_note() -> None:
    listed = ["device_tracker.mesh_zed", "device_tracker.mesh_alice"]
    with_map = render(shipped(), trackers=listed)
    assert with_map.count("type: map") == 1
    assert 'entities: ["device_tracker.mesh_alice", "device_tracker.mesh_zed"]' in with_map
    assert '{%- set listed = ["device_tracker.mesh_alice", "device_tracker.mesh_zed"] -%}' in (
        with_map
    )
    assert "No contact had a position" not in with_map
    without = render(shipped())
    assert "type: map" not in without
    assert "No contact had a position when this dashboard was made." in without
    assert "# @" not in with_map + without


def test_the_translators_notes_stay_in_the_template() -> None:
    """A note above a whole value is a YAML comment: not for the household's file."""
    template = '# Translators: the title\n# of the dashboard\ntitle: _("Mesh")\n# a comment\n'
    assert render(template) == 'title: "Mesh"\n# a comment\n'
    text = render(shipped())
    assert "Translators" not in text.replace("{#- Translators: ", "")
    assert text.startswith("# meshcore-home-gateway dashboard for node radio-1, made by version ")


@pytest.mark.real_catalogs
def test_the_conditional_lines() -> None:
    template = "a\n# @if trackers\nb\n# @else\nc\n# @end\nd\n"
    assert render(template, trackers=["device_tracker.mesh_a"]) == "a\nb\nd\n"
    assert render(template) == "a\nc\nd\n"
    nested = "# @if trackers\n  # @if trackers\nb\n  # @else\nc\n  # @end\n# @else\nd\n# @end\n"
    assert render(nested, trackers=["device_tracker.mesh_a"]) == "b\n"
    assert render(nested) == "d\n"


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("template", "error"),
    [
        ("# @if contacts\na\n# @end\n", "unknown flag"),
        ("# @if trackers\na\n", "not closed"),
        ("a\n# @end\n", "no @if"),
        ("a\n# @else\n", "no @if"),
        ("# @if trackers\n# @else\n# @else\n# @end\n", "second @else"),
        ("# @iff trackers\n", "unknown line"),
    ],
)
def test_a_broken_conditional_is_refused(template: str, error: str) -> None:
    with pytest.raises(TemplateError, match=error):
        render(template)


def test_the_same_inputs_give_the_same_bytes() -> None:
    """No time stamp; LF and a final newline; the trackers in any order."""
    listed = ["device_tracker.mesh_b", "device_tracker.mesh_a", "device_tracker.mesh_c"]
    first = render(shipped(), trackers=listed)
    assert first == render(shipped(), trackers=list(reversed(listed)))
    assert first == render(shipped(), trackers=sorted(listed))
    assert "\r" not in first
    assert first.endswith("{%- endif %}\n")
    assert not first.endswith("\n\n")


# --- the shipped template's rules ---------------------------------------------------------------

# a block scalar: `key: |-` (or `|`); its lines are those indented more than its key
_BLOCK = re.compile(r"^(\s*(?:- )?)[\w-]+: \|-?$")
_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}")
_PRINT = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_SET = re.compile(r"\{%-?\s*set (\w+) = (.*?)\s*-?%\}")
_MACRO_START = re.compile(r"\{%-?\s*macro (\w+)\((\w+)\)\s*-?%\}")
_ESCAPE = re.compile(r"\|\s*(?:e|escape|forceescape)\b")
_INTEGER = re.compile(
    r".*\|\s*(?:int|count)(?:\(\s*-?\d*\s*\))?|\[?\s*(\w+)\s*//\s*\d+(?:\s*,\s*\d+\s*\]\s*\|\s*max)?"
)
_YAML_11 = re.compile(r"(?i)y|n|yes|no|on|off|\d+(?:\.\d+)+\w*")
_PLACEHOLDERS = {"@gw@", "@node@", "@version@", "@trackers@"}
_JINJA_NOTE = re.compile(r"\{#-?\s*Translators:.*-?#\}")
_OPENS = {"<div>": "</div>", "<table>": "</table>", "<ha-qr-code": "</ha-qr-code>"}


@dataclass
class Block:
    """A literal block scalar (a card's content): its lines, by number, without indentation."""

    lines: list[tuple[int, str]] = field(default_factory=list)


def blocks(lines: list[str]) -> list[Block]:
    found: list[Block] = []
    indent = -1
    for number, line in enumerate(lines, 1):
        depth = len(line) - len(line.lstrip())
        if indent >= 0 and line.strip() and depth > indent:
            found[-1].lines.append((number, line.strip()))
            continue
        indent = -1
        if match := _BLOCK.match(line):
            indent = len(match.group(1))
            found.append(Block())
    return found


def _fields(msgid: str) -> list[str]:
    return [name for _, name, _, _ in string.Formatter().parse(msgid) if name is not None]


def _msgid(match: re.Match[str]) -> str:
    return str(json.loads(f'"{match.group(1)}"'))


def _mappings() -> dict[str, list[str]]:
    """The gateway's value lists that the mapping macros give texts for."""
    gateway = config()
    contact = discovery_payload(gateway, Topics(gateway.mqtt.base_topic, "radio1"))["cmps"][
        "contact"
    ]
    return {
        "said": [state.value for state in DeliveryState],
        "telemetry": list(telemetry.STATES),
        "what": list(contact["event_types"]),
    }


def rule_findings(template: str) -> list[str]:  # noqa: PLR0912 - one rule after the other
    """What of the template's rules it breaks: markers, placeholders, translator notes, quoting;
    and per card, the values it prints and its mappings (_card_findings)."""
    lines = template.split("\n")
    found = [f"{n}: a blank line" for n, line in enumerate(lines[:-1], 1) if not line.strip()]
    cards = blocks(lines)
    inside = {number for block in cards for number, _ in block.lines}
    # rules 1, 5, 6 and 4: markers, placeholders, translator comments, quoting
    for number, line in enumerate(lines, 1):
        markers = list(dashboard.MARKER.finditer(line))
        if '_("' in dashboard.MARKER.sub("", line):
            found.append(f'{number}: _(" outside a marker')
        found += [
            f"{number}: the placeholder {placeholder}"
            for placeholder in re.findall(r"@\w+@", line)
            if placeholder not in _PLACEHOLDERS
        ]
        if "@trackers@" in line and '["@trackers@"]' not in line:
            found.append(f"{number}: @trackers@ is a whole list")
        tags = [match.span() for match in _TAG.finditer(line)]
        for marker in markers:
            if any(start <= marker.start() < end for start, end in tags):
                found.append(f"{number}: a marker inside a Jinja tag")
            if number not in inside and not dashboard.VALUE.match(line):
                found.append(f"{number}: a marker neither a whole value nor in a card's content")
            if number not in inside and _fields(_msgid(marker)):
                found.append(f"{number}: a field in a whole value")
        stripped = line.strip()
        below = lines[number] if number < len(lines) else ""
        after = next((later for later in lines[number:] if not later.lstrip().startswith("#")), "")
        if stripped.startswith("# Translators:") and (
            number in inside or not dashboard.VALUE.match(after)
        ):
            found.append(f"{number}: a '# Translators:' line not just above a whole value")
        if _JINJA_NOTE.search(stripped) and (
            number not in inside or not dashboard.MARKER.search(below)
        ):
            found.append(f"{number}: a Jinja translator note not just above a text in a card")
        if _ESCAPE.search(line) and stripped != MACRO:
            found.append(f"{number}: | e outside the macro m")
        if number not in inside and not stripped.startswith("#"):
            value = re.sub(r"^\s*(?:- )?(?:[\w-]+:(?:\s|$))?", "", line)
            tokens = (token.strip() for token in re.split(r"[\[\],]", value))
            found += [
                f"{number}: {token} unquoted"
                for token in tokens
                if _YAML_11.fullmatch(token) or token == "@version@"
            ]
    # rules 2, 3 and 7: per card
    for block in cards:
        found += _card_findings(block)
    if "set node " in template or "set version " in template:
        found.append("the template sets node or version")
    return found


def _card_findings(block: Block) -> list[str]:  # noqa: PLR0912
    """A card prints a value read from Home Assistant only through m() or a mapping, on a line
    that is one whole HTML element, and starts with the macro m; each mapping has a branch per
    value of the gateway's list and m() for a value added later."""
    found: list[str] = []
    texts = [text for _, text in block.lines]
    macros: dict[str, tuple[str, list[str]]] = {}  # name: its parameter and its lines
    body: set[int] = set()
    current = ""
    for number, text in block.lines:
        if text == MACRO:
            continue
        if match := _MACRO_START.search(text):
            current = match.group(1)
            macros[current] = (match.group(2), [])
        if current:
            macros[current][1].append(text)
            body.add(number)
            if "endmacro" in text:
                current = ""
    sets: dict[str, list[str]] = {}
    integers: set[str] = set()
    values: set[str] = set()
    for _, text in block.lines:
        for match in _SET.finditer(text):
            name, source = match.groups()
            sets.setdefault(name, []).append(source)
            if source.startswith("m(") and source.endswith(")"):
                values.add(name)
            elif (whole := _INTEGER.fullmatch(source)) and (
                whole.group(1) is None or whole.group(1) in integers
            ):
                integers.add(name)
    prints = any(_PRINT.search(text) for text in texts if text != MACRO)
    if prints or values:
        if not texts or texts[0] != MACRO:
            found.append(f"{block.lines[0][0]}: a card that prints a value starts with the macro m")
        if texts.count(MACRO) != 1:
            found.append(f"{block.lines[0][0]}: the macro m {texts.count(MACRO)} times")
    for number, text in block.lines:
        if text == MACRO:
            continue
        printed = [match.group(1) for match in _PRINT.finditer(text)]
        for expression in printed:
            call = re.fullmatch(r"(\w+)\(.*\)", expression)
            if call is None or (call.group(1) != "m" and call.group(1) not in macros):
                found.append(f"{number}: {{{{ {expression} }}}} is neither m() nor a mapping")
            elif number in body and call.group(1) != "m":
                found.append(f"{number}: a mapping prints {expression}")
        fields = [f for marker in dashboard.MARKER.finditer(text) for f in _fields(_msgid(marker))]
        for name in fields:
            if name in dashboard.FIXED:
                continue
            if name not in sets:
                found.append(f"{number}: {{{name}}} is set nowhere in its card")
            elif name not in values | integers:
                found.append(f"{number}: {{{name}}} is set from neither m() nor integers")
        if number in body:
            continue
        if text.startswith(">") and (printed or set(fields) & values):
            found.append(f"{number}: an alert line prints a value")
        elif printed or set(fields) & values:
            opening = next((tag for tag in _OPENS if text.startswith(tag)), None)
            if opening is None or not text.endswith(_OPENS[opening]):
                found.append(f"{number}: a value outside one whole <div>, <table> or <ha-qr-code>")
    expected = _mappings()
    for name, (parameter, lines) in macros.items():
        if name not in expected:
            continue
        given = re.findall(rf"\b{parameter} == '([^']*)'", "\n".join(lines))
        if sorted(given) != sorted(expected[name]) or len(set(given)) != len(given):
            found.append(f"{name}: the branches {given}, the gateway's values {expected[name]}")
        branches = [line for line in lines if f"{parameter} == '" in line]
        if any('-%}_("' not in line for line in branches):
            found.append(f"{name}: a branch without a text")
        if not any(line.endswith(f"{{%- else -%}}{{{{ m({parameter}) }}}}") for line in lines):
            found.append(f"{name}: no m({parameter}) for a value added later")
    return found


def test_the_shipped_template_keeps_its_rules() -> None:
    template = shipped()
    assert rule_findings(template) == []
    names = {name for block in blocks(template.split("\n")) for _, t in block.lines for name in
             re.findall(r"macro (\w+)\(", t)}  # fmt: skip
    assert {"m", "ago", "said", "telemetry", "what"} <= names
    assert template.count(MACRO) == 8
    assert len(list(dashboard.MARKER.finditer(template))) > 70


@pytest.mark.parametrize(
    ("old", "new", "finding"),
    [
        (
            '              _("No message yet.")',
            '              {{ m(_("No message yet.")) }}',
            "a marker inside a Jinja tag",
        ),
        ("<br>{{ m(text) }}</div>", "<br>{{ text | e }}</div>", "| e outside the macro m"),
        ("<br>{{ m(text) }}</div>", "<br>{{ text }}</div>", "is neither m() nor a mapping"),
        (" | replace('\\n', ' ') | e }}", " | e }}", "starts with the macro m"),
        (
            '              _("No message yet.")',
            '              # Translators: x\n              _("No message yet.")',
            "'# Translators:' line",
        ),
        ("              {%- elif v == 'away' -%}_(\"away\")\n", "", "telemetry: the branches"),
        ("{%- else -%}{{ m(v) }}", "{%- else -%}{{ v }}", "neither m() nor a mapping"),
        ('> **_("Gateway ready.")**', "> **{{ m(link) }}**", "an alert line prints a value"),
        ('state: "on"', "state: on", "on unquoted"),
        ('state_not: ["@version@", unknown]', "state_not: [@version@, unknown]", "unquoted"),
        ("binary_sensor.@gw@_radio", "binary_sensor.@gx@_radio", "the placeholder @gx@"),
        ("{queue}.", "{queues}.", "{queues} is set nowhere"),
        ("{%- set running = m(", "{%- set running = (", "set from neither m() nor integers"),
        ('heading: _("Gateway")', 'heading: [_("Gateway")]', "neither a whole value"),
        ('title: _("Mesh")', 'title: _("Mesh {node}")', "a field in a whole value"),
        ("<table><tr><th>", "<p><tr><th>", "a value outside one whole"),
        ("views:\n", "views:\n\n", "a blank line"),
    ],
)
def test_the_rules_catch_a_broken_template(old: str, new: str, finding: str) -> None:
    template = shipped()
    assert old in template
    broken = template.replace(old, new, 1)
    assert any(finding in found for found in rule_findings(broken)), rule_findings(broken)


def test_the_mappings_follow_the_gateways_lists() -> None:
    """A new delivery step, telemetry state or contact event needs its text in the template:
    the lists come from the code."""
    lists = _mappings()
    assert lists["said"][:2] == ["queued", "sent"]
    assert "position_in_advert" in lists["what"]
    assert len(lists["telemetry"]) == 9


def _cards(template: str) -> Iterator[list[str]]:
    for block in blocks(template.split("\n")):
        yield [text for _, text in block.lines]


def test_every_card_that_prints_a_value_defines_the_macro_first() -> None:
    printing = [card for card in _cards(shipped()) if any(re.search(r"\bm\(", t) for t in card)]
    assert len(printing) == 8
    assert all(card[0] == MACRO for card in printing)
