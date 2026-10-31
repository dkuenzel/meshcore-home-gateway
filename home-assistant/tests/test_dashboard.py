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
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.service import async_get_cached_service_description
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify
from pytest_homeassistant_custom_component.common import CLIENT_ID
from test_contract import RADIO, fire, guide_block, make_config, problems, state_document
from test_languages import offer

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
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from meshcore_home_gateway.views import contact_document, contact_event, iso
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, HILL_NODE, NOW, Broker, mesh_of

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
CARDS = frozenset({
    "entity", "entities", "button", "entity-button", "glance", "grid", "section", "light",
    "sensor", "thermostat", "weather-forecast", "tile", "heading", "alarm-panel", "area",
    "calendar", "conditional", "empty-state", "energy-compare", "energy-carbon-consumed-gauge",
    "energy-date-selection", "energy-devices-graph", "energy-devices-detail-graph",
    "energy-distribution", "energy-gas-graph", "energy-water-graph",
    "energy-grid-neutrality-gauge", "energy-grid-balance", "energy-solar-consumed-gauge",
    "energy-self-sufficiency-gauge", "energy-solar-graph", "energy-sources-table",
    "energy-usage-graph", "energy-sankey", "water-sankey", "water-flow-sankey",
    "power-sources-graph", "power-sankey", "entity-filter", "error", "home-summary", "shortcut",
    "discovered-devices", "repairs", "alert", "updates", "gauge", "history-graph",
    "horizontal-stack", "distribution", "humidifier", "iframe", "logbook", "map", "markdown",
    "clock", "media-control", "picture-elements", "picture-entity", "picture-glance", "picture",
    "plant-status", "recovery-mode", "toggle-group", "todo-list", "shopping-list", "starting",
    "statistics-graph", "statistic", "vertical-stack",
})  # fmt: skip
VIEWS = frozenset({"masonry", "panel", "sidebar", "sections"})
SECTIONS = frozenset({"grid"})
# the Markdown cards, by a part of their content or visibility
STATUS, VERSION, WRITE = "set problems", "bridge_version", "script.mesh_message"
MESSAGE, SENT, TABLE = "_message'", "_delivery'", "_last_heard"
EVENT, HEARD, QR, HINT = "_contact'", "_add_node'", "contact_link", "set listed"
# the line a value from Home Assistant stands on: one whole HTML element
ELEMENT = re.compile(r"<div>.*</div>|<table>.*</table>|<ha-qr-code .*></ha-qr-code>")
NONE = "\u2013"  # what a card shows for no value: an en dash
MARKDOWN = "MESHCORE_HOME_GATEWAY_MARKDOWN_DIR"
ENTITY_ID = re.compile(
    r"\b(?:binary_sensor|sensor|event|notify|select|text|button|script|device_tracker)"
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
    records = ContactDeviceRecords(JsonStateFile(store / "state.json"))
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


async def gateway(hass: HomeAssistant, config: Config, *, online: bool = True) -> Topics:
    """The gateway's document and controls; the heard nodes offered are the hostile names, raw
    and as the gateway labels them, and Bob."""
    topics = topics_of(config)
    await fire(hass, discovery_topic(config), discovery_payload(config, topics, RADIO))
    labels = [node_label(key, name, COMPANION) for key, name in zip(KEYS, NAMES, strict=True)]
    options = [*labels, *NAMES, node_label(BOB, "Bob", COMPANION)]
    await fire(hass, manage_topic(config), manage_payload(config, topics, options))
    if online:
        await fire(hass, topics.availability, "online", retain=True)
        await fire(hass, topics.state, state_document(known=True) | {"queue": 0}, retain=True)
    return topics


async def deliver(
    hass: HomeAssistant, config: Config, broker: Broker, mesh: Mesh, **state: Any
) -> None:
    """What the contacts' devices announced, and each contact's document, retained."""
    topics = topics_of(config)
    await broker.deliver(hass)
    await fire(hass, topics.availability, "online", retain=True)
    for key, tracked in mesh.contacts.items():
        companion = tracked.node.node_type == COMPANION
        document = contact_document(tracked, telemetry_state="answering" if companion else None)
        await fire(hass, topics.contact(key), document | state, retain=True)


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


def markdown(loaded: dict[str, Any]) -> list[dict[str, Any]]:
    return [card for card in walk(loaded) if card["type"] == "markdown"]


def card(loaded: dict[str, Any], needle: str) -> dict[str, Any]:
    """The one Markdown card whose content or visibility holds `needle`."""
    (found,) = [c for c in markdown(loaded) if needle in json.dumps(c)]
    return found


def render(hass: HomeAssistant, found: dict[str, Any]) -> str:
    """As the frontend asks for it: strict (an error fails the test), `config` and `user`."""
    variables = {"config": found, "user": "Bob"}
    return str(
        Template(found["content"], hass).async_render(variables, parse_result=False, strict=True)
    )


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


def visible(hass: HomeAssistant, found: dict[str, Any]) -> bool:
    """The card's visibility, as the frontend decides `state` conditions (validate-condition.ts
    lines 161-207 at 20260930.2): a missing entity or attribute reads `unknown`, a value naming an
    entity adds its state, and values compare as they are, so a YAML boolean matches no state."""
    for condition in found.get("visibility", []):
        assert condition["condition"] == "state", condition
        state = hass.states.get(condition["entity"])
        attribute = condition.get("attribute")
        value = (
            "unknown"
            if state is None or (attribute and state.attributes.get(attribute) is None)
            else str(state.attributes[attribute]) if attribute else state.state
        )  # fmt: skip
        positive = condition.get("state") is not None
        wanted = condition["state"] if positive else condition["state_not"]
        values = [*wanted] if isinstance(wanted, list) else [wanted]
        values += [s.state for v in values if isinstance(v, str) and (s := hass.states.get(v))]
        if (value in values) != positive:
            return False
    return True


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


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_dashboard_loads_as_a_yaml_mode_dashboard(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, caplog: pytest.LogCaptureFixture, language: str,
) -> None:  # fmt: skip
    """For a node ID with and without '-'; in xx every title is the catalog's."""
    files = {}
    for node_id in ("radio1", "radio-2"):
        config = speaking(language, monkeypatch, node_id)
        broker = Broker()
        contacts_of(config, tmp_path / node_id, broker).update(household(ALICE_NODE))
        files[f"mesh-{node_id}"] = dashboard.dashboard(config, tmp_path / node_id)[0]
    loaded = await load(hass, hass_ws_client, files)
    for node_id, gw in (("radio1", "radio1"), ("radio-2", "radio_2")):
        views = loaded[f"mesh-{node_id}"]["views"]
        assert [v["path"] for v in views] == ["overview", "messages", "contacts", "map"]
        own = {e for e in referenced(views) if ".meshcore_home_gateway_" in e}
        assert len(own) > 15
        assert all(f".meshcore_home_gateway_{gw}_" in e for e in own)
        titles = [loaded[f"mesh-{node_id}"]["title"], *(v["title"] for v in views)]
        titles += [c.get("heading") or c.get("title") or c["name"] for v in views
                   for c in walk(v["sections"])
                   if c["type"] == "heading" or "title" in c or "name" in c]  # fmt: skip
        assert len(titles) == 16
        for title in titles:
            if language == "xx":
                assert title.startswith("‹xx: ")
                assert title.endswith("›")
            else:
                assert "‹" not in title
    assert [v["title"] for v in loaded["mesh-radio1"]["views"]] == [
        said(config, title) for title in ("Overview", "Messages", "Contacts", "Map")
    ]
    assert problems(caplog) == []


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_dashboard_names_only_entities_that_exist(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, caplog: pytest.LogCaptureFixture, language: str,
) -> None:  # fmt: skip
    """The gateway's entities, its controls, the trackers and the guide's script; the same in
    English and in xx, with the household discovered in `language`."""
    offer("xx", monkeypatch, XX)
    config = in_language(make_config(), language)
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    await guide_script(hass)
    files = {
        f"mesh-{each}": dashboard.dashboard(in_language(config, each), tmp_path)[0]
        for each in LANGUAGES
    }
    loaded = await load(hass, hass_ws_client, files)
    names = referenced(loaded["mesh-en"])
    assert names == referenced(loaded["mesh-xx"])
    assert {"device_tracker.mesh_alice", "device_tracker.mesh_hilltop", WRITE} <= names
    assert len(names) > 20
    assert names <= {e.entity_id for e in er.async_get(hass).entities.values()}
    assert problems(caplog) == []


async def test_the_dashboard_uses_only_the_frontends_cards(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    text, _ = dashboard.dashboard(make_config(), tmp_path)
    views = (await load(hass, hass_ws_client, {URL: text}))[URL]["views"]
    assert {v["type"] for v in views} <= VIEWS
    sections = [s for v in views for s in v["sections"]]
    assert {s["type"] for s in sections} <= SECTIONS
    used = {c["type"] for s in sections for c in walk(s["cards"])}
    assert used == {"heading", "markdown", "tile", "entities", "history-graph"}  # no map: none
    assert used <= CARDS
    assert problems(caplog) == []


# --- every card in every situation --------------------------------------------------------------


@pytest.mark.parametrize("language", LANGUAGES)
async def test_every_card_renders_in_every_situation(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, caplog: pytest.LogCaptureFixture, language: str,
) -> None:  # fmt: skip
    """Strictly and without a warning, with the status line's alert of each situation; no card
    listens to a domain or to every state, the contacts table to the time also with no contact."""
    config = speaking(language, monkeypatch)
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    status = card(loaded, STATUS)
    seen: dict[str, dict[str, str]] = {}

    async def situation(name: str, kind: str, msgid: str, **values: str) -> None:
        seen[name] = rendered(hass, loaded)
        assert alert(render(hass, status)) == (kind, said(config, msgid, **values)), name
        assert listening(hass, loaded) == [], name

    await situation("no gateway", "NOTE", "Gateway not found.")
    assert render(hass, card(loaded, HEARD)) == said(
        config, "The gateway's controls are not in Home Assistant yet."
    )
    topics = await gateway(hass, config, online=False)
    await situation("not online yet", "CAUTION", "Gateway offline.")
    await fire(hass, topics.availability, "offline", retain=True)
    await situation("offline", "CAUTION", "Gateway offline.")
    await fire(hass, topics.availability, "online")
    await situation("online without a value", "NOTE", "No value yet.")
    await fire(hass, topics.state, state_document(known=False), retain=True)
    await situation("radio disconnected", "CAUTION", "Radio disconnected.")
    await fire(hass, topics.state, state_document(known=True) | {"queue": 0})
    await situation("no contacts", "TIP", "Gateway ready.")
    assert render(hass, card(loaded, TABLE)) == said(
        config, "No contact has a device in Home Assistant."
    )
    assert render(hass, card(loaded, MESSAGE)) == said(config, "No message yet.")
    assert render(hass, card(loaded, SENT)) == said(config, "Nothing sent yet.")
    await fire(hass, topics.state, state_document(known=True))
    await situation("messages waiting", "WARNING", "Messages waiting: {queue}.", queue="2")
    await fire(hass, topics.nodes, {"total": 1, "look_alike": [BOB], "look_alike_total": 1})
    await situation("a problem", "WARNING", "Check the problems below.")
    await fire(hass, topics.nodes, {"total": 0, "look_alike": [], "look_alike_total": 0})
    await deliver(hass, config, broker, mesh)
    await fire(hass, topics.state, state_document(known=True) | {"queue": 0, "version": "9.9.9"})
    await situation("another version", "TIP", "Gateway ready.")
    assert render(hass, card(loaded, VERSION)) == (
        '<div><ha-alert alert-type="warning">'
        + said(config, "This dashboard was made for version {version}; the gateway runs "
               "{running}. Generate it again.", version=dashboard.__version__, running="9.9.9")
        + "</ha-alert></div>"
    )  # fmt: skip
    for name, outputs in seen.items():
        assert form_findings(outputs) == [], name
    assert problems(caplog) == []


@pytest.mark.parametrize("language", LANGUAGES)
async def test_the_contacts_table_lists_fifty_contacts_newest_first(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, caplog: pytest.LogCaptureFixture, language: str,
) -> None:  # fmt: skip
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
    times = [said(config, text, n=n) for text, n in
             (("{n} min ago", "5"), ("{n} h ago", "3"), ("{n} d ago", "2"))]  # fmt: skip
    found = [table.find(f"<td>{t}</td>") for t in times]
    assert -1 not in found
    assert found == sorted(found)  # newest first
    assert table.count(f"<td>{NONE}</td><td>") == 46  # never heard: no time
    assert listening(hass, loaded) == []
    assert form_findings(outputs) == []
    assert problems(caplog) == []


@pytest.mark.parametrize("language", LANGUAGES)
async def test_no_name_or_text_from_the_mesh_leaves_its_html_element(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path, caplog: pytest.LogCaptureFixture, language: str,
) -> None:  # fmt: skip
    """Each hostile name in turn as a contact's (device and tracker) name, a message's sender, a
    delivery's target, the name of a contact event, a heard node (raw and labelled), the version
    and the contact link; each text as a message's and a delivery's: every card shows it escaped
    on one line that is one HTML element, and no card has a blank line."""
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
        MESSAGE: "sender and text", SENT: "target and text", EVENT: "name", TABLE: "device name",
        HEARD: "a heard node's label", HINT: "tracker not on the map", VERSION: "version",
        QR: "contact link",
    }  # fmt: skip
    every: dict[str, str] = {}
    for i, (key, name, text) in enumerate(zip(KEYS, NAMES, TEXTS, strict=True)):
        message = InboundMessage(
            MessageKind.CONTACT, text, 1_760_000_000 + i, NOW + i, sender_prefix=key[:12]
        )
        await fire(hass, topics.received, received_payload(message, Contact(key, name), None))
        failed = DeliveryEvent(f"m{i}", name, DeliveryState.FAILED, text, 1, "no_acknowledgement")
        await fire(hass, topics.delivery, delivery_payload(failed))
        await fire(hass, topics.contact_event, contact_event(
            "refused", key, name=name, node_type=COMPANION, request="add", request_id=f"r{i}",
            reason="look_alike",
        ))  # fmt: skip
        await fire(hass, topics.state, state_document(known=True) | {
            "version": name, "contact_link": name,
        })  # fmt: skip
        outputs = rendered(hass, loaded)
        every |= {f"{i} {where}": output for where, output in outputs.items()}
        assert form_findings(outputs, [name, text]) == [], (i, name)
        for needle, place in places.items():
            assert shown(name) in render(hass, card(loaded, needle)), (name, place)
        for needle in (MESSAGE, SENT):
            assert shown(text) in render(hass, card(loaded, needle)), (text, needle)
    keep(language, every)
    assert problems(caplog) == []


# --- what shows when ----------------------------------------------------------------------------


async def test_cards_show_only_while_their_state_says_so(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """The version card only while the gateway runs another version; the script's tile only
    with the script, its hint only without; a problem's tile only while it is on."""
    config = make_config()
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    version, hint = card(loaded, VERSION), card(loaded, WRITE)
    (tile,) = [c for c in walk(loaded) if c["type"] == "tile" and c["entity"] == WRITE]
    problem = {
        c["entity"]: c for c in walk(loaded)
        if c["type"] == "tile" and c["entity"].startswith("binary_sensor.") and "visibility" in c
    }  # fmt: skip
    assert len(problem) == 3
    assert not visible(hass, version)  # no gateway
    assert (visible(hass, tile), visible(hass, hint)) == (False, True)
    topics = await gateway(hass, config)
    assert not visible(hass, version)
    assert not any(visible(hass, c) for c in problem.values())
    await fire(hass, topics.state, state_document(known=True) | {"version": "9.9.9"})
    assert visible(hass, version)
    await fire(hass, topics.availability, "offline")  # its attributes go with it
    assert not visible(hass, version)
    await fire(hass, topics.availability, "online")
    await fire(hass, topics.nodes, {"total": 1, "look_alike": [BOB], "look_alike_total": 1})
    assert {e for e, c in problem.items() if visible(hass, c)} == {
        "binary_sensor.meshcore_home_gateway_radio1_look_alike"
    }
    contacts = {"total": 1, "max": 350, "remote_cli": [BOB], "remote_cli_total": 1,
                "advert_carries_position": [BOB], "advert_carries_position_total": 1}  # fmt: skip
    await fire(hass, topics.contacts, contacts, retain=True)
    assert all(visible(hass, c) for c in problem.values())
    await guide_script(hass)
    assert (visible(hass, tile), visible(hass, hint)) == (True, False)
    assert problems(caplog) == []


# --- the map --------------------------------------------------------------------------------------


async def test_the_map_lists_the_trackers_of_the_state_file_and_names_the_others(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
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
        "mesh-early": early, URL: full, "mesh-none": dashboard.dashboard(config, tmp_path)[0],
        "mesh-radio-2": dashboard.dashboard(other, tmp_path / "radio-2")[0],
    }  # fmt: skip
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
    assert render(hass, card(loaded[URL], HINT)) == ""
    assert not [c for c in walk(loaded["mesh-none"]) if c["type"] == "map"]
    note = said(config, "No contact had a position when this dashboard was made. Generate it "
                "again once one has.")  # fmt: skip
    assert note in json.dumps(loaded["mesh-none"], ensure_ascii=False)
    hint = render(hass, card(loaded["mesh-early"], HINT))
    assert hint.startswith('<div><ha-alert alert-type="info">Not on this map: ')
    for name in ("Hilltop", *NAMES):
        assert shown(name) in hint, name
    assert "Alice" not in hint

    (second_map,) = [c for c in walk(loaded["mesh-radio-2"]) if c["type"] == "map"]
    assert second_map["entities"] == ["device_tracker.mesh_alice"]  # radio1's tracker of her
    assert "device_tracker.mesh_alice_2" in trackers  # radio-2's own
    assert render(hass, card(loaded["mesh-radio-2"], HINT)) == ""
    assert hass.states.get("sensor.mesh_alice_telemetry_state_2").state == "answering"
    answering = f"<td>{said(other, 'answering')}</td></tr>"
    assert answering in render(hass, card(loaded["mesh-radio-2"], TABLE))  # her IDs end in _2
    assert problems(caplog) == []


async def test_a_dashboard_shows_only_its_own_gateways_contacts(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Zed, a contact of gateway radio-2 with a position, is in neither radio1's contacts table
    nor its map's hint, and in both of radio-2's."""
    config, other = make_config(), make_config(node_id="radio-2")
    first, second = Broker(), Broker()
    contacts_of(config, tmp_path / "radio1", first).update(household(ALICE_NODE))
    zed = household(Node(ZED, "Zed", COMPANION, position=HOME))
    contacts_of(other, tmp_path / "radio-2", second).update(zed)
    files = {URL: dashboard.dashboard(config, tmp_path / "radio1")[0],
             "mesh-radio-2": dashboard.dashboard(other, tmp_path)[0]}  # fmt: skip
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


# --- the guide's script -------------------------------------------------------------------------


async def test_the_guides_script_is_a_form_that_sends_for_a_household_user(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
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
    for to, text in ((alice, "Dinner <b>now</b>"), (alerts, "Gate open"), (alice, "+493023125000"),
                     (alice, "1.10"), (alerts, "None"), (alice, "  spaced  ")):  # fmt: skip
        mqtt.async_publish.reset_mock()
        await client.send_json_auto_id({
            "type": "call_service", "domain": "script", "service": "mesh_message",
            "service_data": {"to": to, "message": text},
        })  # fmt: skip
        answer = await client.receive_json()
        assert answer["success"], answer
        await hass.async_block_till_done()
        (sent,) = [c.args[:3] for c in mqtt.async_publish.call_args_list]
        expected = topics.contact_send(ALICE) if "alice" in to else topics.send("alerts")
        assert sent == (expected, text, 1)
    assert problems(caplog) == []


# --- machine values as texts --------------------------------------------------------------------

STEPS = {
    "queued": "queued", "sent": "sent", "confirmed": "confirmed", "failed": "failed",
    "expired": "expired", "duplicate": "not sent again: a duplicate", "rejected": "rejected",
}  # fmt: skip
STATES = {
    "waiting": "waiting", "answering": "answering", "no_position": "no position", "away": "away",
    "no_answer": "no answer", "no_path": "no path", "look_alike": "look-alike",
    "excluded": "excluded", "off": "off",
}  # fmt: skip


async def test_steps_states_and_contact_events_show_as_texts(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """A delivery step, a telemetry state and a contact event as the dashboard's text, a reason
    as its code: the codes are an open list (docs/mqtt-api.md)."""
    assert set(STEPS) == {s.value for s in DeliveryState}
    assert set(STATES) == set(telemetry.STATES)
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    topics = await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    event, sent, table = card(loaded, EVENT), card(loaded, SENT), card(loaded, TABLE)

    assert render(hass, event) == "No contact event yet."
    await fire(hass, topics.contact_event, contact_event(
        "refused", BOB, name="Bob", node_type=COMPANION, request="add", request_id="r1",
        reason="look_alike",
    ))  # fmt: skip
    refused = "<div><b>Bob</b> request refused <code>look_alike</code> "
    assert render(hass, event).startswith(refused)
    await fire(hass, topics.contact_event, contact_event("added", ALICE, ALICE_NODE))
    added = r"<div><b>Alice</b> added to the radio \d+ min ago</div>"
    assert re.fullmatch(added, render(hass, event))

    for step, text in STEPS.items():
        reason = "no_acknowledgement" if step == "failed" else None
        delivered = DeliveryEvent("m1", "alerts", DeliveryState(step), "Gate open", 1, reason)
        await fire(hass, topics.delivery, delivery_payload(delivered))
        code = " <code>no_acknowledgement</code>" if reason else ""
        assert render(hass, sent) == f"<div><b>alerts</b> {text}{code}<br>Gate open</div>", step

    alice = "<tr><td>Alice</td><td>Companion</td><td>0 min ago</td><td>{}</td></tr>"
    for state, text in STATES.items():
        document = contact_document(mesh.contacts[ALICE]) | {"telemetry_state": state}
        await fire(hass, topics.contact(ALICE), document)
        assert alice.format(text) in render(hass, table), state
    await fire(hass, topics.contact(ALICE), contact_document(mesh.contacts[ALICE]))
    assert alice.format(NONE) in render(hass, table)  # unknown
    hilltop = f"<tr><td>Hilltop</td><td>Repeater</td><td>{NONE}</td><td>{NONE}</td></tr>"
    assert hilltop in render(hass, table)  # a repeater is not asked
    assert problems(caplog) == []


async def test_the_cards_say_how_long_ago(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, *, tmp_path: Path,
    freezer: FrozenDateTimeFactory, caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """The last message, the last contact event and a contact's last heard time, minutes, hours
    and days later: each card counts on its own."""
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
    for seconds, text, n in ((330, "{n} min ago", "5"), (3.5 * 3600 - 330, "{n} h ago", "3"),
                             (2 * 86400, "{n} d ago", "2")):  # fmt: skip
        freezer.tick(timedelta(seconds=seconds))
        ago = said(config, text, n=n)
        assert render(hass, card(loaded, MESSAGE)) == f"<div><b>Alice</b> {ago}<br>Dinner</div>"
        assert (
            render(hass, card(loaded, EVENT)) == f"<div><b>Alice</b> added to the radio {ago}</div>"
        )
        assert f"<td>Alice</td><td>Companion</td><td>{ago}</td>" in render(
            hass, card(loaded, TABLE)
        )
    assert problems(caplog) == []
