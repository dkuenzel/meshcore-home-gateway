"""Telemetry of the household's companions against the real Home Assistant MQTT integration
(ADR 0018, docs/home-assistant.md "Telemetry of the household's companions").

The gateway's own code builds the messages: the contact devices (ContactDevices), the documents
and events (views), the gateway's discovery, and the readings of an answer as the observer takes
them (core.lpp). The tests check what the guide promises: the entities an answer adds, documents
of an earlier version without a warning, the switch that excludes a contact, every state, the
expiry of the telemetry sensors and the tracker and what a `person` and its triggers make of it,
the gateway's telemetry entities, the removal, no coordinates outside the trackers, and the
examples of the guide exactly as written.
"""

from __future__ import annotations

import asyncio
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed
from test_contract import (
    GUIDE,
    PREFIX,
    TOPICS,
    fire,
    guide_automations,
    guide_block,
    problems,
    state_document,
)

from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.mesh import Mesh, Node, Readings
from meshcore_home_gateway.core.model import Position
from meshcore_home_gateway.core.telemetry import STATES, STATUSES, TelemetryState
from meshcore_home_gateway.mqtt.contact_devices import READING_ENTITIES, ContactDevices
from meshcore_home_gateway.state import JsonStateFile
from meshcore_home_gateway.views import contact_document, contact_event, telemetry_event
from test_contact_devices import (
    ALICE,
    ALICE_ANSWER_IDS,
    ALICE_IDS,
    ALICE_NODE,
    COMPANION,
    NOW,
    Broker,
    contact_entities,
    gateway_devices,
    mesh_of,
    online,
    registry_devices,
    with_gateway,
)

BOB = "b0" * 32
SWITCH, STATE = "switch.mesh_alice_telemetry", "sensor.mesh_alice_telemetry_state"
TRACKER, BATTERY = "device_tracker.mesh_alice", "sensor.mesh_alice_battery"
STATUS = f"sensor.{PREFIX}_telemetry_status"
# Alice's sensors from her answers: battery, MCU temperature and a BME280 on channel 2
SENSORS = sorted(ALICE_ANSWER_IDS - {TRACKER})
# CayenneLPP entries as tests/radio_emulator.py writes them: battery 4.12 V, MCU 23.4 °C, and a
# BME280 on channel 2 (21.3 °C, 55.5 %, 1013.2 hPa, 120 m)
BATTERY_4_12, MCU = "0174019c", "016700ea"
BME280 = "026700d502686f0273279402790078"
# one entry of each type the documents publish, on channel 3: 3.3 V, -0.125 A, 12 W, 21.5 °C,
# 55.5 %, 1013.2 hPa, 120 m, luminosity 300, generic 7, analog input 1.23, 80 %, 868 Hz, 1.5 m
EVERY_TYPE = (
    "0374014a0375ff830380000c036700d703686f03732794037900780365012c"
    "0364000000070302007b0378500376000003640382000005dc"
)
NEW_KEYS = {  # the keys of contacts/<key> that 1.1.0a1 did not write
    "telemetry_state", "telemetry_excluded", "telemetry_current", "position_current",
    "readings_at", "battery_v", "mcu_temperature_c", "readings",
}  # fmt: skip


def gps(position: Position) -> str:
    """A GPS entry on channel 1: cells of 1e-4 degrees and the altitude in cm (12 m)."""
    cells = (round(position.latitude * 10_000), round(position.longitude * 10_000), 1200)
    return "0188" + "".join(cell.to_bytes(3, "big", signed=True).hex() for cell in cells)


def home(hass: HomeAssistant, north_m: float = 0.0) -> Position:
    return Position(hass.config.latitude + north_m / 111_195, hass.config.longitude)


def answer(mesh: Mesh, at: float, *entries: str) -> None:
    """Alice's handheld answers with these entries, taken as Observer.telemetry takes them."""
    payload = lpp.decode(bytes.fromhex("".join(entries)))
    found = lpp.position(payload)
    position, altitude = (found.position, found.altitude_m) if found else (None, None)
    mesh.contact_answered(ALICE, at, position, altitude, Readings(at, lpp.named(payload)))


def document(
    mesh: Mesh, state: TelemetryState | None = "answering", key: str = ALICE, **options: Any
) -> dict[str, Any]:
    """The contact's document; the observer pulls the state from the poller."""
    return contact_document(mesh.contacts[key], telemetry_state=state, **options)


async def with_alice(
    hass: HomeAssistant, tmp_path: Path, *entries: str, state: TelemetryState = "answering"
) -> tuple[Mesh, ContactDevices, Broker]:
    """The gateway, and Alice's device after an answer with `entries` (none: not asked yet);
    her retained document as the broker sends it to the new subscriptions."""
    await with_gateway(hass)
    broker, mesh = Broker(), mesh_of(ALICE_NODE)
    devices = gateway_devices(broker, JsonStateFile(tmp_path / "state.json"))
    if entries:
        answer(mesh, NOW, *entries)
    await announce_alice(hass, mesh, devices, broker, state if entries else "waiting")
    return mesh, devices, broker


async def announce_alice(
    hass: HomeAssistant, mesh: Mesh, devices: ContactDevices, broker: Broker,
    state: TelemetryState = "answering",
) -> None:  # fmt: skip
    """As the observer publishes an answer: her document, then her device with the components
    it gained; the broker sends the retained messages to the new subscriptions only."""
    await fire(hass, TOPICS.contact(ALICE), document(mesh, state))
    devices.update(mesh)
    await broker.deliver(hass)
    await online(hass)
    await fire(hass, TOPICS.contact(ALICE), document(mesh, state), retain=True)


def states(hass: HomeAssistant, *entity_ids: str) -> list[str]:
    return [hass.states.get(e).state for e in entity_ids]


def notifications(hass: HomeAssistant) -> list[str]:
    found = persistent_notification._async_get_or_create_notifications(hass)
    return [n["message"] for n in found.values()]


async def later(hass: HomeAssistant, seconds: float) -> None:
    """Home Assistant's timers that are due within `seconds` run."""
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=seconds))
    await hass.async_block_till_done()


async def elapse(hass: HomeAssistant, freezer: FrozenDateTimeFactory, seconds: float) -> None:
    """`seconds` go by: the clock moves on, which the `for` of a condition reads, and the event
    loop runs the timers then due after what it has ready (async_fire_time_changed would run
    them first: the timeout of an MQTT subscription before its waiting acknowledgement)."""
    freezer.tick(timedelta(seconds=seconds))
    await asyncio.sleep(0)  # the loop takes the timers now due
    await hass.async_block_till_done()


# --- the contact's device -----------------------------------------------------------------------


async def test_an_answer_adds_its_entities_to_the_contact_device(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh, devices, broker = await with_alice(hass, tmp_path)
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS  # the switch and the state at once
    assert states(hass, SWITCH, STATE) == ["on", "waiting"]
    answer(mesh, NOW, BATTERY_4_12, gps(home(hass)), MCU)  # a handheld without sensors
    await announce_alice(hass, mesh, devices, broker)
    tracker = hass.states.get(TRACKER)
    assert tracker.state == "home"
    answer(mesh, NOW + 900, BATTERY_4_12, gps(home(hass)), BME280, MCU)  # sensors allowed now
    await announce_alice(hass, mesh, devices, broker)
    assert hass.states.get(TRACKER).last_updated == tracker.last_updated  # at rest, kept
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS | ALICE_ANSWER_IDS
    registry = er.async_get(hass)
    shown = {}
    for entity_id in SENSORS:
        state, entry = hass.states.get(entity_id), registry.async_get(entity_id)
        shown[entity_id] = (
            state.attributes["friendly_name"], state.state, state.attributes.get("device_class"),
            state.attributes.get("unit_of_measurement"), state.attributes.get("state_class"),
            entry.options["sensor"]["suggested_display_precision"],
            entry.entity_category.value if entry.entity_category else None,
        )  # fmt: skip
    assert shown == {
        BATTERY: ("Alice Battery", "4.12", "voltage", "V", "measurement", 2, None),
        "sensor.mesh_alice_mcu_temperature": (
            "Alice MCU temperature", "23.4", "temperature", "°C", "measurement", 1, "diagnostic",
        ),
        "sensor.mesh_alice_ch2_temperature": (
            "Alice Temperature 2", "21.3", "temperature", "°C", "measurement", 1, None,
        ),
        "sensor.mesh_alice_ch2_humidity": (
            "Alice Humidity 2", "55.5", "humidity", "%", "measurement", 1, None,
        ),
        "sensor.mesh_alice_ch2_barometric_pressure": (
            "Alice Pressure 2", "1013.2", "atmospheric_pressure", "hPa", "measurement", 1, None,
        ),
        "sensor.mesh_alice_ch2_altitude": (
            "Alice Altitude 2", "120.0", "distance", "m", "measurement", 0, None,
        ),
    }  # fmt: skip
    switch, sensor = registry.async_get(SWITCH), registry.async_get(STATE)
    assert (switch.entity_category.value, sensor.entity_category) == ("config", None)
    telemetry_state = hass.states.get(STATE)
    assert (telemetry_state.state, telemetry_state.attributes["options"]) == (
        "answering", list(STATES),
    )  # fmt: skip
    assert telemetry_state.attributes["friendly_name"] == "Alice Telemetry state"
    assert hass.states.get(SWITCH).attributes["friendly_name"] == "Alice Telemetry"
    assert problems(caplog) == []


async def test_every_published_reading_type_gets_its_class_unit_and_precision(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Home Assistant takes the sensor of each reading type as the gateway announces it, without
    a warning (docs/platform-notes.md)."""
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, EVERY_TYPE, MCU)
    registry = er.async_get(hass)
    shown, announced = {}, {}  # by LPP type
    for key, value in document(mesh)["readings"].items():
        component = lpp.component_of(key)
        _channel, lpp_type, _n = lpp.parse_component(component)
        entity_id, entity = f"sensor.mesh_alice_{component}", READING_ENTITIES[lpp_type]
        state, entry = hass.states.get(entity_id), registry.async_get(entity_id)
        shown[lpp_type] = (
            state.state, state.attributes.get("device_class"),
            state.attributes.get("unit_of_measurement"),
            entry.options["sensor"]["suggested_display_precision"],
        )  # fmt: skip
        announced[lpp_type] = (str(value), entity.device_class, entity.unit, entity.precision)
    assert set(announced) == set(lpp.UNITS)  # every type the documents publish
    assert shown == announced
    assert problems(caplog) == []


async def test_older_documents_and_the_clearing_message_log_nothing(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A document that 1.1.0a1 retained, and the empty message that clears a contact's."""
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)),
                                               BME280, MCU)  # fmt: skip
    older = {k: v for k, v in document(mesh).items() if k not in NEW_KEYS}
    await fire(hass, TOPICS.contact(ALICE), older)
    assert set(states(hass, *SENSORS, STATE)) == {"unknown"}
    assert states(hass, SWITCH, TRACKER) == ["on", "home"]  # as with 1.1.0a1
    await fire(hass, TOPICS.contact(ALICE), "")
    assert set(states(hass, *SENSORS, STATE)) == {"unknown"}
    assert states(hass, SWITCH, TRACKER) == ["on", "unavailable"]  # no position known
    assert problems(caplog) == []


async def test_the_switch_sends_the_request_and_follows_the_document(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, MCU)
    request = f"meshcore/radio1/contacts/{ALICE}/telemetry"
    for action, payload, refused in (("turn_off", "off", "on"), ("turn_on", "on", "off")):
        mqtt.async_publish.reset_mock()
        await hass.services.async_call("switch", action, {"entity_id": SWITCH}, blocking=True)
        await hass.async_block_till_done()
        published = [c.args[:4] for c in mqtt.async_publish.call_args_list]
        assert published == [(request, payload, 1, False)]
        assert hass.states.get(SWITCH).state == refused  # not until the gateway took it
        event = contact_event("refused", ALICE, ALICE_NODE, request="telemetry",
                              reason="rate_limited")  # fmt: skip
        await fire(hass, TOPICS.contact_event, event)
        assert hass.states.get(SWITCH).state == refused  # its document is unchanged
        excluded = payload == "off"
        await fire(hass, TOPICS.contact(ALICE), document(
            mesh, "excluded" if excluded else "waiting", excluded=excluded
        ))  # fmt: skip
        assert hass.states.get(SWITCH).state == payload
    assert problems(caplog) == []


async def test_every_state_is_an_option(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh, _devices, _broker = await with_alice(hass, tmp_path)
    for state in STATES:
        await fire(hass, TOPICS.contact(ALICE), document(mesh, state))
        assert hass.states.get(STATE).state == state
    assert problems(caplog) == []


async def test_answers_that_are_not_current_make_the_sensors_and_tracker_unavailable(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)),
                                               BME280, MCU)  # fmt: skip
    before = hass.states.get(TRACKER)
    assert {*states(hass, *SENSORS, TRACKER)} - {"unavailable", "unknown"} == {
        "4.12", "23.4", "21.3", "55.5", "1013.2", "120.0", "home",
    }  # fmt: skip
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "no_position"))  # GPS off for an hour
    assert "unavailable" not in states(hass, *SENSORS)
    assert hass.states.get(TRACKER).state == "unavailable"
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))  # two requests unanswered
    assert set(states(hass, *SENSORS, TRACKER)) == {"unavailable"}
    assert states(hass, STATE, SWITCH) == ["away", "on"]  # the state says why
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "answering"))  # back
    assert "unavailable" not in states(hass, *SENSORS)
    tracker = hass.states.get(TRACKER)
    assert (tracker.state, tracker.attributes["latitude"], tracker.attributes["longitude"]) == (
        "home", before.attributes["latitude"], before.attributes["longitude"],
    )  # fmt: skip
    assert problems(caplog) == []


async def test_a_position_from_an_advert_does_not_expire(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Bob's handheld never answered; his adverts carry a position."""
    await with_gateway(hass)
    broker = Broker()
    mesh = mesh_of(Node(BOB, "Bob", COMPANION, position=home(hass)))
    gateway_devices(broker, JsonStateFile(tmp_path / "state.json")).update(mesh)
    await broker.deliver(hass)
    await online(hass)
    for state in ("away", "no_answer", "excluded"):
        await fire(hass, TOPICS.contact(BOB), document(mesh, state, BOB))
        bob = hass.states.get("device_tracker.mesh_bob")
        assert (bob.state, bob.attributes["position_source"]) == ("home", "advert")
    assert problems(caplog) == []


# --- presence ------------------------------------------------------------------------------------


def presence_automations() -> list[dict[str, Any]]:
    """One automation per presence trigger of the guide's table; each posts its own name."""

    def posting(name: str, trigger: dict[str, Any]) -> dict[str, Any]:
        return {"alias": name, "triggers": [trigger], "actions": [
            {"action": "persistent_notification.create", "data": {"message": name}},
        ]}  # fmt: skip

    alice, zone = {"entity_id": "person.alice"}, {"zone": "zone.home"}
    return [
        posting("zone leave", {"trigger": "zone", **alice, **zone, "event": "leave"}),
        posting("zone enter", {"trigger": "zone", **alice, **zone, "event": "enter"}),
        posting("from home", {"trigger": "state", **alice, "from": "home"}),
        posting("zone.left", {"trigger": "zone.left", "target": alice, "options": zone}),
        posting("zone.entered", {"trigger": "zone.entered", "target": alice, "options": zone}),
        posting("cleared", {"trigger": "zone.occupancy_cleared", "options": zone}),
        posting("detected", {"trigger": "zone.occupancy_detected", "options": zone}),
    ]


async def with_person(hass: HomeAssistant, *trackers: str) -> None:
    config = {"id": "alice", "name": "Alice", "device_trackers": [TRACKER, *trackers]}
    assert await async_setup_component(hass, "person", {"person": [config]})
    assert await async_setup_component(hass, "persistent_notification", {})
    await hass.async_block_till_done()


async def test_a_person_ignores_an_expired_mesh_tracker(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The table of docs/home-assistant.md "Positions and presence", for a person whose only
    GPS tracker is the mesh tracker; then with a second one."""
    phone = "device_tracker.alice_phone"
    await with_person(hass, phone)
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)), MCU)
    assert states(hass, "person.alice", "zone.home") == ["home", "1"]
    assert await async_setup_component(hass, "automation", {"automation": presence_automations()})
    await hass.async_block_till_done()

    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))
    assert states(hass, TRACKER, "person.alice", "zone.home") == ["unavailable", "unknown", "0"]
    assert notifications(hass) == ["zone leave", "from home", "cleared"]  # no zone.left
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "answering"))
    assert states(hass, "person.alice", "zone.home") == ["home", "1"]
    assert notifications(hass)[3:] == ["zone enter", "detected"]  # no zone.entered

    at_home = {"source_type": "gps", "latitude": hass.config.latitude,
               "longitude": hass.config.longitude, "gps_accuracy": 10}  # fmt: skip
    hass.states.async_set(phone, "home", {**at_home, "in_zones": ["zone.home"]})
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))
    assert states(hass, "person.alice", "zone.home") == ["home", "1"]  # it follows the phone
    assert hass.states.get("person.alice").attributes["source"] == phone
    assert problems(caplog) == []


async def test_readings_have_no_coordinates(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass, 500)), BME280, MCU)
    assert hass.states.get(TRACKER).state == "not_home"
    on_map = {
        s.entity_id for s in hass.states.async_all()
        if "latitude" in s.attributes or "longitude" in s.attributes
    }  # fmt: skip
    assert on_map == {TRACKER, "zone.home"}  # only the tracker of all of Alice's entities
    assert problems(caplog) == []


# --- the gateway's telemetry entities ------------------------------------------------------------


def telemetry_state_document(status: str, reason: str | None) -> dict[str, Any]:
    """The state document with the telemetry keys, as Bridge.state_payload writes them."""
    return {
        **state_document(known=True), "telemetry": status, "telemetry_reason": reason,
        "telemetry_airtime_1h_s": 2.464, "telemetry_mesh_airtime_1h_s": 7.392,
    }  # fmt: skip


async def test_the_gateway_telemetry_entities(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_gateway(hass)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)  # 1.0.0: no telemetry
    status = hass.states.get(STATUS)
    assert (status.state, status.attributes["reason"]) == ("unknown", None)
    await fire(hass, TOPICS.state, telemetry_state_document("paused", "no_radio"))
    status = hass.states.get(STATUS)
    assert (status.state, status.attributes["reason"]) == ("paused", "no_radio")
    assert status.attributes["options"] == list(STATUSES)
    assert status.attributes["friendly_name"] == "MeshCore radio Telemetry status"
    await fire(hass, TOPICS.state, telemetry_state_document("on", None))
    status = hass.states.get(STATUS)
    assert (status.state, status.attributes["reason"]) == ("on", None)
    registry = er.async_get(hass)
    assert registry.async_get(STATUS).entity_category.value == "diagnostic"
    for lane in ("telemetry_airtime", "telemetry_mesh_airtime"):
        entry = registry.async_get(f"sensor.{PREFIX}_{lane}")
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
        assert entry.entity_category.value == "diagnostic"
    event = telemetry_event("unanswered", ALICE, ALICE_NODE, "away")
    await fire(hass, TOPICS.telemetry_event, event)
    entity = hass.states.get(f"event.{PREFIX}_telemetry")
    assert entity.attributes["event_types"] == ["answered", "unanswered"]
    assert {k: entity.attributes[k] for k in event} == event
    contact = hass.states.get(f"event.{PREFIX}_contact")
    assert {"excluded", "included"} <= set(contact.attributes["event_types"])
    assert problems(caplog) == []


# --- the removal ---------------------------------------------------------------------------------


@pytest.mark.parametrize("restart", [False, True], ids=["running", "after-a-restart"])
async def test_a_removed_contact_takes_its_switch_and_readings(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path, *,
    restart: bool,
) -> None:  # fmt: skip
    """After a restart Home Assistant ignores an empty document for a device it has not
    discovered since; the placeholders sent first bind each entity, the switch too."""
    _mesh, devices, broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)),
                                              BME280, MCU)  # fmt: skip
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS | ALICE_ANSWER_IDS
    if restart:
        await hass.config_entries.async_reload(
            hass.config_entries.async_entries("mqtt")[0].entry_id
        )
        await hass.async_block_till_done()
        await with_gateway(hass)
        devices = gateway_devices(broker, JsonStateFile(tmp_path / "state.json"))
    devices.update(mesh_of())  # Alice left the radio's table
    await broker.deliver(hass)
    assert f"{PREFIX}_contact_{ALICE}" not in registry_devices(hass)
    assert contact_entities(hass, ALICE) == {}
    assert problems(caplog) == []


# --- the examples of the guide -------------------------------------------------------------------


def guide_automation(alias: str) -> dict[str, Any]:
    (automation,) = [a for a in guide_automations() if a["alias"] == alias]
    return automation


def guide_template() -> str:
    (template,) = re.findall(r"```jinja\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    return str(template)


async def test_the_dashboard_card_and_the_battery_template(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)), MCU)
    card = guide_block("type: entities\ntitle: Alice's handheld")
    assert [row["entity"] for row in card["entities"]] == [STATE, BATTERY, TRACKER, SWITCH]
    # the helper of the guide: a template sensor with its state template
    helper = {"name": "Alice battery level", "state": guide_template(), "device_class": "battery",
              "unit_of_measurement": "%", "state_class": "measurement"}  # fmt: skip
    assert await async_setup_component(hass, "template", {"template": [{"sensor": [helper]}]})
    await hass.async_block_till_done()
    level = "sensor.alice_battery_level"
    assert hass.states.get(level).state == "93"  # 4.12 V
    for hex_volts, expected in (("0183", "73"), ("01b8", "100"), ("0118", "0")):  # 3.87, 4.4, 2.8
        answer(mesh, NOW + 900, f"0174{hex_volts}", MCU)
        await fire(hass, TOPICS.contact(ALICE), document(mesh))
        assert hass.states.get(level).state == expected
    answer(mesh, NOW + 1800, MCU)  # no battery in the answer
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    assert states(hass, BATTERY, level) == ["unknown", "unknown"]
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))
    assert states(hass, BATTERY, level) == ["unavailable", "unknown"]
    assert problems(caplog) == []


async def test_the_battery_low_example_warns_only_while_the_handheld_answers(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, MCU)
    automation = guide_automation("Handheld battery low")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    answer(mesh, NOW + 900, "01740154", MCU)  # 3.4 V
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))  # out of range: unavailable
    mqtt.async_publish.reset_mock()
    await later(hass, 15 * 60)
    assert mqtt.async_publish.call_args_list == []  # it cannot warn
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    await later(hass, 9 * 60)
    assert mqtt.async_publish.call_args_list == []
    await later(hass, 10 * 60 + 1)
    (send,) = [c.args[:4] for c in mqtt.async_publish.call_args_list]
    assert send == (TOPICS.contact_send(ALICE), "Your handheld battery is low", 1, False)
    assert problems(caplog) == []


async def test_the_not_answering_example_posts_one_notification_per_event(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_gateway(hass)
    assert await async_setup_component(hass, "persistent_notification", {})
    automation = guide_automation("Mesh handheld not answering")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    bob = Node(BOB, "**Bob**", COMPANION)  # a name from the mesh: never in the message
    for event in (
        telemetry_event("unanswered", ALICE, ALICE_NODE, "away"),
        telemetry_event("answered", ALICE, ALICE_NODE, "answering"),
        telemetry_event("unanswered", BOB, bob, "away"),
        telemetry_event("unanswered", ALICE, ALICE_NODE, "away"),  # the next absence
    ):
        await fire(hass, TOPICS.telemetry_event, event)
    assert notifications(hass) == [f"{ALICE[:12]}: away", f"{BOB[:12]}: away",
                                   f"{ALICE[:12]}: away"]  # fmt: skip
    assert problems(caplog) == []


async def test_the_left_home_example_skips_a_paused_telemetry_and_a_restart(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    freezer: FrozenDateTimeFactory,
) -> None:  # fmt: skip
    await with_person(hass)
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)), MCU)
    await fire(hass, TOPICS.state, telemetry_state_document("on", None), retain=True)
    automation = guide_automation("Alice left home")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    assert hass.states.get("person.alice").state == "home"

    await fire(hass, TOPICS.availability, "offline")  # the gateway restarts
    assert hass.states.get("person.alice").state == "unknown"
    await elapse(hass, freezer, 10)
    await fire(hass, TOPICS.availability, "online")  # live: the broker clears the retain flag
    assert hass.states.get("person.alice").state == "home"
    await elapse(hass, freezer, 6 * 60)
    assert notifications(hass) == []

    await fire(hass, TOPICS.state, telemetry_state_document("paused", "no_radio"))
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))  # an hour without the radio
    await elapse(hass, freezer, 6 * 60)
    assert notifications(hass) == []

    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    await fire(hass, TOPICS.state, telemetry_state_document("on", None))
    await elapse(hass, freezer, 5 * 60)  # her answers come again
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))  # she left
    await elapse(hass, freezer, 4 * 60)
    assert notifications(hass) == []
    await elapse(hass, freezer, 60 + 1)  # the status has read `on` for 10 minutes
    assert notifications(hass) == ["unknown"]
    assert problems(caplog) == []


async def test_the_left_home_example_waits_for_the_first_answer_after_the_radio_returns(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    freezer: FrozenDateTimeFactory,
) -> None:  # fmt: skip
    """*Telemetry status* reads `on` as soon as the radio is back, and Alice answers later: after
    her answers expired during the outage, or when they expire while she waits for her turn."""
    await with_person(hass)
    mesh, _devices, _broker = await with_alice(hass, tmp_path, BATTERY_4_12, gps(home(hass)), MCU)
    await fire(hass, TOPICS.state, telemetry_state_document("paused", "no_radio"), retain=True)
    automation = guide_automation("Alice left home")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()

    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))  # an hour without the radio
    await elapse(hass, freezer, 4 * 60)
    await fire(hass, TOPICS.state, telemetry_state_document("on", None))  # the radio is back
    await elapse(hass, freezer, 60 + 1)  # the trigger's `for` ends before her first answer
    assert notifications(hass) == []
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    assert hass.states.get("person.alice").state == "home"

    await fire(hass, TOPICS.state, telemetry_state_document("paused", "no_radio"))
    await fire(hass, TOPICS.state, telemetry_state_document("on", None))  # back within the hour
    await elapse(hass, freezer, 2 * 60)
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "away"))  # others are asked first
    await elapse(hass, freezer, 5 * 60 + 1)  # the status has read `on` for 7 minutes
    assert notifications(hass) == []
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    assert hass.states.get("person.alice").state == "home"
    assert problems(caplog) == []


async def test_the_contact_not_changed_example_reports_an_unsaved_exclusion(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_gateway(hass)
    assert await async_setup_component(hass, "persistent_notification", {})
    automation = guide_automation("Mesh contact not changed")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    for kind, reason in (
        ("excluded", None), ("included", None), ("excluded", "not_saved"),
        ("excluded", "not_forgotten"), ("refused", "not_a_companion"),
        ("failed", "state_unwritable"),
    ):  # fmt: skip
        event = contact_event(kind, ALICE, ALICE_NODE, request="telemetry", request_id="x1",
                              reason=reason)  # fmt: skip
        await fire(hass, TOPICS.contact_event, event)
    prefix = ALICE[:12]
    assert notifications(hass) == [
        f"telemetry {prefix}: not_saved", f"telemetry {prefix}: not_forgotten",
        f"telemetry {prefix}: not_a_companion", f"telemetry {prefix}: state_unwritable",
    ]  # fmt: skip
    assert problems(caplog) == []
