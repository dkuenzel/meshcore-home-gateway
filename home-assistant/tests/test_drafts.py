"""The chats' drafts and their Send buttons against the real Home Assistant MQTT integration
(docs/mqtt-api.md, Drafts; docs/home-assistant.md).

The gateway's own drafts part (drafts.py) is the fake gateway: what Home Assistant publishes on a
chat's request topic reaches it, and what it publishes reaches Home Assistant as the broker would
deliver it. Its outbox is a stand-in that takes every text that is not blank. The tests check
what the guide promises: the entities and their IDs, what a text and a press publish (on one
topic, at QoS 0), the echo as the state, `max` per room, no field for hidden targets and contact
targets, a kept field that reads unavailable, the state after a restart of Home Assistant, a press
while Home Assistant's link to the broker is down, the cut of a draft when `max` goes down, and
the controls for a user who is no administrator.
"""

from __future__ import annotations

import gc
import json
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as paho
import pytest
from ha_plugin import CLIENT_ID, async_fire_mqtt_message
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from test_contract import (
    CONFIG,
    PREFIX,
    RADIO,
    SENDER,
    TOPICS,
    announce,
    entity_ids,
    fire,
    make_config,
    problems,
)

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.mesh import Node, Tracked
from meshcore_home_gateway.core.model import DeliveryState
from meshcore_home_gateway.drafts import Drafts
from meshcore_home_gateway.mqtt.contact_devices import contact_discovery_topic, contact_payload
from meshcore_home_gateway.mqtt.discovery import discovery_payload
from meshcore_home_gateway.ports import SendRequest
from meshcore_home_gateway.state import JsonStateFile
from test_contact_devices import Broker, gateway_devices, mesh_of, online

CAROL = "c4" * 32  # a companion that is no target
ALERTS = f"text.{PREFIX}_write_alerts"
ALERTS_SEND = f"button.{PREFIX}_write_alerts_send"
CAROL_TEXT, CAROL_SEND = "text.mesh_carol_write", "button.mesh_carol_write_send"
REQUEST = "meshcore/radio1/homeassistant/draft/target/alerts/request"


class Gateway:
    """The gateway's drafts with a stand-in outbox: the fake gateway of the drafts."""

    def __init__(self, hass: HomeAssistant, config: Config = CONFIG) -> None:
        self.hass = hass
        self.out: list[tuple[str, bytes]] = []
        self.sent: list[SendRequest] = []
        self.taken = 0  # Home Assistant's publishes handed to the drafts so far
        self.drafts = Drafts(
            config,
            TOPICS,
            self,
            self.accept,
            monotonic=time.monotonic,
            call_later=hass.loop.call_later,
            contacts=(CAROL,),
        )

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        assert retain  # the drafts are retained
        self.out.append((topic, payload))

    def accept(self, request: SendRequest) -> DeliveryState:
        if not request.payload.strip():
            return DeliveryState.REJECTED
        self.sent.append(request)
        return DeliveryState.QUEUED

    async def deliver(self) -> None:
        out, self.out = self.out, []
        for topic, payload in out:
            async_fire_mqtt_message(self.hass, topic, payload.decode())
        await self.hass.async_block_till_done()
        await self.hass.async_block_till_done()

    async def take(self, mqtt: Any) -> list[tuple[str, dict[str, Any], int, bool]]:
        """Home Assistant's requests since the last call, handed to the drafts, and the drafts'
        answers to Home Assistant."""
        calls = mqtt.async_publish.call_args_list[self.taken :]
        self.taken += len(calls)
        requests = []
        for call in calls:
            topic, payload, qos, retain = call.args[:4]
            chat = TOPICS.draft_from_request_topic(topic)
            if chat is not None:
                raw = payload if isinstance(payload, bytes) else str(payload).encode()
                requests.append((topic, json.loads(raw), qos, retain))
                self.drafts.request(*chat, raw, retain)
        await self.deliver()
        return requests


async def with_drafts(hass: HomeAssistant, tmp_path: Path, config: Config = CONFIG) -> Gateway:
    """The gateway's device and Carol's, online, and every draft as the connect publishes it."""
    await announce(hass, discovery_payload(config, TOPICS, RADIO))
    broker = Broker()
    gateway_devices(broker, JsonStateFile(tmp_path / "state.json")).update(
        mesh_of(Node(CAROL, "Carol", 1))
    )
    await broker.deliver(hass)
    await online(hass)
    gateway = Gateway(hass, config)
    gateway.drafts.republish()
    await gateway.deliver()
    return gateway


async def call(hass: HomeAssistant, domain: str, service: str, entity: str, **data: Any) -> None:
    await hass.services.async_call(domain, service, {"entity_id": entity, **data}, blocking=True)
    await hass.async_block_till_done()


# --- writing and sending -----------------------------------------------------------------------


async def test_a_text_and_a_press_go_on_the_chats_request_topic_at_qos_0(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    gateway = await with_drafts(hass, tmp_path)
    assert {ALERTS, ALERTS_SEND, CAROL_TEXT, CAROL_SEND} <= entity_ids(hass)
    assert hass.states.get(ALERTS).state == ""  # the draft the gateway published at its connect
    assert hass.states.get(ALERTS).attributes["friendly_name"] == "MeshCore radio Write to alerts"
    assert hass.states.get(ALERTS_SEND).state == "unknown"  # never pressed
    await gateway.take(mqtt)

    await call(hass, "text", "set_value", ALERTS, value="Dinner is ready")
    assert await gateway.take(mqtt) == [(REQUEST, {"text": "Dinner is ready"}, 0, False)]
    assert hass.states.get(ALERTS).state == "Dinner is ready"  # the echo is the state

    await call(hass, "button", "press", ALERTS_SEND)
    assert await gateway.take(mqtt) == [(REQUEST, {"send": True}, 0, False)]
    assert gateway.sent == [SendRequest("alerts", b"Dinner is ready", False)]
    assert hass.states.get(ALERTS).state == ""  # cleared once queued

    await call(hass, "text", "set_value", CAROL_TEXT, value='Back "soon" <b>')
    contact = f"meshcore/radio1/homeassistant/draft/contact/{CAROL}/request"
    assert await gateway.take(mqtt) == [(contact, {"text": 'Back "soon" <b>'}, 0, False)]
    assert hass.states.get(CAROL_TEXT).state == 'Back "soon" <b>'
    await call(hass, "button", "press", CAROL_SEND)
    assert await gateway.take(mqtt) == [(contact, {"send": True}, 0, False)]
    assert gateway.sent[-1] == SendRequest(CAROL, b'Back "soon" <b>', False, contact=True)
    assert problems(caplog) == []


async def test_the_state_reads_only_a_draft(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Anything else on the state topic, a deleted topic too, reads as an empty draft."""
    await with_drafts(hass, tmp_path)
    topic = TOPICS.target_draft("alerts")
    await fire(hass, topic, {"text": "Hello"})
    assert hass.states.get(ALERTS).state == "Hello"
    for other in ("", "not json", [1, 2], {"text": 5}, {"other": "x"}, "unknown"):
        await fire(hass, topic, other)
        assert hass.states.get(ALERTS).state == "", other
    assert problems(caplog) == []


async def test_max_is_the_room_of_each_chat(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    await with_drafts(hass, tmp_path)
    room = {e: hass.states.get(e).attributes["max"] for e in (ALERTS, CAROL_TEXT)}
    assert room == {ALERTS: 160 - len("Gateway") - 2, CAROL_TEXT: 160}
    operator = hass.states.get(f"text.{PREFIX}_write_operator")
    assert operator.attributes["max"] == 160  # a direct message without the nonce
    assert hass.states.get(f"text.{PREFIX}_write_my_target") is not None  # '-' as '_'
    # 2026.10 refuses it with a ValueError, 2026.11 with a ServiceValidationError
    with pytest.raises((ValueError, ServiceValidationError), match=r"maximum length 151\)"):
        await call(hass, "text", "set_value", ALERTS, value="x" * (room[ALERTS] + 1))
    # `max` characters, each written by `tojson` as two escapes: the gateway takes all of it
    gateway = Gateway(hass)
    await gateway.take(mqtt)
    emoji = "\N{SMILING FACE WITH OPEN MOUTH}" * room[ALERTS]
    await call(hass, "text", "set_value", ALERTS, value=emoji)
    ((_, request, _, _),) = await gateway.take(mqtt)
    assert request == {"text": emoji}
    assert hass.states.get(ALERTS).state == emoji
    assert problems(caplog) == []


async def test_hidden_targets_and_contact_targets_have_no_field(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """A contact target's key has no companion field of its own, hidden or not; a hidden target
    no field at all. A field of Carol's kept from before she became a hidden target is always
    unavailable."""
    hidden = make_config(
        "[targets.alerts]\nchannel = 1\n"
        f'[targets.operator]\ncontact = "{SENDER}"\n'
        f'[targets.carol]\ncontact = "{CAROL}"\n'
        '[homeassistant]\nhidden_targets = ["operator", "carol"]\n'
    )
    await with_drafts(hass, tmp_path)
    tracked = Tracked(Node(CAROL, "Carol", 1))
    alone = contact_payload(CONFIG, TOPICS, tracked, "carol")["cmps"]
    kept = contact_payload(hidden, TOPICS, tracked, "carol", keep=alone)
    await fire(hass, contact_discovery_topic(CONFIG, CAROL), kept)
    await announce(hass, discovery_payload(hidden, TOPICS, RADIO))
    for entity in (CAROL_TEXT, CAROL_SEND):
        assert hass.states.get(entity).state == "unavailable", entity
    assert f"text.{PREFIX}_write_carol" not in entity_ids(hass)
    operator = tracked_for(SENDER, "Operator")
    document = contact_payload(hidden, TOPICS, operator, "operator")
    assert {"write", "write_send"}.isdisjoint(document["cmps"])
    assert problems(caplog) == []


def tracked_for(key: str, name: str) -> Tracked:
    return Tracked(Node(key, name, 1))


async def test_after_a_restart_of_home_assistant_the_field_shows_the_retained_draft(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Not unknown: a press then sends what the field shows."""
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    await fire(hass, TOPICS.availability, "online", retain=True)
    await fire(hass, TOPICS.target_draft("alerts"), {"text": "Half a sentence"}, retain=True)
    assert hass.states.get(ALERTS).state == "Half a sentence"
    assert problems(caplog) == []


async def test_a_press_while_home_assistant_is_disconnected_is_not_sent_later(
    hass: HomeAssistant,
    mqtt: Any,
    mqtt_client_mock: Any,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    """The press raises, as paho refuses a publish without a connection. At QoS 0 paho keeps
    nothing for the reconnect (`client.py`, publish), so a Send that failed visibly never goes
    out later, unseen; at QoS 1 it would. Home Assistant does not try again either."""
    gateway = await with_drafts(hass, tmp_path)
    await call(hass, "text", "set_value", ALERTS, value="Gate open")
    await gateway.take(mqtt)
    attempts: list[tuple[str, Any, int, bool]] = []
    connected = mqtt_client_mock.publish.side_effect

    def disconnected(topic: str, payload: Any, qos: int, retain: bool, _p: Any = None) -> Any:
        attempts.append((topic, payload, qos, retain))
        return SimpleNamespace(mid=None, rc=paho.MQTT_ERR_NO_CONN)

    mqtt_client_mock.publish.side_effect = disconnected
    with pytest.raises(HomeAssistantError):
        await call(hass, "button", "press", ALERTS_SEND)
    assert attempts == [(REQUEST, '{"send": true}', 0, False)]
    gateway.taken = len(mqtt.async_publish.call_args_list)  # it never reached the broker
    mqtt_client_mock.publish.side_effect = connected
    mqtt_client_mock.publish.reset_mock()
    mqtt_client_mock.on_connect(mqtt_client_mock, None, 0, SimpleNamespace(is_failure=False))
    await hass.async_block_till_done()
    assert [c for c in mqtt_client_mock.publish.call_args_list if c.args[0] == REQUEST] == []
    await gateway.take(mqtt)
    assert gateway.sent == []  # nothing reached the gateway
    assert hass.states.get(ALERTS).state == "Gate open"


async def test_a_draft_cut_to_a_lower_max_is_taken_with_the_new_discovery(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    """Before the radio's name is known a channel's room is 158; with it 151 for "Gateway". Home
    Assistant writes the state again as soon as it takes a new discovery, and a state longer than
    the new `max` raises there: the gateway cuts the draft and publishes it before the
    discovery, which the old, larger `max` takes."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await online(hass)
    gateway = Gateway(hass)
    gateway.drafts.announcing(None)
    gateway.drafts.request("target", "alerts", json.dumps({"text": "x" * 155}).encode(), False)
    await gateway.deliver()
    assert hass.states.get(ALERTS).state == "x" * 155
    gateway.drafts.announcing(RADIO)  # the radio connected: the announcement follows
    await gateway.deliver()
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    text = hass.states.get(ALERTS)
    assert (text.state, text.attributes["max"]) == ("x" * 151, 151)
    assert problems(caplog) == []


async def test_a_state_longer_than_a_new_max_breaks_the_entity(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Why the cut goes out before the discovery: a draft that came after it would be too late,
    Home Assistant raises while it takes the discovery (components/mqtt/entity.py,
    discovery_update, which writes the state; components/text, the entity's state)."""
    await announce(hass, discovery_payload(CONFIG, TOPICS))
    await online(hass)
    await fire(hass, TOPICS.target_draft("alerts"), {"text": "x" * 155})
    raised: list[BaseException | None] = []
    handler = hass.loop.get_exception_handler()
    hass.loop.set_exception_handler(lambda _, context: raised.append(context.get("exception")))
    try:
        await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
        gc.collect()  # the discovery's task reports its exception when it goes
        await hass.async_block_till_done()
    finally:
        hass.loop.set_exception_handler(handler)
    assert [str(e) for e in raised if isinstance(e, ValueError)] == [
        f"Entity {ALERTS} provides state {'x' * 155} which is too long (maximum length 151)"
    ]


async def test_a_user_who_is_no_administrator_can_write_and_send(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, tmp_path: Path
) -> None:
    gateway = await with_drafts(hass, tmp_path)
    await gateway.take(mqtt)
    user = await hass.auth.async_create_user("Carol", group_ids=[GROUP_ID_USER])
    assert not user.is_admin
    refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    client = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    for domain, service, entity, data in (
        ("text", "set_value", CAROL_TEXT, {"value": "On my way"}),
        ("button", "press", CAROL_SEND, {}),
    ):
        await client.send_json_auto_id(
            {
                "type": "call_service",
                "domain": domain,
                "service": service,
                "target": {"entity_id": entity},
                "service_data": data,
            }
        )
        answer = await client.receive_json()
        assert answer["success"], answer
        await hass.async_block_till_done()
        await gateway.take(mqtt)
    assert gateway.sent == [SendRequest(CAROL, b"On my way", False, contact=True)]
    assert hass.states.get(CAROL_TEXT).state == ""


def test_controls_off_keeps_the_fields(tmp_path: Path) -> None:
    """Sending is no control: `controls = false` changes no field."""
    off = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, controls=False))
    on_cmps = discovery_payload(CONFIG, TOPICS, RADIO)["cmps"]
    off_cmps = discovery_payload(off, TOPICS, RADIO)["cmps"]
    assert {k: v for k, v in on_cmps.items() if k.startswith("write_")} == {
        k: v for k, v in off_cmps.items() if k.startswith("write_")
    }
