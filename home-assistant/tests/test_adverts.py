"""Positions in adverts against the real Home Assistant MQTT integration (ADR 0018, design 6c;
docs/home-assistant.md).

The gateway's own code builds the messages: the contact devices (ContactDevices), the documents,
the index and the events (views), the gateway's discovery, and the check as the core keeps it
(Mesh.advert_checked). The tests check what the guide promises: *Position in adverts* on every
companion's device, also with a document of an earlier version, the gateway's sensor that counts
only the household's companions, the event type on *Contact*, the guide's example exactly as
written, and a tracker that leaves the map when a companion stops sending its position.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from freezegun.api import FrozenDateTimeFactory
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from test_contract import CONFIG, PREFIX, TOPICS, fire, problems

from meshcore_home_gateway.core.mesh import AdvertCheck, Mesh, Node
from meshcore_home_gateway.mqtt.contact_devices import contact_discovery_topic, contact_payload
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.state import JsonStateFile
from meshcore_home_gateway.views import contact_event, contacts_document
from test_contact_devices import (
    ALICE,
    ALICE_NODE,
    COMPANION,
    HILL,
    HILL_IDS,
    HILL_NODE,
    NOW,
    REPEATER,
    Broker,
    contact_entities,
    gateway_devices,
    mesh_of,
    online,
    with_gateway,
)
from test_telemetry import (
    BATTERY_4_12,
    BOB,
    MCU,
    TRACKER,
    announce_alice,
    answer,
    document,
    elapse,
    gps,
    guide_automation,
    home,
    notifications,
    states,
    telemetry_state_document,
    with_person,
)

CAROL = "d4" * 32
SENSOR = "binary_sensor.mesh_alice_advert_position"
GATEWAY_SENSOR = f"binary_sensor.{PREFIX}_advert_position"
TA = int(NOW) - 600  # the contacts' clock at an advert
NEW_KEYS = ("advert_carries_position", "advert_checked_at")  # what 1.1.0a2 did not write


def advert(mesh: Mesh, at: int, carries: bool, node: Node) -> None:
    """A table read shows the contact's advert at `at`, which the gateway checked, as the
    observer does (design 6c §3.2)."""
    key = node.public_key
    others = [t.node for k, t in mesh.contacts.items() if k != key]
    mesh.contacts_read([replace(node, last_advert=at), *others], NOW)
    mesh.advert_checked(key, AdvertCheck(at, carries), NOW)


# --- the contact's device ------------------------------------------------------------------------


async def test_every_companion_shows_whether_its_adverts_carry_its_position(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """The sensor joins a device of 1.1.0a2 and leaves its tracker alone. On, off or unknown:
    for a document of 1.1.0a2, a contact that is no companion any more and the message that
    clears the document; a repeater gets none (design 6c D10)."""
    await with_gateway(hass)
    broker, mesh = Broker(), mesh_of(ALICE_NODE, HILL_NODE)
    answer(mesh, NOW, BATTERY_4_12, gps(home(hass)), MCU)
    earlier = contact_payload(CONFIG, TOPICS, mesh.contacts[ALICE], "alice")
    del earlier["cmps"]["advert_position"]  # Alice's device as 1.1.0a2 announced it
    await fire(hass, contact_discovery_topic(CONFIG, ALICE), earlier)
    await online(hass)
    stored = {k: v for k, v in document(mesh).items() if k not in NEW_KEYS}
    await fire(hass, TOPICS.contact(ALICE), stored, retain=True)
    tracker = hass.states.get(TRACKER)
    assert tracker.state == "home"

    gateway_devices(broker, JsonStateFile(tmp_path / "state.json")).update(mesh)  # 1.1.0a3
    await broker.deliver(hass)
    await online(hass)
    await fire(hass, TOPICS.contact(ALICE), stored, retain=True)  # to the new subscription
    assert hass.states.get(SENSOR).state == "unknown"
    assert hass.states.get(TRACKER).last_updated == tracker.last_updated
    registry = er.async_get(hass)
    entry = registry.async_get(SENSOR)
    assert (entry.entity_category, entry.device_id) == (None, registry.async_get(TRACKER).device_id)
    sensor = hass.states.get(SENSOR)
    assert (sensor.attributes["device_class"], sensor.attributes["friendly_name"]) == (
        "problem", "Alice Position in adverts",
    )  # fmt: skip
    assert "latitude" not in sensor.attributes
    assert set(contact_entities(hass, HILL)) == HILL_IDS  # a repeater shares by design

    seen = []
    for at, carries in ((TA, True), (TA + 60, False)):  # her advert carries it, the next not
        advert(mesh, at, carries, ALICE_NODE)
        await fire(hass, TOPICS.contact(ALICE), document(mesh))
        seen.append(hass.states.get(SENSOR).state)
    mesh_of(replace(ALICE_NODE, node_type=REPEATER), HILL_NODE, mesh=mesh)  # flashed as one
    assert document(mesh)["advert_carries_position"] is None
    await fire(hass, TOPICS.contact(ALICE), document(mesh))
    seen.append(hass.states.get(SENSOR).state)
    await fire(hass, TOPICS.contact(ALICE), "")
    seen.append(hass.states.get(SENSOR).state)
    assert seen == ["on", "off", "unknown", "unknown"]
    assert problems(caplog) == []


# --- the gateway's entities ----------------------------------------------------------------------


async def test_the_gateway_sensor_follows_the_households_companions(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """On for a companion whose checked advert carries its position and for one whose newest
    advert is unchecked while its record holds one (design 6c D5); an excluded one never counts
    (D7). An index of 1.1.0a2 reads `unknown`, without a warning."""
    await with_gateway(hass)
    bob = Node(BOB, "Bob", COMPANION, last_advert=TA, position=home(hass))  # never checked
    carol = Node(CAROL, "Carol", COMPANION)
    mesh = mesh_of(ALICE_NODE, bob, carol)
    advert(mesh, TA, True, ALICE_NODE)
    advert(mesh, TA, True, carol)
    index = contacts_document(mesh, excluded={CAROL})
    await fire(hass, TOPICS.contacts, index, retain=True)
    sensor = hass.states.get(GATEWAY_SENSOR)
    assert (sensor.state, sensor.attributes["contacts"], sensor.attributes["total"]) == (
        "on", [ALICE, BOB], 2,
    )  # fmt: skip

    advert(mesh, TA + 60, False, ALICE_NODE)
    advert(mesh, TA + 60, False, bob)  # his record keeps the position
    await fire(hass, TOPICS.contacts, contacts_document(mesh, excluded={CAROL}))
    sensor = hass.states.get(GATEWAY_SENSOR)
    assert (sensor.state, sensor.attributes["contacts"], sensor.attributes["total"]) == (
        "off", [], 0,
    )  # fmt: skip

    earlier = {k: v for k, v in index.items() if not k.startswith("advert_carries_position")}
    await fire(hass, TOPICS.contacts, earlier)  # as the gateway publishes it at a connect
    sensor = hass.states.get(GATEWAY_SENSOR)
    assert (sensor.state, sensor.attributes["contacts"], sensor.attributes["total"]) == (
        "unknown", [], 0,
    )  # fmt: skip
    assert problems(caplog) == []


async def test_position_in_advert_reaches_the_contact_event(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The discovery lists it, so Home Assistant takes the event (it drops an unlisted type
    with a warning); never a position in it."""
    await with_gateway(hass)
    entity = f"event.{PREFIX}_contact"
    listed = discovery_payload(CONFIG, TOPICS)["cmps"]["contact"]["event_types"]
    assert hass.states.get(entity).attributes["event_types"] == listed
    assert "position_in_advert" in listed
    event = contact_event("position_in_advert", ALICE, ALICE_NODE)
    await fire(hass, TOPICS.contact_event, event)
    state = hass.states.get(entity)
    assert {k: state.attributes[k] for k in event} == event
    assert not {"latitude", "longitude"} & set(state.attributes)
    assert problems(caplog) == []


# --- the examples of the guide -------------------------------------------------------------------


async def test_the_shares_position_example_notifies_once_per_event(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_gateway(hass)
    assert await async_setup_component(hass, "persistent_notification", {})
    automation = guide_automation("Mesh handheld shares its position")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    bob = Node(BOB, "**Bob**", COMPANION)  # a name from the mesh: never in the message
    for event in (
        contact_event("position_in_advert", ALICE, ALICE_NODE),
        contact_event("excluded", ALICE, ALICE_NODE),
        contact_event("position_in_advert", BOB, bob),
        contact_event("position_in_advert", ALICE, ALICE_NODE),  # included again, still sharing
    ):
        await fire(hass, TOPICS.contact_event, event)
    advice = (
        ': its adverts carry its position; turn off "Share Position in Advert" on it and press '
        "Advert near the gateway"
    )
    assert notifications(hass) == [f"{ALICE[:12]}{advice}", f"{BOB[:12]}{advice}",
                                   f"{ALICE[:12]}{advice}"]  # fmt: skip
    assert problems(caplog) == []


async def test_a_position_no_longer_shared_leaves_the_map(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path,
    freezer: FrozenDateTimeFactory,
) -> None:  # fmt: skip
    """Alice's only position came from her adverts. A checked advert without it takes her
    tracker off the map, although the radio's record keeps the position (design 6c D12): her
    person turns `unknown`, and the guide's *Alice left home* fires after its `for`."""
    await with_person(hass)
    await with_gateway(hass)
    shared = replace(ALICE_NODE, position=home(hass))
    broker, mesh = Broker(), mesh_of(shared)
    advert(mesh, TA, True, shared)
    devices = gateway_devices(broker, JsonStateFile(tmp_path / "state.json"))
    await announce_alice(hass, mesh, devices, broker, "waiting")
    tracker = hass.states.get(TRACKER)
    assert (tracker.state, tracker.attributes["position_source"]) == ("home", "advert")
    assert states(hass, "person.alice", SENSOR) == ["home", "on"]
    await fire(hass, TOPICS.state, telemetry_state_document("on", None), retain=True)
    automation = guide_automation("Alice left home")
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    await elapse(hass, freezer, 5 * 60)

    advert(mesh, TA + 60, False, shared)  # she switched it off and pressed Advert
    assert document(mesh, "waiting")["advert_position"] is True  # the record keeps it
    await fire(hass, TOPICS.contact(ALICE), document(mesh, "waiting"))
    assert states(hass, TRACKER, "person.alice", SENSOR) == ["unavailable", "unknown", "off"]
    await elapse(hass, freezer, 4 * 60)
    assert notifications(hass) == []
    await elapse(hass, freezer, 60 + 1)
    assert notifications(hass) == ["unknown"]
    assert problems(caplog) == []
