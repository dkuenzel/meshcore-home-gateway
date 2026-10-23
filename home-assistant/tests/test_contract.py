"""The gateway's MQTT documents against the real Home Assistant MQTT integration.

Home Assistant runs in-process with a mocked broker connection (pytest-homeassistant-custom-
component, which pins the release; home-assistant/requirements.in). The tests check what
docs/home-assistant.md promises: the entity IDs, the device page, the sensors and attributes, the
removal of components, the documented automations, and that Home Assistant logs no warning.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from homeassistant import const
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import async_fire_mqtt_message

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.core.commands import Command
from meshcore_home_gateway.core.model import DeliveryEvent, DeliveryState
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.payloads import command_payload, delivery_payload, rx_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codec import DeviceInfo, SelfInfo
from meshcore_home_gateway.radio.session import RadioInfo

GUIDE = Path(__file__).resolve().parents[2] / "docs" / "home-assistant.md"
SENDER = "a1" * 32
RADIO_KEY = "c3" * 32
TARGETS = (
    f'[targets.alerts]\nchannel = 1\n[targets.operator]\ncontact = "{SENDER}"\n'
    "[targets.my-target]\nchannel = 2\n"
)
PREFIX = "meshcore_home_gateway_radio1"


def make_config(targets: str = TARGETS) -> Config:
    """Every component: targets (one alias with '-'), command words and the packet sensors."""
    return parse_config(f"""
[radio]
transport = "tcp"
host = "192.0.2.10"
[mqtt]
host = "broker.example.org"
node_id = "radio1"
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
        "radio": "disconnected", "battery_v": None, "airtime_1h_s": 0.0,
        "airtime_available_s": 36.0, "airtime_budget_s_per_hour": 36.0, "queue": 0,
        "radio_name": None, "public_key": None, "model": None, "firmware": None,
        "frequency_mhz": None, "bandwidth_khz": None, "spreading_factor": None,
        "coding_rate": None, "tx_power_dbm": None, "version": __version__,
    }  # fmt: skip
    if known:
        state.update(
            radio="connected", battery_v=4.012, airtime_1h_s=3.2, airtime_available_s=9.4,
            queue=2, radio_name="Gateway", public_key=RADIO_KEY, model="Heltec V3",
            firmware="v1.18.0", frequency_mhz=869.618, bandwidth_khz=62.5, spreading_factor=8,
            coding_rate=8, tx_power_dbm=22,
        )  # fmt: skip
    return state


@pytest.fixture
async def mqtt(
    hass: HomeAssistant, mock_hass_config: None, mqtt_mock_entry: Callable[..., Any]
) -> Any:
    """The MQTT integration, set up; what Home Assistant publishes is recorded."""
    return await mqtt_mock_entry()


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
    ids = entity_ids(hass)
    assert len(ids) == len(document["cmps"])
    documented = {
        name for name in re.findall(rf"\b[a-z_]+\.{PREFIX}_\w+", GUIDE.read_text(encoding="utf-8"))
        if not name.endswith("_")  # `..._send_<alias>` and `..._rx_snr` written as a pattern
    }  # fmt: skip
    assert documented <= ids
    assert f"notify.{PREFIX}_send_my_target" in ids  # '-' in an alias becomes '_'
    registry = er.async_get(hass)
    for sensor in ("rx_rssi", "rx_snr"):
        entry = registry.async_get(f"sensor.{PREFIX}_{sensor}")
        assert entry is not None
        assert entry.disabled_by is er.RegistryEntryDisabler.INTEGRATION
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


# --- events and automations --------------------------------------------------------------------


def guide_automations() -> list[dict[str, Any]]:
    """The automations of docs/home-assistant.md, exactly as a user would paste them."""
    blocks = re.findall(r"```yaml\n(.*?)```", GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    return [yaml.safe_load(block) for block in blocks if block.startswith("alias:")]


async def test_the_documented_automations_work(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await fire(hass, TOPICS.availability, "online", retain=True)
    automations = guide_automations()
    assert len(automations) == 3
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "automation", {"automation": automations})
    await hass.async_block_till_done()

    mqtt.async_publish.reset_mock()  # the first command of a new entity must trigger, too
    command = Command("status", "now", SENDER, 1_760_000_000)
    await fire(hass, TOPICS.command, command_payload(command, "Alice", 1_760_000_001.5))
    sends = [c.args for c in mqtt.async_publish.call_args_list]
    assert len(sends) == 1
    assert (sends[0][0], sends[0][2], sends[0][3]) == ("meshcore/radio1/send/operator", 1, False)

    for message_id in ("a", "b"):  # two equal events in a row: both must trigger
        failed = DeliveryEvent(
            id=message_id, target="alerts", state=DeliveryState.FAILED, text="x", attempt=1,
            reason="no_acknowledgement",
        )  # fmt: skip
        await fire(hass, TOPICS.delivery, delivery_payload(failed))
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    messages = [n["message"] for n in notifications.values()]
    assert messages.count("alerts: failed (no_acknowledgement)") == 2
    automation = hass.states.get("automation.mesh_delivery_failed")
    assert automation.attributes["last_triggered"] is not None
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


# --- the guide names the tested release --------------------------------------------------------


def test_the_guide_names_the_tested_release() -> None:
    tested = f"{const.MAJOR_VERSION}.{const.MINOR_VERSION}"
    assert f"tested with {tested}" in GUIDE.read_text(encoding="utf-8")
