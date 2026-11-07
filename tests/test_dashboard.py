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
from types import SimpleNamespace

import pytest

from meshcore_home_gateway import __version__, dashboard, i18n
from meshcore_home_gateway.catalog import Entry
from meshcore_home_gateway.config import Config, load_config, parse_config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.model import DeliveryState, Position
from meshcore_home_gateway.dashboard import TemplateError
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from tests import dashboard_rules as rules
from tests.i18n_helpers import compiled, pseudo, speaking
from tests.test_contact_devices import (
    ALICE,
    BOB,
    CAROL,
    HILL,
    Clock,
    devices,
    mesh_of,
    node,
    stopped,
    topic,
)

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
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"\n'
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
    "{{",
    "{%",
    "{#",
    "<",
    ">",
    "@",
    "](",
    "://",
    "www.",
    '_("',
    "\r",
    "\n",
    "\u2028",
    "\u2029",
    "\u200e",
    "\x7f",
]


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
        node(ALICE, "Alice", position=HERE),
        node(BOB, "Bob"),
        node(CAROL, "Carol", position=HERE),
        node(DAVE, "Dave", position=HERE),
    ]
    mesh = mesh_of(*nodes)
    contacts.update(mesh)
    contacts.discovery_message(topic(CAROL), b"", retained=False)  # the user deleted it
    contacts.update(mesh_of(*nodes[:4], mesh=mesh))  # Dave left the radio
    stopped(tmp_path)  # the service stopped and wrote its state
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
    # ARC-015: a second gateway's contacts begin otherwise, on the map and in the contacts card
    assert dashboard.trackers(records, "mesh_2")[0] == "device_tracker.mesh_2_alice"


def test_the_controls_card_lists_each_contacts_switch_and_button(tmp_path: Path) -> None:
    """IMP-049: by contact, the switch Telemetry and the button Remove from radio it was
    announced with; a slug the gateway never gives is left out, and without any the card is
    not written."""

    def record(slug: str, **components: str) -> dict[str, object]:
        return {"slug": slug, "cmps": components}

    announced = {
        "k1": record("zed", remove="button"),  # a repeater: never asked
        "k2": record("alice", telemetry="switch", remove="button", last_heard="sensor"),
        "k3": record('x"]\n- y', telemetry="switch", remove="button"),
        "k4": record("bob", telemetry="sensor"),  # never a switch of that key
    }
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ha_contact_devices": {"announced": announced}}), encoding="utf-8")
    records = ContactDeviceRecords(JsonStateFile(state, read_only=True))
    expected = ["switch.mesh_alice_telemetry", "button.mesh_alice_remove", "button.mesh_zed_remove"]
    assert dashboard.controls(records) == expected
    assert dashboard.controls(records, "mesh_2")[0] == "switch.mesh_2_alice_telemetry"
    text, _ = dashboard.dashboard(config(), tmp_path)
    assert f"entities: {json.dumps(expected)}" in text
    assert "Telemetry and removal" in text
    state.write_text(json.dumps({"ha_contact_devices": {"announced": {}}}), encoding="utf-8")
    assert "Telemetry and removal" not in dashboard.dashboard(config(), tmp_path)[0]


def test_reason_codes_read_as_texts_and_an_unknown_one_stays_a_code() -> None:
    """IMP-049: each reason the API documents for a delivery and for a contact request has a
    text; one added later, and the radio's own refusals, stay codes, through m()."""
    template = shipped()
    delivery, contact = (template.split("macro why(v)")[i].split("endmacro")[0] for i in (1, 2))
    documented = {
        delivery: """no_acknowledgement unknown_contact unknown_target ambiguous_target
            not_a_companion channel_not_configured too_long too_large invalid_utf8 empty
            retained queue_full queue_ttl no_radio airtime_budget exceeds_airtime_burst
            internal_error""",
        contact: """invalid_request invalid_link unsupported_link unsupported_type unknown_node
            unknown_contact management_off own_key protected busy no_radio untested_firmware
            rate_limited already_contact look_alike table_full not_confirmed outcome_unknown
            not_a_companion too_many not_saved not_forgotten state_unwritable too_large
            invalid_utf8 retained internal_error""",
    }
    for macro, codes in documented.items():
        for code in codes.split():
            assert f"v == '{code}'" in macro, code
    for macro in (delivery, contact):
        assert "{%- else -%}<code>{{ m(v) }}</code>" in macro
    assert "<code>{{ m(reason) }}</code>" not in template


def test_the_contacts_card_finds_the_contacts_by_the_configured_prefix() -> None:
    """ARC-015: `[homeassistant] contact_entity_prefix` reaches the card's entity patterns."""
    template = (Path(dashboard.__file__).parent / dashboard.TEMPLATE).read_text(encoding="utf-8")
    yaml = dashboard.render(
        template,
        node_id="radio1",
        version="1.0",
        trackers=[],
        text=lambda m, v: m,
        contact_prefix="mesh_2",
    )
    assert "sensor\\\\.mesh_2_.+_last_heard" in yaml
    assert "sensor\\\\.mesh_.+_last_heard" not in yaml


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
    assert first.endswith('heading: "Help"\n')  # the last page without a chat
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
_PLACEHOLDERS = {
    "@gw@",
    "@contact@",
    "@node@",
    "@version@",
    "@major@",
    "@minor@",
    "@trackers@",
    "@controls@",
    "@chats@",
}
_JINJA_NOTE = re.compile(r"\{#-?\s*Translators:.*-?#\}")
_OPENS = {
    "<div>": "</div>",
    "<table>": "</table>",
    '<table role="presentation" width="100%">': "</table>",  # several on one line: one element
    "<details>": "</details>",
    "<ha-qr-code": "</ha-qr-code>",
}


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
    each = each_lines(lines)
    # rules 1, 5, 6 and 4: markers, placeholders, translator comments, quoting
    for number, line in enumerate(lines, 1):
        markers = list(dashboard.MARKER.finditer(line))
        if '_("' in dashboard.MARKER.sub("", line):
            found.append(f'{number}: _(" outside a marker')
        found += [
            f"{number}: the placeholder {placeholder}"
            for placeholder in re.findall(r"@[\w.]+@", line)
            if placeholder not in _PLACEHOLDERS and not _of_item(placeholder, each.get(number))
        ]
        for listed in ("@trackers@", "@controls@", "@chats@"):
            if listed in line and f'["{listed}"]' not in line:
                found.append(f"{number}: {listed} is a whole list")
        tags = [match.span() for match in _TAG.finditer(line)]
        for marker in markers:
            if any(start <= marker.start() < end for start, end in tags):
                found.append(f"{number}: a marker inside a Jinja tag")
            if number not in inside and not dashboard.VALUE.match(line):
                found.append(f"{number}: a marker neither a whole value nor in a card's content")
            whole = [] if number in inside else _fields(_msgid(marker))
            if any(name != "name" or number not in each for name in whole):
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
    if any(f"set {name} " in template for name in dashboard.FIXED):
        found.append("the template sets node, version or name")
    return found


def each_lines(lines: list[str]) -> dict[int, str]:
    """The lines inside an @each, by number: the list it repeats."""
    within: dict[int, str] = {}
    opened: list[str] = []
    for number, line in enumerate(lines, 1):
        word, _, argument = line.strip().removeprefix("# @").partition(" ")
        if line.strip().startswith("# @") and word in ("if", "each"):
            opened.append(argument if word == "each" else "")
        elif line.strip() == "# @end" and opened:
            opened.pop()
        elif listed := next((name for name in opened if name), None):
            within[number] = listed
    return within


def _of_item(placeholder: str, listed: str | None) -> bool:
    """An item's field, inside an @each of its list."""
    name, _, key = placeholder.strip("@").partition(".")
    return listed is not None and name == listed and key in dashboard.FIELDS[name]


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
            if source.startswith(("m(", "mt(")) and source.endswith(")"):
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
            if call is None or (call.group(1) not in ("m", "mt") and call.group(1) not in macros):
                found.append(f"{number}: {{{{ {expression} }}}} is neither m() nor a mapping")
            elif number in body and call.group(1) not in ("m", "mt"):
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
                found.append(f"{number}: a value outside one whole element of _OPENS")
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
    names = {
        name
        for block in blocks(template.split("\n"))
        for _, t in block.lines
        for name in re.findall(r"macro (\w+)\(", t)
    }
    assert {"m", "ago", "said", "telemetry", "what"} <= names
    assert template.count(MACRO) == 9
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
        ("<table><tr><th>", '<table role="presentation"><tr><th>', "a value outside one whole"),
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
    assert len(printing) == 9
    assert all(card[0] == MACRO for card in printing)


# --- the template language of 2.1: lists, items, {name} ------------------------------------------


def page(path: str, title: str, **fields: str) -> dict[str, str]:
    """An item of `chats`, every field given: a channel's page unless `key` says otherwise."""
    item = {
        "path": path,
        "title": title,
        "key": "",
        "channel": "1",
        "target": "",
        "aliases": "",
        "pair": "",
        "write": "",
        "send": "",
        "room": "158",
    }
    return item | fields


FAMILY = page(
    "chat-family",
    "Family",
    target="family",
    aliases="alerts,family",
    write="text.meshcore_home_gateway_radio_1_write_family",
    send="button.meshcore_home_gateway_radio_1_write_family_send",
)
ALARM = page("chat-alarm", "Alarm", channel="2", target="alarm", aliases="alarm")
EACH = (
    "views:\n"
    "# @each chats\n"
    '  - title: "@chats.title@"\n'
    '    path: "@chats.path@"\n'
    "# @if chats.write\n"
    '    footer: "@chats.write@ @chats.send@"\n'
    "# @else\n"
    "    footer: none\n"
    "# @end\n"
    "# @end\n"
)


def pages(template: str, *chats: Mapping[str, str], version: str = "1.2.3", **lists: object) -> str:
    return dashboard.render(
        template,
        node_id="radio-1",
        version=version,
        trackers=[],
        text=ENGLISH,
        chats=chats,
        **lists,  # type: ignore[arg-type]
    )


def test_each_repeats_its_lines_per_item_with_the_items_fields() -> None:
    assert pages(EACH, FAMILY, ALARM) == (
        "views:\n"
        '  - title: "Family"\n'
        '    path: "chat-family"\n'
        '    footer: "text.meshcore_home_gateway_radio_1_write_family '
        'button.meshcore_home_gateway_radio_1_write_family_send"\n'
        '  - title: "Alarm"\n'
        '    path: "chat-alarm"\n'
        "    footer: none\n"
    )
    assert pages(EACH) == "views:\n"


def test_a_list_outside_its_each_is_a_flag_and_the_others_are_items_too() -> None:
    template = (
        "# @if chats\nsome\n# @else\nnone\n# @end\n"
        "# @each removes\n- @removes.entity@ @removes.title@ @removes.key@\n# @end\n"
        "# @each switches\n- @switches.entity@\n# @end\n"
    )
    remove = {"entity": "button.mesh_alice_remove", "title": "Alice", "key": ALICE}
    switch = {"entity": "switch.mesh_alice_telemetry", "title": "Alice", "key": ALICE}
    assert pages(template, removes=[remove], switches=[switch]) == (
        f"none\n- button.mesh_alice_remove Alice {ALICE}\n- switch.mesh_alice_telemetry\n"
    )
    assert pages(template, FAMILY).startswith("some\n")


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("template", "error"),
    [
        ("# @each chats\n# @each removes\n# @end\n# @end\n", "nested @each"),
        ("# @each people\n# @end\n", "unknown list"),
        ("# @each chats\na\n", "not closed"),
        ("# @each chats\n# @else\n# @end\n", "@else in an @each"),
        ("# @each\n# @end\n", "unknown list"),
        ("# @if chats.write\na\n# @end\n", "unknown flag"),
        ("# @each removes\n# @if chats.write\n# @end\n# @end\n", "unknown flag"),
        ("# @each chats\n# @if chats.colour\n# @end\n# @end\n", "unknown flag"),
    ],
)
def test_a_broken_each_is_refused(template: str, error: str) -> None:
    with pytest.raises(TemplateError, match=error):
        pages(template, FAMILY)


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    "template",
    [
        "title: @chats.title@\n",  # outside its @each
        "# @each chats\ntitle: @removes.title@\n# @end\n",  # another list's field
        "# @each chats\ntitle: @chats.colour@\n# @end\n",  # no such field
        "title: @gw.x@\n",
    ],
)
def test_a_left_over_item_placeholder_is_refused(template: str) -> None:
    with pytest.raises(TemplateError, match="left over"):
        pages(template, FAMILY)


BAD_ITEMS = [
    ("path", "contact-Alice"),
    ("path", "page-alice"),
    ("path", "chat-" + "a" * 41),
    ("title", "2nd-floor"),
    ("title", " Alice"),
    ("title", "A" * 33),
    ("title", 'x"]'),
    ("key", "A1" * 32),
    ("key", "a1" * 31),
    ("channel", "1.5"),
    ("channel", "-2"),
    ("channel", ""),
    ("target", "a--b"),
    ("target", "Family"),
    ("aliases", "family,,alerts"),
    ("aliases", "family,"),
    ("pair", "a_"),
    ("write", "sensor.meshcore_home_gateway_radio_1_write_family"),
    ("write", "text.Write"),
    ("send", "button.x y"),
    ("room", "0"),
    ("room", "161"),
    ("room", "abc"),
]


@pytest.mark.parametrize(("name", "value"), BAD_ITEMS)
def test_an_item_value_that_fails_its_pattern_is_refused(name: str, value: str) -> None:
    """Item values are never mesh text: each is checked before it goes in."""
    assert dashboard.refused("chats", FAMILY) is None
    item = FAMILY | {name: value}
    assert dashboard.refused("chats", item) == (name, value)
    with pytest.raises(TemplateError, match="an item of chats"):
        pages(EACH, item)


def test_an_item_needs_exactly_its_fields() -> None:
    assert dashboard.refused("chats", {k: v for k, v in FAMILY.items() if k != "room"}) == (
        "room",
        "",
    )
    assert dashboard.refused("chats", FAMILY | {"colour": "red"}) == ("colour", "red")
    remove = {"entity": "button.mesh_alice_remove", "title": "Alice", "key": ALICE}
    assert dashboard.refused("removes", remove) is None
    assert dashboard.refused("removes", remove | {"entity": ""}) == ("entity", "")
    assert dashboard.refused("switches", remove | {"key": ""}) == ("key", "")


@pytest.mark.real_catalogs
def test_name_is_the_items_title_in_whole_values_and_in_card_texts() -> None:
    """{name} names the page or the person of the item, also in a translation; outside an
    @each there is none, and a whole value still takes no other field."""
    template = (
        "# @each chats\n"
        '  - name: _("Write to {name}")\n'
        "    content: |-\n"
        '      <div>_("You cannot write to {name} from here.")</div>\n'
        "# @end\n"
    )
    assert pages(template, FAMILY, ALARM) == (
        '  - name: "Write to Family"\n'
        "    content: |-\n"
        "      <div>You cannot write to Family from here.</div>\n"
        '  - name: "Write to Alarm"\n'
        "    content: |-\n"
        "      <div>You cannot write to Alarm from here.</div>\n"
    )
    german = translated({"Write to {name}": "An {name} schreiben"})
    out = dashboard.render(
        template, node_id="radio-1", version="1.0", trackers=[], text=german, chats=[FAMILY]
    )
    assert '  - name: "An Family schreiben"\n' in out
    for outside in ('name: _("Write to {name}")\n', 'content: |-\n  _("Hello {name}")\n'):
        with pytest.raises(TemplateError, match="only inside an @each"):
            pages(outside, FAMILY)
    with pytest.raises(TemplateError, match="no field"):
        pages('# @each chats\nname: _("Write to {name} on {node}")\n# @end\n', FAMILY)


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("version", "numbers"),
    [("2.1.0", "2 1"), ("10.0.3rc1", "10 0"), ("2.1.0a1", "2 1"), ("1.0", "1 0")],
)
def test_major_and_minor_are_the_versions_first_two_numbers(version: str, numbers: str) -> None:
    """The card of another version compares them with the gateway's version as integers."""
    assert pages("@major@ @minor@\n", version=version) == numbers + "\n"


@pytest.mark.real_catalogs
def test_a_version_without_two_numbers_is_refused() -> None:
    with pytest.raises(TemplateError, match="major and minor"):
        pages("@major@\n", version="dev")


@pytest.mark.real_catalogs
def test_the_chats_placeholder_is_the_pages_as_a_jinja_list() -> None:
    """For the cards that list the chats: Jinja reads JSON objects, numbers and strings."""
    out = pages('{%- set chats = ["@chats@"] -%}\n', FAMILY, ALARM)
    listed = json.loads(out.removeprefix("{%- set chats = ").removesuffix(" -%}\n"))
    assert listed[0] == {
        "path": "chat-family",
        "title": "Family",
        "key": "",
        "channel": 1,
        "target": "family",
        "aliases": ["alerts", "family"],
        "pair": "",
        "write": "text.meshcore_home_gateway_radio_1_write_family",
        "send": "button.meshcore_home_gateway_radio_1_write_family_send",
        "room": 158,
    }
    assert listed[1]["aliases"] == ["alarm"]
    assert listed[1]["write"] == ""
    assert pages('x ["@chats@"]\n') == "x []\n"


# --- the chats list and the controls' items, from the configuration and the state ----------------

GATE, FIRST, ATTACK = "e5" * 32, "f6" * 32, "07" * 32
HOUSEHOLD = f"""
[targets.family]
channel = 1
[targets.alerts]
channel = 1
[targets.alarm]
channel = 2
[targets.2nd-floor]
channel = 3
[targets.a-b]
channel = 4
[targets.a_b]
channel = 5
[targets.bob]
contact = "{BOB}"
prefix = "Home: "
[targets.gate]
contact = "{GATE}"
[homeassistant]
hidden_targets = ["alarm"]
"""


def record(slug: str, **components: str) -> dict[str, object]:
    return {"slug": slug, "cmps": components}


def state(directory: Path, announced: Mapping[str, object]) -> ContactDeviceRecords:
    """The service's state, as it left it, with these contact devices."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "state.json"
    path.write_text(json.dumps({"ha_contact_devices": {"announced": announced}}), encoding="utf-8")
    return ContactDeviceRecords(JsonStateFile(path, read_only=True))


def household(directory: Path) -> ContactDeviceRecords:
    """Alice, Bob (also the target bob), a contact slugged `1st`, one slugged as an attack, and a
    repeater, which takes no texts."""
    return state(
        directory,
        {
            ALICE: record("alice", send="notify", telemetry="switch", remove="button"),
            BOB: record("bob", send="notify", remove="button"),
            FIRST: record("1st", send="notify", remove="button"),
            ATTACK: record('x"]\n- y', send="notify", remove="button"),
            HILL: record("zed_hill", remove="button"),
        },
    )


GW = "meshcore_home_gateway_radio1"


def test_the_chats_list_is_the_targets_then_the_people(tmp_path: Path) -> None:
    """Targets with a field by alias, then the hidden ones, then the people with a device that
    takes texts, by slug. Bob is also a target: his page uses the
    target's field. A contact target without a device gets a page of its own."""
    chats = dashboard.chats(config(more=HOUSEHOLD), household(tmp_path))
    assert [c.path for c in chats.pages] == [
        "chat-2nd-floor",
        "chat-a-b",
        "chat-a_b",
        "chat-family",
        "chat-gate",
        "chat-alarm",
        "contact-1st",
        "contact-alice",
        "contact-bob",
    ]
    by_path = {c.path: c.item() for c in chats.pages}
    assert by_path["chat-family"] == page(
        "chat-family",
        "Family",
        target="family",
        aliases="alerts,family",
        write=f"text.{GW}_write_family",
        send=f"button.{GW}_write_family_send",
    )
    assert by_path["chat-2nd-floor"]["title"] == "2nd floor"
    assert by_path["chat-alarm"] == page(
        "chat-alarm", "Alarm", channel="2", target="alarm", aliases="alarm"
    )
    assert by_path["chat-gate"] == page(
        "chat-gate",
        "Gate",
        key=GATE,
        channel="-1",
        target="gate",
        aliases="gate",
        write=f"text.{GW}_write_gate",
        send=f"button.{GW}_write_gate_send",
        room="160",
    )
    assert by_path["contact-alice"] == page(
        "contact-alice",
        "Alice",
        key=ALICE,
        channel="-1",
        write="text.mesh_alice_write",
        send="button.mesh_alice_write_send",
        room="160",
    )
    assert by_path["contact-1st"]["title"] == "1st"
    assert by_path["contact-bob"] == page(
        "contact-bob",
        "Bob",
        key=BOB,
        channel="-1",
        target="bob",
        aliases="bob",
        write=f"text.{GW}_write_bob",
        send=f"button.{GW}_write_bob_send",
        room="154",
    )
    assert chats.skipped == (("path", 'contact-x"]\n- y'),)
    assert (chats.channels, chats.people, chats.without_page) == (5, 4, 0)


def test_two_aliases_that_home_assistant_merges_get_no_field(tmp_path: Path) -> None:
    """a-b and a_b are both valid aliases, but their entities get one ID in Home Assistant: no
    field on either page, which names the other."""
    chats = dashboard.chats(config(more=HOUSEHOLD), household(tmp_path))
    by_path = {c.path: c.item() for c in chats.pages}
    for path, other in (("chat-a-b", "a_b"), ("chat-a_b", "a-b")):
        assert (by_path[path]["write"], by_path[path]["send"]) == ("", "")
        assert by_path[path]["pair"] == other
    assert all(c.pair == "" for c in chats.pages if c.path not in ("chat-a-b", "chat-a_b"))
    assert chats.pairs == (("a-b", "a_b"),)
    hidden = config(more=HOUSEHOLD.replace('["alarm"]', '["alarm", "a_b"]'))
    alone = dashboard.chats(hidden, household(tmp_path))
    assert alone.pairs == ()
    assert {c.path: c for c in alone.pages}["chat-a-b"].write == f"text.{GW}_write_a_b"


def test_a_hidden_contact_target_gives_its_person_no_field(tmp_path: Path) -> None:
    more = HOUSEHOLD.replace('["alarm"]', '["alarm", "bob"]')
    chats = dashboard.chats(config(more=more), household(tmp_path))
    bob = {c.path: c.item() for c in chats.pages}["contact-bob"]
    assert (bob["target"], bob["write"], bob["send"]) == ("bob", "", "")


def test_the_people_are_capped_at_thirty_pages(tmp_path: Path) -> None:
    announced = {f"{i:02x}" * 32: record(f"p{i:02d}", send="notify") for i in range(32)}
    chats = dashboard.chats(config(), state(tmp_path, announced))
    assert len(chats.pages) == dashboard.CHAT_PAGES == 30
    assert chats.pages[-1].path == "contact-p29"
    assert (chats.people, chats.without_page) == (30, 2)


def test_the_room_is_the_gateways_limit_of_the_chat(tmp_path: Path) -> None:
    """The gateway's own rule (Delivery._text_policy), without the radio's name, less the
    target's prefix and suffix; a direct text leaves room for the delivery code."""
    more = HOUSEHOLD + "[messages]\nconfirm_nonce = true\n"
    gateway = config(more=more)
    chats = {c.path: c for c in dashboard.chats(gateway, household(tmp_path)).pages}
    for target in gateway.targets:
        policy = Delivery._text_policy(SimpleNamespace(config=gateway), target, None)  # type: ignore[arg-type]
        affixes = len(target.prefix.encode()) + len(target.suffix.encode())
        assert dashboard.room(gateway, target) == policy.max_bytes - affixes
    assert chats["contact-bob"].room == 160 - 10 - 6
    assert chats["contact-alice"].room == 150
    assert chats["chat-family"].room == 158


def test_the_controls_items_come_from_the_state(tmp_path: Path) -> None:
    """The switches and remove buttons each contact was announced with, by slug, named after
    it; a slug the gateway never gives is left out."""
    records = household(tmp_path)
    assert dashboard.switches(records) == [
        {"entity": "switch.mesh_alice_telemetry", "title": "Alice", "key": ALICE}
    ]
    assert dashboard.removes(records, "mesh_2") == [
        {"entity": "button.mesh_2_1st_remove", "title": "1st", "key": FIRST},
        {"entity": "button.mesh_2_alice_remove", "title": "Alice", "key": ALICE},
        {"entity": "button.mesh_2_bob_remove", "title": "Bob", "key": BOB},
        {"entity": "button.mesh_2_zed_hill_remove", "title": "Zed hill", "key": HILL},
    ]


def test_the_dashboard_has_a_page_per_chat(tmp_path: Path) -> None:
    """The pages of the chats list, each with its field, or without one and saying why."""
    household(tmp_path)
    built = dashboard.build(config(more=HOUSEHOLD), tmp_path)
    text = built.yaml
    for chat in built.chats.pages:
        assert f'    path: "{chat.path}"\n' in text
    assert text.count("subview: true") == 4 + len(built.chats.pages)
    assert f'entity: "text.{GW}_write_family"\n' in text
    assert f'entity: "button.{GW}_write_family_send"\n' in text
    assert "_write_a_b" not in text
    assert "_write_alarm" not in text
    pair = "the targets {{ a }} and {{ b }} get the same entity IDs in Home Assistant"
    assert text.count(pair) == 2
    for a, b in (("a-b", "a_b"), ("a_b", "a-b")):  # each page names its own target first
        assert f"{{%- set a = m('{a}') -%}}\n              {{%- set b = m('{b}') -%}}" in text
    assert 'name: "Write to Family"\n' in text
    assert built.written_at == (tmp_path / "state.json").stat().st_mtime
    assert dashboard.build(config(), tmp_path / "none").written_at is None


# --- the rules of 2.1 that need the dashboard's structure (dashboard_rules.py) -------------------

# What the shipped template still breaks, by rule: the cards of 2.0 that stages 4 and 5 rewrite
# (docs/development.md, the dashboard's template). Each rewrite lowers the counts, to none.
PENDING = {
    "rule 3": 2,  # the 2.0 card of the switches and remove buttons
    "rule 4": 7,  # 2.0's problem tiles, its version card, the script's tile and hint
    "rule 5": 1,  # the remove buttons as entity rows
    "rule 8": 122,  # help at every card, About in every view, the Help page (stages 4 and 5)
    "rule 9": 1,  # the status line and the problem cards
    "rule 11": 3,  # the status line, the script's hint, the map's note
    "rule 13": 4,  # Last message and Last sent
    "rule 17": 1,  # the script's hint
    "rule 19": 1,  # m() of 2.0
}


def rule_counts(found: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for finding in found:
        rule = finding.split(":")[0]
        counts[rule] = counts.get(rule, 0) + 1
    return counts


def added(template: str) -> list[str]:
    """The findings a changed template has that the shipped one has not."""
    before = rules.findings(shipped())
    return [finding for finding in rules.findings(template) if finding not in before]


def into_help(*lines: str) -> str:
    """The shipped template with these lines after the heading of the Help page."""
    heading = '            heading: _("Help")\n'
    template = shipped()
    assert template.count(heading) == 1
    return template.replace(heading, heading + "".join(f"{line}\n" for line in lines))


def card(*content: str, **keys: str) -> list[str]:
    """A Markdown card of these content lines, for into_help()."""
    head = ["          - type: markdown", *(f"            {k}: {v}" for k, v in keys.items())]
    return [*head, "            content: |-", *(f"              {line}" for line in content)]


def test_the_kept_rules_take_mt_and_the_new_elements() -> None:
    """A value through mt() on a <details> line or on a line of tables of presentation
    is one element (rule 1)."""
    template = into_help(
        *card(
            MACRO,
            MT,
            "<details><summary>_(\"Help\")</summary>{{ mt(states('sensor.x')) }}</details>",
            '<table role="presentation" width="100%"><tr><td>{{ m(states(\'sensor.y\')) }}</td>'
            '</tr></table><table role="presentation" width="100%"><tr><td></td></tr></table>',
        )
    )
    assert rule_findings(template) == []
    assert rule_findings(template.replace("</details>", "</details>x")) != []


def test_the_reader_reads_the_templates_yaml() -> None:
    text = (
        "# a comment\n"
        "title: Mesh\n"
        "views:\n"
        '  - title: "A \\"quoted\\" title"\n'
        "    max_columns: 3\n"
        "    subview: true\n"
        "    entities: [\"a.b\", unknown, 'it''s']\n"
        "    footer:\n"
        "      card:\n"
        "        type: tile\n"
        "    cards:\n"
        "      - type: markdown\n"
        "        content: |-\n"
        "          # not a comment\n"
        "          second\n"
        "        value_template: >-\n"
        "          {{ a\n"
        "             and b }} c\n"
        "          d\n"
        "      - plain\n"
        "    empty:\n"
    )
    assert rules.load(text) == {
        "title": "Mesh",
        "views": [
            {
                "title": 'A "quoted" title',
                "max_columns": 3,
                "subview": True,
                "entities": ["a.b", "unknown", "it's"],
                "footer": {"card": {"type": "tile"}},
                "cards": [
                    {
                        "type": "markdown",
                        "content": "# not a comment\nsecond",
                        "value_template": "{{ a\n   and b }} c\nd",
                    },
                    "plain",
                ],
                "empty": None,
            }
        ],
    }
    assert rules.load(rendered := rules.rendered(shipped()))["views"][0]["path"] == "overview"
    assert len(rules.load(rendered)["views"]) == 7 + len(rules.HOUSEHOLD.chats)


@pytest.mark.parametrize(
    "text",
    ["a: {b: 1}\n", "a: &x 1\n", "a: 1\na: 2\n", "a: 1\n  b: 2\n", "a:\n\t- b\n", "a: [b,\n"],
)
def test_the_reader_refuses_yaml_the_template_does_not_use(text: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - each of several messages
        rules.load(text)


def test_the_shipped_template_breaks_only_what_stages_4_and_5_rewrite() -> None:
    """The numbered rules on the shipped template: all hold but those of PENDING, and
    those exactly as often as counted."""
    assert rule_counts(rules.findings(shipped())) == PENDING


def test_the_views_are_three_tabs_and_pages_without_a_tab() -> None:
    """Overview, Chats and Contacts show their titles alone; Map, Gateway, Settings, Help and a
    page per chat open from links; no page has a fixed way back."""
    views = rules.load(rules.rendered(shipped()))["views"]
    assert [(v["path"], v.get("subview", False)) for v in views] == [
        ("overview", False),
        ("messages", False),
        ("contacts", False),
        ("map", True),
        ("gateway", True),
        ("settings", True),
        ("help", True),
        *((chat["path"], True) for chat in rules.HOUSEHOLD.chats),
    ]
    assert [v["title"] for v in views[:3]] == ["Overview", "Chats", "Contacts"]
    assert not any("icon" in v or "back_path" in v for v in views)
    chats = {v["path"]: v for v in views[7:]}
    assert all(v["max_columns"] == 1 for v in chats.values())
    assert {path for path, v in chats.items() if "footer" in v} == {
        "chat-family",
        "contact-alice",
        "contact-bob",
    }
    pair = rules.texts(chats["chat-a-b"])
    assert any("{%- set a = m('a-b') -%}\n{%- set b = m('a_b') -%}" in t for t in pair)


def test_every_link_resolves_to_a_page_of_this_dashboard() -> None:
    """Rule 6: the links and navigation paths of the shipped template, each to a page."""
    loaded = rules.load(rules.rendered(shipped()))
    paths = {view["path"] for view in loaded["views"]}
    links = [
        n["navigation_path"]
        for n, _ in rules.nodes(loaded)
        if isinstance(n, dict) and "navigation_path" in n
    ]
    links += [h for t in rules.texts(loaded) for h in re.findall(r'href="([^"]*)"', t)]
    assert sorted({link.removeprefix("./") for link in links}) == [
        "gateway",
        "help",
        "map",
        "settings",
    ]
    assert {link.removeprefix("./") for link in links} <= paths
    assert rules.rule_6(shipped(), None, loaded, rules.HOUSEHOLD) == []


MT = "{%- macro mt(x) -%}{{ m(x) }}{%- endmacro -%}"
GUIDE_LINK = rules.GUIDE_URL.replace("{version}", "@version@")
BROKEN = [
    # rule 2: a mapping over a closed list
    (
        card(
            MACRO,
            "{%- macro audience(v) -%}",
            "{%- if v == 'deny' -%}_(\"never\")",
            "{%- elif v == 'contacts' -%}_(\"contacts you allowed\")",
            "{%- else -%}<code>{{ m(v) }}</code>",
            "{%- endif -%}",
            "{%- endmacro -%}",
            "<div>{{ audience(states('sensor.x')) }}</div>",
        ),
        "rule 2: ",
    ),
    # rule 3: a guard without the serial number (M5), with has_value() (M6), naming one entity
    (
        ["@@", "device_attr(d, 'serial_number') == '@chats.key@') }}", "true) }}"],
        "rule 3: text.mesh_alice_write",
    ),
    (
        ["@@", "{{ gw is not none and d", "{{ has_value(t) and gw is not none and d"],
        "rule 3: has_value()",
    ),
    (
        ["@@", "{%- set b = '@chats.send@' -%}", "{%- set b = 'button.x' -%}"],
        "rule 3: chat-family: the footer",
    ),
    # rule 4: a state condition
    (
        [
            "          - type: tile",
            "            entity: sensor.@gw@_nodes",
            "            visibility:",
            "              - condition: state",
            "                entity: sensor.@gw@_nodes",
            '                state: "0"',
        ],
        "rule 4: condition: state on sensor.meshcore_home_gateway_radio1_nodes",
    ),
    # rule 5: a remove as an entity row, a remove that does not name the person
    (
        [
            "          - type: entities",
            "            entities:",
            "              - button.@contact@_carol_remove",
        ],
        "rule 5: button.mesh_carol_remove",
    ),
    (
        [
            "# @each removes",
            "          - type: button",
            "            tap_action:",
            "              action: perform-action",
            "              perform_action: button.press",
            "              target:",
            '                entity_id: "@removes.entity@"',
            "              confirmation:",
            '                text: _("Remove this contact?")',
            "# @end",
        ],
        "rule 5: a remove of",
    ),
    # rule 6: a link out of the dashboard, to the guide of another version, of another form
    (card('<div><a href="/lovelace/gateway">_("Gateway")</a></div>'), "rule 6: a link nowhere"),
    (
        card(
            f'<div><a href="{GUIDE_LINK.replace("v@version@", "main")}#a-second-gateway">'
            '_("Help")</a></div>'
        ),
        "rule 6: a link nowhere",
    ),
    (
        card(f'<div><a href="{GUIDE_LINK}#no-such-section">_("Help")</a></div>'),
        "rule 6: a link nowhere",
    ),
    (
        card('<div><a href="./help" target="_blank">_("Help")</a></div>'),
        "rule 6: a link of another form",
    ),
    # rule 7: a marker in an attribute
    (card('<div><a href="./help" title="_("Help")">x</a></div>'), "rule 7: "),
    # rule 8: a card without help
    (card('<div>_("Help")</div>'), "rule 8: help: a markdown card without help"),
    # rule 9: the problem cards in another order than the status line
    (
        [
            *card(
                "{%- macro problem(v) -%}",
                "{%- if v == 'clock' -%}_(\"Help\")",
                "{%- elif v == 'full' -%}_(\"Help\")",
                "{%- else -%}{{ m(v) }}",
                "{%- endif -%}",
                "{%- endmacro -%}",
            ),
            *card("{#- problem: full -#}", '_("Help")'),
            *card("{#- problem: clock -#}", '_("Help")'),
        ],
        "rule 9: the status line has ['clock', 'full']",
    ),
    # rule 11: a GitHub alert
    (card("> [!NOTE]", '> _("Help")'), "rule 11: "),
    # rule 13: a message's text through m(), a card without mt(), two forms of m()
    (
        card(
            MACRO,
            MT,
            "{%- set e = 'event.@gw@_message' -%}",
            "<div>{{ m(state_attr(e, 'text')) }}</div>",
        ),
        "a message text through m(), not mt()",
    ),
    (
        card(MACRO, "{%- set e = 'event.@gw@_message' -%}", "<div>{{ m(e.get('text')) }}</div>"),
        "a message text without mt()",
    ),
    (
        card(MACRO.replace("' ')", "'')", 1), "<div>{{ m(states('sensor.x')) }}</div>"),
        "rule 13: 2 forms of m()",
    ),
    # rule 15: an enabled key, a numeric bound as a string
    (
        [
            "          - type: tile",
            "            entity: sensor.@gw@_nodes",
            "            visibility:",
            "              - condition: numeric_state",
            "                entity: sensor.@gw@_nodes",
            '                above: "5"',
            "                enabled: true",
        ],
        "rule 15: ",
    ),
    # rule 17: a guide section named, Home Assistant's menus in a household line
    (card('<div>_("See A second gateway in the guide.")</div>'), "rule 17: 'See A second gateway"),
    (card('<div>_("Open Settings › Devices & services.")</div>'), "rule 17: 'Open Settings"),
    # rule 18: the footer's tile first
    (
        [
            "@@",
            (
                "          - type: markdown\n            text_only: true\n"
                "            show_empty: false\n"
            ),
            "          - type: markdown\n            text_only: true\n",
        ],
        "rule 18: chat-family: the refused line shows",
    ),
    # rule 19: m() strips the joiner U+200D
    (
        card(
            MACRO.replace("| string |", "| string | regex_replace('[\\u200d]', '') |"),
            "<div>{{ m(states('sensor.x')) }}</div>",
        ),
        "rule 19: m() removes also ['U+200D']",
    ),
    # rule 20: a chat page with a fixed way back
    (
        [
            "@@",
            '    path: "@chats.path@"\n',
            '    path: "@chats.path@"\n    back_path: ./messages\n',
        ],
        "rule 20: chat-family has a back_path",
    ),
]


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    ("change", "finding"), BROKEN, ids=lambda x: x if isinstance(x, str) else ""
)
def test_each_rule_finds_its_break(change: list[str], finding: str) -> None:
    """A break of each rule, as a card on the Help page or as a change of the template (@@)."""
    if change[0] == "@@":
        _, old, new = change
        assert old in shipped()
        broken = shipped().replace(old, new)
    else:
        broken = into_help(*change)
    assert any(finding in f for f in added(broken)), added(broken)


HELPED = '<details><summary>_("Help")</summary>_("Help")</details>'


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    "change",
    [
        card(f'<div><a href="{GUIDE_LINK}#a-second-gateway">_("Help")</a></div>', HELPED),
        card(
            '<div>_("For your administrator: Home Assistant\'s Settings › Devices & services.")'
            "</div>",
            HELPED,
        ),
        card('<div>_("Help")</div>', HELPED),
        card('<table role="presentation" width="100%"><tr><td>_("Help")</td></tr></table>', HELPED),
        [
            *card(
                "{%- macro problem(v) -%}",
                "{%- if v == 'clock' -%}_(\"Help\")",
                "{%- elif v == 'full' -%}_(\"Help\")",
                "{%- else -%}{{ m(v) }}",
                "{%- endif -%}",
                "{%- endmacro -%}",
                HELPED,
            ),
            *card("{#- problem: taken_over -#}", '_("Help")', HELPED),
            *card("{#- problem: clock -#}", '_("Help")', HELPED),
            *card("{#- problem: full -#}", '_("Help")', HELPED),
        ],
    ],
)
def test_what_a_rule_allows_adds_no_finding(change: list[str]) -> None:
    """A link to the guide's anchor of this version, the menus in a line for the administrator,
    a card with its help, a table of presentation, the problem cards in the status line's order
    (a class the status line tests first, such as a takeover, aside)."""
    assert added(into_help(*change)) == []


def test_m_removes_the_controls_and_the_gateways_invisible_characters() -> None:
    """Rule 19: the class is the C0 and C1 controls but tab, LF and CR, and the invisible
    characters the gateway drops before it reads a contact link; U+200C and U+200D stay."""
    written = (
        "\\u0000-\\u0008\\u000b\\u000c\\u000e-\\u001f\\u007f-\\u009f\\u00ad\\u061c\\u200b"
        "\\u200e\\u200f\\u202a-\\u202e\\u2060-\\u2064\\u2066-\\u2069\\ufeff"
    )
    assert rules.removed_by(written) == rules.REMOVED
    assert {0x200C, 0x200D, 0x09, 0x0A, 0x0D} & rules.REMOVED == set()
    macro = MACRO.replace("| string |", f"| string | regex_replace('[{written}]', '') |")
    template = into_help(*card(macro, "<div>{{ m(states('sensor.x')) }}</div>"))
    assert not [f for f in rules.findings(template) if f.startswith("rule 19: m() removes also")]


def test_the_tab_titles_fit_a_phone_in_every_shipped_language() -> None:
    """Rule 14: Overview, Chats and Contacts together have at most 22 letters in every catalog
    shipped; an over-long catalog is caught."""
    for language in i18n.LANGUAGES:
        assert rules.tab_letters(shipped(), i18n.translator(language)) <= rules.TAB_LETTERS
    assert rules.tab_letters(shipped(), i18n.ENGLISH) == 21


@pytest.mark.real_catalogs
def test_an_over_long_tab_title_is_caught() -> None:
    long = translated({"Overview": "Übersicht", "Chats": "Unterhaltungen", "Contacts": "Kontakte"})
    german = compiled(
        "de",
        [
            Entry("Overview", ("Übersicht",), i18n.DASHBOARD),
            Entry("Chats", ("Unterhaltungen",), i18n.DASHBOARD),
            Entry("Contacts", ("Kontakte",), i18n.DASHBOARD),
        ],
    )
    assert long("Chats", {}) == "Unterhaltungen"
    assert rules.tab_letters(shipped(), german) == 31 > rules.TAB_LETTERS


@pytest.mark.real_catalogs
def test_a_quoted_label_is_quoted_in_every_catalog() -> None:
    """Rule 16: a help that quotes a label is in QUOTES, and each catalog's help quotes
    the catalog's label: German "Senden" in the help while the button says "Abschicken" fails."""
    help_ = 'Type, then tap \\"Send\\".'
    template = (
        "views:\n"
        '  - title: _("Help")\n'
        '    action_name: _("Send")\n'
        "    content: |-\n"
        f'      <div>_("{help_}")</div>\n'
    )
    quoted = 'Type, then tap "Send".'
    german = compiled(
        "de",
        [
            Entry("Send", ("Abschicken",), i18n.DASHBOARD),
            Entry(quoted, ("Tippe, dann „Senden“.",), i18n.DASHBOARD),
        ],
    )
    assert rules.quote_findings(template, {}, [i18n.ENGLISH]) == [
        f"rule 16: {quoted!r} quotes 'Send', not in QUOTES"
    ]
    assert rules.quote_findings(template, {quoted: "Send"}, [i18n.ENGLISH]) == []
    assert rules.quote_findings(template, {quoted: "Send"}, [german]) == [
        "rule 16: de: 'Tippe, dann „Senden“.' does not say 'Abschicken'"
    ]
    fixed = compiled(
        "de",
        [
            Entry("Send", ("Abschicken",), i18n.DASHBOARD),
            Entry(quoted, ("Tippe, dann „Abschicken“.",), i18n.DASHBOARD),
        ],
    )
    assert rules.quote_findings(template, {quoted: "Send"}, [fixed]) == []
    bare = template.replace(help_, "Type, then Send.")
    assert rules.quote_findings(bare, {}, [i18n.ENGLISH]) == []  # a bare word is no quote


def test_the_example_configuration_gives_the_same_bytes_every_time(tmp_path: Path) -> None:
    """Same configuration and state, same file: the contacts in any order of the state file."""
    example = load_config(ROOT / "deploy" / "config.example.toml")
    records = {
        ALICE: record("alice", send="notify", telemetry="switch", remove="button"),
        BOB: record("bob", send="notify", remove="button"),
        HILL: record("zed_hill", remove="button", position="device_tracker"),
    }
    state(tmp_path / "one", records)
    state(tmp_path / "two", dict(reversed(records.items())))
    first = dashboard.build(example, tmp_path / "one").yaml
    assert first == dashboard.build(example, tmp_path / "one").yaml
    assert first == dashboard.build(example, tmp_path / "two").yaml
    assert '    path: "chat-alerts"\n' in first
    assert '    path: "chat-operator"\n' in first  # a contact target without a device
    assert '    path: "contact-alice"\n' in first
