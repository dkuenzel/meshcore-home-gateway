"""The dashboard generator (dashboard.py): the template of the package filled in for one gateway,
its texts in [homeassistant] language and guarded, the map's trackers from the service's state;
and the template's own rules, checked here on the shipped template. How Home Assistant loads the
dashboard and renders its cards is for the contract tests (make ha-test)."""

from __future__ import annotations

import ast
import inspect
import json
import re
import string
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from meshcore_home_gateway import __version__, config_view, dashboard, i18n, outbox
from meshcore_home_gateway.catalog import Entry, read_po
from meshcore_home_gateway.config import Config, load_config, parse_config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core import text as text_module
from meshcore_home_gateway.core.commands import Rejection
from meshcore_home_gateway.core.mesh import NODE_TYPES
from meshcore_home_gateway.core.model import DeliveryState, Position
from meshcore_home_gateway.dashboard import TemplateError
from meshcore_home_gateway.mqtt.discovery import discovery_payload, text_room
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.outbox import Delivery
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from meshcore_home_gateway.views import SELF_ADVERT_REASONS
from tests import dashboard_rules as rules
from tests.i18n_helpers import PSEUDO_MARK, compiled, pseudo, speaking
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
POT = ROOT / "src" / "meshcore_home_gateway" / "locale" / "meshcore-home-gateway.pot"
DAVE = "e5" * 32
HERE = Position(52.1, 9.2)
# what every Markdown card that prints a value from Home Assistant defines first
MACRO = (
    r"{%- macro m(x) -%}{{ (x or '') | string | regex_replace('[\u0000-\u0008\u000b\u000c"
    r"\u000e-\u001f\u007f-\u009f\u00ad\u061c\u200b\u200e\u200f\u202a-\u202e\u2060-\u2064"
    r"\u2066-\u2069\ufeff]', '') | regex_replace('[\t\r\n\u2028\u2029]', ' ') | e }}"
    r"{%- endmacro -%}"
)
# what every card that prints a number defines after m(): the number in the dashboard's language,
# its decimal mark in place of the point, a whole one without ".0" (#145), rounded to `digits`
# places when given; anything else as m() prints it (rule 22)
NUM = (
    "{%- macro num(v, digits=none) -%}{%- set f = v | float if v | is_number else none -%}"
    "{%- set f = f | round(digits) if f is not none and digits is not none else f -%}"
    "{%- if f is none -%}{{ m(v) }}{%- elif f % 1 == 0 -%}{{ m(f | int | string) }}"
    "{%- else -%}{{ m(f | string | replace('.', '@decimal@')) }}{%- endif -%}{%- endmacro -%}"
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


def render(
    template: str, text: Speaker = ENGLISH, trackers: list[str] | None = None, decimal: str = "."
) -> str:
    return dashboard.render(
        template,
        node_id="radio-1",
        version="1.2.3",
        trackers=trackers or [],
        text=text,
        decimal=decimal,
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
    assert f"'binary_sensor.meshcore_home_gateway_{gateway}_radio'" in text
    assert f"'sensor.meshcore_home_gateway_{gateway}_message_history'" in text
    assert f"This dashboard was made for version {__version__}; the gateway runs " in text
    major, minor = __version__.split(".")[:2]
    assert f"!= ({int(major)}, {int(minor)})" in text  # P10: the gateway's major and minor
    assert f"meshcore_home_gateway_{node_id}_" not in text or node_id == gateway
    assert f"Home Assistant has no entity of node {node_id}. " in text
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
    template = 'title: _("Mesh")\ncontent: |-\n  {%- set mins = 3 -%}_("{mins} min")\n'
    texts = {"Mesh": f"Netz {bad} x", "{mins} min": f"{{mins}} {bad} Min."}
    assert render(template, translated(texts)) == (
        'title: "Mesh"\ncontent: |-\n  {%- set mins = 3 -%}{{ mins }} min\n'
    )


def test_a_translation_with_a_field_of_its_own_gives_the_english_text() -> None:
    template = 'content: |-\n  {%- set mins = 3 -%}_("{mins} min")\n'
    for wrong in ("{m} Min.", "{mins.real} Min.", "{0} Min.", "{mins Min."):
        assert render(template, translated({"{mins} min": wrong})).endswith("{{ mins }} min\n")
    assert render(template, translated({"{mins} min": "{mins} Min."})).endswith("{{ mins }} Min.\n")


def test_the_pseudo_language_passes_the_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every text of the shipped template goes through the catalog, fields kept."""
    speaking(monkeypatch, pseudo())
    text, _ = dashboard.dashboard(config(more='[homeassistant]\nlanguage = "xx"\n'), tmp_path)
    assert '\ntitle: "‹Mesh›"\n' in text
    assert "‹{{ mins }} min›" in text
    assert "‹For your administrator: Home Assistant has no entity of node radio1. " in text
    english, _ = dashboard.dashboard(config(), tmp_path)
    assert "‹" not in english
    text = text.replace(f"replace('.', '{PSEUDO_MARK}')", "replace('.', '.')")  # num()
    assert re.sub(r"[‹›]", "", text) == re.sub(r"[‹›]", "", english)  # menus: Settings › …


@pytest.mark.real_catalogs
def test_no_translation_makes_a_marker_or_a_placeholder() -> None:
    """The left-overs are looked for in the template, before any text goes in: a translation,
    even one the guard lets through, cannot make the command fail."""
    template = 'content: |-\n  @_("gw")@ _("Mesh")("x") _("Overview")\n'
    texts = {"gw": "gw", "Mesh": "_", "Overview": '@gw@ _("x")'}
    out = render(template, translated(texts))
    assert out == 'content: |-\n  @gw@ _("x") Overview\n'


def test_the_decimal_mark_goes_where_num_writes_a_point() -> None:
    """@decimal@ becomes the dashboard's decimal mark, the point of English unless the caller
    gives the language's (i18n.DECIMAL_MARKS, #145)."""
    template = "content: |-\n  {{ v | string | replace('.', '@decimal@') }}\n"
    assert render(template) == "content: |-\n  {{ v | string | replace('.', '.') }}\n"
    german = render(template, decimal=",")
    assert german == "content: |-\n  {{ v | string | replace('.', ',') }}\n"
    for mark in (*i18n.DECIMAL_MARKS.values(), PSEUDO_MARK):
        assert f"replace('.', '{mark}')" in render(shipped(), decimal=mark)


@pytest.mark.parametrize(
    "mark", ["'", '"', "\\", "@", "<", "{", "a", "5", " ", "\u00a0", "\u200e", "", ".,"]
)
def test_a_decimal_mark_that_is_no_punctuation_or_breaks_the_template_is_refused(
    mark: str,
) -> None:
    """The mark stands inside a quoted Jinja string: one punctuation character, no quote,
    backslash or what a card would read as code (unsafe())."""
    with pytest.raises(TemplateError, match="decimal mark"):
        render("content: |-\n  {{ '@decimal@' }}\n", decimal=mark)


@pytest.mark.real_catalogs
@pytest.mark.parametrize("language", ["en", "de"])
def test_the_dashboard_writes_numbers_with_its_languages_decimal_mark(
    language: str, tmp_path: Path
) -> None:
    """Each card's num() puts the language's decimal mark in place of the point: German writes
    0,4 (#145); the mark comes from locale data, not from a translator."""
    text, _ = dashboard.dashboard(
        config(more=f'[homeassistant]\nlanguage = "{language}"\n'), tmp_path
    )
    mark = i18n.DECIMAL_MARKS[language]
    defined = text.count("{%- macro num(v, digits=none) -%}")
    assert defined >= 6  # the cards that print a number, and a chat's About on each chat page
    assert text.count(f"replace('.', '{mark}')") == defined
    assert "@decimal@" not in text


def test_a_language_the_tests_offer_writes_its_own_decimal_mark(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    speaking(monkeypatch, pseudo())
    text, _ = dashboard.dashboard(config(more='[homeassistant]\nlanguage = "xx"\n'), tmp_path)
    assert f"replace('.', '{PSEUDO_MARK}')" in text
    assert "replace('.', '.')" not in text


@pytest.mark.real_catalogs
@pytest.mark.parametrize(
    "template",
    [
        'title: _("Mesh"\n',
        'content: |-\n  _("open\n',
        "title: @gx@\n",
        "title: @contact@\n",  # 2.1's template names no contact's entity ID by its prefix
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


def test_the_switches_and_remove_buttons_are_guarded_rows_of_the_state(tmp_path: Path) -> None:
    """IMP-049, D-25, D-26: each contact's switch and remove button from the state, each as a
    row whose guard pins it to this gateway and to the contact's key (rule 3); a remove asks
    first, naming the contact; a slug the gateway never gives is left out; without any, a line
    says so (D-32). The card of 2.0, a fixed list of IDs, is gone (HA-01, HA-05)."""
    zed = DAVE

    def record(slug: str, **components: str) -> dict[str, object]:
        return {"slug": slug, "cmps": components}

    announced = {
        zed: record("zed", remove="button"),  # a repeater: never asked
        ALICE: record("alice", telemetry="switch", remove="button", last_heard="sensor"),
        CAROL: record('x"]\n- y', telemetry="switch", remove="button"),
        BOB: record("bob", telemetry="sensor"),  # never a switch of that key
    }
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"ha_contact_devices": {"announced": announced}}), encoding="utf-8")
    text, _ = dashboard.dashboard(config(), tmp_path)
    contacts = next(v for v in rules.load(text)["views"] if v["path"] == "contacts")
    rows = [
        (n["row"], n["conditions"][0]["value_template"])
        for n, _ in rules.nodes(contacts)
        if isinstance(n, dict) and n.get("type") == "conditional"
    ]
    switches = [row["entity"] for row, _ in rows if "entity" in row]
    removes = [row["tap_action"]["target"]["entity_id"] for row, _ in rows if "entity" not in row]
    assert switches == ["switch.mesh_alice_telemetry"]
    assert removes == ["button.mesh_alice_remove", "button.mesh_zed_remove"]
    keys = [ALICE, ALICE, zed]
    for (row, guard), key in zip(rows, keys, strict=True):
        assert f"device_attr(device_id(e), 'serial_number') == '{key}'" in guard, row
    asked = [row["tap_action"]["confirmation"]["text"] for row, _ in rows if "entity" not in row]
    assert asked[0].startswith("Alice can then no longer write to this home")
    assert "Telemetry and removal" not in text
    assert "No contact can be asked yet." not in text
    state.write_text(json.dumps({"ha_contact_devices": {"announced": {}}}), encoding="utf-8")
    text, _ = dashboard.dashboard(config(), tmp_path)
    assert "No contact can be asked yet." in text
    assert "No contact can be removed here." in text
    assert "type: conditional" not in text


def test_the_generator_takes_no_fixed_list_of_controls() -> None:
    """Design §6.1: the flag `controls` and `["@controls@"]` of 2.0 are gone; the switches and
    buttons are the items of `switches` and `removes`."""
    assert "controls" not in inspect.signature(dashboard.render).parameters
    assert not hasattr(dashboard, "controls")
    assert "@controls@" not in shipped()
    assert "# @if controls" not in shipped()
    with pytest.raises(TemplateError, match="unknown flag"):
        render("# @if controls\na\n# @end\n")


# the reasons of docs/mqtt-api.md, Delivery and Managing contacts
DELIVERY_REASONS = [
    "no_acknowledgement",
    "unknown_contact",
    "unknown_target",
    "ambiguous_target",
    "not_a_companion",
    "channel_not_configured",
    "too_long",
    "too_large",
    "invalid_utf8",
    "empty",
    "retained",
    "queue_full",
    "stopping",
    "queue_ttl",
    "no_radio",
    "airtime_budget",
    "exceeds_airtime_burst",
    "internal_error",
]
CONTACT_REASONS = [
    "invalid_request",
    "invalid_link",
    "unsupported_link",
    "unsupported_type",
    "unknown_node",
    "unknown_contact",
    "management_off",
    "own_key",
    "protected",
    "busy",
    "no_radio",
    "untested_firmware",
    "rate_limited",
    "already_contact",
    "look_alike",
    "table_full",
    "not_confirmed",
    "outcome_unknown",
    "not_a_companion",
    "too_many",
    "not_saved",
    "not_forgotten",
    "state_unwritable",
    "too_large",
    "invalid_utf8",
    "retained",
    "internal_error",
]
# the reasons of docs/mqtt-api.md, Commands: why the gateway refused a received command
COMMAND_REASONS = [reason.value for reason in Rejection]


def test_reason_codes_read_as_texts_and_an_unknown_one_stays_a_code() -> None:
    """IMP-049: each reason the API documents for a delivery, for a contact request and for a
    refused command has a text, in every copy of the card's macro why() (the thread, Latest
    messages, the refused line; the last contact event; the last command on Gateway); one added
    later, and the radio's own refusals, stay codes, through m(); a card with why() prints no
    reason as a bare code."""
    template = shipped()
    macros = [part.split("endmacro")[0] for part in template.split("macro why(v)")[1:]]
    delivery = [macro for macro in macros if "v == 'no_acknowledgement'" in macro]
    contact = [macro for macro in macros if "v == 'invalid_request'" in macro]
    command = [macro for macro in macros if "v == 'code_locked'" in macro]
    # Latest messages, a chat, its refused line; the contact event; Latest of each kind
    assert (len(delivery), len(contact), len(command)) == (3, 1, 1)
    assert len(macros) == 5
    assert len(set(delivery)) == 1  # one text, copied (DR-30)
    for found, codes in (
        (delivery[0], DELIVERY_REASONS),
        (contact[0], CONTACT_REASONS),
        (command[0], COMMAND_REASONS),
    ):
        for code in codes:
            assert f"v == '{code}'" in found, code
        assert "{%- else -%}<code>{{ m(v) }}</code>" in found
    for card in _cards(template):
        if any("macro why(v)" in line for line in card):
            assert not any(re.search(r"<code>\{\{ m\((reason|code)\) \}\}", t) for t in card)


def _reasons_written(*modules: ModuleType) -> set[str]:
    """The reasons the code of `modules` ends a delivery with, as it writes them: the second
    argument of _FinalError, the first of TextRejectedError, a keyword `reason=` and the value a
    variable `reason` gets, each a string or a choice of strings. The radio's own refusals pass
    through from its answer and stay codes."""
    found: set[str] = set()

    def strings(*nodes: ast.AST | None) -> set[str]:
        return {
            leaf.value
            for node in nodes
            if node is not None
            for leaf in ast.walk(node)
            if isinstance(leaf, ast.Constant) and isinstance(leaf.value, str)
        }

    for module in modules:
        for each in ast.walk(ast.parse(inspect.getsource(module))):
            if isinstance(each, ast.Call):
                called = getattr(each.func, "id", getattr(each.func, "attr", ""))
                if called == "_FinalError":
                    found |= strings(*each.args[1:2])
                elif called == "TextRejectedError":
                    found |= strings(*each.args[:1])
                found |= strings(*(k.value for k in each.keywords if k.arg == "reason"))
            elif isinstance(each, ast.Assign):
                for target in each.targets:
                    if isinstance(target, ast.Name) and target.id == "reason":
                        found |= strings(each.value)
                    elif isinstance(target, ast.Tuple) and isinstance(each.value, ast.Tuple):
                        pairs = zip(target.elts, each.value.elts, strict=True)
                        found |= strings(
                            *(v for t, v in pairs if isinstance(t, ast.Name) and t.id == "reason")
                        )
    return found


def test_every_reason_the_gateway_publishes_has_words_on_the_dashboard() -> None:
    """The reasons come from the gateway, not from a list kept here: a refused command's from
    core.commands.Rejection (code_locked, #79, came so), a delivery's from the outbox's code and
    the text policy's. One the gateway adds later without words on the dashboard fails here,
    as one the API does not document; the radio's own refusals stay codes."""
    assert set(COMMAND_REASONS) == {reason.value for reason in Rejection}
    assert "code_locked" in COMMAND_REASONS
    written = _reasons_written(outbox, text_module)
    assert written == set(DELIVERY_REASONS)
    macros = [part.split("endmacro")[0] for part in shipped().split("macro why(v)")[1:]]
    for reasons, copies in ((written, 3), (set(COMMAND_REASONS), 1)):
        found = [m for m in macros if set(re.findall(r"v == '(\w+)'", m)) == reasons]
        assert len(found) == copies, sorted(reasons)


# which messages a chat's thread holds: a channel's by its index, a person's by the key
THREAD_FILTER = (
    "((e.get('kind') == 'channel' and e.get('channel_index') == chan) if chan >= 0 else "
    "(e.get('kind') == 'contact' and e.get('contact_key') == key))"
)
# the lines of the thread that pick, order, open and count its messages, by a word of each
THREAD_RULES = (
    "regex_replace(' #[0-9a-f]{8}$'",  # the delivery code dropped (PC 16)
    "set trouble = e.get(",  # what shows in yellow
    "set state = ",
    "ns.ids",  # the history's ids, and a pending message among them skipped (PC 20)
    "ns.rows",  # every message of the chat, by acceptance or arrival (D-17)
    "sort(attribute='k')",
    "ns.open",  # the open rows: at most 6, past the second within 360 characters (D-17)
    "set earlier",
    "ns.trouble",  # the problems among the folded ones
)


def test_latest_messages_keep_the_threads_rules_as_the_same_text() -> None:
    """Design §2.3, C2: Latest messages shows the same rows as a chat's thread, "one macro text,
    one rule to test": its lines that drop the delivery code, order the messages, skip a pending
    message the history holds and choose the open rows are the thread's, with every chat's
    messages in place of one chat's (M3, M47, M55 and M56 on its copy). The previews of Latest
    chats and All chats drop the code as the thread does."""
    found = list(_cards(shipped()))
    (thread,) = [card for card in found if any("macro row(e)" in line for line in card)]
    (latest,) = [card for card in found if any("macro entry(e)" in line for line in card)]

    def ruled(card: list[str]) -> list[str]:
        lines = [line for line in card if any(word in line for word in THREAD_RULES)]
        every = (line.replace(f" and {THREAD_FILTER}", "") for line in lines)
        return [line.replace(THREAD_FILTER, "true") for line in every]

    assert len(ruled(thread)) == 11
    assert ruled(latest) == ruled(thread)
    dropped = [line for card in found for line in card if THREAD_RULES[0] in line]
    assert len(dropped) == 4  # the thread, Latest messages, Latest chats, All chats
    assert len(set(dropped)) == 1


def test_the_contacts_cards_find_the_contacts_by_their_device_not_a_prefix(
    tmp_path: Path,
) -> None:
    """HA-06, D-31: the contacts table, the sensors and the lists of Contacts find this gateway's
    contacts by their devices below its own, never by an entity-ID prefix, so a prefix set later
    keeps them: the dashboard is the same whatever `[homeassistant] contact_entity_prefix`, and
    the template has no placeholder for it."""
    template = (Path(dashboard.__file__).parent / dashboard.TEMPLATE).read_text(encoding="utf-8")
    other = config(more='[homeassistant]\ncontact_entity_prefix = "mesh_2"\n')
    assert other.homeassistant.contact_entity_prefix == "mesh_2"
    assert dashboard.dashboard(other, tmp_path) == dashboard.dashboard(config(), tmp_path)
    assert "@contact@" not in template
    contacts = rules.load(rules.rendered(shipped()))["views"][2]
    table = next(t for t in rules.texts(contacts) if "<th>Asking</th>" in t)
    assert "select('is_device_attr', 'via_device_id', gw)" in table


@pytest.mark.parametrize("state", ["missing", "a directory", "not json"])
def test_without_a_readable_state_the_map_is_the_note(state: str, tmp_path: Path) -> None:
    if state == "a directory":
        (tmp_path / "state.json").mkdir()
    elif state == "not json":
        (tmp_path / "state.json").write_text("{", encoding="utf-8")
    text, count = dashboard.dashboard(config(), tmp_path)
    assert count == 0
    assert "Nobody had a position when this dashboard was made." in text
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
    assert "Nobody had a position" not in with_map
    without = render(shipped())
    assert "type: map" not in without
    assert "Nobody had a position when this dashboard was made." in without
    assert "> [!" not in with_map + without  # a coloured box, no GitHub alert (rule 11)
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
    assert first.endswith("needs Home Assistant 2026.10 or later.</li></ul></details>\n")  # Help
    assert not first.endswith("\n\n")


# --- a card that may be empty (the stage-5 browser check) ---------------------------------------

MAY_BE_EMPTY = (
    "views:\n"
    "  - title: Overview\n"
    "    path: overview\n"
    "    cards:\n"
    "      - type: markdown\n"
    "        text_only: true\n"
    "        show_empty: false\n"
    "{visibility}"
    "        content: |-\n"
    "          {%- if is_state('binary_sensor.@gw@_radio', 'off') -%}\n"
    "\n"
    '          <div>_("The radio is disconnected.")</div>\n'
    "          {%- endif -%}\n"
    "      - type: markdown\n"
    "        content: x\n"
)
GUARD = (
    "          - condition: template\n            value_template: >-\n              {{ true }}\n"
)
BODY = (
    "{%- if is_state('binary_sensor.meshcore_home_gateway_radio_1_radio', 'off') -%}\n"
    "\n"
    "<div>The radio is disconnected.</div>\n"
    "{%- endif -%}"
)
SHOWN = {
    "condition": "template",
    "value_template": "{%- set body -%}\n" + BODY + "\n{%- endset -%}\n{{ body | trim != '' }}",
}


@pytest.mark.parametrize(
    ("visibility", "conditions"),
    [
        ("", [SHOWN]),
        (
            "        visibility:\n" + GUARD,
            [{"condition": "template", "value_template": "{{ true }}"}, SHOWN],
        ),
    ],
)
def test_a_card_that_may_be_empty_shows_by_a_condition_of_its_content(
    visibility: str, conditions: list[dict[str, str]]
) -> None:
    """Home Assistant's card wrapper takes a Markdown card that hides itself (`show_empty:
    false`, nothing rendered) out of the page, and its template with it: it does not show again
    until the page is loaded again (`hui-card`'s `_setElementVisibility`). The stage-5 browser
    check saw it: the refused line under a chat's field stayed away after a press with nothing
    stored, and a problem card stayed away on an open Overview. A card that hides by its
    visibility conditions comes back once they hold, so the made card shows by a condition of its
    own content, after its other conditions, and no longer hides itself."""
    made = render(MAY_BE_EMPTY.replace("{visibility}", visibility))
    assert "show_empty" not in made
    first, second = rules.load(made)["views"][0]["cards"]
    assert first["visibility"] == conditions
    assert first["content"] == BODY
    assert second == {"type": "markdown", "content": "x"}


def test_a_card_that_may_be_empty_needs_its_content_last_as_a_block() -> None:
    with pytest.raises(TemplateError, match="content"):
        render(MAY_BE_EMPTY.replace("{visibility}", "").replace("content: |-", "content: >-"))


# --- the shipped template's rules ---------------------------------------------------------------

# a block scalar: `key: |-` (or `|`); its lines are those indented more than its key
_BLOCK = re.compile(r"^(\s*(?:- )?)[\w-]+: \|-?$")
_TAG = re.compile(r"\{\{.*?\}\}|\{%.*?%\}|\{#.*?#\}")
_PRINT = re.compile(r"\{\{\s*(.*?)\s*\}\}")
_SET = re.compile(r"\{%-?\s*set (\w+) = (.*?)\s*-?%\}")
_MACRO_START = re.compile(r"\{%-?\s*macro (\w+)\((\w+)(?:, \w+=\w+)*\)\s*-?%\}")
_ESCAPE = re.compile(r"\|\s*(?:e|escape|forceescape)\b")
_INTEGER = re.compile(
    r".*\|\s*(?:int|count)(?:\(\s*-?\d*\s*\))?|\[?\s*(\w+)\s*//\s*\d+(?:\s*,\s*\d+\s*\]\s*\|\s*max)?"
)
_YAML_11 = re.compile(r"(?i)y|n|yes|no|on|off|\d+(?:\.\d+)+\w*")
_PLACEHOLDERS = {
    "@gw@",
    "@node@",
    "@version@",
    "@major@",
    "@minor@",
    "@trackers@",
    "@chats@",
    "@models@",
    "@guide@",
    "@decimal@",
}
_JINJA_NOTE = re.compile(r"\{#-?\s*Translators:.*-?#\}")
_CLASS = re.compile(r"\{#- problem: \w+ -#\}")  # a problem card's class (rule 9)
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
        "node": list(NODE_TYPES.values()),
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
        for listed in ("@trackers@", "@chats@", "@models@"):
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
    """A card prints a value read from Home Assistant only through m(), mt() or a macro of the
    card, on a line that is one whole HTML element, and starts with the macro m (after the class
    of a problem card, rule 9); a macro prints only through m(), mt() and the macros above it;
    each mapping has a branch per value of the gateway's list and m() for a value added later."""
    found: list[str] = []
    texts = [text for _, text in block.lines]
    if texts and _CLASS.fullmatch(texts[0]):
        texts = texts[1:]
    macros: dict[str, tuple[str, list[str]]] = {}  # name: its parameter and its lines
    body: dict[int, str] = {}  # a macro's line: the macro
    current = ""
    for number, text in block.lines:
        if text == MACRO:
            continue
        if match := _MACRO_START.search(text):
            current = match.group(1)
            macros[current] = (match.group(2), [])
        if current:
            macros[current][1].append(text)
            body[number] = current
            if "endmacro" in text:
                current = ""
    order = list(macros)
    sets: dict[str, list[str]] = {}
    integers: set[str] = set()
    values: set[str] = set()
    for _, text in block.lines:
        for match in _SET.finditer(text):
            name, source = match.groups()
            sets.setdefault(name, []).append(source)
            call = re.fullmatch(r"(\w+)\(.*\)", source)
            if call is not None and (call.group(1) in ("m", "mt") or call.group(1) in macros):
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
                callee, caller = call.group(1), body[number]
                if order.index(callee) >= order.index(caller):
                    found.append(f"{number}: a macro prints {expression}, not one above it")
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
        added = (f"{{{{ m({parameter}) }}}}", f"<code>{{{{ m({parameter}) }}}}</code>")  # D-8
        if not any(line.endswith(f"{{%- else -%}}{a}") for line in lines for a in added):
            found.append(f"{name}: no m({parameter}) for a value added later")
    return found


def _lines(template: str) -> Iterator[tuple[int, str]]:
    """Every line of the cards' contents, by number, without indentation."""
    for block in blocks(template.split("\n")):
        yield from block.lines


def test_the_shipped_template_keeps_its_rules() -> None:
    template = shipped()
    assert rule_findings(template) == []
    names = {
        name
        for block in blocks(template.split("\n"))
        for _, t in block.lines
        for name in re.findall(r"macro (\w+)\(", t)
    }
    assert {
        "m",
        "mt",
        "num",
        "when",
        "mark",
        "why",
        "problem",
        "place",
        "telemetry",
        "what",
    } <= names
    assert {line for _, line in _lines(template) if rules.NUM_MACRO.search(line)} == {NUM}
    assert template.count(MACRO) == 43
    assert template.count(NUM) == 7
    # the number fields of rule 22: those the template sets through num()
    assert {
        name
        for name, value in re.findall(r"set (\w+) = (.*?)\s*-?%\}", template)
        if value.startswith("num(")
    } == rules.NUMBERS
    assert len(list(dashboard.MARKER.finditer(template))) > 70


SETTINGS = '            heading: _("Settings")\n'
LISTED = "{%- macro listed(v) -%}{%- for k in v[:3] -%}{{ who(k) }}"


@pytest.mark.parametrize(
    ("old", "new", "finding"),
    [
        (
            '              <div>_("No contact event yet.")</div>',
            '              <div>{{ m(_("No contact event yet.")) }}</div>',
            "a marker inside a Jinja tag",
        ),
        ("<b>{{ m(r.title) }}</b>", "<b>{{ r.title | e }}</b>", "| e outside the macro m"),
        ("<b>{{ m(r.title) }}</b>", "<b>{{ r.title }}</b>", "is neither m() nor a mapping"),
        (
            " | regex_replace('[\\t\\r\\n\\u2028\\u2029]', ' ') | e }}",
            " | e }}",
            "starts with the macro m",
        ),
        (
            '              <div>_("No contact event yet.")</div>',
            '              # Translators: x\n              <div>_("No contact event yet.")</div>',
            "'# Translators:' line",
        ),
        (
            "              {%- elif v == 'away' -%}_(\"no answer lately\")\n",
            "",
            "telemetry: the branches",
        ),
        ("{%- else -%}{{ m(v) }}", "{%- else -%}{{ v }}", "neither m() nor a mapping"),
        (
            '              <details><summary>_("What this gateway does")',
            (
                '              > {{ m(states("sensor.x")) }}\n'
                '              <details><summary>_("What this gateway does")'
            ),
            "an alert line prints a value",
        ),
        (SETTINGS, SETTINGS + "            show_state: on\n", "on unquoted"),
        (SETTINGS, SETTINGS + "            version: @version@\n", "unquoted"),
        ("binary_sensor.@gw@_radio", "binary_sensor.@gx@_radio", "the placeholder @gx@"),
        ("{waiting}.", "{waitings}.", "{waitings} is set nowhere"),
        ("{%- set running = m(", "{%- set running = (", "set from neither m() nor integers"),
        ("{%- set first = problem(", "{%- set first = other(", "{first} is set from neither"),
        (LISTED, LISTED.replace("who(k)", "listed(k)"), "not one above it"),
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
    """After a problem card's class (rule 9)."""
    printing = [card for card in _cards(shipped()) if any(re.search(r"\bm\(", t) for t in card)]
    assert len(printing) == 43
    assert all(card[_CLASS.fullmatch(card[0]) is not None] == MACRO for card in printing)
    assert sum(_CLASS.fullmatch(card[0]) is not None for card in printing) == 8


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
        "write": True,  # whether it has a field: its entity IDs stand only in its footer's guard
        "room": 158,
    }
    assert listed[1]["aliases"] == ["alarm"]
    assert listed[1]["write"] is False
    assert pages('x ["@chats@"]\n') == "x []\n"


@pytest.mark.real_catalogs
def test_the_models_placeholder_is_each_model_with_its_type(tmp_path: Path) -> None:
    """D-7: Contacts says a contact's type in the words of a chat page, and Home Assistant knows
    only its device's model, which the gateway names in [homeassistant] language (ADR 0019):
    ["@models@"] is the list of each model as the gateway names it, with its node type
    (core.mesh.NODE_TYPES), for Jinja's dict()."""
    pairs = [("Companion", "chat"), ("Room server", "room")]
    out = dashboard.render(
        '{%- set models = dict(["@models@"]) -%}\n',
        node_id="radio-1",
        version="1.2.3",
        trackers=[],
        text=ENGLISH,
        models=pairs,
    )
    assert out == '{%- set models = dict([["Companion", "chat"], ["Room server", "room"]]) -%}\n'
    assert pages('x ["@models@"]\n') == "x []\n"
    for wrong in [("<b>Companion</b>", "chat"), ("Companion", "chat }}")]:
        with pytest.raises(TemplateError, match="a model"):
            pages('x ["@models@"]\n', models=[wrong])
    english = [("Companion", "chat"), ("Repeater", "repeater"), ("Room server", "room")]
    assert dashboard.models(i18n.ENGLISH) == [*english, ("Sensor", "sensor")]
    text, _ = dashboard.dashboard(config(), tmp_path)
    assert json.dumps([[*pair] for pair in dashboard.models(i18n.ENGLISH)]) in text


@pytest.mark.real_catalogs
def test_a_model_the_guard_refuses_is_named_in_english() -> None:
    """A model's name goes into a card's Jinja: one that holds what no text of the dashboard may
    (dashboard.unsafe(), the catalog check's rule) stays out, its English name in its place; a
    device of that model then shows its model through m()."""
    names = {"Companion": "<b>Gefährte</b>", "Repeater": "Wiederholer"}
    entries = [Entry(k, (v,), i18n.HOME_ASSISTANT) for k, v in names.items()]
    assert dashboard.models(compiled("de", entries))[:2] == [
        ("Companion", "chat"),
        ("Wiederholer", "repeater"),
    ]


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


def test_the_room_is_the_drafts_max_before_the_radio_is_known() -> None:
    """The room a chat page names is the `max` the gateway announces for the chat's draft while
    it does not know the radio's name (discovery.text_room): with `ascii_only`, a target's prefix
    and suffix count as the ASCII they are sent as ("ö" as "oe", the en dash as "-")."""
    affixes = 'prefix = "Home: "\n'
    more = HOUSEHOLD.replace(affixes, 'prefix = "Größe: "\nsuffix = " \u2013 Grüße"\n')
    gateway = config(more=more + "[messages]\nascii_only = true\n")
    for target in [*gateway.targets, None]:
        assert dashboard.room(gateway, target) == text_room(gateway, target, None)
    bob = next(target for target in gateway.targets if target.alias == "bob")
    assert dashboard.room(gateway, bob) == 160 - len("Groesse: ") - len(" - Gruesse")


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


LONG = "Garden shed repeater on the north side"  # its slug has the 32 characters a slug may have


def test_a_slug_the_gateway_made_unique_keeps_its_page_and_controls(tmp_path: Path) -> None:
    """Two companions of one long name: the gateway gives the second the slug of the first and
    `_` with four digits of its key (mqtt/contact_devices.py), 37 characters. Its title is cut to
    32 and keeps the digits, on its page and on its switch and button."""
    contacts, _ = devices(tmp_path, clock=Clock())
    contacts.update(mesh_of(node(ALICE, LONG), node(BOB, LONG)))
    stopped(tmp_path)
    records = ContactDeviceRecords(JsonStateFile(tmp_path / "state.json", read_only=True))
    first, second = sorted(record.slug for record in records.announced.values())
    assert (len(first), second) == (32, f"{first}_{BOB[:4]}")
    chats = dashboard.chats(config(), records)
    assert chats.skipped == ()
    titles = ["Garden shed repeater on the nort", f"Garden shed repeater on the {BOB[:4]}"]
    assert [(c.path, c.title) for c in chats.pages] == [
        (f"contact-{first}", titles[0]),
        (f"contact-{second}", titles[1]),
    ]
    assert [item["title"] for item in dashboard.switches(records)] == titles
    assert [item["title"] for item in dashboard.removes(records)] == titles
    assert dashboard.left_out(records) == ()


def test_a_control_whose_values_fail_their_patterns_is_reported(tmp_path: Path) -> None:
    """A switch or button of a slug the gateway never gives is left out, and the command line
    says so (D-39), as it says so of a page."""
    records = household(tmp_path)
    assert dashboard.left_out(records, "mesh_2") == (("entity", 'button.mesh_2_x"]\n- y_remove'),)
    assert dashboard.build(config(), tmp_path).left_out == dashboard.left_out(records)


def test_a_contact_targets_page_is_not_capped(tmp_path: Path) -> None:
    """A contact target whose contact has a device has that person's page, with the target's
    field: the cap leaves it, as it leaves every target's page, and cuts the other people."""
    announced = {f"{i:02x}" * 32: record(f"p{i:02d}", send="notify") for i in range(31)}
    announced[GATE] = record("zz_gate", send="notify")
    gate = f'[targets.gate]\ncontact = "{GATE}"\n'
    chats = dashboard.chats(config(more=gate), state(tmp_path, announced))
    assert [c.path for c in chats.pages[-2:]] == ["contact-p29", "contact-zz_gate"]
    assert (chats.pages[-1].target, chats.pages[-1].write) == ("gate", f"text.{GW}_write_gate")
    assert (chats.people, chats.without_page) == (31, 1)


def test_a_channels_page_is_led_by_its_first_visible_alias(tmp_path: Path) -> None:
    """alarm comes first on channel 1 but is hidden: family leads the page and gives it its
    field, and the page lists both aliases."""
    more = (
        "[targets.alarm]\nchannel = 1\n[targets.family]\nchannel = 1\n"
        '[homeassistant]\nhidden_targets = ["alarm"]\n'
    )
    (chat,) = dashboard.chats(config(more=more), state(tmp_path, {})).pages
    assert chat.item() == page(
        "chat-family",
        "Family",
        target="family",
        aliases="alarm,family",
        write=f"text.{GW}_write_family",
        send=f"button.{GW}_write_family_send",
    )


def test_the_public_channel_is_a_channel(tmp_path: Path) -> None:
    """Channel 0, the public channel, is a channel's page, not a person's."""
    chats = dashboard.chats(config(more="[targets.public]\nchannel = 0\n"), state(tmp_path, {}))
    assert [(c.path, c.channel) for c in chats.pages] == [("chat-public", 0)]
    assert (chats.channels, chats.people) == (1, 0)


def test_a_path_taken_skips_the_second_page(tmp_path: Path) -> None:
    """Two contacts of one slug (a state the gateway does not write): the first by key has the
    page, and the command line names the path of the other."""
    records = state(tmp_path, {BOB: record("x", send="notify"), ALICE: record("x", send="notify")})
    chats = dashboard.chats(config(), records)
    assert [(c.path, c.key) for c in chats.pages] == [("contact-x", ALICE)]
    assert chats.skipped == (("path", "contact-x"),)


@pytest.mark.parametrize(
    "content", [b"{not json", b"[]", b"\xff"], ids=["not json", "a list", "not utf-8"]
)
def test_a_state_that_cannot_be_read_gives_no_time(content: bytes, tmp_path: Path) -> None:
    """A state that reads as no contacts has no time to report: the command line does not say
    that its contacts are those of a state it could not read."""
    (tmp_path / "state.json").write_bytes(content)
    built = dashboard.build(config(), tmp_path)
    assert (built.chats.people, built.written_at) == (0, None)


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


def test_the_shipped_template_keeps_the_rules_of_2_1() -> None:
    """The numbered rules on the shipped template: every view of 2.1 keeps every one of them
    (stage 5 wrote the cards of 2.0 again)."""
    assert rules.findings(shipped()) == []


def test_the_refused_line_under_the_thread_breaks_rules_8_and_18() -> None:
    """#143: under the thread, the footer, which sticks to the bottom of the screen, covered the
    refused line until the page was scrolled. Moved back there on the made dashboard, the line is
    not above the thread (rule 18), and it has no help of its own (rule 8)."""
    template = shipped()
    loaded = rules.load(rules.rendered(template))
    [view] = [v for v in loaded["views"] if v["path"] == "chat-family"]
    [section] = view["sections"]
    listed = section["cards"]
    [line] = [card for card in listed if rules.is_refused_line(card)]
    listed.remove(line)
    [thread] = [n for n, card in enumerate(listed) if rules.is_thread(card)]
    listed.insert(thread + 1, line)
    assert rules.rule_18(template, None, loaded, rules.HOUSEHOLD) == [
        "rule 18: chat-family: the refused line is not above the thread"
    ]
    assert rules.rule_8(template, None, loaded, rules.HOUSEHOLD) == [
        "rule 8: chat-family: a markdown card without help"
    ]
    # the thread first and the line last: no card is above the thread, the last one is not
    listed.remove(line)
    del listed[:thread]
    listed.append(line)
    assert rules.rule_18(template, None, loaded, rules.HOUSEHOLD) == [
        "rule 18: chat-family: the refused line is not above the thread"
    ]


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


def test_help_opens_from_the_about_of_each_tab_and_from_every_chat_page() -> None:
    """D-5: Help opens from each view's "About this view": the last card of Overview, Chats,
    Contacts, Map, Gateway and Settings, and on a chat page the help under its thread, whose
    reasons in words Help explains ("Why a message was not sent")."""
    views = rules.load(rules.rendered(shipped()))["views"]
    link = '<a href="./help">Help</a>'
    for view in views[:6]:
        about = view["sections"][-1]["cards"][-1]["content"].split("\n")[-1]
        assert about.startswith("<details>"), view["path"]
        assert link in about, view["path"]
    for view in views[7:]:
        helps = [
            line
            for section in view["sections"]
            for card in section["cards"]
            for line in str(card.get("content", "")).split("\n")
            if line.startswith("<details><summary>What ✓ and ✓✓ mean")
        ]
        assert len(helps) == 1, view["path"]
        assert link in helps[0], view["path"]


def test_a_text_with_a_field_tells_the_translators_what_the_field_holds() -> None:
    """Every text of the dashboard with a field has a translators' note (docs/development.md,
    Translations); one that asks for the keyboard's key says which word to use, with ↵ (D-65)."""
    _, entries = read_po(POT.read_text(encoding="utf-8"))
    dashboard_ = [e for e in entries if e.msgctxt == i18n.DASHBOARD]
    assert [e.msgid for e in dashboard_ if _fields(e.msgid) and not e.comments] == []
    for entry in dashboard_:
        if rules.KEY in entry.msgid:
            note = " ".join(entry.comments)
            assert "Return (↵)" in note, entry.msgid
            assert rules.KEY_NAME in note, entry.msgid


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
    pages = {link.removeprefix("./") for link in links if not link.startswith(("./{{", "http"))}
    assert sorted(pages) == [
        "contacts",
        "gateway",
        "help",
        "map",
        "messages",
        "overview",
        "settings",
    ]
    assert pages <= paths
    # each chat of Latest chats and All chats, from the list of the chat pages
    assert {link for link in links if link.startswith("./{{")} == {"./{{ m(r.path) }}"}
    # the guide's section, of this dashboard's version (D-30)
    guide = rules.GUIDE_URL.format(version=rules.VERSION)
    sections = {guide + "#a-second-gateway", guide + "#privacy"}
    assert {link for link in links if link.startswith("http")} == sections
    assert rules.rule_6(shipped(), None, loaded, rules.HOUSEHOLD) == []


MT = "{%- macro mt(x) -%}{{ m(x) }}{%- endmacro -%}"
GUIDE_LINK = rules.GUIDE_URL.replace("{version}", "@version@")
HELPED = '<details><summary>_("Help")</summary>_("Help")</details>'
# a chat page's field, the footer's first card, and a Markdown card put in front of it
FIELD = (
    '          - type: entities\n            entities:\n              - entity: "@chats.write@"\n'
)
IN_FOOTER = "          - type: markdown\n            content: x\n"
# the first two links of the row under Overview's status line, and the row
MAP_LINK, GATEWAY_LINK = '<a href="./map">_("Map")</a>', '<a href="./gateway">_("Gateway")</a>'
ROW = (
    f'<div>{MAP_LINK} · {GATEWAY_LINK} · <a href="./settings">_("Settings")</a> · '
    '<a href="./help">_("Help")</a></div>'
)
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
    # rule 3, per entity of a footer: neither tested to exist, the field's state not tested,
    # the button not on the field's device
    (
        [
            "@@",
            (
                "and states[t] is not none and not is_state(t, 'unavailable')\n"
                "                 and states[b] is not none and not is_state(b, 'unavailable')"
            ),
            (
                "and not is_state(t, 'unavailable')\n"
                "                 and not is_state(b, 'unavailable')"
            ),
        ],
        "rule 3: text.mesh_alice_write: its guard lacks states[…] is not none",
    ),
    (
        [
            "@@",
            "and states[t] is not none and not is_state(t, 'unavailable')",
            "and states[t] is not none",
        ],
        "rule 3: text.mesh_alice_write: its guard lacks not is_state(…, 'unavailable')",
    ),
    (
        ["@@", "d is not none and device_id(b) == d", "d is not none"],
        "rule 3: button.mesh_alice_write_send: its guard lacks its device",
    ),
    (
        ["@@", "d is not none and device_id(b) == d", "d is not none"],
        f"rule 3: button.{rules.GW}_write_family_send: its guard lacks its device",
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
            "              - button.mesh_carol_remove",
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
    # rule 6: a chat page's link outside the list of the chats, or not from a loop over it
    (
        card('<div><a href="./{{ x.path }}">_("Help")</a></div>'),
        "rule 6: a link nowhere in this dashboard: ./{{ x.path }}",
    ),
    (
        card(
            '{%- set listed = ["@chats@"] -%}',
            '<div><a href="./{{ x.path }}">_("Help")</a></div>',
        ),
        "rule 6: a link nowhere in this dashboard: ./{{ x.path }}",
    ),
    (
        card(
            "{%- for x in states.sensor -%}",
            '<div><a href="./{{ x.path }}">_("Help")</a></div>',
            "{%- endfor -%}",
        ),
        "rule 6: a link nowhere in this dashboard: ./{{ x.path }}",
    ),
    # rule 7: a marker in an attribute
    (card('<div><a href="./help" title="_("Help")">x</a></div>'), "rule 7: "),
    # rule 8: a card without help, where no card with help follows it
    (
        ["@@", SETTINGS, SETTINGS + "\n".join(card('<div>_("Help")</div>')) + "\n"],
        "rule 8: settings: a markdown card without help",
    ),
    # rule 8: a Markdown card holds its own help, also when a card with help follows it (M19)
    (
        [
            "@@",
            SETTINGS,
            SETTINGS
            + "\n".join([*card('<div>_("Help")</div>'), *card(HELPED, text_only="true")])
            + "\n",
        ],
        "rule 8: settings: a markdown card without help",
    ),
    # rule 9: a problem card missing from the status line's order, a second status line
    (["@@", "{#- problem: clock -#}", "{#- problem: time -#}"], "rule 9: the status line has"),
    (card("{%- macro problem(v) -%}", "{%- endmacro -%}"), "rule 9: 2 status cards"),
    # rule 11: a GitHub alert
    (card("> [!NOTE]", '> _("Help")'), "rule 11: "),
    # rule 12: the row under Overview's status line without the map, with the map last, twice
    (
        ["@@", f"{MAP_LINK} · {GATEWAY_LINK}", GATEWAY_LINK],
        "rule 12: the links under overview's status line are ['gateway', 'settings', 'help']",
    ),
    (
        ["@@", f"{MAP_LINK} · {GATEWAY_LINK}", GATEWAY_LINK.replace("</a>", f"</a> · {MAP_LINK}")],
        "rule 12: the links under overview's status line are ['gateway', 'map', 'settings',",
    ),
    (
        ["@@", f"<div>{MAP_LINK} · ", f"<div>{MAP_LINK}</div>\n              <div>{MAP_LINK} · "],
        "rule 12: 2 rows of links under overview's status line",
    ),
    # rule 12: the row in a condition that never holds, or in a fold over several lines
    (
        ["@@", ROW, f"{{%- if false %}}\n              {ROW}\n              {{%- endif %}}"],
        "rule 12: the row of links under overview's status line is inside",
    ),
    (
        [
            "@@",
            ROW,
            f'<details><summary>_("Help")</summary>\n              {ROW}\n              </details>',
        ],
        "rule 12: the row of links under overview's status line is inside",
    ),
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
    # rule 18: the refused line shows while empty, is not above the thread, or is in the
    # footer again, where it pushed the Send tile below a phone's screen (H-27)
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
    (
        ["@@", "{%- set e = 'event.@gw@_delivery' -%}", "{%- set e = 'event.@gw@_message' -%}"],
        "rule 18: chat-family: the refused line is not above the thread",
    ),
    (
        ["@@", f"        cards:\n{FIELD}", f"        cards:\n{IN_FOOTER}{FIELD}"],
        "rule 18: chat-family: the footer is vertical-stack of ['markdown', 'entities', 'tile']",
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
    # rule 22: two forms of num(), num() without its macro, a number formatted with '%.', a
    # number with a fraction through m(): a value of Home Assistant, and one whose key names a
    # unit
    (
        card(
            MACRO, NUM.replace("f % 1 == 0", "f == f | int"), "<div>{{ num(states('s.x')) }}</div>"
        ),
        "rule 22: 2 forms of num()",
    ),
    (
        card(
            MACRO,
            "{% macro num(v) -%}{{ m(v) }}{%- endmacro -%}",
            "<div>{{ num(states('sensor.x')) }}</div>",
        ),
        "rule 22: 2 forms of num()",
    ),
    (card(MACRO, "<div>{{ num(states('sensor.x')) }}</div>"), "num() without its macro"),
    (
        card(MACRO, "<div>{{ m('%.1f' | format(states('sensor.x') | float(0))) }}</div>"),
        "a number formatted with '%.'",
    ),
    (
        card(
            MACRO, "{%- set v = state_attr('sensor.x', 'lag') -%}", "<div>{{ m(v | string) }}</div>"
        ),
        "m(v | string) of a number",
    ),
    (
        card(MACRO, "<div>{{ m(state_attr('binary_sensor.@gw@_radio', 'frequency_mhz')) }}</div>"),
        "frequency_mhz through m(), not num()",
    ),
    # rule 22: a number field printed as it is, or not set in its card
    (
        card(MACRO, NUM, "{%- set volts = m(states('sensor.x')) -%}", "<div>{{ volts }}</div>"),
        "the number {volts} not set through num()",
    ),
    (card(MACRO, NUM, '<div>_("{volts} V")</div>'), "the number {volts} not set through num()"),
    (
        card(
            MACRO,
            NUM,
            "{%- set volts = num(states('sensor.x'), 1) -%}",
            "{%- set volts = m(states('sensor.y')) -%}",
            '<div>_("{volts} V")</div>',
        ),
        "the number {volts} not set through num()",
    ),
    # rule 22: a number field set through m() from a variable: a contact's battery, the lag
    (
        ["@@", "{%- set volts = num(r.battery, 1) -%}", "{%- set volts = m(r.battery) -%}"],
        "the number {volts} not set through num()",
    ),
    (
        [
            "@@",
            "{%- set slow = num(lag if lag is number else none, 1) -%}",
            "{%- set slow = m(lag) -%}",
        ],
        "the number {slow} not set through num()",
    ),
]


@pytest.mark.parametrize(
    ("text", "depth"),
    [
        ("{#- Translators: the <details> below -#}\n{%- if x %}a{% endif %}\n<!-- {% if -->", 0),
        ("{%- macro m(x) -%}{{ x }}{%- endmacro -%}\n{%- for e in y if e -%}\n{%- endfor -%}", 0),
        ("{%- if x -%}\n{%- elif y -%}\n{%- else -%}", 1),
        ("<details><summary>a</summary>\n{%- for e in y %}", 2),
        ("{#- the row is hidden:\n", 1),
        ("{#- a comment\nover two lines -#}", 0),
        ("<!-- the row is hidden:\n", 1),
        ("{#- a comment that names\n{% if %} over two lines -#}", 0),
        ("<!-- a comment that names\n<details> over two lines -->", 0),
        ("<!-- a comment closed as HTML allows --!>", 0),
        ("<!-- the row is hidden, not by --!\n", 1),
    ],
)
def test_rule_12_counts_the_blocks_open_before_the_row(text: str, depth: int) -> None:
    """Rule 12: the row under Overview's status line stands at the top level of its card, outside
    every {% if %}, {% for %}, macro, <details> and comment that is still open; what a closed
    comment names opens nothing, HTML's comment also ends at --!>, and an elif or else opens no
    block."""
    assert rules._depth(text) == depth


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
        card("{#- problem: taken_over -#}", '_("Help")', HELPED),
        card(
            MACRO,
            NUM,
            "{%- set n = states('sensor.x') | int(0) -%}",
            "<div>{{ m(n | string) }} {{ num(states('sensor.y'), 1) }}</div>",
            HELPED,
        ),
        [  # a tile's help in the text-only card after it (design D-10)
            "          - type: tile",
            "            entity: sensor.@gw@_nodes",
            *card(HELPED, text_only="true"),
        ],
    ],
)
def test_what_a_rule_allows_adds_no_finding(change: list[str]) -> None:
    """A link to the guide's anchor of this version, the menus in a line for the administrator,
    a card with its help, a table of presentation, a problem card of a class that the status
    line tests before its problems, such as a takeover (rule 9), a whole number through m() and
    a number through num() (rule 22)."""
    assert added(into_help(*change)) == []


@pytest.mark.real_catalogs
def test_a_card_that_loops_over_the_chats_may_link_each_to_its_page() -> None:
    """Rule 6: `./{{ x.path }}` resolves in a card that loops over the chats list (stage 4's All
    chats), each x one of its pages."""
    allowed = card(
        '{%- for x in ["@chats@"] -%}',
        '<div><a href="./{{ x.path }}">_("Help")</a></div>',
        "{%- endfor -%}",
    )
    assert not [f for f in added(into_help(*allowed)) if f.startswith("rule 6")]


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


def test_the_name_beside_send_fits_a_phone_in_every_shipped_language() -> None:
    """The tile beside a chat's Send button names the key and the button (rules.KEY_NAME); at
    360 px it has room for the English name, 17 letters. A shipped catalog's name has no more:
    German's "Eingabe, dann Senden" was cut in the browser check."""
    for language in i18n.LANGUAGES:
        name = rules.said(i18n.translator(language), rules.KEY_NAME)
        assert len(name) <= rules.SEND_NAME_LETTERS, (language, name)
    assert len(rules.said(i18n.ENGLISH, rules.KEY_NAME)) == rules.SEND_NAME_LETTERS


def test_the_advert_rows_label_fits_a_phone_in_every_shipped_language() -> None:
    """A shipped catalog's label of the row has no more letters than the English one: German's
    "Advert an Funkgeräte in der Nähe" was cut beside "Senden" in the browser check."""
    for language in i18n.LANGUAGES:
        label = rules.said(i18n.translator(language), rules.ADVERT_ROW)
        assert len(label) <= len(rules.ADVERT_ROW), (language, label)
    assert len(rules.ADVERT_ROW) == 23


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


# the chat's help and the line of a Send without text, which name the button and the key
HOW = next(help_ for help_, labels in rules.QUOTES.items() if "Write to {name}" in labels)
NOTHING = next(help_ for help_ in rules.QUOTES if help_.startswith("Nothing was sent"))


def alike(texts: Mapping[str, str]) -> dict[str, str]:
    """These translations, and each other text of QUOTES with the labels these translate as
    they translate them: a catalog that says every label as its control does."""
    out = dict(texts)
    for help_, labels in rules.QUOTES.items():
        if help_ not in out:
            out[help_] = help_
            for label in sorted(labels, key=len, reverse=True):
                out[help_] = out[help_].replace(label, texts.get(label, label))
    return out


GERMAN = {  # a catalog that says the labels and the key as its controls do
    "Send": "Abschicken",
    "Contacts": "Kontakte",
    "Return, then Send": "Eingabe, dann Abschicken",
    "Write to {name}": "An {name} schreiben",
    HOW: (
        "Tippe ins Feld „An {name} schreiben“ unten, tippe Eingabe (↵), dann Abschicken. "
        "Eingabe behält nur den Text; Abschicken sendet ihn."
    ),
    NOTHING: "Nichts gesendet. Tippe im Feld Eingabe (↵), dann Abschicken.",
    "Remove": "Entfernen",
    "Remove a contact": "Entfernen eines Kontakts",
    "Remove {name} from the radio?": "Entfernen von {name} aus dem Funkgerät?",
    "Paste a key or contact link into the field above, then tap Return (↵).": (
        "Füge einen Schlüssel oder Kontaktlink ins Feld oben ein, dann tippe Eingabe (↵)."
    ),
    "A key, or a contact link from the MeshCore app (Share contact), is added when you tap "
    "Return (↵).": (
        "Ein Schlüssel oder ein Kontaktlink aus der MeshCore-App (Kontakt teilen) kommt dazu, "
        "wenn du Eingabe (↵) tippst."
    ),
    "At the bottom of a chat's page: type, tap Return (↵), then tap Send. Return only keeps the "
    "text at the gateway; Send sends it, once.": (
        "Unten auf der Seite eines Chats: tippe, dann Eingabe (↵), dann Abschicken. Eingabe "
        "behält den Text nur am Gateway; Abschicken sendet ihn, einmal."
    ),
    "On Contacts: paste a key or a contact link from the MeshCore app, then tap Return (↵).": (
        "Unter Kontakte: füge einen Schlüssel oder Kontaktlink aus der MeshCore-App ein, dann "
        "tippe Eingabe (↵)."
    ),
}
GERMAN = alike(GERMAN)


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
    assert rules.quote_findings(template, {quoted: ("Send",)}, [i18n.ENGLISH]) == []
    assert rules.quote_findings(template, {quoted: ("Send",)}, [german]) == [
        "rule 16: de: 'Tippe, dann „Senden“.' does not say 'Abschicken'"
    ]
    fixed = compiled(
        "de",
        [
            Entry("Send", ("Abschicken",), i18n.DASHBOARD),
            Entry(quoted, ("Tippe, dann „Abschicken“.",), i18n.DASHBOARD),
        ],
    )
    assert rules.quote_findings(template, {quoted: ("Send",)}, [fixed]) == []
    bare = template.replace(help_, "Type, then Send.")  # D-63: a label of NAMED, bare
    assert rules.quote_findings(bare, {}, [i18n.ENGLISH]) == [
        "rule 16: 'Type, then Send.' names 'Send', not in QUOTES"
    ]
    plain = {"Type, then Send.": ("Send",)}  # the word in its plain sense
    assert rules.quote_findings(bare, {}, [i18n.ENGLISH], plain) == []
    other = bare.replace('_("Send")', '_("Close")').replace("then Send", "then Close")
    assert rules.quote_findings(other, {}, [i18n.ENGLISH]) == []  # Close is not of NAMED
    named = other + '    name: _("Tap \\"Help\\" first")\n'  # a quote in a whole value
    assert rules.quote_findings(named, {}, [i18n.ENGLISH]) == [
        "rule 16: 'Tap \"Help\" first' quotes 'Help', not in QUOTES"
    ]


@pytest.mark.real_catalogs
def test_a_quoted_label_may_change_the_case_of_its_first_letter() -> None:
    """Rule 16: a language's grammar may write a label's first letter in the other case, as a
    German heading ends in its button's verb: "Kontakt entfernen" says "Entfernen". Another word,
    or another letter in the other case, is another word."""
    template = (
        "views:\n"
        '  - title: _("Contacts")\n'
        '    action_name: _("Remove")\n'
        "    content: |-\n"
        '      <div>_("Remove a contact")</div>\n'
    )
    quotes = {"Remove a contact": ("Remove",)}

    def german(heading: str) -> i18n.Translator:
        return compiled(
            "de",
            [
                Entry("Remove", ("Entfernen",), i18n.DASHBOARD),
                Entry("Remove a contact", (heading,), i18n.DASHBOARD),
            ],
        )

    assert rules.quote_findings(template, quotes, [german("Kontakt entfernen")]) == []
    assert rules.quote_findings(template, quotes, [german("Entfernen eines Kontakts")]) == []
    for other in ("Kontakt löschen", "Kontakt eNtfernen"):
        assert rules.quote_findings(template, quotes, [german(other)]) == [
            f"rule 16: de: {other!r} does not say 'Entfernen'"
        ]


@pytest.mark.real_catalogs
def test_the_name_beside_send_says_the_buttons_label() -> None:
    """Rule 16 on the shipped template: "Return, then Send", the name beside the Send button,
    names the button's label, so it is in QUOTES; a catalog whose name says another word than
    its button fails (§6.2). A whole value that names a button's label bare needs QUOTES too;
    a heading that shares a word with another label does not."""
    assert rules.QUOTES["Return, then Send"] == ("Send",)
    texts = {**GERMAN, "Return, then Send": "Eingabe, dann Senden"}
    german = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in texts.items()])
    assert rules.quote_findings(shipped(), rules.QUOTES, [german], rules.PLAIN) == [
        "rule 16: de: 'Eingabe, dann Senden' does not say 'Abschicken'"
    ]
    fixed = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in GERMAN.items()])
    assert rules.quote_findings(shipped(), rules.QUOTES, [fixed, i18n.ENGLISH], rules.PLAIN) == []
    found = rules.quote_findings(shipped(), {}, [i18n.ENGLISH], rules.PLAIN)
    for finding in (
        "rule 16: 'Return, then Send' names 'Send', not in QUOTES",  # also on Help
        f"rule 16: {HOW!r} quotes 'Write to {{name}}', not in QUOTES",
        "rule 16: 'Remove a contact' names 'Remove', not in QUOTES",
        "rule 16: 'Remove {name} from the radio?' names 'Remove', not in QUOTES",
    ):
        assert finding in found
    assert len(found) == sum(len(labels) for labels in rules.QUOTES.values())  # each one needed


@pytest.mark.real_catalogs
def test_a_label_named_without_quote_marks_is_said_as_its_control_says_it() -> None:
    """Rule 16 (D-63, HC 19): a text that names a label of NAMED by its words, with or without
    quote marks, is in QUOTES: a catalog whose button says "Wieder anzeigen" while the help
    says "Erneut zeigen" fails, and so for Ask for position and battery and Add a contact. A
    text that says the words in their plain sense is in PLAIN; an entry of PLAIN whose text no
    longer says them is found."""
    labels = {
        "Show again": "Wieder anzeigen",
        "Ask for position and battery": "Nach Position und Akku fragen",
        "Add a contact": "Kontakt hinzufügen",
    }
    drifting = {
        "Show again brings back all hidden contacts.": (
            "Erneut zeigen holt alle verborgenen Kontakte zurück."
        ),
        "Someone turned Ask for position and battery off or on for this person.": (
            "Jemand hat das Abfragen von Standort und Batterie für diese Person geändert."
        ),
        "To write back from here, add that person under Add a contact.": (
            "Um von hier zurückzuschreiben, nimm die Person unter Neuer Kontakt auf."
        ),
    }
    texts = alike(labels) | drifting
    german = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in texts.items()])
    found = rules.quote_findings(shipped(), rules.QUOTES, [german], rules.PLAIN)
    assert sorted(found) == sorted(
        f"rule 16: de: {text!r} does not say {labels[label]!r}"
        for text, label in zip(drifting.values(), labels, strict=True)
    )
    fixed = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in alike(labels).items()])
    assert rules.quote_findings(shipped(), rules.QUOTES, [fixed], rules.PLAIN) == []
    gone = {**rules.PLAIN, "Gone.": ("Send",)}
    assert rules.quote_findings(shipped(), rules.QUOTES, [i18n.ENGLISH], gone) == [
        "rule 16: PLAIN has 'Gone.' with 'Send', which it does not say"
    ]


@pytest.mark.real_catalogs
def test_the_help_says_send_and_return_as_the_button_and_its_name_do() -> None:
    """Rule 16 (D-63, D-65): the chat's help and the line for a Send without text name the
    button Send as it is labelled, and the keyboard's key before ↵ in the word of the name
    beside the button; a German catalog whose help says "Senden" while the button says
    "Abschicken", or "Zeilenumbruch" while the name says "Eingabe", fails."""
    assert rules.QUOTES[HOW] == ("Write to {name}", "Send")
    assert rules.QUOTES[NOTHING] == ("Send",)
    texts = {**GERMAN, HOW: GERMAN[HOW].replace("Abschicken", "Senden")}
    german = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in texts.items()])
    assert rules.quote_findings(shipped(), rules.QUOTES, [german], rules.PLAIN) == [
        f"rule 16: de: {texts[HOW]!r} does not say 'Abschicken'"
    ]
    texts = {**GERMAN, NOTHING: GERMAN[NOTHING].replace("Eingabe", "Zeilenumbruch")}
    german = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in texts.items()])
    named = "not as 'Eingabe, dann Abschicken'"
    assert rules.key_findings(shipped(), [german]) == [
        f"rule 16: de: {texts[NOTHING]!r} names the key 'Zeilenumbruch', {named}"
    ]
    fixed = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in GERMAN.items()])
    assert rules.key_findings(shipped(), [fixed, i18n.ENGLISH]) == []
    bare = "Nichts gesendet. Tippe Eingabe, dann Abschicken."  # no sign: no key named
    texts = {**GERMAN, NOTHING: bare}
    without = compiled("de", [Entry(k, (v,), i18n.DASHBOARD) for k, v in texts.items()])
    assert rules.key_findings(shipped(), [without]) == [
        f"rule 16: de: {bare!r} names the key '', {named}"
    ]


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


# --- Contacts, Map, Gateway, Settings and Help ----------------------------------------------------


def view_of(path: str) -> dict[str, Any]:
    """A view of the shipped template, rendered for the household of the rules."""
    return next(v for v in rules.load(rules.rendered(shipped()))["views"] if v["path"] == path)


def kinds(view: dict[str, Any]) -> list[str]:
    """The cards of a view in order: a heading by its text, another card by its type."""
    return [
        f"heading {c['heading']}" if c["type"] == "heading" else c["type"]
        for section in view["sections"]
        for c in section["cards"]
    ]


WORDS = [  # D-8: the telemetry state and its words, as a chat page and Contacts say them
    ("waiting", "not asked yet"),
    ("answering", "answering"),
    ("no_position", "answering, no position"),
    ("away", "no answer lately"),
    ("no_answer", "no answer for a day"),
    ("no_path", "not reachable now"),
    ("look_alike", "not asked: look-alike"),
    ("excluded", "not asked (switch off)"),
    ("off", "off for everyone"),
]


def test_the_asking_words_are_the_chat_pages_words() -> None:
    """HC 23, D-8 (HA-08): the column Asking on Contacts says the telemetry state with the words
    of a chat page, from one text of the macro telemetry(), and its help explains them as the
    chat page does, word for word; Help explains them too. No card says "excluded" for a
    companion nobody included (M9)."""
    found = [card for card in _cards(shipped()) if any("macro telemetry(v)" in t for t in card)]
    assert len(found) == 3  # People and devices, Sensors, a chat page's About

    def macro(card: list[str]) -> str:
        at = next(i for i, t in enumerate(card) if "macro telemetry(v)" in t)
        end = next(i for i in range(at, len(card)) if "endmacro" in card[i])
        return "\n".join(card[at : end + 1])

    assert len({macro(card) for card in found}) == 1
    for code, word in WORDS:
        assert f"{{%- elif v == '{code}' -%}}_(\"{word}\")" in macro(found[0]) or (
            f"{{%- if v == '{code}' -%}}_(\"{word}\")" in macro(found[0])
        ), code
    assert '_("excluded")' not in shipped()
    explained = re.findall(r'<li><b>_\("([^"]*)"\)</b>: _\("([^"]*)"\)</li>', shipped())
    chat = [pair for pair in explained if pair[0] in dict(WORDS).values()]
    assert len(chat) == 3 * len(WORDS)  # the chat page, People and devices, Help
    assert len(set(chat)) == len(WORDS)  # one explanation per word
    people = next(card for card in found if any('<th>_("Asking")</th>' in t for t in card))
    assert people != found[-1]


def test_contacts_lists_people_then_asks_adds_and_removes() -> None:
    """Design §2.5: the badges, People and devices with the column Asking (HC 23); Sensors;
    Ask for position and battery; Add or remove, the QR code under "Let someone add this home"
    before the remove rows (HC 28); Nodes heard nearby without it; About last. The badges have a
    heading of their own: beside "People and devices" the badge row scrolled at 360 and 390 px
    and hid Help (stage-5 browser check); alone they have the width of the page."""
    contacts = view_of("contacts")
    assert kinds(contacts)[:6] == [
        "heading ",
        "heading People and devices",
        "markdown",
        "heading Sensors",
        "markdown",
        "heading Ask for position and battery",
    ]
    assert "badges" not in contacts["sections"][0]["cards"][1]
    heading = contacts["sections"][0]["cards"][0]
    assert [b["tap_action"]["navigation_path"] for b in heading["badges"]] == [
        "./gateway",
        "./settings",
        "./help",
    ]
    add = kinds(contacts).index("heading Add or remove")
    assert kinds(contacts)[add : add + 11] == [
        "heading Add or remove",
        "markdown",  # why adding or removing does not work now (D-29)
        "heading Add a contact",
        "entities",
        "markdown",  # the result and the Return line (D-65)
        "heading Let someone add this home",
        "markdown",  # the QR code
        "entities",  # Advert to radios nearby (ADR 0033)
        "markdown",  # what became of the last advert, and its help
        "heading Remove a contact",
        "entities",
    ]
    subtitles = [c for s in contacts["sections"] for c in s["cards"] if c["type"] == "heading"]
    assert {c["heading"] for c in subtitles if c.get("heading_style") == "subtitle"} == {
        "Add a contact",
        "Let someone add this home",
        "Remove a contact",
    }
    table = next(t for t in rules.texts(contacts) if "<th>Asking</th>" in t)
    assert "<th>Name</th><th>Heard</th><th>Battery</th><th>Asking</th>" in table
    nearby = contacts["sections"][-1]["cards"]
    assert nearby[0]["heading"] == "Nodes heard nearby"
    assert not any("ha-qr-code" in str(c) for c in nearby)
    qr = [t for t in rules.texts(contacts) if "<ha-qr-code" in t]
    assert len(qr) == 1
    assert "Add this home's radio in the MeshCore app" in qr[0]


ADVERT_BUTTON = f"button.{rules.GW}_send_advert"


def _advert_cards(contacts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The row of Advert to radios nearby and the card after it, on Contacts."""
    listed = [c for section in contacts["sections"] for c in section["cards"]]
    (row,) = [n for n, c in enumerate(listed) if ADVERT_BUTTON in json.dumps(c.get("entities"))]
    return listed[row], listed[row + 1]


def test_the_advert_row_shows_while_its_button_can_be_pressed() -> None:
    """ADR 0033: under the QR code of Let someone add this home, the row Advert to
    radios nearby with Send, which presses the gateway's button without asking (one advert, at
    most every 10 minutes); the row shows only while the button is there and not unavailable:
    with `radio_controls` or `controls` off, the gateway offline or the radio not connected it
    is unavailable, and a row that stays pressable would do nothing."""
    card, _ = _advert_cards(view_of("contacts"))
    assert card["visibility"] == [
        {
            "condition": "template",
            "value_template": (
                f"{{{{ states['{ADVERT_BUTTON}'] is not none\n"
                f"   and not is_state('{ADVERT_BUTTON}', 'unavailable') }}}}"
            ),
        }
    ]
    (row,) = card["entities"]
    assert row == {
        "type": "button",
        "entity": ADVERT_BUTTON,
        "name": "Advert to radios nearby",
        "icon": "mdi:broadcast",
        "action_name": "Send",
        "tap_action": {
            "action": "perform-action",
            "perform_action": "button.press",
            "target": {"entity_id": ADVERT_BUTTON},
        },
    }


def test_the_advert_line_reads_the_event_and_names_every_reason() -> None:
    """The card under the row says what became of the last advert for 10 minutes, a refusal in
    words for each reason the gateway gives (a radio's own error stays a code), and holds the
    row's help: what the advert does, that everyone here can send it, and the key's first digits
    to compare with."""
    _, line = _advert_cards(view_of("contacts"))
    content = line["content"]
    assert line["text_only"] is True
    assert content.split("\n")[-1].startswith("<details><summary>What the advert does</summary>")
    assert f"set e = 'event.{rules.GW}_advert'" in content
    assert "now() - at < timedelta(minutes=10)" in content.replace("clock", "now()")
    for reason in [*SELF_ADVERT_REASONS, "table_full"]:
        assert f"v == '{reason}'" in content, reason
    assert "{%- else -%}<code>{{ m(v) }}</code>" in content
    # who can send it, only while they can: not under the line that says it is off
    assert "{% if not off %}<li>Everyone who uses Home Assistant here can send it.</li>" in content
    assert "A node with this name and another key is someone else." in content


def test_help_says_why_no_advert_was_sent_and_names_the_row() -> None:
    """Help: the row and its button among every button and switch, and a fold with the words of
    each reason the line under the row can give."""
    said = " ".join(rules.texts(view_of("help")))
    assert "<b>Advert to radios nearby</b>, <b>Send</b>: On Contacts, under Let someone" in said
    (fold,) = [t for t in rules.texts(view_of("help")) if "Why no advert was sent" in t]
    _, line = _advert_cards(view_of("contacts"))
    words = re.findall(r"-%\}([^{]+)\n", line["content"].split("macro cause(v)")[1])
    assert len(words) == len(SELF_ADVERT_REASONS) + 1  # and the radio's table_full
    for word in words:
        assert f"<b>{word}</b>" in fold, word
    busy, soon = "<b>another advert is being sent</b>", "<b>only one advert every 10 minutes</b>"
    assert f"{busy}: Another press is being handled" in fold  # no next time after busy
    assert f"{soon}: The radio sends at most one advert every 10 minutes" in fold
    assert "<b>the radio was busy</b>: The radio had no room for it just then." in fold


def test_settings_show_whether_the_advert_button_can_be_pressed() -> None:
    """Off when `radio_controls` or `controls` is off, on with the key, a dash for
    a gateway before 2.2, whose document has no such key."""
    settings = " ".join(rules.texts(view_of("settings")))
    assert (
        "<tr><td>Advert button in Home Assistant</td><td>{% if a.get('radio_controls') == false "
        "or a.get('controls') == false %}off{% elif a.get('radio_controls') == true %}on"
        "{% else %}\u2013{% endif %}</td>"
    ) in settings
    assert "<code>[homeassistant]</code> <code>radio_controls</code>" in settings


def test_adding_by_key_says_return_and_never_enter() -> None:
    """D-65, D-7: the field's label ends without "(press Enter)", and the line below it asks for
    Return (↵); no household text says Enter. Hidden contacts and Show again (D-28)."""
    contacts = view_of("contacts")
    rows = [r for c in rules.nodes(contacts) for r in [c[0]] if isinstance(r, dict) and "name" in r]
    names = {r.get("entity", r.get("type")): r["name"] for r in rows if "row" not in r}
    assert names[f"select.{GW}_add_node"] == "Add at once: pick a node heard nearby"
    assert names[f"text.{GW}_add_contact"] == "Add by key or contact link"
    (hidden,) = [r for r in rows if r.get("entity") == f"button.{GW}_show_hidden"]
    assert (hidden["type"], hidden["name"], hidden["action_name"]) == (
        "button",
        "Hidden contacts",
        "Show again",
    )
    assert "confirmation" not in hidden["tap_action"]
    result = next(t for t in rules.texts(contacts) if "event.meshcore_home_gateway" in t)
    first = next(line for line in result.split("\n") if line.startswith("<div>"))
    assert (
        first == "<div>Paste a key or contact link into the field above, then tap Return (↵).</div>"
    )
    texts = rules.msgids(shipped())
    assert [t for t in texts if re.search(r"\bEnter\b", t)] == []
    assert "Show contacts deleted in Home Assistant again" not in shipped()


def test_the_gateway_page_has_health_radio_settings_and_the_latest_events() -> None:
    """Design §2.6: Health, whose row "Messages on their way" counts as the status line does
    (contract item 8); Radio without the tile of airtime (D-65): its value is Health's row
    "Airtime left this hour"; Radio settings with the effective audience (D-58); Latest of each
    kind; About last."""
    gateway = view_of("gateway")
    assert kinds(gateway) == [
        "heading Gateway",
        "markdown",
        "heading Radio",
        "markdown",
        "entities",
        "markdown",
        "history-graph",
        "markdown",
        "heading Radio settings",
        "markdown",
        "heading Latest of each kind",
        "markdown",
        "markdown",
    ]
    assert not [
        c for c, _ in rules.nodes(gateway) if isinstance(c, dict) and c.get("type") == "tile"
    ]
    assert f"sensor.{GW}_airtime_available" not in [
        r.get("entity") for r, _ in rules.nodes(gateway) if isinstance(r, dict)
    ]
    health = gateway["sections"][0]["cards"][1]["content"]
    for row in ("Messages on their way", "Airtime left this hour", "Radio link", "Free disk"):
        assert f"<td>{row}</td>" in health, row
    waiting = "e.get('step') == 'queued'"
    status = view_of("overview")["sections"][0]["cards"][0]["content"]
    assert "now() - t > timedelta(seconds=60)" in health
    assert waiting in status
    assert "q.get('step') == 'queued'" in health
    settings = gateway["sections"][2]["cards"][1]["content"]
    for label in ("battery", "position", "readings"):
        assert f"<td>Who may ask this radio for its {label}</td>" in settings


def test_settings_show_what_the_administrator_set_and_change_nothing() -> None:
    """Design §2.7 (owner Q2): a card per group, each a table with the configuration key in
    <code>; no control on the page; the versions always."""
    settings = view_of("settings")
    assert [k for k in kinds(settings) if k.startswith("heading")] == [
        "heading Settings",
        "heading Chats and messages",
        "heading Privacy",
        "heading Contacts and Home Assistant",
        "heading Radio and airtime",
        "heading Versions",
    ]
    assert set(kinds(settings)) == {
        *(k for k in kinds(settings) if k.startswith("heading")),
        "markdown",
    }
    tables = [t for t in rules.texts(settings) if "<th>What it means</th>" in t]
    assert len(tables) == 4
    for section, key in (
        ("messages", "confirm_nonce"),
        ("contacts", "heard_nodes"),
        ("homeassistant", "controls"),
        ("radio.profile", "manage"),
        ("commands", "codes_file"),
    ):
        assert any(f"<code>[{section}]</code> <code>{key}</code>" in t for t in tables), key
    assert any(
        f"<td>This dashboard</td><td>{rules.VERSION}</td>" in t for t in rules.texts(settings)
    )


def test_the_clock_card_says_how_the_gateway_keeps_the_radios_clock() -> None:
    """The card shows while the radio's clock stays 5 minutes off. The gateway moves it forward
    whenever it is behind and sets it back, by restarting the radio, only when it is up to an
    hour ahead, at most once a day, and since 2.0.0's review only when the next connect could
    move it forward again (configuration.md, set_clock): "sets it at most once a day" said
    otherwise."""
    (card,) = [t for t in rules.texts(view_of("overview")) if "{#- problem: clock -#}" in t]
    fold = card.partition("<details>")[2]
    assert "The gateway sets it at most once a day." not in fold
    for words in ("forward", "back", "at most once a day", "an hour", "computer's clock"):
        assert words in fold, words
    # #86: and only when the next connect can move it forward from there again
    assert "can move it forward again" in fold


def test_the_clock_card_says_the_gateway_leaves_the_clock_while_set_clock_is_off() -> None:
    """The radio's clock offset is published whatever `[radio] set_clock` says, and with it
    off the gateway neither moves the clock nor restarts the radio (radio/session.py): the card
    reads the setting from the configuration document and says so then, naming the key as
    Settings does; a gateway that does not say, being offline or before 2.1, keeps the text of
    the setting's default."""
    (card,) = [t for t in rules.texts(view_of("overview")) if "{#- problem: clock -#}" in t]
    fold = card.partition("<details>")[2]
    assert "state_attr('sensor.meshcore_home_gateway_radio1_configuration', 'radio')" in card
    assert ".get('set_clock') == false" in card
    off, _, on = fold.partition("{% else %}")
    assert "The gateway does not set this radio's clock" in off
    assert "<code>[radio]</code> <code>set_clock</code>" in off
    assert "forward" not in off
    assert "forward" in on


def _command_fold() -> list[str]:
    """The items of Help's fold "Why a command was refused"."""
    help_ = " ".join(rules.texts(view_of("help")))
    fold = help_.partition("<summary>Why a command was refused</summary>")[2]
    return fold.partition("</ul></details>")[0].removeprefix("<ul>").split("</li>")


def test_help_names_both_meanings_of_no_code() -> None:
    """Since the gateway ignores the secret of a key that is no sender (#80), `code_missing`
    also refuses a sender without a code secret, whose phone's clock is never the cause and
    which only the administrator helps (`commands enrol` or `revoke`): Help says so apart from
    the authenticator app's code, and Settings' row of the command codes says that the others
    cannot command while a secret is ignored (docs/configuration.md, An ignored secret)."""
    items = _command_fold()
    (no_code,) = [i for i in items if i.startswith("<li><b>no code</b>:")]
    assert "without a code secret" in no_code
    assert "ignores" in no_code
    assert "administrator" in no_code
    assert "Check the clock of the sender's phone." not in no_code
    (wrong,) = [i for i in items if "<b>wrong code</b>" in i]
    assert "<b>code used already</b>" in wrong
    assert "Check the clock of the sender's phone." in wrong
    (row,) = [t for t in rules.texts(view_of("settings")) if "<code>codes_file</code>" in t]
    codes = row.partition("<td>Command codes</td>")[2].partition("<code>")[0]
    assert "ignore" in codes


def test_help_says_a_refused_command_shows_until_the_next_message() -> None:
    """The refusal is only on `received` (command.accepted false), whose next message of any
    kind, a channel message included, replaces it: Latest of each kind shows a refused command
    only until then (home-assistant/tests/test_gateway.py), and a sender locked out after wrong
    codes stays in the gateway's log. Help and the guide say that, not that it is the newest."""
    first = _command_fold()[0]
    assert "until the next message" in first
    assert "log" in first
    guide = " ".join(rules.GUIDE.read_text(encoding="utf-8").split())
    assert "counts as the newest while it came after the newest accepted one" not in guide
    assert "until the next message" in guide


def _configuration_keys() -> dict[str, set[str]]:
    """The keys of each section's table in docs/configuration.md, by the section's name."""
    found: dict[str, set[str]] = {}
    section = None
    for line in (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            match = re.fullmatch(r"## `\[([\w.]+)\]`", line)
            section = match.group(1) if match else None
        elif section and (key := re.match(r"\| `(\w+)` \|", line)):
            found.setdefault(section, set()).add(key.group(1))
    return found


def test_settings_name_the_files_keys_and_read_what_the_document_still_has() -> None:
    """Each key Settings names is one of its section in docs/configuration.md, where the
    administrator finds it: `[commands] codes_file`, not the document's `codes`; and each
    value it reads from *Configuration* is one the document still has (config_view)."""
    document = config_view.shown_settings(config())
    files = _configuration_keys()
    settings = view_of("settings")
    tables = [t for t in rules.texts(settings) if "<th>What it means</th>" in t]
    named = re.findall(r"<code>\[([\w.]+)\]</code>((?: <code>\w+</code>)+)", "".join(tables))
    assert len(named) >= 18
    for section, keys in named:
        for key in re.findall(r"<code>(\w+)</code>", keys):
            assert key in files.get(section, set()), f"[{section}] {key}"
    read = 0
    for card in rules.texts(settings):
        bound = dict(re.findall(r"set (\w+) = state_attr\(c, '(\w+)'\)", card))
        for name, key in re.findall(r"\b(\w+)\.get\('(\w+)'\)", card):
            if bound.get(name) in document:
                assert key in document[bound[name]], f"{bound[name]}.{key}"
                read += 1
    assert read >= 25
    assert "<code>[commands]</code> <code>codes_file</code>" in "".join(tables)
    assert "a code that changes every 30 seconds" in "".join(tables)
    assert "a command also needs a code" not in "".join(tables)  # not a sender without one


# the longest word the last column of a Settings table takes at 360 px beside the other two (the
# stage-5 browser check measured 154 px for radio.profile.manage, 20 letters, and too little for
# messages.publish_channels, 25)
CODE_LETTERS = 21


def test_settings_name_each_key_by_its_section_and_its_name_so_the_tables_fit_a_phone() -> None:
    """The stage-5 browser check at 360 px: a dotted key such as messages.publish_channels is one
    word, too wide for the last column, and three of the four tables ran past their cards. As
    docs/configuration.md writes them, the section in brackets and the key are two words."""
    settings = view_of("settings")
    words = [w for t in rules.texts(settings) for w in re.findall(r"<code>([^<{]*)</code>", t)]
    assert "contact_entity_prefix" in words
    assert [w for w in words if "." in w and not w.startswith("[")] == []
    assert max(len(w) for w in words) <= CODE_LETTERS, max(words, key=len)


# DR-14: what Help names, besides the titles and the names rule 8 finds by themselves
HELP_TOPICS = (
    "What this gateway does",
    "The views",
    "Every button and switch",
    "Kinds of messages",
    "What this dashboard cannot do",
    "When something is wrong",
    "Why a message was not sent",
    "Who sees what",
    "For your administrator",
)


def test_help_says_what_the_gateway_goes_by_for_a_position_broadcast_to_everyone() -> None:
    """The second place that states the warning as certain (owner feedback on 2.1.0): Help's
    words for Overview's first line say, as the card does, that the gateway goes by the position
    the radio remembers until it has read the device's newest advert."""
    said = " ".join(rules.texts(view_of("help")))
    assert (
        "<li><b>a position broadcast to everyone</b>: A person's device tells everyone in range "
        "where it is. Until the gateway has read the device's newest advert, it goes by the "
        "position this home's radio still remembers for it.</li>"
    ) in said


def test_help_names_every_page_and_every_control() -> None:
    """DR-14 (rule 8): Help names every view and every name and button label of the dashboard:
    Chats, Contacts, Map, Gateway, Settings, Write to …, Return, then Send, and those of stage 5;
    it has the topics of the system, also what this dashboard cannot do and the kinds of
    messages (HC 23, HC 33). Without one of them, rule 8 finds it."""
    help_ = view_of("help")
    summaries = [
        m.group(1)
        for t in rules.texts(help_)
        for m in re.finditer(r"<details><summary>([^<]*)</summary>", t)
    ]
    assert [t for t in HELP_TOPICS if t not in summaries] == []
    said = " ".join(rules.texts(help_))
    for label in (
        "Chats",
        "Contacts",
        "Map",
        "Gateway",
        "Settings",
        "Write to …",
        "Return, then Send",
        "Ask for position and battery",
        "Add at once: pick a node heard nearby",
        "Add by key or contact link",
        "Remove",
        "Hidden contacts",
        "Show again",
        "Advert to radios nearby",
        "Background noise",
        "Radio battery",
    ):
        assert f"<b>{label}</b>" in said, label
    assert "guide's script" not in said
    assert "draft and send" in said
    # §2.8: control and its effect; the controls above act, only links and badges open a page
    assert "Every link and button above goes to a page" not in said
    assert (
        "The links, and the badges Gateway, Settings and Help, open a page of this dashboard; "
        "the back arrow returns to where you came from."
    ) in said
    for label in ("Return, then Send", "Hidden contacts", "Radio battery"):
        broken = shipped().replace(f'<b>_("{label}")</b>', "<b>x</b>")
        assert f"rule 8: Help does not list {label!r}" in rules.findings(broken), label
