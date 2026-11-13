"""*Send advert nearby* and *Advert nearby* against the real Home Assistant MQTT integration
(ADR 0033, docs/home-assistant.md "Advert nearby").

The gateway's own code builds the documents. The tests check what the guide promises: the button on
the gateway's device and what a press publishes; its availability with the gateway and the radio,
and with `[homeassistant] radio_controls` and `controls` (unavailable, never removed, so the
gateway's device and its entities stay); that a user who is no administrator can press it; that
Home Assistant starting again presses nothing; each outcome on the event entity with every key
of the event as an attribute; the German names.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from ha_plugin import CLIENT_ID
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from test_contract import (
    CONFIG,
    PREFIX,
    RADIO,
    TOPICS,
    announce,
    device,
    entity_ids,
    fire,
    problems,
    state_document,
)

from meshcore_home_gateway import i18n
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.views import self_advert_event

BUTTON, EVENT = f"button.{PREFIX}_send_advert", f"event.{PREFIX}_advert"
REQUEST = "meshcore/radio1/advert/send"
NOW = 1_800_000_000.0  # 2027-01-15T08:00:00+00:00


def settings(**homeassistant: Any) -> Config:
    return replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, **homeassistant))


async def connected(hass: HomeAssistant, config: Config = CONFIG) -> None:
    """The gateway's documents as after a connect: announced, online, the radio connected."""
    await announce(hass, discovery_payload(config, TOPICS, RADIO))
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)


def advert_requests(mqtt: Any) -> list[tuple[Any, ...]]:
    return [c.args[:4] for c in mqtt.async_publish.call_args_list if c.args[0] == REQUEST]


def unavailable(hass: HomeAssistant) -> bool:
    return hass.states.get(BUTTON).state == "unavailable"


async def test_the_button_joins_the_gateway_device_and_publishes_send(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """One press, one request: `send` on `advert/send` at QoS 0, not retained. The event
    entity is on the same device. A control, so no entity category."""
    await connected(hass)
    assert {BUTTON, EVENT} <= entity_ids(hass)
    registry = er.async_get(hass)
    for entity_id in (BUTTON, EVENT):
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.device_id == device(hass).id
        assert entry.entity_category is None
    state = hass.states.get(BUTTON)
    assert state.attributes["friendly_name"] == "MeshCore radio Send advert nearby"
    assert state.state == "unknown"  # never pressed
    mqtt.async_publish.reset_mock()
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()
    assert advert_requests(mqtt) == [(REQUEST, "send", 0, False)]
    assert hass.states.get(BUTTON).state != "unknown"  # the time of the press
    assert problems(caplog) == []


async def test_the_button_needs_the_gateway_and_the_radio(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Unavailable until the state document says the radio is connected, and while the gateway
    is offline or its radio not connected; available again once both are back."""
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    await fire(hass, TOPICS.availability, "online", retain=True)
    assert unavailable(hass)  # no state document yet
    await fire(hass, TOPICS.state, state_document(known=False), retain=True)
    assert unavailable(hass)
    await fire(hass, TOPICS.state, state_document(known=True))
    assert not unavailable(hass)
    await fire(hass, TOPICS.availability, "offline")  # the gateway's last will
    assert unavailable(hass)
    await fire(hass, TOPICS.availability, "online")
    assert not unavailable(hass)
    assert problems(caplog) == []


@pytest.mark.parametrize(
    "off",
    [{"radio_controls": False}, {"controls": False}, {"radio_controls": False, "controls": False}],
)
async def test_the_settings_make_the_button_unavailable_at_once_both_ways(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, off: dict[str, bool]
) -> None:
    """`radio_controls = false`, and `controls = false` as for every control (SEC-008, HA-02):
    unavailable as soon as Home Assistant has the new discovery, available again as soon as it
    has the one with both on, without waiting for a message on the availability topic."""
    await connected(hass)
    assert not unavailable(hass)
    await announce(hass, discovery_payload(settings(**off), TOPICS, RADIO))
    assert unavailable(hass)
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    assert not unavailable(hass)
    assert problems(caplog) == []


@pytest.mark.parametrize("disabled", [False, True])
async def test_radio_controls_off_keeps_the_button_and_the_device(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, *, disabled: bool
) -> None:
    """The button stays announced, unavailable: the gateway's device keeps every entity and
    the settings its users gave them, also when a user had disabled the button; Home Assistant
    clears no discovery topic (a component that goes could take the whole device away)."""
    await connected(hass)
    registry = er.async_get(hass)
    if disabled:
        registry.async_update_entity(BUTTON, disabled_by=er.RegistryEntryDisabler.USER)
        await hass.async_block_till_done()
    registry.async_update_entity(EVENT, name="Advert of the house")
    before = {e: registry.async_get(e) for e in entity_ids(hass)}
    mqtt.async_publish.reset_mock()
    await announce(hass, discovery_payload(settings(radio_controls=False), TOPICS, RADIO))
    assert {e: registry.async_get(e) for e in entity_ids(hass)} == before
    assert discovery_topic(CONFIG) not in [c.args[0] for c in mqtt.async_publish.call_args_list]
    if not disabled:
        assert unavailable(hass)
    assert problems(caplog) == []


async def test_a_user_who_is_no_administrator_can_press_it(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any
) -> None:
    """The guide says so: the action has no check of its own, and every user can call it, from
    the dashboard or the entity search."""
    await connected(hass)
    user = await hass.auth.async_create_user("Carol", group_ids=[GROUP_ID_USER])
    assert not user.is_admin
    refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    client = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    mqtt.async_publish.reset_mock()
    await client.send_json_auto_id(
        {
            "type": "call_service",
            "domain": "button",
            "service": "press",
            "target": {"entity_id": BUTTON},
        }
    )
    answer = await client.receive_json()
    assert answer["success"], answer
    await hass.async_block_till_done()
    assert advert_requests(mqtt) == [(REQUEST, "send", 0, False)]


async def test_home_assistant_starting_again_presses_nothing(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """A reload of the MQTT entry, as at a restart, restores the button with the time of its
    last press and publishes no request; nor does the gateway's next announcement."""
    await connected(hass)
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await hass.async_block_till_done()
    pressed = hass.states.get(BUTTON).state
    mqtt.async_publish.reset_mock()
    await hass.config_entries.async_reload(hass.config_entries.async_entries("mqtt")[0].entry_id)
    await hass.async_block_till_done()
    await connected(hass)
    assert hass.states.get(BUTTON).state == pressed
    assert advert_requests(mqtt) == []
    assert problems(caplog) == []


@pytest.mark.parametrize(
    ("kind", "reason", "name", "airtime_s"),
    [
        ("sent", None, "Gateway", 1.164),
        ("refused", "too_soon", None, None),
        ("failed", "table_full", "Gateway", 1.164),
    ],
)
async def test_each_outcome_reaches_the_event_entity(
    hass: HomeAssistant,
    mqtt: Any,
    caplog: pytest.LogCaptureFixture,
    *,
    kind: str,
    reason: str | None,
    name: str | None,
    airtime_s: float | None,
) -> None:
    """Every key of the `advert` event is an attribute, `null` ones too; Home Assistant warns
    of nothing."""
    await connected(hass)
    event = self_advert_event(
        kind, "x1", reason=reason, name=name, airtime_s=airtime_s, next_at=NOW + 600
    )
    await fire(hass, TOPICS.advert_event, event)
    state = hass.states.get(EVENT)
    assert state.state != "unknown"
    for key, value in event.items():
        assert state.attributes[key] == value, key
    assert state.attributes["next_at"] == "2027-01-15T08:10:00+00:00"
    assert problems(caplog) == []


async def test_the_advert_entities_have_german_names(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """`[homeassistant] language = "de"`: the catalog's names; the entity IDs stay."""
    german = settings(language="de")
    device_name = i18n.translator("de").ha_name("MeshCore radio")
    await connected(hass, german)
    assert hass.states.get(BUTTON).attributes["friendly_name"] == (
        f"{device_name} Advert in der Nähe senden"
    )
    assert hass.states.get(EVENT).attributes["friendly_name"] == f"{device_name} Advert in der Nähe"
    assert problems(caplog) == []
