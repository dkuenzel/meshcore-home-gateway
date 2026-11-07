"""The generated dashboard against the real Home Assistant (docs/home-assistant.md, Dashboard).

The household is made by the gateway's own code: the gateway and its controls, Alice (a companion
with GPS), Hilltop (a repeater with a position) and a companion for each hostile name, whose
devices ContactDevices announces and whose state file it writes. dashboard.dashboard() makes the
dashboard from that file; Home Assistant loads it as a YAML-mode dashboard, and every Markdown card
is rendered with Home Assistant's own template engine as the frontend asks for it (strict, with
the variables `config` and `user`). The tests check that it loads, names only entities that exist
and only cards the pinned frontend has, shows each situation, keeps every name and text from the
mesh inert, follows the contacts, and that the guide's script sends; in English and in the
pseudo-language `xx` (test_languages.py), whose marks hold ": ", so a text written as a YAML value
without quotes would not load.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import time
from collections.abc import Iterable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import markupsafe
import pytest
from freezegun.api import FrozenDateTimeFactory
from ha_plugin import CLIENT_ID
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_get_cached_service_description
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify
from test_contract import (
    PREFIX,
    RADIO,
    fire,
    guide_block,
    make_config,
    problems,
    state_document,
)
from test_languages import offer
from visibility import household_viewer

from meshcore_home_gateway import dashboard, i18n
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.core.contacts import Contact, node_label
from meshcore_home_gateway.core.mesh import Mesh, Node
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
    Position,
)
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.manage_entities import manage_payload, manage_topic
from meshcore_home_gateway.mqtt.payloads import delivery_payload, received_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.state import ContactDeviceRecords
from meshcore_home_gateway.views import contact_document, contact_event, iso
from test_contact_devices import (
    ALICE,
    ALICE_NODE,
    COMPANION,
    HILL_NODE,
    NOW,
    Broker,
    Ticking,
    mesh_of,
)

# what anyone in radio range may write: markup, links, images, autolinks, a forged alert, Jinja
ATTACKS = [
    "<img src=//example.org/p.png>",
    "[tap me](https://example.org/x)",
    "visit www.example.org or https://example.org/y",
    "**bold** _it_ `code` | pipe | ~~del~~",
    "![](https://example.org/i.png)",
    "line one\n\n[after blank](https://example.org/z) <b>x</b>",
    '"><ha-alert alert-type="error">fake</ha-alert>',
    "{{ 7 * 7 }} {% if true %}x{% endif %}",
]
BREAKS = ["e\n\n![](//e.x/p.png)", "e\r\r![](//e.x/q.png)", "e\r\n\r\n[x](https://e.x)"]
# a contact's name is at most 31 bytes in the radio's table: every entry cut there
NAMES = BREAKS + [attack.encode()[:31].decode(errors="ignore") for attack in ATTACKS]
TEXTS = BREAKS + ATTACKS
KEYS = [f"{0x30 + i:02x}" * 32 for i in range(len(NAMES))]
BOB, ZED = "d5" * 32, "e7" * 32
HOME = Position(52.37, 9.73)
LANGUAGES = ["en", "xx"]
XX = "‹{language}: {text}›"  # ": " in every text: unquoted as a YAML value, a mapping
URL = "mesh-radio1"
# the types of the frontend Home Assistant 2026.10.0 pins (20260930.2): cards in
# src/panels/lovelace/create-element/create-card-element.ts lines 20-108, views in
# create-view-element.ts, sections in create-section-element.ts
CARDS = frozenset(
    {
        "entity",
        "entities",
        "button",
        "entity-button",
        "glance",
        "grid",
        "section",
        "light",
        "sensor",
        "thermostat",
        "weather-forecast",
        "tile",
        "heading",
        "alarm-panel",
        "area",
        "calendar",
        "conditional",
        "empty-state",
        "energy-compare",
        "energy-carbon-consumed-gauge",
        "energy-date-selection",
        "energy-devices-graph",
        "energy-devices-detail-graph",
        "energy-distribution",
        "energy-gas-graph",
        "energy-water-graph",
        "energy-grid-neutrality-gauge",
        "energy-grid-balance",
        "energy-solar-consumed-gauge",
        "energy-self-sufficiency-gauge",
        "energy-solar-graph",
        "energy-sources-table",
        "energy-usage-graph",
        "energy-sankey",
        "water-sankey",
        "water-flow-sankey",
        "power-sources-graph",
        "power-sankey",
        "entity-filter",
        "error",
        "home-summary",
        "shortcut",
        "discovered-devices",
        "repairs",
        "alert",
        "updates",
        "gauge",
        "history-graph",
        "horizontal-stack",
        "distribution",
        "humidifier",
        "iframe",
        "logbook",
        "map",
        "markdown",
        "clock",
        "media-control",
        "picture-elements",
        "picture-entity",
        "picture-glance",
        "picture",
        "plant-status",
        "recovery-mode",
        "toggle-group",
        "todo-list",
        "shopping-list",
        "starting",
        "statistics-graph",
        "statistic",
        "vertical-stack",
    }
)
VIEWS = frozenset({"masonry", "panel", "sidebar", "sections"})
SECTIONS = frozenset({"grid"})
# the Markdown cards, by a part of their content or visibility
WRITE = "script.mesh_message"  # the guide's script, for automations: no card names it (D-22)
TABLE = "sort(attribute=0, reverse=true)"  # the contacts table of 2.0
# the gateway's sensors of 2.1 that the cards read; a gateway of 2.0 has none of them
HISTORY = f"sensor.{PREFIX}_message_history"
PENDING = f"sensor.{PREFIX}_pending_messages"
CONFIGURATION = f"sensor.{PREFIX}_configuration"
OF_2_1 = (HISTORY, PENDING, CONFIGURATION)
# the contact event's entity; a reason code such as 'unknown_contact' ends the same way
EVENT = re.compile(r"event\.meshcore_home_gateway_\w+_contact'")
HEARD, QR, HINT = "_add_node'", "contact_link", "set listed"
# the line a value from Home Assistant stands on: one whole HTML element
ELEMENT = re.compile(
    r"<div>.*</div>|<table>.*</table>|<ha-qr-code .*></ha-qr-code>|<details>.*</details>"
    r'|<table role="presentation" width="100%">.*</table>'
)
NONE = "\u2013"  # what a card shows for no value: an en dash
# the pages that are no chat: the three tabs first
PAGES = ("overview", "messages", "contacts", "map", "gateway", "settings", "help")
MARKDOWN = "MESHCORE_HOME_GATEWAY_MARKDOWN_DIR"
ENTITY_ID = re.compile(
    r"\b(?:binary_sensor|sensor|event|notify|select|text|button|switch|script|device_tracker)"
    r"\.[a-z0-9_]+\b"
)


def speaking(language: str, monkeypatch: pytest.MonkeyPatch, node_id: str = "radio1") -> Config:
    """The gateway's configuration, its names and dashboard in `language`: xx offered first."""
    offer(language, monkeypatch, XX)
    return in_language(make_config(node_id=node_id), language)


def in_language(config: Config, language: str) -> Config:
    names = dataclasses.replace(config.homeassistant, language=language)
    return dataclasses.replace(config, homeassistant=names)


def said(config: Config, msgid: str, **values: str) -> str:
    """A text of the dashboard in the configuration's language."""
    translator = i18n.translator(config.homeassistant.language)
    return translator.pgettext(i18n.DASHBOARD, msgid, **values)


def topics_of(config: Config) -> Topics:
    return Topics(config.mqtt.base_topic, config.mqtt.node_id)


def contacts_of(config: Config, store: Path, broker: Broker) -> ContactDevices:
    """The contacts' devices as the gateway announces them; its state file in `store`."""
    records = ContactDeviceRecords(Ticking(store / "state.json"))
    return ContactDevices(config, topics_of(config), broker, records, wall_clock=lambda: NOW)


def household(*nodes: Node) -> Mesh:
    """The radio's contacts; Alice answered telemetry with the position of her GPS."""
    mesh = mesh_of(*nodes)
    if ALICE in mesh.contacts:
        mesh.contact_answered(ALICE, NOW, HOME, 12.0)
    return mesh


def hostile() -> list[Node]:
    """A companion with a position for each hostile name."""
    return [
        Node(key, name, COMPANION, position=Position(52.0 + i / 100, 9.0))
        for i, (key, name) in enumerate(zip(KEYS, NAMES, strict=True))
    ]


def undrafted(document: dict[str, Any]) -> dict[str, Any]:
    """A device's discovery document without the draft fields and Send buttons that a gateway of
    2.1 announces (G4): `write_<alias>` and its `_send` on the gateway's device, `write` and
    `write_send` on a contact's. A test that gives a chat its fields itself starts from it."""
    components = document.get("cmps")
    if not isinstance(components, dict):
        return document
    kept = {k: v for k, v in components.items() if not re.fullmatch(r"write(_[\w-]+)?", k)}
    return document | {"cmps": kept}


async def gateway(
    hass: HomeAssistant, config: Config, *, online: bool = True, drafts: bool = True
) -> Topics:
    """The gateway's document and controls; the heard nodes offered are the hostile names, raw
    and as the gateway labels them, and Bob. Without `drafts`, without the drafts of a gateway
    of 2.1 (undrafted())."""
    topics = topics_of(config)
    payload = discovery_payload(config, topics, RADIO)
    await fire(hass, discovery_topic(config), payload if drafts else undrafted(payload))
    labels = [node_label(key, name, COMPANION) for key, name in zip(KEYS, NAMES, strict=True)]
    options = [*labels, *NAMES, node_label(BOB, "Bob", COMPANION)]
    await fire(hass, manage_topic(config), manage_payload(config, topics, options))
    if online:
        await fire(hass, topics.availability, "online", retain=True)
        await fire(hass, topics.state, state_document(known=True) | {"queue": 0}, retain=True)
    return topics


async def deliver(
    hass: HomeAssistant,
    config: Config,
    broker: Broker,
    mesh: Mesh,
    *,
    drafts: bool = True,
    **state: Any,
) -> None:
    """What the contacts' devices announced, and each contact's document, retained; without
    `drafts`, without the drafts of a gateway of 2.1 (undrafted())."""
    topics = topics_of(config)
    if not drafts:
        broker.sent = [(topic, _undrafted(payload)) for topic, payload in broker.sent]
    await broker.deliver(hass)
    await fire(hass, topics.availability, "online", retain=True)
    for key, tracked in mesh.contacts.items():
        companion = tracked.node.node_type == COMPANION
        document = contact_document(tracked, telemetry_state="answering" if companion else None)
        await fire(hass, topics.contact(key), document | state, retain=True)


def _undrafted(payload: bytes) -> bytes:
    try:
        document = json.loads(payload)
    except ValueError:
        return payload
    return json.dumps(undrafted(document)).encode() if isinstance(document, dict) else payload


def documented(
    hass: HomeAssistant, entity: str, messages: list[dict[str, Any]], **more: Any
) -> None:
    """The history's or the pending messages' sensor as the gateway of 2.1 announces it
    (docs/mqtt-api.md, History and Messages on their way): the document as its attributes, the
    newest message's time or the count as its state. The cards' rules are tested on this state;
    test_the_dashboard_reads_the_sensors_of_a_gateway_of_2_1 checks that discovery makes the
    sensors, which is the gateway's work."""
    document = {"total": len(messages), "truncated": False, "messages": messages} | more
    if entity == HISTORY:
        state = messages[0]["at"] if messages else "unknown"
    else:
        state = str(document["total"])
    hass.states.async_set(entity, state, document)


def keep(language: str, outputs: dict[str, str]) -> None:
    """The cards' outputs for tests/manual/markdown_pipeline.mjs, when MARKDOWN names a
    directory (docs/testing.md)."""
    if folder := os.environ.get(MARKDOWN):
        target = Path(folder) / f"markdown-{language}.json"
        target.write_text(json.dumps(outputs, ensure_ascii=False, indent=0), encoding="utf-8")


def place(folder: Path, files: dict[str, str]) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for url, text in files.items():
        (folder / f"{url}.yaml").write_text(text, encoding="utf-8")


async def load(hass: HomeAssistant, hass_ws_client: Any, files: dict[str, str]) -> dict[str, Any]:
    """The dashboards as YAML-mode dashboards set up as the guide shows, each under its own path,
    as the frontend gets them."""
    place(Path(hass.config.path("dashboards")), files)
    (shown,) = guide_block("lovelace:")["lovelace"]["dashboards"].values()
    assert shown["filename"] == f"dashboards/{URL}.yaml"
    dashboards = {url: shown | {"filename": f"dashboards/{url}.yaml"} for url in files}
    assert await async_setup_component(hass, "lovelace", {"lovelace": {"dashboards": dashboards}})
    client = await hass_ws_client(hass)
    loaded = {}
    for url in files:
        await client.send_json_auto_id({"type": "lovelace/config", "url_path": url})
        answer = await client.receive_json()
        assert answer["success"], answer
        loaded[url] = answer["result"]
    return loaded


async def made(
    hass: HomeAssistant, hass_ws_client: Any, config: Config, store: Path
) -> dict[str, Any]:
    """The dashboard of `config` from the state file in `store`, loaded."""
    return (await load(hass, hass_ws_client, {URL: dashboard.dashboard(config, store)[0]}))[URL]


def walk(node: Any) -> Iterator[dict[str, Any]]:
    """Every view, section and card."""
    if isinstance(node, dict):
        if "type" in node:
            yield node
        for value in node.values():
            yield from walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk(value)


def markdown(loaded: dict[str, Any] | list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [card for card in walk(loaded) if card["type"] == "markdown"]


def card(
    loaded: dict[str, Any] | list[dict[str, Any]], needle: str | re.Pattern[str]
) -> dict[str, Any]:
    """The one Markdown card whose content or visibility holds `needle`, in a dashboard or in a
    page's cards (cards())."""

    def holds(text: str) -> bool:
        return bool(needle.search(text)) if isinstance(needle, re.Pattern) else needle in text

    (found,) = [c for c in markdown(loaded) if holds(json.dumps(c))]
    return found


def page(loaded: dict[str, Any], needle: str) -> dict[str, Any]:
    """The one view or subview that `needle` names: its path (`overview`, `chat-family`), the
    alias or slug after a chat page's kind (`family`, `alice`), or, for a subview, a contact's
    key that its guards pin."""

    def names(view: dict[str, Any]) -> set[str]:
        path = str(view.get("path", ""))
        kind, _, rest = path.partition("-")
        return {path, rest} if kind in ("chat", "contact") and rest else {path}

    found = [v for v in loaded["views"] if needle in names(v)]
    if not found and re.fullmatch(r"[0-9a-f]{64}", needle):
        found = [v for v in loaded["views"] if v.get("subview") and f"'{needle}'" in json.dumps(v)]
    assert len(found) == 1, (needle, [v.get("path") for v in found])
    return found[0]


def cards(loaded: dict[str, Any], needle: str) -> list[dict[str, Any]]:
    """Every card of the page that `needle` names (page()), in the file's order: its sections'
    cards, its own (a masonry or panel view), its header's and footer's card; nested cards and
    rows with a type included, as walk() finds them. card(cards(...), needle) picks one."""
    view = page(loaded, needle)
    tops = [each for section in view.get("sections", []) for each in section.get("cards", [])]
    tops += view.get("cards", [])
    tops += [view[part]["card"] for part in ("header", "footer") if "card" in view.get(part, {})]
    return list(walk(tops))


def render(hass: HomeAssistant, found: dict[str, Any]) -> str:
    """As the frontend asks for it: strict (an error fails the test), `config` and `user`."""
    variables = {"config": found, "user": "Bob"}
    return str(
        Template(found["content"], hass).async_render(variables, parse_result=False, strict=True)
    )


def shows_nothing(hass: HomeAssistant, found: dict[str, Any]) -> None:
    """The card renders to exactly "": a Markdown card with `show_empty: false` hides only then
    (hui-markdown-card.ts lines 184-187 at 20260930.4, 232-235 at 20261028.4). Core strips what a
    template renders (helpers/template/__init__.py line 474 at 2026.10.2, 493 at 2026.11.0), so
    spaces and line breaks are nothing already; an element without text, `&nbsp;` or a zero-width
    space is not, and shows an empty box. Without `show_empty: false` even "" shows one."""
    assert found.get("show_empty") is False, f"{found['content'][:60]!r} has no show_empty: false"
    output = render(hass, found)
    assert output == "", f"{found['content'][:60]!r} shows {output!r}"


def rendered(hass: HomeAssistant, loaded: dict[str, Any]) -> dict[str, str]:
    """Every Markdown card, by its content's first line and position."""
    return {f"{i}: {c['content'][:60]}": render(hass, c) for i, c in enumerate(markdown(loaded))}


def listening(hass: HomeAssistant, loaded: dict[str, Any]) -> list[str]:
    """What the cards listen to that they should not: a domain or every state; and the contacts
    table without the time, which it needs to show a new contact within a minute."""
    found = []
    for each in markdown(loaded):
        info = Template(each["content"], hass).async_render_to_info(
            {"config": each, "user": "Bob"}, strict=True
        )
        if info.domains or info.domains_lifecycle or info.all_states:
            found.append(f"{each['content'][:60]}: {info}")
        if TABLE in each["content"] and not info.has_time:
            found.append("the contacts table without the time")
    return found


def shown(entry: str) -> str:
    """An entry as a card shows it: line breaks as spaces, escaped."""
    return str(markupsafe.escape(entry.replace("\r", " ").replace("\n", " ")))


def form_findings(outputs: dict[str, str], placed: Iterable[str] = ()) -> list[str]:
    """No card has a blank line or a CR; each line with a placed entry is one whole HTML
    element, and the raw entry is nowhere when it holds markup or a line break."""
    findings = [
        f"{where}: a blank line or a CR"
        for where, text in outputs.items()
        if "\r" in text or re.search(r"\n[ \t]*\n", text)
    ]
    for entry in placed:
        for where, text in outputs.items():
            if any(c in entry for c in "<\r\n") and entry in text:
                findings.append(f"{where}: {entry!r} raw")
            findings += [
                f"{where}: {entry!r} outside one HTML element: {line[:80]!r}"
                for line in text.split("\n")
                if shown(entry) in line and not ELEMENT.fullmatch(line)
            ]
    return findings


def alert(output: str) -> tuple[str, str]:
    """The status line's alert type and its bold text."""
    match = re.fullmatch(r"> \[!(\w+)\]\n> \*\*(.*?)\*\* .*", output, re.DOTALL)
    assert match, output
    return match.group(1), match.group(2)


def referenced(loaded: dict[str, Any]) -> set[str]:
    return set(ENTITY_ID.findall(json.dumps(loaded)))


async def guide_script(hass: HomeAssistant) -> dict[str, Any]:
    """The guide's script, saved as the script editor saves it: its ID from its alias."""
    script = guide_block("alias: Mesh message")
    config = {"script": {slugify(script["alias"]): script}}
    assert await async_setup_component(hass, "script", config)
    await hass.async_block_till_done()
    return script


# --- it loads, and names what exists ------------------------------------------------------------


async def test_a_component_set_up_after_the_websocket_client_still_registers_its_views(
    hass: HomeAssistant, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The cards are rendered while discovery still sets up notify, whose repairs registers a
    view: the test client must keep the router open, as Home Assistant's own server does."""
    await hass_ws_client(hass)
    assert await async_setup_component(hass, "repairs", {})
    assert problems(caplog) == []


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_dashboard_loads_as_a_yaml_mode_dashboard(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    language: str,
) -> None:
    """For a node ID with and without '-'; in xx every title is the catalog's, but a chat page's,
    which is its alias or contact's slug. Three tabs show their titles alone; the other pages
    have no tab and no fixed way back."""
    files = {}
    for node_id in ("radio1", "radio-2"):
        config = speaking(language, monkeypatch, node_id)
        broker = Broker()
        contacts_of(config, tmp_path / node_id, broker).update(household(ALICE_NODE))
        files[f"mesh-{node_id}"] = dashboard.dashboard(config, tmp_path / node_id)[0]
    loaded = await load(hass, hass_ws_client, files)
    chats = ["chat-alerts", "chat-my-target", "contact-alice"]  # Alice is the target operator
    for node_id, gw in (("radio1", "radio1"), ("radio-2", "radio_2")):
        views = loaded[f"mesh-{node_id}"]["views"]
        assert [v["path"] for v in views] == [*PAGES, *chats]
        assert [v["path"] for v in views if not v.get("subview")] == list(PAGES[:3])
        assert not [v["path"] for v in views if "icon" in v or "back_path" in v]
        own = {e for e in referenced(views) if ".meshcore_home_gateway_" in e}
        assert len(own) > 15
        assert all(f".meshcore_home_gateway_{gw}_" in e for e in own)
        pages = [v for v in views if v["path"] in PAGES]
        titles = [loaded[f"mesh-{node_id}"]["title"], *(v["title"] for v in pages)]
        titles += [
            c.get("heading") or c.get("title") or c["name"]
            for v in pages
            for c in walk(v["sections"])
            if c["type"] == "heading" or "title" in c or "name" in c
        ]
        assert len(titles) == 22  # with *Telemetry and removal*: Alice has a device
        for title in titles:
            if language == "xx":
                assert title.startswith("‹xx: ")
                assert title.endswith("›")
            else:
                assert "‹" not in title
        assert [v["title"] for v in views if v["path"] in chats] == ["Alerts", "My target", "Alice"]
    assert [v["title"] for v in loaded["mesh-radio1"]["views"][:7]] == [
        said(config, title)
        for title in ("Overview", "Chats", "Contacts", "Map", "Gateway", "Settings", "Help")
    ]
    assert problems(caplog) == []


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_dashboard_names_only_entities_that_exist(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    language: str,
) -> None:
    """The gateway's entities, its controls, the trackers, the contacts' switches and buttons;
    the same in English and in xx, with the household discovered in `language`; not the guide's
    script (D-22). The gateway's sensors of 2.1 and the chat pages' fields and Send buttons, which
    only their guards name, as the gateway of 2.1 announces them (G4): `-` in an alias as `_`."""
    offer("xx", monkeypatch, XX)
    config = in_language(make_config(), language)
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    files = {
        f"mesh-{each}": dashboard.dashboard(in_language(config, each), tmp_path)[0]
        for each in LANGUAGES
    }
    loaded = await load(hass, hass_ws_client, files)
    names = referenced(loaded["mesh-en"])
    assert names == referenced(loaded["mesh-xx"])
    assert {"device_tracker.mesh_alice", "device_tracker.mesh_hilltop"} <= names
    assert WRITE not in names
    controls = {
        "switch.mesh_alice_telemetry",
        "button.mesh_alice_remove",
        "button.mesh_hilltop_remove",
    }
    assert controls <= names  # IMP-049: the household's controls; a repeater is never asked
    assert "switch.mesh_hilltop_telemetry" not in names
    assert len(names) > 20
    fields = {e for e in names if re.fullmatch(r"(text|button)\.\w+_write(_\w+)?", e)}
    assert fields == {
        f"text.{PREFIX}_write_{alias}" for alias in ("alerts", "my_target", "operator")
    } | {f"button.{PREFIX}_write_{alias}_send" for alias in ("alerts", "my_target", "operator")}
    registered = {e.entity_id for e in er.async_get(hass).entities.values()}
    assert names <= registered
    assert problems(caplog) == []


async def test_the_dashboard_reads_the_sensors_of_a_gateway_of_2_1(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The messages, the messages on their way and the configuration: sensors that the gateway
    announces since 2.1, each named by the dashboard and in the registry once it is announced."""
    config = make_config()
    names = referenced(await made(hass, hass_ws_client, config, tmp_path))
    assert set(OF_2_1) <= names
    await gateway(hass, config)
    registered = {e.entity_id for e in er.async_get(hass).entities.values()}
    assert sorted(set(OF_2_1) - registered) == []
    assert problems(caplog) == []


async def test_the_dashboard_uses_only_the_frontends_cards(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    text, _ = dashboard.dashboard(make_config(), tmp_path)
    views = (await load(hass, hass_ws_client, {URL: text}))[URL]["views"]
    assert {v["type"] for v in views} <= VIEWS
    sections = [s for v in views for s in v["sections"]]
    assert {s["type"] for s in sections} <= SECTIONS
    used = {c["type"] for s in sections for c in walk(s["cards"])}
    used |= {c["type"] for v in views for c in walk(v.get("footer", {}).get("card", {}))}
    # no map: no tracker; button: a heading's badges; vertical-stack: a chat page's footer
    assert used == {
        "heading",
        "markdown",
        "tile",
        "entities",
        "history-graph",
        "button",
        "vertical-stack",
    }
    assert used <= CARDS
    assert problems(caplog) == []


# --- every card in every situation --------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_card_renders_in_every_situation(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    language: str,
) -> None:
    """Contract items 10 and 11: strictly and without a warning or a blank line: no gateway,
    not online yet, offline, no report yet, the radio disconnected, a gateway of 2.0 (none of the
    sensors of 2.1), the messages waiting, a problem, no message yet, the most messages the
    history and the pending messages hold, the configuration with telemetry off and its targets
    cut, offline with messages. No card listens to a domain or to every state. The status line's
    words in each: test_overview.py."""
    config = speaking(language, monkeypatch)
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    seen: dict[str, dict[str, str]] = {}

    def situation(name: str) -> None:
        seen[name] = rendered(hass, loaded)
        assert listening(hass, loaded) == [], name

    situation("no gateway")
    assert render(hass, card(loaded, HEARD)) == said(
        config, "The gateway's controls are not in Home Assistant yet."
    )
    topics = await gateway(hass, config, online=False)
    situation("not online yet")
    await fire(hass, topics.availability, "offline", retain=True)
    situation("offline")
    await fire(hass, topics.availability, "online")
    situation("no report yet")
    await fire(hass, topics.state, state_document(known=False), retain=True)
    situation("radio disconnected")
    await fire(hass, topics.state, state_document(known=True) | {"queue": 0})
    for entity in OF_2_1:  # discovered by a gateway of 2.1
        hass.states.async_remove(entity)
    situation("a gateway of 2.0")
    assert render(hass, card(loaded, TABLE)) == said(
        config, "No contact has a device in Home Assistant."
    )
    update = (
        "Chats need gateway version 2.1 or later. Ask your administrator to update the gateway."
    )
    assert render(hass, card(loaded, "macro entry(e)")).startswith(f"<div>{said(config, update)}")
    await fire(hass, topics.state, state_document(known=True))
    situation("messages waiting")
    await fire(hass, topics.nodes, {"total": 1, "look_alike": [BOB], "look_alike_total": 1})
    situation("a problem")
    documented(hass, HISTORY, [])
    documented(hass, PENDING, [])
    situation("no message yet")
    assert render(hass, card(loaded, "macro entry(e)")).startswith(
        "<div>" + said(config, "No message yet.") + "</div>"
    )
    _, pending = entries(0, "Bob", "x" * 160)
    worst = [entries(i, "Bob", "<&>" * 53)[0]["messages"][i % 2] for i in range(50)]
    documented(hass, HISTORY, worst, truncated=True, total=60)
    documented(hass, PENDING, pending["messages"] * 6, truncated=True, total=9)
    situation("the most messages")
    configuration = {
        "telemetry": {"enabled": False},
        "messages": {"truncate": False, "dedup_window_s": 120},
        "targets": {"total": 200, "truncated": True, "items": []},
    }
    hass.states.async_set(CONFIGURATION, iso(time.time()), configuration)
    situation("telemetry off, targets cut")
    await fire(hass, topics.availability, "offline")
    situation("offline with messages")
    for name, outputs in seen.items():
        assert form_findings(outputs) == [], name
    assert problems(caplog) == []


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_contacts_table_lists_fifty_contacts_newest_first(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    language: str,
) -> None:
    """Each with the time since it was heard, as the card's text, or no time."""
    config = speaking(language, monkeypatch)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    many = [Node(f"{i:02x}" * 32, f"Contact {i}", COMPANION) for i in range(16, 64)]
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE, *many)
    contacts_of(config, tmp_path, broker).update(mesh)
    await deliver(hass, config, broker, mesh)
    now = time.time()
    for node, ago in zip(many, (330, 3.5 * 3600, 2.5 * 86400), strict=False):
        document = contact_document(mesh.contacts[node.public_key], telemetry_state="answering")
        await fire(hass, topics.contact(node.public_key), document | {"heard_at": iso(now - ago)})
    outputs = rendered(hass, loaded)
    table = render(hass, card(loaded, TABLE))
    assert table.count("<tr>") == 51
    times = [
        said(config, text, n=n)
        for text, n in (("{n} min ago", "5"), ("{n} h ago", "3"), ("{n} d ago", "2"))
    ]
    found = [table.find(f"<td>{t}</td>") for t in times]
    assert -1 not in found
    assert found == sorted(found)  # newest first
    assert table.count(f"<td>{NONE}</td><td>") == 46  # never heard: no time
    assert listening(hass, loaded) == []
    assert form_findings(outputs) == []
    assert problems(caplog) == []


# what else anyone may write, or a radio may hold in a name (#114, H-22): direction and invisible
# characters, controls, the soft hyphen, line and paragraph separators, NUL
INVISIBLE = [
    "right\u202eleft",
    "zero\u200bwidth",
    "iso\u2066late\u2069",
    "ctl\u0001x",
    "nel\u0085x",
    "soft\u00adhy",
    "ls\u2028ps\u2029x",
    "nul\u0000x",
]
# what m() drops or turns into a space: no output may hold one (U+200B: only mt()'s breaks)
DROPPED = re.compile(
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u00ad\u061c\u200e\u200f\u202a-\u202e\u2028\u2029\u2060-\u2064\u2066-\u2069\ufeff]"
)


def entries(i: int, name: str, text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A message from `name` on channel 1 and one to Alice that failed for the reason `name`,
    both with `text`, and one to Alice on its way: the history and the pending messages as the
    gateway of 2.1 publishes them (docs/mqtt-api.md, History and Messages on their way)."""
    at = iso(time.time() - 60 - i)
    keys: dict[str, Any] = {"hops": None, "snr_db": None, "suspect": None, "reason": None}
    history = [
        keys
        | {"at": at, "direction": "in", "kind": "channel", "contact_key": None, "channel_index": 1}
        | {"name": name, "text": text, "state": None, "target": "alerts", "id": None}
        | {"queued_at": None},
        keys
        | {"at": at, "direction": "out", "kind": "contact", "contact_key": ALICE}
        | {"channel_index": None, "name": name, "text": text, "state": "failed", "target": None}
        | {"id": f"h{i}", "queued_at": at, "suspect": False, "reason": name},
    ]
    pending = {
        "id": f"p{i}",
        "queued_at": iso(time.time() - 5),
        "target": None,
        "kind": "contact",
        "contact_key": ALICE,
        "channel_index": None,
        "name": name,
        "text": text,
        "step": "sent",
        "attempt": 1,
        "max_attempts": 3,
    }
    return (
        {"total": 2, "truncated": False, "messages": history},
        {"total": 1, "truncated": False, "messages": [pending]},
    )


@pytest.mark.parametrize("language", LANGUAGES)
async def test_no_name_or_text_from_the_mesh_leaves_its_html_element(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    language: str,
) -> None:
    """Contract item 9: each hostile name in turn as a contact's (device and tracker) name, a
    message's sender and recipient, a reason, the name of a contact event, a heard node (raw
    and labelled), the version and the contact link; each text as a message's, received, sent
    and on its way, and as a refused one: every card shows it escaped on one line that is one
    HTML element, no card has a blank line, a Jinja expression stays text, no image and no link
    but the dashboard's own, one alert in the refused line, no control, direction or invisible
    character, and a zero-width space only from mt(). The messages are set as the gateway of 2.1
    announces them (documented())."""
    config = speaking(language, monkeypatch)
    broker = Broker()
    devices = contacts_of(config, tmp_path, broker)
    devices.update(household(ALICE_NODE))  # the dashboard made when only Alice had a position
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    mesh = household(ALICE_NODE, HILL_NODE, *hostile())
    devices.update(mesh)
    await deliver(hass, config, broker, mesh, telemetry_state="no_position")
    places = {
        EVENT: "name",
        TABLE: "device name",
        HEARD: "a heard node's label",
        HINT: "tracker not on the map",
        QR: "contact link",
        "Why someone is missing": "device name, where everyone is",
        "macro entry(e)": "sender, Latest messages",
    }
    alices = card(cards(loaded, "alice"), "macro row(e)")
    every: dict[str, str] = {}
    corpus = [*zip(KEYS, NAMES, TEXTS, strict=True)]
    corpus += [(f"{0x60 + i:02x}" * 32, x, x) for i, x in enumerate(INVISIBLE)]
    without_mt = {i for i, c in enumerate(markdown(loaded)) if "macro mt(x)" not in c["content"]}
    refused = page(loaded, "alice")["footer"]["card"]["cards"][0]
    for i, (key, name, text) in enumerate(corpus):
        history, pending = entries(i, name, text)
        hass.states.async_set(HISTORY, history["messages"][0]["at"], history)
        hass.states.async_set(PENDING, "1", pending)
        duplicate = DeliveryEvent(f"m{i}", ALICE, DeliveryState.DUPLICATE, text, 0)
        await fire(hass, topics.delivery, delivery_payload(duplicate))
        await fire(
            hass,
            topics.contact_event,
            contact_event(
                "refused",
                key,
                name=name,
                node_type=COMPANION,
                request="add",
                request_id=f"r{i}",
                reason="look_alike",
            ),
        )
        await fire(
            hass,
            topics.state,
            state_document(known=True) | {"version": name, "contact_link": name},
        )
        outputs = rendered(hass, loaded)
        outputs["the refused line"] = render(hass, refused)
        every |= {f"{i} {where}": output for where, output in outputs.items()}
        assert form_findings(outputs, [name, text]) == [], (i, name)
        for n, (where, output) in enumerate(outputs.items()):
            assert "<img" not in output, where
            assert not DROPPED.search(output), (where, name)
            assert n not in without_mt or "\u200b" not in output, where
            for href in re.findall(r'href="([^"]*)"', output):
                assert href.startswith(("./", "https://github.com/")), (where, href)
        if "{{ 7 * 7 }}" in text:  # shown as written, never worked out (the times aside)
            said = render(hass, card(loaded, "macro entry(e)"))
            assert "{{ 7 * 7 }}" in said
            assert "49" not in re.sub(r"\d\d:\d\d", "", said)
        if i >= len(NAMES):
            continue
        for needle, place in places.items():
            said = render(hass, card(loaded, needle)).replace("\u200b", "")
            assert shown(name) in said, (name, place)
        said = render(hass, alices).replace("\u200b", "")
        assert shown(name) in said, (name, "Alice's thread: the reason, a code")
        for each in (alices, card(loaded, "macro entry(e)")):
            said = render(hass, each).replace("\u200b", "")
            assert shown(text) in said, (text, each["content"][:40])
        said = outputs["the refused line"].replace("\u200b", "")
        assert shown(text) in said, text
        assert said.count("<ha-alert") == 1
    keep(language, every)
    assert problems(caplog) == []


async def test_long_texts_with_many_escapes_stay_within_a_card(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 13: 50 messages of 160 characters that escape to several times their
    length, and 50 contacts: every card stays under 200 000 characters, renders in under 50 ms
    (the median of five renders), and keeps its lines whole."""
    config = make_config()
    many = [Node(f"{i:02x}" * 32, f"Contact {i}", COMPANION) for i in range(16, 66)]
    broker, mesh = Broker(), household(ALICE_NODE, *many)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    heavy = ("<&>\"'" * 32)[:160]
    messages = [entries(i, f"Contact {i + 16}", heavy)[0]["messages"][i % 2] for i in range(50)]
    documented(hass, HISTORY, messages, truncated=True, total=50)
    outputs = {}
    for each in markdown(loaded):
        times = []
        for _ in range(5):
            started = time.perf_counter()
            output = render(hass, each)
            times.append(time.perf_counter() - started)
        where = each["content"][:60]
        outputs[where] = output
        assert len(output) < 200_000, where
        assert sorted(times)[2] < 0.05, (where, sorted(times)[2])
    assert form_findings(outputs, [heavy]) == []
    assert problems(caplog) == []


# --- what shows when ----------------------------------------------------------------------------


async def test_no_card_needs_the_guides_script(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 18, HA-09 (#110), D-22: no card names the script Mesh message, and no
    condition is a state condition on an entity that may be missing (rule 4): writing is the
    chat pages' fields."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    assert WRITE not in json.dumps(loaded)
    assert "state_not" not in json.dumps(loaded)
    states = [
        c
        for node in walk(loaded)
        for c in [*node.get("visibility", []), *node.get("conditions", [])]
        if c.get("condition") == "state"
    ]
    assert all(c["entity"] == f"binary_sensor.{PREFIX}_radio" and "attribute" in c for c in states)
    assert problems(caplog) == []


# --- the map --------------------------------------------------------------------------------------


async def test_the_map_lists_the_trackers_of_the_state_file_and_names_the_others(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The trackers known when the dashboard was made; without a state file a note instead of
    the map; the hint names this gateway's contacts with a position that the map lacks, by key:
    for Alice shared with gateway radio-2, radio-2's dashboard lists radio1's tracker of her and
    names nobody."""
    config, other = make_config(), make_config(node_id="radio-2")
    broker = Broker()
    devices = contacts_of(config, tmp_path / "radio1", broker)
    devices.update(household(ALICE_NODE))
    early, count = dashboard.dashboard(config, tmp_path / "radio1")
    assert count == 1
    mesh = household(ALICE_NODE, HILL_NODE, *hostile())
    devices.update(mesh)
    full, count = dashboard.dashboard(config, tmp_path / "radio1")
    assert count == 2 + len(NAMES)
    shared = Broker()
    contacts_of(other, tmp_path / "radio-2", shared).update(
        household(Node(ALICE, "Alice", COMPANION, position=HOME))
    )
    files = {
        "mesh-early": early,
        URL: full,
        "mesh-none": dashboard.dashboard(config, tmp_path)[0],
        "mesh-radio-2": dashboard.dashboard(other, tmp_path / "radio-2")[0],
    }
    loaded = await load(hass, hass_ws_client, files)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    await gateway(hass, other)
    await deliver(hass, other, shared, household(Node(ALICE, "Alice", COMPANION, position=HOME)))

    trackers = set(hass.states.async_entity_ids("device_tracker"))
    (shown_map,) = [c for c in walk(loaded[URL]) if c["type"] == "map"]
    assert shown_map["entities"] == sorted(shown_map["entities"])
    assert set(shown_map["entities"]) == trackers - {"device_tracker.mesh_alice_2"}
    assert (shown_map["hours_to_show"], shown_map["theme_mode"]) == (24, "auto")
    shows_nothing(hass, card(loaded[URL], HINT))
    assert not [c for c in walk(loaded["mesh-none"]) if c["type"] == "map"]
    note = said(
        config,
        "No contact had a position when this dashboard was made. Generate it again once one has.",
    )
    assert note in json.dumps(loaded["mesh-none"], ensure_ascii=False)
    hint = render(hass, card(loaded["mesh-early"], HINT))
    assert hint.startswith('<div><ha-alert alert-type="info">Not on this map: ')
    for name in ("Hilltop", *NAMES):
        assert shown(name) in hint, name
    assert "Alice" not in hint

    (second_map,) = [c for c in walk(loaded["mesh-radio-2"]) if c["type"] == "map"]
    assert second_map["entities"] == ["device_tracker.mesh_alice"]  # radio1's tracker of her
    assert "device_tracker.mesh_alice_2" in trackers  # radio-2's own
    shows_nothing(hass, card(loaded["mesh-radio-2"], HINT))
    assert hass.states.get("sensor.mesh_alice_telemetry_state_2").state == "answering"
    answering = f"<td>{said(other, 'answering')}</td></tr>"
    assert answering in render(hass, card(loaded["mesh-radio-2"], TABLE))  # her IDs end in _2
    assert problems(caplog) == []


async def test_a_dashboard_shows_only_its_own_gateways_contacts(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Zed, a contact of gateway radio-2 with a position, is in neither radio1's contacts table
    nor its map's hint, and in both of radio-2's."""
    config, other = make_config(), make_config(node_id="radio-2")
    first, second = Broker(), Broker()
    contacts_of(config, tmp_path / "radio1", first).update(household(ALICE_NODE))
    zed = household(Node(ZED, "Zed", COMPANION, position=HOME))
    contacts_of(other, tmp_path / "radio-2", second).update(zed)
    files = {
        URL: dashboard.dashboard(config, tmp_path / "radio1")[0],
        "mesh-radio-2": dashboard.dashboard(other, tmp_path)[0],
    }
    loaded = await load(hass, hass_ws_client, files)
    await gateway(hass, config)
    await deliver(hass, config, first, household(ALICE_NODE))
    await gateway(hass, other)
    await deliver(hass, other, second, zed)
    assert "<td>Alice</td>" in render(hass, card(loaded[URL], TABLE))
    for needle in (TABLE, HINT):
        assert "Zed" not in render(hass, card(loaded[URL], needle)), needle
        assert "Zed" in render(hass, card(loaded["mesh-radio-2"], needle)), needle
    assert problems(caplog) == []


# --- the chat pages' footers ------------------------------------------------------------------


def drafts(owner: str, entity: str, *, via: str | None = None, key: str = "") -> dict[str, Any]:
    """A discovery document that gives the device `owner` a draft field and its Send button with
    the entity IDs the gateway gives them; `via` and `key`: a contact's device below the gateway."""
    device: dict[str, Any] = {"ids": [owner]}
    if via is not None:
        device |= {"via_device": via, "sn": key}
    return {
        "dev": device,
        "o": {"name": "contract test"},
        "cmps": {
            f"{entity}_text": {
                "p": "text",
                "unique_id": f"{owner}_{entity}",
                "default_entity_id": f"text.{entity}",
                "command_topic": f"test/{entity}",
            },
            f"{entity}_send": {
                "p": "button",
                "unique_id": f"{owner}_{entity}_send",
                "default_entity_id": f"button.{entity}_send",
                "command_topic": f"test/{entity}",
            },
        },
    }


async def test_a_chat_pages_footer_shows_once_its_own_fields_exist(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """For a household user: the footer of a target's chat, of Alice's (whose field is the target
    operator's, a field of the gateway) and of Bob's (his own device's field) hides while its
    entities are missing and shows once they exist, each on the device the guard pins."""
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE, Node(BOB, "Bob", COMPANION))
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    viewer = await household_viewer(hass, hass_ws_client)
    footers = {v["path"]: (v, v["footer"]["card"]) for v in loaded["views"] if "footer" in v}
    assert set(footers) == {"chat-alerts", "chat-my-target", "contact-alice", "contact-bob"}
    await gateway(hass, config, drafts=False)
    await deliver(hass, config, broker, mesh, drafts=False)
    assert not [path for path, footer in footers.items() if await viewer.shown(*footer)]
    own = discovery_payload(config, topics_of(config), RADIO)["dev"]["ids"][0]
    for alias in ("alerts", "my_target", "operator"):
        topic = f"homeassistant/device/{own}_{alias}/config"
        await fire(hass, topic, drafts(own, f"{own}_write_{alias}"))
    bob = f"{own}_contact_{BOB}"
    await fire(
        hass,
        f"homeassistant/device/{bob}_drafts/config",
        drafts(bob, "mesh_bob_write", via=own, key=BOB),
    )
    shown = [path for path, footer in footers.items() if await viewer.shown(*footer)]
    assert shown == list(footers), viewer.errors
    assert problems(caplog) == []


async def test_a_chat_pages_footer_hides_while_one_of_its_fields_cannot_be_used(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The guard tests each entity of a footer on its own: a field that is unavailable while its
    button is not, a button with the expected ID on another device than its field, and a field
    registered without a state (disabled) each hide their footer; a chat whose fields are as
    the gateway announces them shows its footer."""
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE, Node(BOB, "Bob", COMPANION))
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    viewer = await household_viewer(hass, hass_ws_client)
    footers = {v["path"]: (v, v["footer"]["card"]) for v in loaded["views"] if "footer" in v}
    await gateway(hass, config, drafts=False)
    await deliver(hass, config, broker, mesh, drafts=False)
    own = discovery_payload(config, topics_of(config), RADIO)["dev"]["ids"][0]
    await fire(
        hass, f"homeassistant/device/{own}_alerts/config", drafts(own, f"{own}_write_alerts")
    )
    unavailable = drafts(own, f"{own}_write_my_target")  # never online
    unavailable["cmps"][f"{own}_write_my_target_text"]["availability_topic"] = "test/never"
    await fire(hass, f"homeassistant/device/{own}_my_target/config", unavailable)
    split = drafts(own, f"{own}_write_operator")  # Alice's page: the button on another device
    foreign = {"dev": {"ids": ["foreign"]}, "o": split["o"], "cmps": {}}
    foreign["cmps"]["send"] = split["cmps"].pop(f"{own}_write_operator_send")
    await fire(hass, f"homeassistant/device/{own}_operator/config", split)
    await fire(hass, "homeassistant/device/foreign/config", foreign)
    bob = f"{own}_contact_{BOB}"
    disabled = drafts(bob, "mesh_bob_write", via=own, key=BOB)
    disabled["cmps"]["mesh_bob_write_text"]["enabled_by_default"] = False
    await fire(hass, f"homeassistant/device/{bob}_drafts/config", disabled)
    assert hass.states.get(f"text.{own}_write_my_target").state == "unavailable"
    assert hass.states.get(f"button.{own}_write_operator_send").state != "unavailable"
    assert hass.states.get("text.mesh_bob_write") is None
    assert er.async_get(hass).async_get("text.mesh_bob_write") is not None
    shown = [path for path, footer in footers.items() if await viewer.shown(*footer)]
    assert shown == ["chat-alerts"], viewer.errors
    assert problems(caplog) == []


# --- the guide's script -------------------------------------------------------------------------


async def test_the_guides_script_is_a_form_that_sends_for_a_household_user(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Its more-info dialog shows a form of its fields (the frontend reads them from the services'
    descriptions): `To` picks a notify entity of MQTT, a target or a contact. A user who is no
    administrator sends with it to a contact and to a target, each one publish with QoS 1."""
    config = make_config()
    topics = await gateway(hass, config)
    broker, mesh = Broker(), household(ALICE_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    await deliver(hass, config, broker, mesh)
    script = await guide_script(hass)
    assert hass.states.get(WRITE) is not None

    user = await hass.auth.async_create_user("Bob", group_ids=[GROUP_ID_USER])
    assert not user.is_admin
    token = hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    )
    client = await hass_ws_client(hass, token)
    # what `get_services` sends the frontend for it; that command also reads every other
    # integration's services.yaml, some of which need packages the test environment lacks
    description = async_get_cached_service_description(hass, "script", "mesh_message")
    assert description is not None
    fields = description["fields"]
    assert set(fields) == {"to", "message"}
    assert fields["to"]["required"] is True
    (pick,) = fields["to"]["selector"]["entity"]["filter"]
    assert pick == {"integration": "mqtt", "domain": ["notify"]}  # as the script loader keeps it
    assert (fields["message"]["required"], set(fields["message"]["selector"])) == (True, {"text"})
    assert script["alias"] == "Mesh message"

    alice, alerts = "notify.mesh_alice", "notify.meshcore_home_gateway_radio1_send_alerts"
    # also texts Home Assistant would read as numbers or None, and spaces it would strip
    for to, text in (
        (alice, "Dinner <b>now</b>"),
        (alerts, "Gate open"),
        (alice, "+493023125000"),
        (alice, "1.10"),
        (alerts, "None"),
        (alice, "  spaced  "),
    ):
        mqtt.async_publish.reset_mock()
        await client.send_json_auto_id(
            {
                "type": "call_service",
                "domain": "script",
                "service": "mesh_message",
                "service_data": {"to": to, "message": text},
            }
        )
        answer = await client.receive_json()
        assert answer["success"], answer
        await hass.async_block_till_done()
        (sent,) = [c.args[:3] for c in mqtt.async_publish.call_args_list]
        expected = topics.contact_send(ALICE) if "alice" in to else topics.send("alerts")
        assert sent == (expected, text, 1)
    assert problems(caplog) == []


# --- machine values as texts --------------------------------------------------------------------

# each step of a delivery as a chat shows it: in the thread (the history's final states, the pending
# messages' steps) or in the footer's refused line
STEPS = {
    "queued": "… waiting to send",
    "sent": "… sent, waiting for confirmation (try 1 of 3)",
    "confirmed": "✓✓ delivered",
    "failed": "✗ not delivered (no acknowledgement)",
    "expired": "✗ not sent: waited too long (no radio)",
    "duplicate": "Not sent again: the same text went to this chat a moment ago.",
    "rejected": "Not sent: text too long.",
}
STATES = {
    "waiting": "waiting",
    "answering": "answering",
    "no_position": "no position",
    "away": "away",
    "no_answer": "no answer",
    "no_path": "no path",
    "look_alike": "look-alike",
    "excluded": "excluded",
    "off": "off",
}


async def test_steps_states_and_contact_events_show_as_texts(  # noqa: PLR0915 - one by one
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A delivery step (in a chat's thread or its refused line), a telemetry state, a contact
    event and a reason as the dashboard's text; a reason added later, or one of the radio's
    refusals, as its code: the codes are an open list (docs/mqtt-api.md)."""
    assert set(STEPS) == {s.value for s in DeliveryState}
    assert set(STATES) == set(telemetry.STATES)
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    event, table = card(loaded, EVENT), card(loaded, TABLE)
    alice = page(loaded, "alice")
    thread, line = (
        card(cards(loaded, "alice"), "macro row(e)"),
        alice["footer"]["card"]["cards"][0],
    )

    assert render(hass, event) == "No contact event yet."
    await fire(
        hass,
        topics.contact_event,
        contact_event(
            "refused",
            BOB,
            name="Bob",
            node_type=COMPANION,
            request="add",
            request_id="r1",
            reason="look_alike",
        ),
    )
    refused = "<div><b>Bob</b> request refused (another contact has the same key prefix) "
    assert render(hass, event).startswith(refused)
    await fire(hass, topics.contact_event, contact_event("added", ALICE, ALICE_NODE))
    added = r"<div><b>Alice</b> added to the radio \d+ min ago</div>"
    assert re.fullmatch(added, render(hass, event))

    at = iso(time.time() - 30)
    keys = {"kind": "contact", "contact_key": ALICE, "channel_index": None, "name": "Alice"}
    keys |= {"text": "Gate open", "target": "operator", "id": "m1", "queued_at": at}
    for step, text in STEPS.items():
        documented(hass, HISTORY, [])
        documented(hass, PENDING, [])
        state = DeliveryState(step)
        if step in ("queued", "sent"):
            entry = keys | {"step": step, "attempt": int(step == "sent"), "max_attempts": 3}
            documented(hass, PENDING, [entry])
        elif step in ("rejected", "duplicate"):
            reason = "too_long" if step == "rejected" else None
            await fire(
                hass,
                topics.delivery,
                delivery_payload(DeliveryEvent("r", ALICE, state, reason=reason)),
            )
            assert text in render(hass, line), step
            continue
        else:
            reason = {"failed": "no_acknowledgement", "expired": "no_radio"}.get(step)
            entry = keys | {"at": at, "direction": "out", "state": step, "suspect": False}
            documented(hass, HISTORY, [entry | {"reason": reason, "hops": None, "snr_db": None}])
        assert text in render(hass, thread), step
    documented(
        hass,
        HISTORY,
        [keys | {"at": at, "direction": "out", "state": "failed"} | {"reason": "error_7"}],
    )
    assert "✗ not delivered (<code>error_7</code>)" in render(hass, thread)  # the radio's: open
    documented(
        hass,
        HISTORY,
        [keys | {"at": at, "direction": "out", "state": "confirmed"} | {"suspect": True}],
    )
    assert "⚠ delivered? maybe forged" in render(hass, thread)  # SEC-001

    row = "<tr><td>Alice</td><td>Companion</td><td>0 min ago</td><td>{}</td></tr>"
    for state, text in STATES.items():
        document = contact_document(mesh.contacts[ALICE]) | {"telemetry_state": state}
        await fire(hass, topics.contact(ALICE), document)
        assert row.format(text) in render(hass, table), state
    await fire(hass, topics.contact(ALICE), contact_document(mesh.contacts[ALICE]))
    assert row.format(NONE) in render(hass, table)  # unknown
    hilltop = f"<tr><td>Hilltop</td><td>Repeater</td><td>{NONE}</td><td>{NONE}</td></tr>"
    assert hilltop in render(hass, table)  # a repeater is not asked
    assert problems(caplog) == []


async def test_the_cards_say_how_long_ago(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The last contact event and a contact's last heard time, minutes, hours and days later:
    each card counts on its own. A chat shows a message's clock time: today "10:31", the day
    before "yesterday 10:31", older ones with the day and the month (D-9)."""
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    now = time.time()
    message = InboundMessage(
        MessageKind.CONTACT, "Dinner", 1_760_000_000, now, sender_prefix=ALICE[:12]
    )
    await fire(hass, topics.received, received_payload(message, Contact(ALICE, "Alice"), None))
    await fire(hass, topics.contact_event, contact_event("added", ALICE, ALICE_NODE))
    document = contact_document(mesh.contacts[ALICE], telemetry_state="answering")
    await fire(hass, topics.contact(ALICE), document | {"heard_at": iso(now)})
    for seconds, text, n in (
        (330, "{n} min ago", "5"),
        (3.5 * 3600 - 330, "{n} h ago", "3"),
        (2 * 86400, "{n} d ago", "2"),
    ):
        freezer.tick(timedelta(seconds=seconds))
        ago = said(config, text, n=n)
        assert (
            render(hass, card(loaded, EVENT)) == f"<div><b>Alice</b> added to the radio {ago}</div>"
        )
        assert f"<td>Alice</td><td>Companion</td><td>{ago}</td>" in render(
            hass, card(loaded, TABLE)
        )
    thread = card(cards(loaded, "alice"), "macro row(e)")
    at = dt_util.as_local(dt_util.utc_from_timestamp(now))
    keys = {"direction": "in", "kind": "contact", "contact_key": ALICE, "name": "Alice"}
    documented(hass, HISTORY, [keys | {"at": iso(now), "text": "Dinner"}])
    output = render(hass, thread)
    month = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    assert f"<small>{at.day} {month[at.month - 1]} {at:%H:%M}</small>" in output
    freezer.move_to(dt_util.utc_from_timestamp(now + 86400))
    assert f"<small>yesterday {at:%H:%M}</small>" in render(hass, thread)
    freezer.move_to(dt_util.utc_from_timestamp(now + 60))
    assert f"<small>{at:%H:%M}</small>" in render(hass, thread)
    assert problems(caplog) == []
