"""The controls that add and remove contacts against the real Home Assistant MQTT integration
(ADR 0017, docs/home-assistant.md "Adding and removing contacts").

The gateway's own code builds the documents. The tests check what the guide promises: the select
and the text on the gateway's device with their entity IDs, what a choice and a text publish, the
pattern that keeps contact cards out, the availability with the radio and the switch (also with
documents an earlier version retained, without a warning), the remove button of each contact, the
look-alike sensor, the gateway's contact link with its QR card, the dashboard button, the
automation of the examples, and the removal of a control of an earlier version.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from homeassistant.auth.const import GROUP_ID_USER
from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.template import Template
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import CLIENT_ID
from test_contract import (
    CONFIG,
    PREFIX,
    RADIO,
    TOPICS,
    announce,
    device,
    entity_ids,
    fire,
    guide_automations,
    guide_block,
    problems,
    state_document,
)

from meshcore_home_gateway.core.mesh import Heard, Mesh, Node
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.manage_entities import manage_payload, manage_topic, node_options
from meshcore_home_gateway.protocol.links import contact_link
from meshcore_home_gateway.state import JsonStateFile
from meshcore_home_gateway.views import contact_document, contacts_document, nodes_document
from test_contact_devices import Broker, gateway_devices, mesh_of, online

SELECT, TEXT = f"select.{PREFIX}_add_node", f"text.{PREFIX}_add_contact"
LOOK_ALIKE = f"binary_sensor.{PREFIX}_look_alike"
BOB, DAVE = "b0" * 32, "d4" * 32
NOW = 1_800_000_000.0
LINK = f"meshcore://contact/add?name=Bob&public_key={BOB}&type=1"


def heard(*nodes: tuple[str, str, int]) -> Mesh:
    mesh = Mesh()
    for key, name, node_type in nodes:
        mesh.node_heard(Node(key, name, node_type, last_advert=int(NOW)), Heard(NOW, "advert"))
    return mesh


async def with_controls(hass: HomeAssistant, options: list[str], *, manage: bool = True) -> None:
    """The gateway's documents as after a connect: online, radio connected, management on."""
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    await fire(hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, options))
    await online(hass)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)
    await fire(hass, TOPICS.contacts, contacts_document(Mesh(), manage=manage), retain=True)


def publishes(mqtt: Any) -> list[tuple[Any, ...]]:
    return [c.args[:4] for c in mqtt.async_publish.call_args_list]


# --- the select and the text ------------------------------------------------------------------


async def test_the_controls_join_the_gateway_device(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_controls(hass, [])
    assert {SELECT, TEXT} <= entity_ids(hass)
    registry = er.async_get(hass)
    for entity_id in (SELECT, TEXT):
        entry = registry.async_get(entity_id)
        assert entry is not None
        assert entry.device_id == device(hass).id  # one device: the gateway's
        assert entry.entity_category is not None
        assert entry.entity_category.value == "config"
    assert hass.states.get(SELECT).attributes["friendly_name"] == "MeshCore radio Add heard node"
    assert hass.states.get(SELECT).state == "unknown"
    assert hass.states.get(TEXT).state == ""
    assert problems(caplog) == []


async def test_a_choice_publishes_the_label_and_the_select_stays_unchosen(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    mesh = heard((BOB, "Bob", 1), (DAVE, "Club", 3))
    options = node_options(mesh)
    await with_controls(hass, options)
    assert hass.states.get(SELECT).attributes["options"] == options
    for _ in range(2):  # the same choice again: it is still a request
        mqtt.async_publish.reset_mock()
        await hass.services.async_call(
            "select", "select_option", {"entity_id": SELECT, "option": options[0]}, blocking=True
        )
        await hass.async_block_till_done()
        assert publishes(mqtt) == [("meshcore/radio1/homeassistant/add_node", options[0], 1, False)]
        await fire(hass, TOPICS.contacts, contacts_document(Mesh(), manage=True))
        assert hass.states.get(SELECT).state == "unknown"
    assert problems(caplog) == []


async def test_new_options_keep_the_entity(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_controls(hass, [])
    entry = er.async_get(hass).async_get(SELECT)
    assert entry is not None
    options = node_options(heard((BOB, "Bob", 1)))
    await fire(hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, options))
    assert hass.states.get(SELECT).attributes["options"] == options
    again = er.async_get(hass).async_get(SELECT)
    assert again is not None
    assert (again.id, again.entity_id) == (entry.id, entry.entity_id)  # the same entity
    await fire(hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, []))  # none heard
    assert hass.states.get(SELECT).attributes["options"] == []
    assert problems(caplog) == []


async def test_more_than_100_option_changes_in_an_hour_make_home_assistant_warn(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Why the gateway sends at most 70 changes an hour (mqtt/manage_entities.py): from the
    101st, Home Assistant logs a warning that asks for a bug report against itself."""
    await with_controls(hass, [])
    for i in range(100):
        await fire(hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, [f"node {i}"]))
    assert hass.states.get(SELECT).attributes["options"] == ["node 99"]
    assert problems(caplog) == []
    await fire(hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, ["node 100"]))
    (warning,) = problems(caplog)
    assert "is updating its capabilities too often" in warning


@pytest.mark.parametrize("text", [BOB, BOB.upper(), LINK, f" {BOB} ", f"{LINK} "])
async def test_a_key_or_link_is_published(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, text: str
) -> None:
    await with_controls(hass, [])
    mqtt.async_publish.reset_mock()
    await hass.services.async_call(
        "text", "set_value", {"entity_id": TEXT, "value": text}, blocking=True
    )
    await hass.async_block_till_done()
    assert publishes(mqtt) == [("meshcore/radio1/add_contact", text, 1, False)]
    assert hass.states.get(TEXT).state == ""
    assert problems(caplog) == []


@pytest.mark.parametrize(
    "text",
    [
        "meshcore://" + "11" * 120,  # a contact card: the field would cut it
        "Bob", BOB[:-2], "meshcore://contact/add?name=Bob Smith", "x" * 300,
    ],
)  # fmt: skip
async def test_other_text_is_refused_before_it_is_sent(
    hass: HomeAssistant, mqtt: Any, text: str
) -> None:
    await with_controls(hass, [])
    mqtt.async_publish.reset_mock()
    with pytest.raises((ValueError, HomeAssistantError)):  # 2026.10: a ValueError
        await hass.services.async_call(
            "text", "set_value", {"entity_id": TEXT, "value": text}, blocking=True
        )
    await hass.async_block_till_done()
    assert publishes(mqtt) == []


async def test_the_controls_need_the_radio_and_management(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_controls(hass, [], manage=False)
    assert {hass.states.get(e).state for e in (SELECT, TEXT)} == {"unavailable"}
    await fire(hass, TOPICS.contacts, contacts_document(Mesh(), manage=True))
    assert hass.states.get(SELECT).state == "unknown"
    assert hass.states.get(TEXT).state == ""
    await fire(hass, TOPICS.state, state_document(known=False))  # the radio went away
    assert {hass.states.get(e).state for e in (SELECT, TEXT)} == {"unavailable"}
    await fire(hass, TOPICS.state, state_document(known=True))
    old = {k: v for k, v in contacts_document(Mesh()).items() if k != "manage"}
    await fire(hass, TOPICS.contacts, old)  # retained by a version without the switch
    assert {hass.states.get(e).state for e in (SELECT, TEXT)} == {"unavailable"}
    assert problems(caplog) == []


async def test_a_control_of_an_earlier_version_is_removed(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """Also when Home Assistant has not discovered it since its start: the placeholder sent
    first binds it (as for the main document, tests/test_contract.py)."""
    registry = er.async_get(hass)
    (entry,) = hass.config_entries.async_entries("mqtt")
    for platform, key in (("select", "add_group"), ("text", "add_note")):
        unique_id = f"{PREFIX}_{key}"
        registry.async_get_or_create(
            platform, "mqtt", unique_id, config_entry=entry, suggested_object_id=unique_id
        )
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    removed = {"add_group": "select", "add_note": "text"}
    for _ in range(2):
        await fire(
            hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, [], removed, bind=True)
        )
        await fire(hass, manage_topic(CONFIG), manage_payload(CONFIG, TOPICS, [], removed))
        assert registry.async_get_entity_id("select", "mqtt", f"{PREFIX}_add_group") is None
        assert registry.async_get_entity_id("text", "mqtt", f"{PREFIX}_add_note") is None
    assert {SELECT, TEXT} <= entity_ids(hass)
    assert problems(caplog) == []


async def test_deleting_the_gateway_device_clears_the_controls_topic_too(
    hass: HomeAssistant, mqtt: Any, *, hass_ws_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """The gateway watches the controls' topic like its own, so they come back with its next
    announcement (tests/test_bridge.py)."""
    assert await async_setup_component(hass, "config", {})
    await with_controls(hass, [])
    mqtt.async_publish.reset_mock()
    client = await hass_ws_client()
    await client.send_json(  # the "Delete" button of the device page
        {"id": 1, "type": "config/device_registry/remove", "device_id": device(hass).id}
    )
    assert (await client.receive_json())["success"]
    await hass.async_block_till_done()
    cleared = {(t, p, r) for t, p, _q, r in publishes(mqtt)}
    assert cleared == {(discovery_topic(CONFIG), None, True), (manage_topic(CONFIG), None, True)}
    assert problems(caplog) == []


# --- removing ----------------------------------------------------------------------------------


async def with_bob(hass: HomeAssistant, tmp_path: Path) -> Mesh:
    """The gateway with the controls, and Bob's device; the retained documents as the broker
    sends them to the new subscriptions."""
    broker = Broker()
    await with_controls(hass, [])
    mesh = mesh_of(Node(BOB, "Bob", 1))
    gateway_devices(broker, JsonStateFile(tmp_path / "state.json")).update(mesh)
    await broker.deliver(hass)
    await online(hass)
    await fire(hass, TOPICS.state, state_document(known=True), retain=True)
    await fire(hass, TOPICS.contact(BOB), contact_document(mesh.contacts[BOB], manage=True),
               retain=True)  # fmt: skip
    return mesh


async def test_the_remove_button_is_available_while_a_request_would_be_taken(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    mesh = await with_bob(hass, tmp_path)
    button = "button.mesh_bob_remove"
    document = TOPICS.contact(BOB)
    tracked = mesh.contacts[BOB]
    assert hass.states.get(button).state == "unknown"  # can be pressed
    mqtt.async_publish.reset_mock()
    await hass.services.async_call("button", "press", {"entity_id": button}, blocking=True)
    await hass.async_block_till_done()
    assert publishes(mqtt) == [(f"meshcore/radio1/contacts/{BOB}/remove", "remove", 1, False)]

    await fire(hass, document, contact_document(tracked, protected=True, manage=True))
    assert hass.states.get(button).state == "unavailable"
    await fire(hass, document, contact_document(tracked, manage=True))
    await fire(hass, TOPICS.state, state_document(known=False))
    assert hass.states.get(button).state == "unavailable"
    await fire(hass, TOPICS.state, state_document(known=True))
    old = {k: v for k, v in contact_document(tracked).items() if k != "removable"}
    await fire(hass, document, old)  # retained by a version without the key
    assert hass.states.get(button).state == "unavailable"
    await fire(hass, document, contact_document(tracked, manage=True))
    assert hass.states.get(button).state != "unavailable"  # the time of the last press
    await fire(hass, TOPICS.availability, "offline")  # the gateway's last will: the rest is old
    assert hass.states.get(button).state == "unavailable"
    assert problems(caplog) == []


async def test_a_user_who_is_no_administrator_can_press_the_remove_button(
    hass: HomeAssistant, mqtt: Any, hass_ws_client: Any, tmp_path: Path
) -> None:
    """Only administrators open the device page, but the action of its button has no check of its
    own, and the entity search opens the button for every user (docs/home-assistant.md)."""
    await with_bob(hass, tmp_path)
    user = await hass.auth.async_create_user("Carol", group_ids=[GROUP_ID_USER])
    assert not user.is_admin
    refresh = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    client = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh))
    mqtt.async_publish.reset_mock()
    await client.send_json_auto_id({
        "type": "call_service", "domain": "button", "service": "press",
        "target": {"entity_id": "button.mesh_bob_remove"},
    })  # fmt: skip
    answer = await client.receive_json()
    assert answer["success"], answer
    await hass.async_block_till_done()
    assert publishes(mqtt) == [(f"meshcore/radio1/contacts/{BOB}/remove", "remove", 1, False)]


# --- the look-alike sensor and the contact link -----------------------------------------------


async def test_a_look_alike_node_is_a_problem(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_controls(hass, [])
    twin = BOB[:12] + "00" * 26
    mesh = heard((BOB, "Bob", 1), (twin, "Bob", 1), (DAVE, "Dave", 1))
    await fire(hass, TOPICS.nodes, nodes_document(mesh), retain=True)
    sensor = hass.states.get(LOOK_ALIKE)
    assert (sensor.state, sensor.attributes["nodes"], sensor.attributes["total"]) == (
        "on", sorted([BOB, twin]), 2,
    )  # fmt: skip
    await fire(hass, TOPICS.nodes, nodes_document(heard((DAVE, "Dave", 1))))
    assert hass.states.get(LOOK_ALIKE).state == "off"
    old = {k: v for k, v in nodes_document(mesh).items() if not k.startswith("look_alike")}
    await fire(hass, TOPICS.nodes, old)  # retained by an earlier version
    sensor = hass.states.get(LOOK_ALIKE)
    assert (sensor.state, sensor.attributes["nodes"], sensor.attributes["total"]) == ("off", [], 0)
    assert problems(caplog) == []


async def test_the_qr_card_shows_the_gateways_contact_link(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await announce(hass, discovery_payload(CONFIG, TOPICS, RADIO))
    await online(hass)
    card = Template(guide_block("type: markdown\ntitle: Add the gateway")["content"], hass)
    old = {k: v for k, v in state_document(known=True).items() if k != "contact_link"}
    await fire(hass, TOPICS.state, old, retain=True)  # an earlier version's state: no link
    assert card.async_render(parse_result=False).strip() == "The radio is not known yet."
    link = contact_link('Gate "<b>"', RADIO.public_key)
    await fire(hass, TOPICS.state, {**state_document(known=True), "contact_link": link})
    attribute = hass.states.get(f"binary_sensor.{PREFIX}_radio").attributes["contact_link"]
    assert attribute == link
    rendered = card.async_render(parse_result=False).strip()
    (data,) = re.fullmatch(
        r'<ha-qr-code data="([^"<>]*)" scale="4"></ha-qr-code>', rendered
    ).groups()
    assert data == link.replace("&", "&amp;")  # the browser reads the link back
    assert problems(caplog) == []


# --- the dashboard and the automation of the guide ----------------------------------------------


async def test_the_dashboard_button_removes_the_contact(
    hass: HomeAssistant, mqtt: Any, tmp_path: Path
) -> None:
    await with_bob(hass, tmp_path)
    card = guide_block("type: entities\ntitle: Mesh contacts")
    first, second, row = card["entities"]
    assert (first["entity"], second["entity"]) == (SELECT, TEXT)
    # the button entity row: disabled while the button is unavailable, asks first; a `type:
    # button` row would stay pressable and do nothing then (docs/platform-notes.md)
    assert set(row) == {"entity", "name", "confirmation"}
    assert row["confirmation"]["text"].startswith("Remove Bob from the radio?")
    mqtt.async_publish.reset_mock()
    await hass.services.async_call("button", "press", {"entity_id": row["entity"]}, blocking=True)
    await hass.async_block_till_done()
    assert publishes(mqtt) == [(f"meshcore/radio1/contacts/{BOB}/remove", "remove", 1, False)]


async def test_the_automation_reports_a_contact_not_changed(
    hass: HomeAssistant, mqtt: Any, caplog: pytest.LogCaptureFixture
) -> None:
    await with_controls(hass, [])
    (automation,) = [a for a in guide_automations() if a["alias"] == "Mesh contact not changed"]
    assert await async_setup_component(hass, "persistent_notification", {})
    assert await async_setup_component(hass, "automation", {"automation": [automation]})
    await hass.async_block_till_done()

    def refused(reason: str, key: str | None, kind: str = "refused") -> dict[str, Any]:
        return {"event_type": kind, "key": key, "prefix": key[:12] if key else None,
                "name": "**Mallory**", "type": "chat", "request": "add", "id": "r1",
                "reason": reason}  # fmt: skip

    await fire(hass, TOPICS.contact_event, refused("already_contact", BOB))  # left out
    await fire(hass, TOPICS.contact_event, refused("look_alike", BOB))
    await fire(hass, TOPICS.contact_event, refused("invalid_link", None))
    await fire(hass, TOPICS.contact_event, refused("outcome_unknown", DAVE, "failed"))
    notifications = persistent_notification._async_get_or_create_notifications(hass)
    assert [n["message"] for n in notifications.values()] == [
        f"add {BOB[:12]}: look_alike", "add no key: invalid_link",
        f"add {DAVE[:12]}: outcome_unknown",
    ]  # fmt: skip
    assert problems(caplog) == []
