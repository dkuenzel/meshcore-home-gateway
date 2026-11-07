"""The gateway's MQTT documents against the real Home Assistant MQTT integration.

Home Assistant runs in-process with a mocked broker connection (pytest-homeassistant-custom-
component, which pins the release; home-assistant/requirements.in). The tests check what
docs/home-assistant.md promises: the entity IDs, the device page, the sensors and attributes, the
removal of components, the documented automations, and that Home Assistant logs no warning.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from freezegun.api import FrozenDateTimeFactory
from ha_plugin import async_fire_mqtt_message
from homeassistant import const
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from homeassistant.util import slugify

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.core.commands import Command
from meshcore_home_gateway.core.contacts import Contact
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    DeliveryState,
    InboundMessage,
    MessageKind,
)
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.manage_entities import manage_payload, manage_topic
from meshcore_home_gateway.mqtt.payloads import (
    command_payload,
    delivery_payload,
    received_payload,
    rx_payload,
)
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import RadioInfo
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.protocol.links import contact_link
from meshcore_home_gateway.views import HistoryEntry, history_document, settings_document

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "home-assistant.md"
SENDER = "a1" * 32
RADIO_KEY = "c3" * 32
TARGETS = (
    f'[targets.alerts]\nchannel = 1\n[targets.operator]\ncontact = "{SENDER}"\n'
    "[targets.my-target]\nchannel = 2\n"
)
PREFIX = "meshcore_home_gateway_radio1"
# Since 2026.11 Home Assistant clears a device's discovery topic once for each entity it removes
# with the device, and no longer when a single component goes while the device stays
# (components/mqtt/entity.py:816-875, async_remove_entity_discovery_payload); a cleared topic now
# removes the device's disabled components too (components/mqtt/discovery.py:290-326). Before,
# it cleared the topic once, and itself whenever it removed a disabled component (#115).
CLEARS_PER_ENTITY = (const.MAJOR_VERSION, const.MINOR_VERSION) >= (2026, 11)


def make_config(targets: str = TARGETS, node_id: str = "radio1") -> Config:
    """Every component: targets (one alias with '-'), command words and the packet sensors."""
    return parse_config(f"""
[radio]
transport = "tcp"
tcp_unauthenticated = true
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "{node_id}"
insecure = true
[diagnostics]
publish_rx_log = true
[commands]
senders = ["{SENDER}"]
words = ["status", "arm"]
{targets}
""")


CONFIG = make_config()
TOPICS = Topics(CONFIG.mqtt.base_topic, CONFIG.mqtt.node_id)
RADIO = RadioInfo(
    SelfInfo(bytes.fromhex(RADIO_KEY), "Gateway", 22, 22, 869_618, 62_500, 8, 8, True),
    DeviceInfo(9, 350, 40, "1 Oct 2026", "Heltec V3", "v1.18.0", 0),
)


def state_document(*, known: bool) -> dict[str, Any]:
    """A state document as Bridge.state_payload writes it, before and after the radio connects."""
    state: dict[str, Any] = {
        "radio": "disconnected",
        "battery_v": None,
        "airtime_1h_s": 0.0,
        "airtime_available_s": 36.0,
        "airtime_budget_s_per_hour": 36.0,
        "queue": 0,
        "radio_name": None,
        "public_key": None,
        "model": None,
        "firmware": None,
        "frequency_mhz": None,
        "bandwidth_khz": None,
        "spreading_factor": None,
        "coding_rate": None,
        "tx_power_dbm": None,
        "contact_link": None,
        "version": __version__,
    }
    if known:
        state.update(
            radio="connected",
            battery_v=4.012,
            airtime_1h_s=3.2,
            airtime_available_s=9.4,
            queue=2,
            radio_name="Gateway",
            public_key=RADIO_KEY,
            model="Heltec V3",
            firmware="v1.18.0",
            frequency_mhz=869.618,
            bandwidth_khz=62.5,
            spreading_factor=8,
            coding_rate=8,
            tx_power_dbm=22,
            contact_link=contact_link("Gateway", RADIO_KEY),
        )
    return state


async def fire(hass: HomeAssistant, topic: str, payload: Any, *, retain: bool = False) -> None:
    """Deliver a message as the broker would: `retain` only for the first message after the
    subscription (MQTT 3.1.1 clears the flag for live messages; Home Assistant skips a second
    retained message on a subscription)."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    async_fire_mqtt_message(hass, topic, text, retain=retain)
    await hass.async_block_till_done()
    await hass.async_block_till_done()


async def announce(hass: HomeAssistant, document: dict[str, Any]) -> None:
    await fire(hass, discovery_topic(CONFIG), document)


def entity_ids(hass: HomeAssistant) -> set[str]:
    (entry,) = hass.config_entries.async_entries("mqtt")
    entries = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {e.entity_id for e in entries}


def device(hass: HomeAssistant) -> dr.DeviceEntry:
    (entry,) = hass.config_entries.async_entries("mqtt")
    (only,) = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return only


def problems(caplog: pytest.LogCaptureFixture) -> list[str]:
    """Warnings and errors of Home Assistant itself (the test plugin's frame warning aside)."""
    return [
        f"{r.levelname} {r.name}: {r.getMessage()}"
        for r in caplog.records
        if r.name.startswith("homeassistant")
        and r.name != "homeassistant.helpers.frame"
        and r.levelno >= logging.WARNING
    ]


def snapshot(hass: HomeAssistant, ids: set[str]) -> dict[str, Any]:
    return {
        e: (s.state, s.last_changed, dict(s.attributes))
        for e in sorted(ids)
        if (s := hass.states.get(e)) is not None
    }


# --- discovery ---------------------------------------------------------------------------------


async def test_discovery_creates_the_documented_entities(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    document = discovery_payload(CONFIG, TOPICS)
    await announce(hass, document)
    controls = manage_payload(CONFIG, TOPICS, [])  # the controls that add contacts
    await fire(hass, manage_topic(CONFIG), controls)
    ids = entity_ids(hass)
    assert len(ids) == len(document["cmps"]) + len(controls["cmps"])
    documented = {
        name
        for name in re.findall(rf"\b[a-z_]+\.{PREFIX}_\w+", GUIDE.read_text(encoding="utf-8"))
        if not name.endswith("_")  # `..._send_<alias>` and `..._rx_snr` written as a pattern
    }
    assert documented <= ids
    assert f"notify.{PREFIX}_send_my_target" in ids  # '-' in an alias becomes '_'
    registry = er.async_get(hass)
    for sensor in ("rx_rssi", "rx_snr"):
        entry = registry.async_get(f"sensor.{PREFIX}_{sensor}")
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert problems(caplog) == []


async def test_a_hyphen_in_the_node_id_becomes_an_underscore_only_in_the_entity_ids(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Discovery topic, device and unique IDs keep the node ID as it is, so node IDs that differ
    only in '-' and '_' are two devices; their entity IDs would be equal, so Home Assistant
    numbers the second set."""
    hyphen, underscore = make_config(node_id="radio-1"), make_config(node_id="radio_1")
    document = discovery_payload(hyphen, Topics(hyphen.mqtt.base_topic, "radio-1"))
    await fire(hass, discovery_topic(hyphen), document)
    expected = {
        f"{c['p']}.meshcore_home_gateway_radio_1_{key.replace('-', '_')}"
        for key, c in document["cmps"].items()
    }
    assert entity_ids(hass) == expected

    other = discovery_payload(underscore, Topics(underscore.mqtt.base_topic, "radio_1"))
    await fire(hass, discovery_topic(underscore), other)
    (entry,) = hass.config_entries.async_entries("mqtt")
    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 2
    assert entity_ids(hass) == expected | {f"{e}_2" for e in expected}
    assert problems(caplog) == []


async def test_a_null_device_field_would_reject_the_whole_document(
    hass: HomeAssistant, mqtt: Any
) -> None:
    """Why the gateway leaves unknown radio fields out instead of sending null."""
    document = discovery_payload(CONFIG, TOPICS)
    document["dev"]["mdl"] = None
    await announce(hass, document)
    assert entity_ids(hass) == set()


async def test_the_device_page_shows_the_radio_once_it_is_known(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    assert (device(hass).model, device(hass).sw_version) == (None, None)
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)
    before = snapshot(hass, entity_ids(hass))

    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    found = device(hass)
    assert (found.manufacturer, found.model) == ("MeshCore", "Heltec V3")
    assert found.sw_version == "v1.18.0"
    assert found.serial_number == RADIO_KEY
    assert snapshot(hass, entity_ids(hass)) == before  # the re-announcement changes no state

    await announce(hass, discovery_payload(CONFIG, TOPICS))  # a restart before the radio is known
    assert device(hass).model == "Heltec V3"  # Home Assistant keeps the stored identity
    assert problems(caplog) == []


async def test_a_component_of_an_earlier_configuration_is_removed(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    ids = entity_ids(hass)
    mqtt.async_publish.reset_mock()
    fewer = make_config(TARGETS.replace(f'[targets.operator]\ncontact = "{SENDER}"\n', ""))
    removed = {"send_operator": "notify", "send_never_announced": "notify"}
    await announce(hass, discovery_payload(fewer, TOPICS, RADIO, removed))
    assert ids - entity_ids(hass) == {f"notify.{PREFIX}_send_operator"}
    await announce(hass, discovery_payload(fewer, TOPICS, RADIO, removed))  # again: no effect
    assert ids - entity_ids(hass) == {f"notify.{PREFIX}_send_operator"}
    published = [call.args[0] for call in mqtt.async_publish.call_args_list]
    assert discovery_topic(CONFIG) not in published  # Home Assistant clears nothing itself
    assert problems(caplog) == []


@pytest.mark.parametrize(
    ("key", "platform"),
    [("send_old", "notify"), ("command", "event"), ("rx_old", "sensor"), ("old", "binary_sensor")],
)
async def test_a_component_is_removed_also_after_a_restart_of_home_assistant(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, key: str, platform: str
) -> None:
    """The entity is left from an earlier run of Home Assistant, which therefore ignores a bare
    {"p": ...}; the placeholder sent first binds it, so the removal takes effect."""
    registry = er.async_get(hass)
    (entry,) = hass.config_entries.async_entries("mqtt")
    unique_id = f"{PREFIX}_{key}"
    registry.async_get_or_create(
        platform, "mqtt", unique_id, config_entry=entry, suggested_object_id=unique_id
    )
    fewer = parse_config(  # this run's configuration has no such component
        f"""
[radio]
transport = "tcp"
tcp_unauthenticated = true
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
{TARGETS}
"""
    )
    removed = {key: platform}
    await announce(hass, discovery_payload(fewer, TOPICS, None, removed))
    assert registry.async_get_entity_id(platform, "mqtt", unique_id) is not None

    for _ in range(2):  # as the bridge sends it, and again: nothing is left either way
        await announce(hass, discovery_payload(fewer, TOPICS, None, removed, bind=True))
        await announce(hass, discovery_payload(fewer, TOPICS, None, removed))
        assert registry.async_get_entity_id(platform, "mqtt", unique_id) is None
    assert f"notify.{PREFIX}_send_alerts" in entity_ids(hass)
    assert problems(caplog) == []


async def test_a_disabled_component_goes_without_losing_the_device_for_long(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The packet sensors are disabled by default. Before 2026.11, removing a disabled entity
    makes Home Assistant clear the device's topic itself, and the broker hands that back to it:
    all the device's entities go. The gateway sees it and announces again, which restores them;
    the removal, sent again, then finds nothing disabled and Home Assistant stays quiet. Since
    2026.11 the device keeps its other entities throughout (CLEARS_PER_ENTITY)."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    fewer = parse_config(f"""
[radio]
transport = "tcp"
tcp_unauthenticated = true
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
insecure = true
[commands]
senders = ["{SENDER}"]
words = ["status", "arm"]
{TARGETS}
""")
    removed = {"rx_rssi": "sensor", "rx_snr": "sensor"}
    expected = set(discovery_payload(fewer, TOPICS)["cmps"])

    async def announce_removal() -> list[tuple[Any, ...]]:
        mqtt.async_publish.reset_mock()
        await announce(hass, discovery_payload(fewer, TOPICS, None, removed, bind=True))
        await announce(hass, discovery_payload(fewer, TOPICS, None, removed))
        published = [c.args[:4] for c in mqtt.async_publish.call_args_list]
        for topic, payload, _qos, _retain in published:  # what the broker hands back
            await fire(hass, topic, "" if payload is None else payload)
        return published

    def kept() -> set[str]:
        return {e.rsplit("_radio1_", 1)[1] for e in entity_ids(hass)}

    cleared = await announce_removal()
    if CLEARS_PER_ENTITY:
        assert cleared == []
        assert kept() == {k.replace("-", "_") for k in expected}
    else:
        assert {(t, p, r) for t, p, _q, r in cleared} == {(discovery_topic(CONFIG), None, True)}
        assert entity_ids(hass) == set()  # until the gateway announces again
    assert await announce_removal() == []  # the gateway's answer to the cleared topic
    assert kept() == {k.replace("-", "_") for k in expected}
    # before 2026.11: "No device components to cleanup" for the copies of the cleared topic
    assert {p.split(":")[0] for p in problems(caplog)} == (
        set() if CLEARS_PER_ENTITY else {"WARNING homeassistant.components.mqtt.discovery"}
    )


# --- state -------------------------------------------------------------------------------------


async def test_the_state_document_fills_sensors_and_attributes(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.state, state_document(known=False), retain=True)
    radio = hass.states.get(f"binary_sensor.{PREFIX}_radio")
    assert radio is not None
    assert (radio.state, radio.attributes["radio_name"]) == ("off", None)
    assert hass.states.get(f"sensor.{PREFIX}_battery").state == "unknown"

    await fire(hass, TOPICS.state, state_document(known=True))
    radio = hass.states.get(f"binary_sensor.{PREFIX}_radio")
    assert radio.state == "on"
    assert radio.attributes["frequency_mhz"] == 869.618
    assert radio.attributes["public_key"] == RADIO_KEY
    assert radio.attributes["bridge_version"] == __version__
    available = hass.states.get(f"sensor.{PREFIX}_airtime_available")
    assert (available.state, available.attributes["budget_s_per_hour"]) == ("9.4", 36.0)
    assert hass.states.get(f"sensor.{PREFIX}_queue").state == "2"
    assert hass.states.get(f"sensor.{PREFIX}_battery").state == "4.012"

    await fire(hass, TOPICS.availability, "offline")  # the last will, or a clean stop
    assert {hass.states.get(e).state for e in snapshot(hass, entity_ids(hass))} == {"unavailable"}
    assert problems(caplog) == []


async def test_a_degraded_gateway_is_a_problem_with_its_health_as_attributes(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """ARC-012: the problem sensor follows `health.degraded`, its attributes are the section; a
    state document of an earlier version, without one, leaves it unknown without a warning."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)
    sensor = f"binary_sensor.{PREFIX}_degraded"
    assert hass.states.get(sensor).state == "unknown"
    health = {
        "degraded": True,
        "history": "broken",
        "state_file": "ok",
        "exclusions": "ok",
        "errors_1h": 2,
        "loop_lag_max_s": 0.012,
        "events_dropped_1h": 0,
        "disk_free_mb": 5120,
    }
    await fire(hass, TOPICS.state, state_document(known=True) | {"health": health})
    degraded = hass.states.get(sensor)
    assert degraded.state == "on"
    assert {key: degraded.attributes[key] for key in health if key != "degraded"} == {
        key: value for key, value in health.items() if key != "degraded"
    }
    well = health | {"degraded": False, "history": "ok", "errors_1h": 0}
    await fire(hass, TOPICS.state, state_document(known=True) | {"health": well})
    assert hass.states.get(sensor).state == "off"
    assert problems(caplog) == []


async def test_packet_sensors_show_the_last_packet_once_enabled(
    hass: HomeAssistant, mqtt: Any
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    registry = er.async_get(hass)
    for sensor in ("rx_rssi", "rx_snr"):
        registry.async_update_entity(f"sensor.{PREFIX}_{sensor}", disabled_by=None)
    await hass.config_entries.async_reload(hass.config_entries.async_entries("mqtt")[0].entry_id)
    await hass.async_block_till_done()
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.rx, rx_payload(5.25, -97, b"\x01\x02", 1_760_000_000.0))
    assert hass.states.get(f"sensor.{PREFIX}_rx_rssi").state == "-97"
    assert hass.states.get(f"sensor.{PREFIX}_rx_snr").state == "5.25"


async def test_the_radio_profile_shows_its_status_and_settings(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """ADR 0020: the settings document as an enum sensor with every setting as attributes, and
    an event per setting written or found changed."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    report = ProfileReport(
        "auto",
        14,
        True,
        False,
        live={"manual_add": True, "name": "Kitchen radio"},
        desired={"manual_add": True, "name": "Gateway"},
        drift={"name": "Kitchen radio"},
    )
    await fire(hass, TOPICS.settings, settings_document(report, 1_800_000_000.0), retain=True)
    sensor = hass.states.get(f"sensor.{PREFIX}_profile")
    assert sensor.state == "drift"
    assert sensor.attributes["settings"]["name"] == {
        "live": "Kitchen radio",
        "desired": "Gateway",
        "state": "drift",
    }
    assert sensor.attributes["options"] == ["ok", "written", "drift", "differs", "problem"]
    await fire(
        hass,
        TOPICS.setting_event,
        {
            "event_type": "written",
            "setting": "manual_add",
            "reason": "drift",
            "live": False,
            "desired": True,
        },
    )
    event = hass.states.get(f"event.{PREFIX}_setting")
    assert (event.attributes["event_type"], event.attributes["setting"]) == (
        "written",
        "manual_add",
    )
    assert problems(caplog) == []


# --- the message history -----------------------------------------------------------------------


async def test_the_message_history_shows_the_newest_message_and_outlives_the_gateway(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The `history` document as a timestamp sensor: unknown while it holds no message, with its
    keys readable; then the newest message's time, the messages as attributes with every key.
    Its own availability keeps it while the gateway is offline: the messages stay readable."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    sensor = f"sensor.{PREFIX}_message_history"
    await fire(hass, TOPICS.history, history_document([]), retain=True)
    empty = hass.states.get(sensor)
    assert empty.state == "unknown"
    assert [empty.attributes[k] for k in ("total", "truncated", "messages")] == [0, False, []]

    entries = [
        HistoryEntry(
            1_800_000_000.0,
            "in",
            "channel",
            "On my way",
            channel_index=1,
            name="Carol",
            hops=1,
            snr_db=6.25,
            target="alerts",
        ),
        HistoryEntry(
            1_800_000_060.0,
            "out",
            "contact",
            "Back soon",
            contact_key=SENDER,
            name="Alice",
            state="confirmed",
            target="operator",
            id="1a2b3c4d",
            queued_at=1_800_000_058.0,
            suspect=False,
        ),
    ]
    document = history_document(entries)
    await fire(hass, TOPICS.history, document)
    history = hass.states.get(sensor)
    assert (history.state, history.attributes["device_class"]) == (
        "2027-01-15T08:01:00+00:00",
        "timestamp",
    )
    assert history.attributes["messages"] == document["messages"]
    assert history.attributes["messages"][0]["queued_at"] == "2027-01-15T08:00:58+00:00"

    await fire(hass, TOPICS.availability, "offline")  # the last will, or a clean stop
    assert hass.states.get(f"sensor.{PREFIX}_queue").state == "unavailable"
    kept = hass.states.get(sensor)
    assert (kept.state, kept.attributes["messages"]) == (history.state, document["messages"])
    assert problems(caplog) == []


async def test_the_message_history_comes_back_after_a_restart_of_home_assistant(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Home Assistant restarted while the gateway is offline: the broker's retained history is
    all it gets, and the sensor shows it; the gateway's availability says offline."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "offline", retain=True)
    entry = HistoryEntry(1_800_000_000.0, "in", "contact", "hi", SENDER, name="Alice")
    await fire(hass, TOPICS.history, history_document([entry]), retain=True)
    history = hass.states.get(f"sensor.{PREFIX}_message_history")
    assert (history.state, history.attributes["total"]) == ("2027-01-15T08:00:00+00:00", 1)
    assert hass.states.get(f"sensor.{PREFIX}_queue").state == "unavailable"
    assert problems(caplog) == []


# --- events and automations --------------------------------------------------------------------


def guide_automations() -> list[dict[str, Any]]:
    """The automations of docs/home-assistant.md, exactly as a user would paste them (its
    script, which has no triggers, aside: tests/test_dashboard.py)."""
    blocks = re.findall(r"```yaml\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    loaded = [yaml.safe_load(block) for block in blocks if block.startswith("alias:")]
    return [automation for automation in loaded if "triggers" in automation]


async def test_the_documented_automations_work(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    automations = guide_automations()
    # the contacts' one: tests/test_manage_contacts.py; the one calling the guide's script to
    # send and wait, here only set up: test_the_guides_script_waits_for_its_own_final_event
    assert len(automations) == 10
    telemetry = {"Handheld battery low", "Mesh handheld not answering", "Alice left home"}
    assert telemetry <= {a["alias"] for a in automations}  # tests/test_telemetry.py, with Alice
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(
        hass, "automation", {"automation": [a for a in automations if a["alias"] not in telemetry]}
    )
    await hass.async_block_till_done()

    mqtt.async_publish.reset_mock()  # the first command of a new entity must trigger, too
    command = Command("status", "now", SENDER, 1_760_000_000)
    await fire(hass, TOPICS.command, command_payload(command, "Alice", 1_760_000_001.5))
    sends = [c.args for c in mqtt.async_publish.call_args_list]
    assert len(sends) == 1
    assert (sends[0][0], sends[0][2], sends[0][3]) == ("meshcore/radio1/send/operator", 1, False)

    for message_id in ("a", "b"):  # two equal events in a row: both must trigger
        failed = DeliveryEvent(
            id=message_id,
            target="alerts",
            state=DeliveryState.FAILED,
            text="x",
            attempt=1,
            reason="no_acknowledgement",
        )
        await fire(hass, TOPICS.delivery, delivery_payload(failed))
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    messages = [n["message"] for n in notifications.values()]
    assert messages.count("alerts: failed (no_acknowledgement)") == 2
    automation = hass.states.get("automation.mesh_delivery_failed")
    assert automation.attributes["last_triggered"] is not None
    assert problems(caplog) == []


@pytest.mark.parametrize(
    "case",
    [("availability", "The gateway is offline"), ("state", "The gateway has lost its radio")],
    ids=["gateway", "radio"],
)
async def test_the_outage_automation_tells_after_five_minutes(
    hass: HomeAssistant,
    mqtt: Any,
    freezer: FrozenDateTimeFactory,
    caplog: pytest.LogCaptureFixture,
    case: tuple[str, str],
) -> None:
    """OPS-003: the gateway offline (its last will) or its radio away for five minutes."""
    topic, message = case
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)
    (automation,) = [a for a in guide_automations() if a["alias"] == "Mesh gateway away"]
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()
    if topic == "availability":
        await fire(hass, TOPICS.availability, "offline")
    else:
        await fire(hass, TOPICS.state, state_document(known=False))

    async def elapse(seconds: float) -> None:
        freezer.tick(timedelta(seconds=seconds))
        await asyncio.sleep(0)
        await hass.async_block_till_done()

    def messages() -> list[str]:
        notes = persistent_notification._async_get_or_create_notifications(hass)
        return [n["message"] for n in notes.values()]

    await elapse(4 * 60)
    assert messages() == []
    await elapse(60 + 1)
    assert messages() == [message]
    assert problems(caplog) == []


async def test_events_carry_every_payload_key_as_attributes(hass: HomeAssistant, mqtt: Any) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    payload = command_payload(Command("arm", "away now", SENDER, 1_760_000_000), "Alice", 1.0)
    await fire(hass, TOPICS.command, payload)
    event = hass.states.get(f"event.{PREFIX}_command")
    assert event.attributes["event_type"] == "arm"
    for key, value in payload.items():
        if key != "event_type":
            assert event.attributes[key] == value, key


async def test_retained_events_are_ignored(hass: HomeAssistant, mqtt: Any) -> None:
    """Why the gateway never retains events."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    payload = command_payload(Command("arm", "", SENDER, 1_760_000_000), "Alice", 1.0)
    await fire(hass, TOPICS.command, payload, retain=True)
    assert hass.states.get(f"event.{PREFIX}_command").state == "unknown"


# --- first steps ------------------------------------------------------------------------------


def guide_block(start: str) -> dict[str, Any]:
    """The YAML block of docs/home-assistant.md that starts with `start`."""
    blocks = re.findall(r"```yaml\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    (block,) = [b for b in blocks if b.startswith(start)]
    loaded: dict[str, Any] = yaml.safe_load(block)
    return loaded


async def test_the_guides_script_waits_for_its_own_final_event(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """EXT-018: the script sends through the target's notify entity, takes the `id` of the first
    delivery event for that target and returns its request's final event: the events of other
    requests do not end it, and a suspect acknowledgement is no confirmation."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    script = guide_block("alias: Send over the mesh and wait")
    name = slugify(script["alias"])
    assert await async_setup_component(hass, "script", {"script": {name: script}})
    await hass.async_block_till_done()

    async def until(check: Callable[[], bool]) -> None:
        for _ in range(500):  # the script waits in a task of its own: no async_block_till_done
            if check():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("timed out")

    async def send(*events: DeliveryEvent) -> Any:
        mqtt.async_publish.reset_mock()
        call = asyncio.ensure_future(
            hass.services.async_call(
                "script",
                name,
                {"recipient": "operator", "message": "1.10"},  # a text, not a number
                blocking=True,
                return_response=True,
            )
        )
        await until(lambda: bool(mqtt.async_publish.call_args_list))
        (sent,) = [c.args[:3] for c in mqtt.async_publish.call_args_list]
        assert sent == (TOPICS.send("operator"), "1.10", 1)
        for event in events:
            async_fire_mqtt_message(hass, TOPICS.delivery, json.dumps(delivery_payload(event)))
            await asyncio.sleep(0.05)
        return await asyncio.wait_for(call, 5)

    def event(ident: str, state: DeliveryState, target: str = "operator", **more: Any) -> Any:
        return DeliveryEvent(id=ident, target=target, state=state, text="1.10", **more)

    confirmed = await send(
        event("a", DeliveryState.QUEUED),
        event("b", DeliveryState.CONFIRMED, target="alerts", suspect=False),  # another request
        event("a", DeliveryState.SENT, attempt=1),
        event("a", DeliveryState.CONFIRMED, attempt=1, rtt_ms=2300, suspect=False),
    )
    assert confirmed == {"confirmed": True, "event_type": "confirmed", "reason": None}
    suspect = await send(
        event("c", DeliveryState.QUEUED),
        event("c", DeliveryState.CONFIRMED, attempt=1, rtt_ms=10, suspect=True),
    )
    assert suspect == {"confirmed": False, "event_type": "confirmed", "reason": None}
    duplicate = await send(event("d", DeliveryState.DUPLICATE))
    assert duplicate == {"confirmed": False, "event_type": "duplicate", "reason": None}
    assert problems(caplog) == []


async def test_the_markdown_card_of_the_first_steps_shows_the_last_message(
    hass: HomeAssistant, mqtt: Any
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    card = Template(guide_block("type: markdown\ntitle: Last mesh message")["content"], hass)
    assert card.async_render(parse_result=False).strip() == "none yet"
    message = InboundMessage(
        MessageKind.CONTACT, "Hello", 1_760_000_000, 1_760_000_001.0, sender_prefix=SENDER[:12]
    )
    await fire(
        hass, TOPICS.received, received_payload(message, Contact(SENDER, "Alice"), "operator")
    )
    assert card.async_render(parse_result=False).strip() == "<div><b>Alice</b>: Hello</div>"


HOSTILE = (
    "![](https://example.org/p.png) [x](https://example.org) <img src=x>\n\n**b** www.example.org"
)


@pytest.mark.parametrize(
    ("kind", "text", "sender"),
    [
        (MessageKind.CONTACT, HOSTILE, Contact(SENDER, "<i>Mallory</i> [y](https://example.org)")),
        (MessageKind.CHANNEL, f"<b>Mallory</b>: {HOSTILE}", None),  # the claimed name, too
        (MessageKind.CONTACT, HOSTILE, Contact(SENDER, "x\n\n![](https://example.org/p.png)")),
        (MessageKind.CONTACT, HOSTILE, Contact(SENDER, "e\r\r![](//e.x/q.png)")),  # CR alone
        (MessageKind.CONTACT, HOSTILE.replace("\n", "\r"), Contact(SENDER, "Alice")),
    ],
)
async def test_the_markdown_card_keeps_a_hostile_text_inert(
    hass: HomeAssistant, mqtt: Any, kind: MessageKind, text: str, sender: Contact | None
) -> None:
    """Anyone in radio range writes the text and a channel sender's name, and a contact chooses
    its own, line breaks included. In a Markdown paragraph even an escaped text becomes an image
    or a link; in an HTML block on one line, the escaped text stays text (CommonMark: a blank line
    ends the block)."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    card = Template(guide_block("type: markdown\ntitle: Last mesh message")["content"], hass)
    message = InboundMessage(
        kind,
        text,
        1_760_000_000,
        1_760_000_001.0,
        sender_prefix=SENDER[:12],
        channel_index=1 if kind is MessageKind.CHANNEL else None,
    )
    await fire(hass, TOPICS.received, received_payload(message, sender, "alerts"))
    output = card.async_render(parse_result=False)
    assert not re.search(r"\n[ \t]*\n.*!\[", output, re.DOTALL)  # no image after a blank line
    assert "\r" not in output  # CR alone also ends a line, and two a block
    lines = [line for line in output.splitlines() if line.strip()]
    assert len(lines) == 1
    (line,) = lines
    assert line.startswith("<div>")
    assert line.endswith("</div>")
    inner = line.removeprefix("<div>").removesuffix("</div>")
    assert "<" not in inner.replace("<b>", "", 1).replace("</b>", "", 1)  # only the card's own tag
    assert "&lt;img src=x&gt;" in inner


async def test_the_actions_of_the_first_steps_send_a_message(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The action for the developer tools, and the one of the dashboard button."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    button = guide_block("type: button")["tap_action"]
    assert button["action"] == "perform-action"
    for action in (guide_block("action: notify.send_message"), button):
        name = action.get("perform_action") or action["action"]  # the button names it apart
        domain, service = name.split(".")
        mqtt.async_publish.reset_mock()
        await hass.services.async_call(
            domain, service, action["data"], blocking=True, target=action["target"]
        )
        await hass.async_block_till_done()
        (send,) = [c.args for c in mqtt.async_publish.call_args_list]
        assert (send[0], send[1], send[2]) == (
            "meshcore/radio1/send/alerts",
            "Test from Home Assistant",
            1,
        )
    assert problems(caplog) == []


# --- the guide names the tested release --------------------------------------------------------


def test_the_guide_names_the_tested_release() -> None:
    """The requirements line names every release series the contract tests run with (`make
    ha-test HA_SERIES=…`), this one among them: "tested with 2026.10 and 2026.11" (#115)."""
    tested = f"{const.MAJOR_VERSION}.{const.MINOR_VERSION}"
    (line,) = [
        line
        for line in GUIDE.read_text(encoding="utf-8").splitlines()
        if line.startswith("* Home Assistant ") and "tested with" in line
    ]
    named = re.findall(r"\d{4}\.\d+", line.split("tested with", 1)[1])
    assert tested in named, line
