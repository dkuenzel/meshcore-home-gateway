"""The generated dashboard's Contacts page against the real Home Assistant: the people and devices,
the sensors, asking for position and battery, adding and removing (docs/home-assistant.md,
Dashboard).

The household and the helpers are those of test_dashboard.py: the gateway's own discovery and its
contacts' devices, the cards rendered strictly as the frontend asks for them, and a household
user's browser for what shows (visibility.py). Contract items 4 (the guards of the contacts'
switches and remove buttons), 6 (why the controls cannot act), 7 (the words of asking), 12
(every control as a household user), 15 (`controls` turned off and on) and 16 (the battery of a
sensor node).
"""

from __future__ import annotations

import dataclasses
import json
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from ha_plugin import CLIENT_ID, async_fire_time_changed
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from test_contract import PREFIX, RADIO, fire, make_config, problems, state_document
from test_overview import index
from visibility import Viewer, household_viewer

from meshcore_home_gateway import dashboard
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.mesh import Node, Readings
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.views import config_document, contact_document, contact_event, iso
from test_contact_devices import ALICE, ALICE_NODE, COMPANION, HILL_NODE, NOW, Broker
from test_dashboard import (
    BOB,
    CONFIGURATION,
    NONE,
    SHOWS_NOTHING,
    ZED,
    card,
    cards,
    contacts_of,
    deliver,
    gateway,
    household,
    load,
    made,
    markdown,
    page,
    render,
    shows_nothing,
    topics_of,
)

# the cards of Contacts, by a part of their template
PEOPLE = "set c = namespace(heard='', word='', battery=none, path='')"
SENSORS = "set c = namespace(heard='', word='', values=[], readings=0)"
ASKING = "(is_state(radio, 'unavailable') or off) -%}"  # why the switches are missing
# why adding does not work
ADDING = "(is_state(radio, 'unavailable') or is_state(radio, 'off') or off or code) -%}"
RESULT = "set e = 'event."
FOREIGN = "set ns = namespace(items=[], found=[])"  # controls under another ID (D-30)
UNLISTED = "set ns = namespace(keys=[], names=[])"
SHED = "c3" * 32
HEARD = "_add_node', 'options'"  # Nodes heard nearby
OFFLINE = '<div><ha-alert alert-type="error">The gateway is offline. These controls come back'


def rows(view: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The guarded rows of a page, by the entity they show or press."""
    found = {}
    for node in [n for n in walk(view) if n.get("type") == "conditional"]:
        row = node["row"]
        found[row.get("entity") or row["tap_action"]["target"]["entity_id"]] = node
    return found


def walk(node: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if isinstance(node, dict):
        out.append(node)
        for value in node.values():
            out += walk(value)
    elif isinstance(node, list):
        for value in node:
            out += walk(value)
    return out


def entities_card(view: dict[str, Any], entity: str) -> dict[str, Any]:
    """The entities card that holds the guarded row of `entity`."""
    (found,) = [
        c
        for c in walk(view)
        if c.get("type") == "entities" and entity in json.dumps(c.get("entities"))
    ]
    return found


async def household_page(
    hass: HomeAssistant,
    hass_ws_client: Any,
    config: Config,
    tmp_path: Path,
    *nodes: Node,
) -> tuple[dict[str, Any], Viewer]:
    """The dashboard of a household of these contacts, loaded, and a household user's browser;
    the gateway online with them, each contact one that a remove request would take."""
    broker, mesh = Broker(), household(*nodes)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh, removable=True)
    await connected(hass, config)
    return loaded, await household_viewer(hass, hass_ws_client)


async def connected(hass: HomeAssistant, config: Config) -> None:
    """The gateway's state with its radio connected and its contacts' index, which takes
    requests, again: a control announced after the first ones waits for the next, as from the
    gateway every minute."""
    await fire(hass, topics_of(config).state, state_document(known=True) | {"queue": 0})
    await fire(hass, topics_of(config).contacts, index())


def in_prefix(config: Config, prefix: str) -> Config:
    names = dataclasses.replace(config.homeassistant, contact_entity_prefix=prefix)
    return dataclasses.replace(config, homeassistant=names)


# --- People and devices, Sensors ---------------------------------------------------------------


# D-8, HA-08: each telemetry state in the words of a chat page
WORDS = {
    "waiting": "not asked yet",
    "answering": "answering",
    "no_position": "answering, no position",
    "away": "no answer lately",
    "no_answer": "no answer for a day",
    "no_path": "not reachable now",
    "look_alike": "not asked: look-alike",
    "excluded": "not asked (switch off)",
    "off": "off for everyone",
}


async def test_people_and_devices_lists_this_gateways_contacts_with_the_words_of_asking(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 7, HA-08, HC 23: the count of the table's contacts, each of this gateway's
    contacts by its device, heard newest first, with a link to its chat page where it has one,
    its type in the words of a chat page (D-7), its battery and the column Asking in the words
    of a chat page (D-8): "not asked (switch off)" for a companion nobody included, never
    "excluded" (M9); a state added later as its code; no word for a repeater, which is never
    asked."""
    config = make_config()
    loaded, _ = await household_page(
        hass, hass_ws_client, config, tmp_path, ALICE_NODE, HILL_NODE, Node(BOB, "Bob", COMPANION)
    )
    topics = topics_of(config)
    await fire(hass, topics.contacts, index(total=3), retain=True)
    people = card(cards(loaded, "contacts"), PEOPLE)
    said = render(hass, people)
    assert said.startswith("<div>3 of 350 contacts on the radio.</div>\n<table><tr><th>Name</th>")
    alice = '<a href="./contact-alice">Alice</a><br><small>person</small>'  # D-7, not its model
    assert alice in said
    assert "<td>Hilltop<br><small>repeater</small></td>" in said  # no chat page: no link
    mesh = household(ALICE_NODE, Node(BOB, "Bob", COMPANION))
    now = time.time()
    for key, ago in ((BOB, 60), (ALICE, 3600)):
        document = contact_document(mesh.contacts[key], telemetry_state="answering")
        await fire(hass, topics.contact(key), document | {"heard_at": iso(now - ago)})
    said = render(hass, people)
    assert said.index(">Bob<") < said.index(">Alice<") < said.index(">Hilltop<")  # newest first
    for state, word in WORDS.items():
        hass.states.async_set("sensor.mesh_alice_telemetry_state", state, {"options": list(WORDS)})
        assert f"{alice}</td><td>" in render(hass, people)
        row = render(hass, people).split(alice)[1].split("</tr>")[0]
        assert row.endswith(f"<td>{word}</td>"), (state, row)
    hass.states.async_set("sensor.mesh_alice_telemetry_state", "later", {"options": list(WORDS)})
    assert "<td><code>later</code></td></tr>" in render(hass, people)
    hilltop = render(hass, people).split("<td>Hilltop")[1].split("</tr>")[0]
    assert hilltop.endswith(f"<td>{NONE}</td><td>{NONE}</td>")  # no battery, never asked
    assert "excluded" not in render(hass, people)
    assert problems(caplog) == []


async def test_a_sensor_nodes_voltage_is_a_reading_and_no_battery(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 16, CP F-18: a sensor node that reports a voltage on channel 2 has a sensor
    of the class voltage that is no battery: the column Battery stays empty for it, and Sensors
    lists the reading by its name without the device's; a companion's battery fills its column
    (D-31: the class voltage and an ID that ends in _battery)."""
    asked = '[telemetry]\nask_types = ["chat", "sensor"]\n'
    config = parse_config(
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"\n'
        f'[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\ninsecure = true\n{asked}'
    )
    shed = Node(SHED, "Shed sensor", NodeType.SENSOR)
    broker, mesh = Broker(), household(ALICE_NODE, shed)
    payload = lpp.decode(bytes.fromhex("0174019c" + "0274014a"))  # 4.12 V; channel 2: 3.3 V
    mesh.contact_answered(ALICE, NOW, None, None, Readings(NOW, lpp.named(payload)))
    payload = lpp.decode(bytes.fromhex("0274014a"))  # the sensor: channel 2, 3.3 V
    mesh.contact_answered(SHED, NOW, None, None, Readings(NOW, lpp.named(payload)))
    devices = contacts_of(config, tmp_path, broker)
    devices.update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    for key in (ALICE, SHED):
        document = contact_document(mesh.contacts[key], telemetry_state="answering")
        await fire(hass, topics_of(config).contact(key), document, retain=False)
    voltage = hass.states.get("sensor.mesh_shed_sensor_ch2_voltage")
    assert voltage is not None
    assert voltage.attributes["device_class"] == "voltage"
    people = render(hass, card(cards(loaded, "contacts"), PEOPLE))
    shed_row = people.split("<td>Shed sensor")[1].split("</tr>")[0]
    assert f"<td>{NONE}</td><td>answering</td>" in shed_row  # no battery
    alice_row = people.split(">Alice</a>")[1].split("</tr>")[0]
    assert "<td>4.1 V</td>" in alice_row
    sensors = render(hass, card(cards(loaded, "contacts"), SENSORS))
    assert "<b>Shed sensor</b><br><small>Voltage 2 3.3 V</small>" in sensors
    # the battery as People and devices shows it, to a tenth of a volt
    assert "<b>Alice</b><br><small>Battery 4.1 V · Voltage 2 3.3 V</small>" in sensors
    assert problems(caplog) == []


async def test_sensors_say_why_none_reports(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """K2's empty state, and while the configuration asks no sensor, who would change that
    (HC 25)."""
    config = make_config()
    loaded, _ = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    sensors = card(cards(loaded, "contacts"), SENSORS)
    assert render(hass, sensors).startswith("<div>No sensor reports readings.</div>\n<details>")
    hass.states.async_set(CONFIGURATION, iso(time.time()), {"telemetry": {"ask_types": ["chat"]}})
    assert render(hass, sensors).startswith(
        "<div>No sensor reports readings.</div>\n<div>Sensors are not asked in this home's "
        "settings (your administrator).</div>"
    )
    assert problems(caplog) == []


# --- the guards (contract item 4) ---------------------------------------------------------------


async def test_a_second_gateways_alice_has_her_own_rows_and_the_first_names_the_other_ids(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA-01, D-23, D-30: radio1 and radio-2 share Alice; radio-3 has another contact named
    Alice. Each dashboard names the IDs its state file gives, switch.mesh_alice_telemetry, which
    Home Assistant gave radio1's Alice. On radio1's dashboard her switch and her unpressed remove
    button show (H-8: never has_value); on the others' both hide, since the device is another
    gateway's (M4) and, for radio-3, another key, and a yellow card names the IDs this gateway's
    contact has, for the administrator."""
    nodes = {"radio1": ALICE_NODE, "radio-2": ALICE_NODE, "radio-3": Node(ZED, "Alice", COMPANION)}
    files, configs = {}, {}
    for node_id, node in nodes.items():
        configs[node_id] = config = make_config(node_id=node_id)
        broker = Broker()
        contacts_of(config, tmp_path / node_id, broker).update(household(node))
        files[f"mesh-{node_id}"] = dashboard.dashboard(config, tmp_path / node_id)[0]
        await gateway(hass, config)
        await deliver(hass, config, broker, household(node), removable=True)
        await connected(hass, config)
    loaded = await load(hass, hass_ws_client, files)
    viewer = await household_viewer(hass, hass_ws_client)
    assert hass.states.get("button.mesh_alice_remove").state == "unknown"  # never pressed
    for node_id, shown in (("radio1", True), ("radio-2", False), ("radio-3", False)):
        view = page(loaded[f"mesh-{node_id}"], "contacts")
        guarded = rows(view)
        assert set(guarded) == {"switch.mesh_alice_telemetry", "button.mesh_alice_remove"}
        for entity, row in guarded.items():
            assert await viewer.shown(view, row) is shown, (node_id, entity, viewer.errors)
            frame = entities_card(view, entity)  # no empty frame around hidden rows (HC 10)
            assert await viewer.shown(view, frame) is shown, (node_id, entity, viewer.errors)
        lists = [c for c in markdown(view) if FOREIGN in c["content"]]
        assert len(lists) == 2  # the switches' and the remove buttons'
        for each in lists:  # while a row of its list hides, and only then (its guards)
            assert await viewer.shown(view, each) is not shown, (node_id, viewer.errors)
        named = [text for text in (render(hass, c) for c in lists) if text]
        if shown:
            assert named == []
            continue
        suffix = "_2" if node_id == "radio-2" else "_3"
        assert len(named) == 2, (node_id, named)
        for text, real in zip(
            named,
            (f"switch.mesh_alice_telemetry{suffix}", f"button.mesh_alice_remove{suffix}"),
            strict=True,
        ):
            assert text.startswith(
                '<div><ha-alert alert-type="warning"><b>Alice cannot be changed here.</b><br>'
                "For your administrator: on this gateway, this contact's control has another "
                f"entity ID: {real}. "
            ), text
    assert problems(caplog) == []


async def test_a_slug_another_contact_took_hides_the_rows_of_the_first(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CP F-04, M5: the dashboard was made while Alice was a contact; now another contact named
    Alice, of the same gateway, has her entity IDs. Her rows hide (the serial number is not the
    key the dashboard was made for), no card names an ID for her (the contact is gone), and the
    new Alice is "Not listed here yet" until the dashboard is made again."""
    config = make_config()
    contacts_of(config, tmp_path / "then", Broker()).update(household(ALICE_NODE))
    loaded = await made(hass, hass_ws_client, config, tmp_path / "then")
    broker, mesh = Broker(), household(Node(ZED, "Alice", COMPANION))
    contacts_of(config, tmp_path / "now", broker).update(mesh)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh, removable=True)
    await connected(hass, config)
    entry = er.async_get(hass).async_get("switch.mesh_alice_telemetry")
    assert entry is not None
    device = dr.async_get(hass).async_get(entry.device_id)
    assert device is not None
    assert device.serial_number == ZED
    viewer = await household_viewer(hass, hass_ws_client)
    view = page(loaded, "contacts")
    for entity, row in rows(view).items():
        assert not await viewer.shown(view, row), entity
    assert [render(hass, c) for c in markdown(view) if FOREIGN in c["content"]] == ["", ""]
    unlisted = [render(hass, c) for c in markdown(view) if UNLISTED in c["content"]]
    assert unlisted[0].startswith(
        '<div><ha-alert alert-type="info">Not listed here yet: Alice. Ask your administrator '
        "to make the dashboard again.</ha-alert></div>"
    )
    assert problems(caplog) == []


async def test_a_removed_contact_leaves_no_row_and_no_warning(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA-05 a: after Alice left the radio her entities are gone: her rows hide, no card names
    an ID or "not listed", the table lists Hilltop alone, and Home Assistant logs nothing."""
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    devices = contacts_of(config, tmp_path, broker)
    devices.update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh, removable=True)
    devices.update(household(HILL_NODE))
    await deliver(hass, config, broker, household(HILL_NODE), removable=True)
    await connected(hass, config)
    assert hass.states.get("switch.mesh_alice_telemetry") is None
    viewer = await household_viewer(hass, hass_ws_client)
    view = page(loaded, "contacts")
    for entity, row in rows(view).items():
        assert await viewer.shown(view, row) is (entity == "button.mesh_hilltop_remove"), entity
    for each in markdown(view):
        if FOREIGN in each["content"] or UNLISTED in each["content"]:
            shows_nothing(hass, each)
    people = render(hass, card(cards(loaded, "contacts"), PEOPLE))
    assert "Alice" not in people
    assert "Hilltop" in people
    assert problems(caplog) == []


@pytest.mark.parametrize("removable", [False, True])
async def test_with_no_contact_removable_the_section_says_so(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    removable: bool,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Maintainer's use of 2.1.1rc1: *Remove a contact* stood as a heading alone while no contact
    could be removed (every one kept on purpose, as the chats' contacts are by default). The
    section then says so; while a contact can be removed, its row shows and the line does not."""
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE, HILL_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh, removable=removable)
    await connected(hass, config)
    viewer = await household_viewer(hass, hass_ws_client)
    view = page(loaded, "contacts")
    removes = {entity: row for entity, row in rows(view).items() if entity.endswith("_remove")}
    assert set(removes) == {"button.mesh_alice_remove", "button.mesh_hilltop_remove"}
    for entity, row in removes.items():
        assert await viewer.shown(view, row) is removable, entity
    (help_,) = [c for c in markdown(view) if "What removing does" in c["content"]]
    said = render(hass, help_).startswith("<div>No contact can be removed here.</div>")
    assert said is not removable
    assert problems(caplog) == []


async def test_a_prefix_set_later_keeps_the_table_and_names_the_real_ids(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """HA-06, M23: the contacts got their IDs with the prefix mesh; the dashboard is made after
    the administrator set mesh_2. The table still lists them by their devices, the rows of the
    IDs with mesh_2 hide, and the cards for the administrator name the IDs they have."""
    config = make_config()
    broker, mesh = Broker(), household(ALICE_NODE)
    contacts_of(config, tmp_path, broker).update(mesh)
    later = in_prefix(config, "mesh_2")
    loaded = await made(hass, hass_ws_client, later, tmp_path)
    await gateway(hass, config)
    await deliver(hass, config, broker, mesh)
    view = page(loaded, "contacts")
    assert set(rows(view)) == {"switch.mesh_2_alice_telemetry", "button.mesh_2_alice_remove"}
    viewer = await household_viewer(hass, hass_ws_client)
    for entity, row in rows(view).items():
        assert not await viewer.shown(view, row), entity
        assert not await viewer.shown(view, entities_card(view, entity)), entity  # no frame
    assert ">Alice</a>" in render(hass, card(cards(loaded, "contacts"), PEOPLE))
    named = [render(hass, c) for c in markdown(view) if FOREIGN in c["content"]]
    assert ["entity ID: switch.mesh_alice_telemetry. " in t for t in named] == [True, False]
    assert ["entity ID: button.mesh_alice_remove. " in t for t in named] == [False, True]
    assert problems(caplog) == []


async def test_a_guard_follows_the_registry_within_a_second(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """CP F-06: a row's guard reads the registry, which tells no listener; core asks the
    template again every second, so a change of the registry alone hides the row within one."""
    config = make_config()
    loaded, viewer = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    (condition,) = rows(page(loaded, "contacts"))["switch.mesh_alice_telemetry"]["conditions"]
    client = viewer.client
    await client.send_json_auto_id({"type": "subscribe_condition", "condition": condition})
    assert (await client.receive_json())["success"]
    assert (await client.receive_json())["event"]["result"] is True
    entry = er.async_get(hass).async_get("switch.mesh_alice_telemetry")
    assert entry is not None
    assert entry.device_id is not None
    dr.async_get(hass).async_update_device(entry.device_id, serial_number=ZED)
    await hass.async_block_till_done()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=1.5))
    await hass.async_block_till_done()
    assert (await client.receive_json())["event"]["result"] is False
    assert problems(caplog) == []


# --- why the controls cannot act (contract items 6 and 15) --------------------------------------


async def test_while_the_gateway_is_offline_the_controls_hide_and_a_red_line_says_why(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 6, D-29, HC 10: offline, People and devices, Ask for position and battery
    and Add or remove each say so in red; the lists of switches and remove buttons are not shown,
    the sections still are; back online, the lines go and the lists show."""
    config = make_config()
    loaded, viewer = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    view = page(loaded, "contacts")
    contacts = cards(loaded, "contacts")
    for needle in (ASKING, ADDING):
        shows_nothing(hass, card(contacts, needle))
    lists = [
        entities_card(view, e) for e in ("switch.mesh_alice_telemetry", "button.mesh_alice_remove")
    ]
    assert [await viewer.shown(view, each) for each in lists] == [True, True]
    await fire(hass, topics_of(config).availability, "offline")
    for needle in (ASKING, ADDING):
        assert OFFLINE in render(hass, card(contacts, needle)), needle
    assert (  # the table above them has no controls of its own
        '<div><ha-alert alert-type="error"><b>The gateway is offline.</b> Contacts can be '
        "added and removed again once it is back.</ha-alert></div>"
    ) in render(hass, card(contacts, PEOPLE))
    assert [await viewer.shown(view, each) for each in lists] == [False, False]
    for section in view["sections"]:
        assert await viewer.shown(view, section)
    await fire(hass, topics_of(config).availability, "online")
    assert [await viewer.shown(view, each) for each in lists] == [True, True]
    assert problems(caplog) == []


async def test_while_the_radio_is_disconnected_contacts_says_so_in_red_as_every_page_does(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-6, D-29, HC 10: with the radio disconnected, People and devices and Add or remove say
    so in red in the words of a chat page, also while the gateway names it no_radio; the remove
    buttons are unavailable, so their list leaves no empty frame; the switches, which still
    work, stay. Connected again, the line goes."""
    config = make_config()
    loaded, viewer = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    view = page(loaded, "contacts")
    contacts = cards(loaded, "contacts")
    topics = topics_of(config)
    await fire(hass, topics.state, state_document(known=False))
    await fire(hass, topics.contacts, index(manage_reason="no_radio"))
    assert hass.states.get(f"binary_sensor.{PREFIX}_radio").state == "off"
    red = (
        '<div><ha-alert alert-type="error"><b>The radio is disconnected.</b> Contacts can be '
        "added and removed again once it is back.</ha-alert></div>"
    )
    assert render(hass, card(contacts, ADDING)).startswith(red)
    assert red in render(hass, card(contacts, PEOPLE))
    s3 = render(hass, card(cards(loaded, "alice"), "Why messages wait"))
    assert s3.startswith('<div><ha-alert alert-type="error"><b>The radio is disconnected.</b>')
    shows_nothing(hass, card(contacts, ASKING))
    switches, removes = (
        entities_card(view, e) for e in ("switch.mesh_alice_telemetry", "button.mesh_alice_remove")
    )
    assert await viewer.shown(view, switches)
    assert not await viewer.shown(view, removes)  # every row hidden: no empty frame (HC 10)
    await connected(hass, config)
    shows_nothing(hass, card(contacts, ADDING))
    assert "The radio is disconnected." not in render(hass, card(contacts, PEOPLE))
    assert await viewer.shown(view, removes)
    assert problems(caplog) == []


async def test_with_asking_off_in_the_settings_the_switches_say_they_wait_for_it(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """K3a, D-29: with asking turned off in the configuration ([telemetry] enabled = false) the
    switches still show and work, and a blue line above them says who turned asking off and
    that they take effect once it is on."""
    config = make_config()
    loaded, viewer = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    asking = card(cards(loaded, "contacts"), ASKING)
    shows_nothing(hass, asking)
    hass.states.async_set(CONFIGURATION, iso(time.time()), {"telemetry": {"enabled": False}})
    assert render(hass, asking).startswith(
        '<div><ha-alert alert-type="info">Your administrator turned asking off in this home\'s '
        "settings: the switches take effect once it is on.</ha-alert></div>"
    )
    view = page(loaded, "contacts")
    assert await viewer.shown(view, entities_card(view, "switch.mesh_alice_telemetry"))
    assert problems(caplog) == []


REASONS = {
    "management_off": (
        "info",
        "The gateway adds and removes no contacts: your administrator turned that off.",
    ),
    "no_radio": (
        "info",
        "The radio is not connected. Contacts can be added and removed again once it is.",
    ),
    "untested_firmware": (
        "warning",
        (
            "The radio's firmware is not tested with this gateway, so it adds and removes no "
            "contacts. Ask your administrator to update the gateway or the radio."
        ),
    ),
    "later": (
        "warning",
        (
            "Contacts cannot be added or removed now. Tell your administrator this code: "
            "<code>later</code>"
        ),
    ),
}


async def test_each_reason_the_gateway_refuses_requests_is_said_above_the_controls(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 6, HA-04, #99: each `manage_reason` in its colour, above the controls to
    add and remove and under the count of People and devices; the switches are not concerned."""
    config = make_config()
    loaded, _ = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    contacts = cards(loaded, "contacts")
    for reason, (kind, text) in REASONS.items():
        await fire(hass, topics_of(config).contacts, index(manage_reason=reason))
        line = f'<div><ha-alert alert-type="{kind}">{text}</ha-alert></div>'
        assert render(hass, card(contacts, ADDING)).startswith(line), reason
        assert line in render(hass, card(contacts, PEOPLE)), reason
        shows_nothing(hass, card(contacts, ASKING))
    await fire(hass, topics_of(config).contacts, index())
    shows_nothing(hass, card(contacts, ADDING))
    assert problems(caplog) == []


@pytest.mark.parametrize("report", ["configuration", "inferred"])
async def test_controls_turned_off_hide_the_controls_and_say_so_at_once_both_ways(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    report: str,
) -> None:
    """Contract items 6 and 15, HA-02 (G7): with `controls = false` the switches and remove
    buttons are unavailable as soon as the gateway announces itself so, their rows hide, and a
    blue line says your administrator manages contacts outside Home Assistant; read from the
    configuration (G6), or for a gateway before 2.1 inferred from its controls; set back, all
    returns at once."""
    config = make_config()
    off = dataclasses.replace(config.homeassistant, controls=False)
    off_config = dataclasses.replace(config, homeassistant=off)
    broker, mesh = Broker(), household(ALICE_NODE)
    contacts_of(off_config, tmp_path, broker).update(mesh)
    loaded = await made(hass, hass_ws_client, config, tmp_path)
    await gateway(hass, off_config)
    await deliver(hass, off_config, broker, mesh, removable=True)
    await connected(hass, config)
    if report == "configuration":
        document = config_document(time.time(), [], {"homeassistant": {"controls": False}})
        await fire(hass, topics_of(config).config, document, retain=True)
    else:
        hass.states.async_remove(CONFIGURATION)
    viewer = await household_viewer(hass, hass_ws_client)
    view = page(loaded, "contacts")
    blue = (
        '<div><ha-alert alert-type="info">Your administrator turned these controls off: contacts '
        "are managed outside Home Assistant.</ha-alert></div>"
    )
    for needle in (ASKING, ADDING):
        assert render(hass, card(cards(loaded, "contacts"), needle)).startswith(blue), needle
    assert [await viewer.shown(view, row) for row in rows(view).values()] == [False, False]
    lists = [entities_card(view, entity) for entity in rows(view)]
    assert [await viewer.shown(view, each) for each in lists] == [False, False]  # HC 10
    on_broker = Broker()
    contacts_of(config, tmp_path / "on", on_broker).update(mesh)
    await fire(hass, discovery_topic(config), discovery_payload(config, topics_of(config), RADIO))
    await deliver(hass, config, on_broker, mesh, removable=True)
    await connected(hass, config)
    if report == "configuration":
        document = config_document(time.time(), [], {"homeassistant": {"controls": True}})
        await fire(hass, topics_of(config).config, document)
    shows_nothing(hass, card(cards(loaded, "contacts"), ASKING))
    assert [await viewer.shown(view, row) for row in rows(view).values()] == [True, True]
    assert [await viewer.shown(view, each) for each in lists] == [True, True]
    assert problems(caplog) == []


# --- adding a contact ---------------------------------------------------------------------------


async def test_nodes_heard_nearby_counts_the_nodes_it_lists(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """K5: the count above the list is the list's, the nodes one can add, not every node the
    radio heard (repeaters and look-alikes too: the nodes sensor); offline, a line says so
    instead of a list that may be old."""
    config = make_config()
    loaded, _ = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    heard = card(cards(loaded, "contacts"), HEARD)
    options = hass.states.get(f"select.{PREFIX}_add_node").attributes["options"]
    assert len(options) > 3
    said = render(hass, heard)
    assert said.startswith(f"<div>Nodes you can add: {len(options)}.</div>\n<table>")
    assert said.split("<details>")[0].count("<tr>") == len(options)
    nodes = {"total": 3, "truncated": False, "nodes": []}
    await fire(hass, topics_of(config).nodes, nodes, retain=True)
    assert render(hass, heard).startswith(f"<div>Nodes you can add: {len(options)}.</div>")
    await fire(hass, topics_of(config).availability, "offline")
    gone = "<div>The gateway is offline: this shows again when it is back.</div>"
    assert render(hass, heard).startswith(f"{gone}\n<details>")
    assert problems(caplog) == []


async def test_the_result_of_adding_says_what_happened_under_the_field(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """D-65, DR-21: the line under the field asks for Return (↵); the last contact event follows
    with its time, a refusal in yellow with its reason; a companion added without a chat page
    says that the administrator makes the dashboard again (HC 15)."""
    config = make_config()
    loaded, _ = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    result = card(cards(loaded, "contacts"), RESULT)
    first = "<div>Paste a key or contact link into the field above, then tap Return (↵).</div>\n"
    assert render(hass, result).startswith(first + "<div>No contact event yet.</div>\n<details>")
    topics = topics_of(config)
    refused = contact_event(
        "refused",
        BOB,
        name="Bob",
        node_type=COMPANION,
        request="add",
        request_id="r1",
        reason="look_alike",
    )
    await fire(hass, topics.contact_event, refused)
    assert render(hass, result).startswith(
        first + '<div><ha-alert alert-type="warning"><b>Bob</b> request refused (another '
        "contact has the same key prefix) · "
    )
    await fire(hass, topics.contact_event, contact_event("added", BOB, Node(BOB, "Bob", COMPANION)))
    said = render(hass, result)
    assert said.startswith(first + "<div><b>Bob</b> added to the radio · ")
    assert "<div>To write to Bob here, your administrator makes the dashboard again.</div>" in said
    await fire(hass, topics.contact_event, contact_event("added", ALICE, ALICE_NODE))
    assert "To write to" not in render(hass, result)  # Alice has her page
    await fire(hass, topics.availability, "offline")  # not "No contact event yet."
    gone = "<div>The gateway is offline: this shows again when it is back.</div>\n"
    assert render(hass, result).startswith(first + gone + "<details>")
    assert problems(caplog) == []


@pytest.mark.parametrize("name", [*SHOWS_NOTHING, "Carol"])
async def test_the_result_of_adding_names_the_key_when_a_name_shows_nothing(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    name: str,
) -> None:
    """A contact event whose name shows nothing, made only of invisible characters such as a
    zero-width space or drawn as nothing such as a Hangul filler, names the contact by its
    prefix under the field, as Latest of each kind on the page Gateway does: in a refusal, in
    an addition and in the line that asks for a chat page, never by an empty bold name. A
    stranger so named is offered in Add heard node by its key (heard_nodes "full"). A name
    that shows stays."""
    config = make_config()
    loaded, _ = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE)
    result = card(cards(loaded, "contacts"), RESULT)
    topics = topics_of(config)
    shown = name if name == "Carol" else ZED[:12]
    refused = contact_event(
        "refused",
        ZED,
        name=name,
        node_type=COMPANION,
        request="add",
        request_id="r1",
        reason="look_alike",
    )
    await fire(hass, topics.contact_event, refused)
    assert f'<ha-alert alert-type="warning"><b>{shown}</b> request refused (' in render(
        hass, result
    )
    await fire(
        hass, topics.contact_event, contact_event("added", ZED, name=name, node_type=COMPANION)
    )
    said = render(hass, result)
    assert f"<div><b>{shown}</b> added to the radio · " in said, said
    asks = f"<div>To write to {shown} here, your administrator makes the dashboard again.</div>"
    assert asks in said, said
    assert problems(caplog) == []


# --- every control as a household user (contract item 12) ---------------------------------------


async def test_every_control_works_for_a_user_who_is_no_administrator(
    hass: HomeAssistant,
    mqtt: Any,
    hass_ws_client: Any,
    *,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Contract item 12: each control of the dashboard as a household user calls it: a chat's
    field and its Send button (the drafts of 2.1), Add by key or contact link, Add at once, a
    switch both ways, the remove row's own action and Hidden contacts' Show again, each
    publishing to the gateway; the remove row's and Show again's actions are those the
    dashboard names. None is for administrators only."""
    config = make_config()
    bob = Node(BOB, "Bob", COMPANION)
    loaded, _ = await household_page(hass, hass_ws_client, config, tmp_path, ALICE_NODE, bob)
    view = page(loaded, "contacts")
    user = await hass.auth.async_create_user("Carol", group_ids=[GROUP_ID_USER])
    assert not user.is_admin
    token = hass.auth.async_create_access_token(
        await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    )
    client = await hass_ws_client(hass, token)
    remove = rows(view)["button.mesh_bob_remove"]["row"]["tap_action"]
    (show,) = [
        r["tap_action"] for r in walk(view) if r.get("entity") == f"button.{PREFIX}_show_hidden"
    ]
    assert remove["confirmation"]["text"].startswith("Bob can then no longer write")
    assert "confirmation" not in show
    options = hass.states.get(f"select.{PREFIX}_add_node").attributes["options"]
    alerts, switch = f"{PREFIX}_write_alerts", "switch.mesh_bob_telemetry"
    calls: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
        ("text", "set_value", {"value": "Hi"}, {"entity_id": f"text.{alerts}"}),
        ("button", "press", {}, {"entity_id": f"button.{alerts}_send"}),
        ("text", "set_value", {"value": BOB}, {"entity_id": f"text.{PREFIX}_add_contact"}),
        (
            "select",
            "select_option",
            {"option": options[-1]},
            {"entity_id": f"select.{PREFIX}_add_node"},
        ),
        ("switch", "turn_off", {}, {"entity_id": switch}),
        ("switch", "turn_on", {}, {"entity_id": switch}),
        (*remove["perform_action"].split("."), {}, remove["target"]),  # type: ignore[list-item]
        (*show["perform_action"].split("."), {}, show["target"]),  # type: ignore[list-item]
    ]
    for domain, service, data, target in calls:
        mqtt.async_publish.reset_mock()
        await client.send_json_auto_id(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "service_data": data,
                "target": target,
            }
        )
        answer = await client.receive_json()
        assert answer["success"], (domain, service, answer)
        await hass.async_block_till_done()
        assert mqtt.async_publish.call_args_list, (domain, service)
    assert problems(caplog) == []
