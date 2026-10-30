"""The contacts' devices against the real Home Assistant MQTT integration (ADR 0017).

The gateway's own code (ContactDevices) produces the messages, and they reach Home Assistant as
the broker would deliver them. The tests check what docs/home-assistant.md promises: a device per
contact under the gateway's, its entity IDs and names, its states, the map only for trackers, the
removal also after a restart of Home Assistant, and a device the user deleted staying away.
"""

from __future__ import annotations

import fnmatch
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message
from test_contract import CONFIG, GUIDE, PREFIX, RADIO, TOPICS, announce, fire, problems

from meshcore_home_gateway.core.mesh import Heard, Mesh, Node, Signal
from meshcore_home_gateway.core.model import InboundMessage, MessageKind, Position
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices, contact_discovery_topic
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.mqtt.payloads import received_payload
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from meshcore_home_gateway.views import (
    contact_document,
    contacts_document,
    encode,
    nodes_document,
    stats_document,
)

ALICE, HILL = "a1" * 32, "e5" * 32
NOW = 1_800_000_000.0
COMPANION, REPEATER = 1, 2


class Broker:
    """Collects what the gateway publishes; `deliver` hands it to Home Assistant in order."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        self.sent.append((topic, payload))

    async def deliver(self, hass: HomeAssistant) -> None:
        sent, self.sent = self.sent, []
        for topic, payload in sent:
            async_fire_mqtt_message(hass, topic, payload.decode("utf-8"))
        await hass.async_block_till_done()
        await hass.async_block_till_done()


def mesh_of(*nodes: Node, mesh: Mesh | None = None) -> Mesh:
    mesh = mesh or Mesh()
    mesh.contacts_read(nodes, NOW)
    return mesh


ALICE_NODE = Node(ALICE, "Alice", COMPANION)
HILL_NODE = Node(HILL, "Hilltop", REPEATER, position=Position(52.123456, 9.654321))


@pytest.fixture
def broker() -> Broker:
    return Broker()


@pytest.fixture
def store(tmp_path: Path) -> JsonStateFile:
    return JsonStateFile(tmp_path / "state.json")


def gateway_devices(broker: Broker, store: JsonStateFile) -> ContactDevices:
    return ContactDevices(
        CONFIG, TOPICS, broker, ContactDeviceRecords(store), wall_clock=lambda: NOW
    )


async def with_gateway(hass: HomeAssistant) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    await online(hass)


async def online(hass: HomeAssistant) -> None:
    """The retained availability, as the broker sends it for each new subscription."""
    await fire(hass, TOPICS.availability, "online", retain=True)


def registry_devices(hass: HomeAssistant) -> dict[str, dr.DeviceEntry]:
    (entry,) = hass.config_entries.async_entries("mqtt")
    found = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {next(iter(d.identifiers))[1]: d for d in found}


def contact_entities(hass: HomeAssistant, key: str) -> dict[str, er.RegistryEntry]:
    device = registry_devices(hass).get(f"{PREFIX}_contact_{key}")
    if device is None:
        return {}
    entries = er.async_entries_for_device(
        er.async_get(hass), device.id, include_disabled_entities=True
    )
    return {e.entity_id: e for e in entries}


def friendly(hass: HomeAssistant, entity_id: str) -> str:
    state = hass.states.get(entity_id)
    assert state is not None, entity_id
    return str(state.attributes["friendly_name"])


ALICE_IDS = {
    "notify.mesh_alice", "event.mesh_alice_message", "sensor.mesh_alice_last_heard",
    "sensor.mesh_alice_hops", "sensor.mesh_alice_snr", "button.mesh_alice_remove",
    "switch.mesh_alice_telemetry", "sensor.mesh_alice_telemetry_state",
}  # fmt: skip
HILL_IDS = {
    "device_tracker.mesh_hilltop", "sensor.mesh_hilltop_last_heard", "sensor.mesh_hilltop_hops",
    "sensor.mesh_hilltop_snr", "button.mesh_hilltop_remove",
}  # fmt: skip


async def test_every_contact_gets_a_device_below_the_gateway(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    await with_gateway(hass)
    gateway_devices(broker, store).update(mesh_of(ALICE_NODE, HILL_NODE))
    await broker.deliver(hass)
    devices = registry_devices(hass)
    gateway, alice = devices[PREFIX], devices[f"{PREFIX}_contact_{ALICE}"]
    assert alice.via_device_id == gateway.id
    assert (alice.name, alice.manufacturer, alice.model, alice.serial_number) == (
        "Alice", "MeshCore", "Companion", ALICE,
    )  # fmt: skip
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS
    assert set(contact_entities(hass, HILL)) == HILL_IDS
    assert not any(entry.disabled_by for entry in contact_entities(hass, ALICE).values())
    assert friendly(hass, "notify.mesh_alice") == "Alice"
    assert friendly(hass, "sensor.mesh_alice_last_heard") == "Alice Last heard"
    assert friendly(hass, "device_tracker.mesh_hilltop") == "Hilltop Position"
    documented = set(re.findall(r"\b[a-z_]+\.mesh_alice\w*", GUIDE.read_text(encoding="utf-8")))
    assert documented - {"device_tracker.mesh_alice"} <= ALICE_IDS  # Hilltop has the tracker
    assert problems(caplog) == []


async def test_the_contact_document_fills_its_entities(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    await with_gateway(hass)
    mesh = mesh_of(ALICE_NODE, HILL_NODE)
    mesh.contact_heard(ALICE, Heard(NOW, "message", Signal(2, 6.25, -101)))
    gateway_devices(broker, store).update(mesh)
    await broker.deliver(hass)
    await online(hass)
    for key in (ALICE, HILL):
        await fire(hass, TOPICS.contact(key), contact_document(mesh.contacts[key]), retain=True)
    assert hass.states.get("sensor.mesh_alice_last_heard").state == "2027-01-15T08:00:00+00:00"
    assert hass.states.get("sensor.mesh_alice_hops").state == "2"
    assert hass.states.get("sensor.mesh_hilltop_last_heard").state == "unknown"
    tracker = hass.states.get("device_tracker.mesh_hilltop")
    assert tracker.state == "not_home"
    assert (tracker.attributes["latitude"], tracker.attributes["longitude"]) == (
        52.123456, 9.654321,
    )  # fmt: skip
    assert tracker.attributes["position_source"] == "advert"
    on_map = {
        s.entity_id for s in hass.states.async_all()
        if "latitude" in s.attributes and "longitude" in s.attributes
    }  # fmt: skip
    assert on_map == {"device_tracker.mesh_hilltop", "zone.home"}  # nothing else on the map
    assert problems(caplog) == []


async def test_a_position_from_telemetry_carries_its_accuracy(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """A telemetry position has `gps_accuracy` 15, which widens the zone test: 110 m from the
    centre of a 100 m home zone is `home`. An advert's has none (0). A resting companion's
    answers change no tracker state."""
    await with_gateway(hass)
    near_home = Position(hass.config.latitude + 110 / 111_195, hass.config.longitude)
    mesh = mesh_of(ALICE_NODE, HILL_NODE)
    mesh.contact_answered(ALICE, NOW, near_home, 12.0)
    gateway_devices(broker, store).update(mesh)
    await broker.deliver(hass)
    await online(hass)
    first = contact_document(mesh.contacts[ALICE], telemetry_state="answering")  # current
    await fire(hass, TOPICS.contact(ALICE), first, retain=True)
    await fire(hass, TOPICS.contact(HILL), contact_document(mesh.contacts[HILL]), retain=True)
    alice = hass.states.get("device_tracker.mesh_alice")
    assert alice.state == "home"
    assert alice.attributes["gps_accuracy"] == 15
    assert (alice.attributes["position_source"], alice.attributes["altitude_m"]) == (
        "telemetry", 12.0,
    )  # fmt: skip
    hill = hass.states.get("device_tracker.mesh_hilltop")
    assert hill.attributes["gps_accuracy"] == 0
    mesh.contact_answered(ALICE, NOW + 900, near_home, 12.0)  # the next answer, at rest
    document = contact_document(mesh.contacts[ALICE], telemetry_state="answering")
    assert document != first  # its time received moved
    await fire(hass, TOPICS.contact(ALICE), document)
    assert hass.states.get("device_tracker.mesh_alice").last_updated == alice.last_updated
    moved = Position(near_home.latitude + 30 / 111_195, near_home.longitude)  # 30 m on
    mesh.contact_answered(ALICE, NOW + 1800, moved, 12.0)
    moved_document = contact_document(mesh.contacts[ALICE], telemetry_state="answering")
    await fire(hass, TOPICS.contact(ALICE), moved_document)
    assert hass.states.get("device_tracker.mesh_alice").attributes["latitude"] == pytest.approx(
        moved.latitude
    )
    old = contact_document(mesh.contacts[HILL])  # a document of an earlier version: no warning
    del old["position_accuracy_m"], old["position_received_at"]
    await fire(hass, TOPICS.contact(HILL), {**old, "latitude": 52.2})
    hill = hass.states.get("device_tracker.mesh_hilltop")
    assert (hill.attributes["latitude"], hill.attributes["gps_accuracy"]) == (52.2, 0)
    assert problems(caplog) == []


async def test_a_tracker_without_a_position_leaves_the_map(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    await with_gateway(hass)
    mesh = mesh_of(HILL_NODE)
    gateway_devices(broker, store).update(mesh)
    await broker.deliver(hass)
    await online(hass)
    document = contact_document(mesh.contacts[HILL])
    await fire(hass, TOPICS.contact(HILL), document, retain=True)
    assert hass.states.get("device_tracker.mesh_hilltop").state == "not_home"
    await fire(hass, TOPICS.contact(HILL), {**document, "latitude": None, "longitude": None})
    tracker = hass.states.get("device_tracker.mesh_hilltop")
    assert tracker.state == "unavailable"
    assert "latitude" not in tracker.attributes
    assert hass.states.get("sensor.mesh_hilltop_hops").state != "unavailable"
    await fire(hass, TOPICS.availability, "offline")  # live: no retain flag
    assert hass.states.get("sensor.mesh_hilltop_hops").state == "unavailable"
    assert problems(caplog) == []


async def test_messages_and_sending_per_contact(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    await with_gateway(hass)
    gateway_devices(broker, store).update(mesh_of(ALICE_NODE))
    await broker.deliver(hass)
    await online(hass)
    message = InboundMessage(
        MessageKind.CONTACT, "x" * 300, int(NOW), NOW, sender_prefix=ALICE[:12]
    )
    payload = received_payload(message, None, None)
    await fire(hass, TOPICS.contact_received(ALICE), payload)
    event = hass.states.get("event.mesh_alice_message")
    assert event.attributes["event_type"] == "contact"
    assert event.attributes["text"] == "x" * 300  # attributes have no length limit
    mqtt.async_publish.reset_mock()
    await hass.services.async_call(
        "notify", "send_message", {"message": "Dinner is ready"}, blocking=True,
        target={"entity_id": "notify.mesh_alice"},
    )  # fmt: skip
    (send,) = [c.args for c in mqtt.async_publish.call_args_list]
    assert send[:4] == (f"meshcore/radio1/contacts/{ALICE}/send", "Dinner is ready", 1, False)
    assert problems(caplog) == []


async def test_a_contact_that_left_loses_its_device(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    await with_gateway(hass)
    devices = gateway_devices(broker, store)
    mesh = mesh_of(ALICE_NODE, HILL_NODE)
    devices.update(mesh)
    await broker.deliver(hass)
    devices.update(mesh_of(HILL_NODE, mesh=mesh))
    await broker.deliver(hass)
    assert f"{PREFIX}_contact_{ALICE}" not in registry_devices(hass)
    assert not ALICE_IDS & {e.entity_id for e in er.async_get(hass).entities.values()}
    assert set(contact_entities(hass, HILL)) == HILL_IDS  # the others stay
    assert problems(caplog) == []


@pytest.mark.parametrize("pending", [False, True], ids=["left-while-stopped", "pending"])
async def test_the_removal_works_after_a_restart_of_home_assistant(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile, *,
    caplog: pytest.LogCaptureFixture, pending: bool,
) -> None:  # fmt: skip
    """Home Assistant ignores an empty document for a device it has not discovered since its own
    start; the placeholders sent first bind the entities it kept."""
    await with_gateway(hass)
    gateway_devices(broker, store).update(mesh_of(ALICE_NODE, HILL_NODE))
    await broker.deliver(hass)
    if pending:  # removed while Home Assistant was down: sent again after its birth message
        gateway_devices(broker, store).update(mesh_of(HILL_NODE))
        broker.sent.clear()
    await hass.config_entries.async_reload(hass.config_entries.async_entries("mqtt")[0].entry_id)
    await hass.async_block_till_done()
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS  # kept from before the restart
    await with_gateway(hass)
    restarted = gateway_devices(broker, store)
    if pending:
        restarted.announce_all(birth=True)
    else:  # the gateway restarts too, and Alice left meanwhile
        restarted.update(mesh_of(HILL_NODE))
    await broker.deliver(hass)
    assert f"{PREFIX}_contact_{ALICE}" not in registry_devices(hass)
    assert contact_entities(hass, ALICE) == {}
    assert problems(caplog) == []


async def test_a_device_the_user_deletes_stays_hidden_until_shown(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile, *, hass_ws_client: Any,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    assert await async_setup_component(hass, "config", {})
    await with_gateway(hass)
    devices = gateway_devices(broker, store)
    mesh = mesh_of(ALICE_NODE, HILL_NODE)
    devices.update(mesh)
    await broker.deliver(hass)
    registry = dr.async_get(hass)
    alice = registry_devices(hass)[f"{PREFIX}_contact_{ALICE}"]
    registry.async_update_device(alice.id, name_by_user="Alice (phone)")
    mqtt.async_publish.reset_mock()
    client = await hass_ws_client()
    # the "Delete" button of the device page
    await client.send_json(
        {"id": 1, "type": "config/device_registry/remove", "device_id": alice.id}
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    published = [c.args[:4] for c in mqtt.async_publish.call_args_list]
    assert published == [(contact_discovery_topic(CONFIG, ALICE), None, 0, True)]
    # the broker hands it to the gateway's subscription, with the retain flag cleared (live)
    devices.discovery_message(published[0][0], b"", retained=False)
    (event_topic, event), *others = broker.sent
    assert (event_topic, json.loads(event)["event_type"]) == (TOPICS.contact_event, "hidden")
    assert others == []
    broker.sent.clear()
    devices.update(mesh)
    devices.announce_all()  # also after Home Assistant's next start
    assert contact_discovery_topic(CONFIG, ALICE) not in {t for t, _ in broker.sent}
    await broker.deliver(hass)
    assert f"{PREFIX}_contact_{ALICE}" not in registry_devices(hass)

    devices.show_hidden()
    await broker.deliver(hass)
    back = registry_devices(hass)[f"{PREFIX}_contact_{ALICE}"]
    assert (back.id, back.name_by_user) == (alice.id, "Alice (phone)")  # as the user had it
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS
    assert problems(caplog) == []


async def test_equal_names_on_two_gateways_get_numbered_entity_ids(
    hass: HomeAssistant, mqtt: Any, broker: Broker, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    from test_contract import make_config  # noqa: PLC0415

    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

    await with_gateway(hass)
    gateway_devices(broker, JsonStateFile(tmp_path / "one.json")).update(mesh_of(ALICE_NODE))
    other = make_config(node_id="radio2")
    other_topics = Topics(other.mqtt.base_topic, "radio2")
    records = ContactDeviceRecords(JsonStateFile(tmp_path / "two.json"))
    ContactDevices(other, other_topics, broker, records).update(mesh_of(ALICE_NODE))
    await broker.deliver(hass)
    ids = {e.entity_id for e in er.async_get(hass).entities.values()}
    assert {"notify.mesh_alice", "notify.mesh_alice_2"} <= ids
    assert problems(caplog) == []


def home_assistant_publishes(mqtt: Any) -> list[tuple[Any, ...]]:
    published = [c.args[:4] for c in mqtt.async_publish.call_args_list]
    mqtt.async_publish.reset_mock()
    return published


def warnings_of(caplog: pytest.LogCaptureFixture) -> set[str]:
    return {re.sub(r" for .*| <.*", "", p) for p in problems(caplog)}


async def test_a_removal_sent_again_after_home_assistant_started_leaves_nothing(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Home Assistant took the removal, then restarted: the removal sent once more brings the
    device back for a moment and removes it again, with cosmetic warnings only."""
    await with_gateway(hass)
    devices = gateway_devices(broker, store)
    mesh = mesh_of(ALICE_NODE, HILL_NODE)
    devices.update(mesh)
    await broker.deliver(hass)
    devices.update(mesh_of(HILL_NODE, mesh=mesh))
    await broker.deliver(hass)
    await hass.config_entries.async_reload(hass.config_entries.async_entries("mqtt")[0].entry_id)
    await hass.async_block_till_done()
    await with_gateway(hass)
    devices.announce_all(birth=True)
    await broker.deliver(hass)
    assert f"{PREFIX}_contact_{ALICE}" not in registry_devices(hass)
    assert home_assistant_publishes(mqtt) == []
    assert warnings_of(caplog) <= {
        "WARNING homeassistant.helpers.dispatcher: Unable to remove unknown dispatcher",
    }
    devices.announce_all(birth=True)  # done: not sent again
    assert all(t != contact_discovery_topic(CONFIG, ALICE) for t, _ in broker.sent)


async def test_an_entity_the_user_disabled_never_hides_the_contact(
    hass: HomeAssistant, mqtt: Any, broker: Broker, store: JsonStateFile,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Home Assistant clears a device's topic itself when it removes a disabled entity. The
    gateway never removes a single component, and the copy of a removal it sees is no deletion."""
    await with_gateway(hass)
    devices = gateway_devices(broker, store)
    mesh = mesh_of(ALICE_NODE, HILL_NODE)
    devices.update(mesh)
    await broker.deliver(hass)
    registry = er.async_get(hass)
    registry.async_update_entity(
        "sensor.mesh_alice_hops", disabled_by=er.RegistryEntryDisabler.USER
    )
    registry.async_update_entity(
        "event.mesh_alice_message", disabled_by=er.RegistryEntryDisabler.USER
    )
    await hass.async_block_till_done()
    home_assistant_publishes(mqtt)
    devices.update(mesh_of(Node(ALICE, "Alice", 4), HILL_NODE, mesh=mesh))  # now a sensor
    await broker.deliver(hass)
    assert home_assistant_publishes(mqtt) == []
    assert set(contact_entities(hass, ALICE)) == ALICE_IDS  # nothing removed
    assert problems(caplog) == []
    devices.update(mesh_of(HILL_NODE, mesh=mesh))  # Alice leaves the table
    await broker.deliver(hass)
    for topic, _payload, _qos, _retain in home_assistant_publishes(mqtt):
        devices.discovery_message(topic, b"", retained=False)  # the broker hands it on, live
    assert ContactDeviceRecords(store).hidden == set()
    assert broker.sent == []  # no "hidden" event
    assert contact_entities(hass, ALICE) == {}
    assert warnings_of(caplog) == {
        "WARNING homeassistant.components.mqtt.discovery: No device components to cleanup",
    }  # its own empty message, which the broker hands back to it too


# --- the gateway's own entities for the observer's documents -------------------------------------


async def test_the_observer_documents_fill_the_gateway_entities(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_gateway(hass)
    mesh = mesh_of(ALICE_NODE, Node(HILL, "Hilltop", REPEATER, flags=0x10))
    from meshcore_home_gateway.core.mesh import AirtimeMeter  # noqa: PLC0415

    await fire(hass, TOPICS.contacts, contacts_document(mesh), retain=True)
    await fire(hass, TOPICS.nodes, nodes_document(mesh), retain=True)
    await fire(hass, TOPICS.stats, stats_document(None, AirtimeMeter(), None), retain=True)
    states = {s.entity_id: s for s in hass.states.async_all()}
    assert states[f"sensor.{PREFIX}_contacts"].state == "2"
    assert states[f"sensor.{PREFIX}_nodes"].state == "0"
    remote = states[f"binary_sensor.{PREFIX}_remote_cli"]
    assert (remote.state, remote.attributes["contacts"]) == ("on", [HILL])
    assert states[f"binary_sensor.{PREFIX}_advert_position"].state == "off"
    assert states[f"sensor.{PREFIX}_noise_floor"].state == "unknown"
    await fire(hass, TOPICS.contact_event, {"event_type": "hidden", "key": ALICE, "name": "Alice",
                                            "type": "chat"})  # fmt: skip
    assert hass.states.get(f"event.{PREFIX}_contact").attributes["event_type"] == "hidden"
    mqtt.async_publish.reset_mock()
    await hass.services.async_call(
        "button", "press", {"entity_id": f"button.{PREFIX}_show_hidden"}, blocking=True
    )
    (press,) = [c.args[:2] for c in mqtt.async_publish.call_args_list]
    assert press == ("meshcore/radio1/homeassistant/show_hidden", "show")
    assert problems(caplog) == []


# --- the recorder exclusions of the guide ---------------------------------------------------------


def guide_recorder() -> dict[str, Any]:
    blocks = re.findall(r"```yaml\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    (recorder,) = [yaml.safe_load(b) for b in blocks if b.startswith("recorder:")]
    return dict(recorder["recorder"]["exclude"])


def contact_entity_ids() -> Iterator[str]:
    for entity_id in ALICE_IDS | HILL_IDS:
        yield entity_id
        yield f"{entity_id}_2"  # numbered by Home Assistant (another gateway, the same name)


def test_the_recorder_exclusions_keep_only_the_texts_out() -> None:
    globs = guide_recorder()["entity_globs"]
    excluded = {e for e in contact_entity_ids() if any(fnmatch.fnmatch(e, g) for g in globs)}
    assert excluded == {"event.mesh_alice_message", "event.mesh_alice_message_2"}
    gateway = {f"event.{PREFIX}_message", f"sensor.{PREFIX}_contacts", f"event.{PREFIX}_contact"}
    assert not {e for e in gateway if any(fnmatch.fnmatch(e, g) for g in globs)}
    assert encode({"text": "ä"}) == '{"text":"ä"}'.encode()
