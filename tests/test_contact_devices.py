"""Home Assistant devices for the radio's contacts (ADR 0017, docs/home-assistant.md)."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.core.mesh import Heard, Mesh, Node, Tracked
from meshcore_home_gateway.core.model import InboundMessage, MessageKind, Position
from meshcore_home_gateway.mqtt.contact_devices import (
    ECHO_S,
    ContactDevices,
    contact_discovery_subscription,
    contact_discovery_topic,
    contact_payload,
    key_from_discovery_topic,
    slugify,
)
from meshcore_home_gateway.mqtt.payloads import received_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codes import ContactFlag, NodeType
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from meshcore_home_gateway.views import contact_document
from tests.test_discovery import STATE_KEYS, template_keys

ALICE, BOB, HILL, CAROL = "a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32
NOW = 1_800_000_000.0
CONFIG = parse_config(
    '[radio]\ntransport = "tcp"\nhost = "h"\n[mqtt]\nhost = "h"\nnode_id = "radio1"\n'
)
TOPICS = Topics(CONFIG.mqtt.base_topic, CONFIG.mqtt.node_id)
GATEWAY = "meshcore_home_gateway_radio1"


class Recorder:
    def __init__(self) -> None:
        self.published: list[tuple[str, bytes]] = []

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        assert not retain  # discovery documents are never retained (ADR 0010)
        self.published.append((topic, payload))

    def take(self) -> list[tuple[str, Any]]:
        sent = [(t, json.loads(p) if p else None) for t, p in self.published]
        self.published.clear()
        return sent


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


def node(key: str, name: str, node_type: int = NodeType.CHAT, **fields: Any) -> Node:
    return Node(key, name, node_type, **fields)


def mesh_of(*nodes: Node, mesh: Mesh | None = None) -> Mesh:
    mesh = mesh or Mesh()
    mesh.contacts_read(nodes, NOW)
    return mesh


def devices(
    tmp_path: Path, limit: int = 50, clock: Clock | None = None, **options: Any
) -> tuple[ContactDevices, Recorder]:
    config = replace(CONFIG, homeassistant=replace(CONFIG.homeassistant, contact_devices=limit))
    bus = Recorder()
    records = ContactDeviceRecords(JsonStateFile(tmp_path / "state.json"))
    return ContactDevices(config, TOPICS, bus, records, wall_clock=clock or Clock(), **options), bus


def topic(key: str) -> str:
    return contact_discovery_topic(CONFIG, key)


# --- the document --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "slug"),
    [
        ("Alice", "alice"), ("Jürgen's Handy", "jurgen_s_handy"), ("Łódź 2", "lodz_2"),
        ("Groß", "gross"), ("  --Bob--  ", "bob"), ("🙂", ""), ("Ærø", "aero"),
        ("x" * 40, "x" * 32), ("a" * 31 + " b", "a" * 31),
    ],
)  # fmt: skip
def test_slugs_are_valid_entity_id_parts(name: str, slug: str) -> None:
    assert slugify(name) == slug
    assert re.fullmatch(r"([a-z0-9]+(_[a-z0-9]+)*)?", slug)


def test_topics_sit_below_the_gateway_device() -> None:
    assert topic(ALICE) == f"homeassistant/device/{GATEWAY}/contact_{ALICE}/config"
    assert contact_discovery_subscription(CONFIG) == f"homeassistant/device/{GATEWAY}/+/config"
    assert key_from_discovery_topic(CONFIG, topic(ALICE)) == ALICE
    for other in (f"homeassistant/device/{GATEWAY}/config", topic(ALICE)[:-7] + "/state",
                  f"homeassistant/device/{GATEWAY}/contact_{ALICE.upper()}/config",
                  f"homeassistant/device/{GATEWAY}/contact_a1/config"):  # fmt: skip
        assert key_from_discovery_topic(CONFIG, other) is None


def test_a_companion_can_be_messaged_and_sends_messages() -> None:
    tracked = Tracked(node(ALICE, "Alice"))
    document = contact_payload(CONFIG, TOPICS, tracked, "alice")
    cmps = document["cmps"]
    assert set(cmps) == {"last_heard", "hops", "snr", "message", "send", "remove"}
    assert cmps["send"]["name"] is None  # named like the device
    assert cmps["send"]["command_topic"] == f"meshcore/radio1/contacts/{ALICE}/send"
    assert cmps["message"]["state_topic"] == f"meshcore/radio1/contacts/{ALICE}/received"
    assert cmps["message"]["event_types"] == ["contact"]
    # Home Assistant clears the topic itself on removing a device with a disabled entity
    assert all("enabled_by_default" not in c for c in cmps.values())
    assert "name" not in cmps["snr"]  # Home Assistant names it after its class, translated
    assert {c["default_entity_id"] for c in cmps.values()} == {
        "sensor.mesh_alice_last_heard", "sensor.mesh_alice_hops", "sensor.mesh_alice_snr",
        "event.mesh_alice_message", "notify.mesh_alice", "button.mesh_alice_remove",
    }  # fmt: skip
    for key, component in cmps.items():
        assert component["unique_id"] == f"{GATEWAY}_contact_{ALICE}_{key}"
    assert document["dev"] == {
        "ids": [f"{GATEWAY}_contact_{ALICE}"], "name": "Alice", "mf": "MeshCore",
        "mdl": "Companion", "sn": ALICE, "via_device": GATEWAY,
    }  # fmt: skip
    assert document["availability"] == [{"topic": "meshcore/radio1/availability"}]


def test_the_remove_button_is_available_only_while_a_request_would_be_taken() -> None:
    """Its availability replaces the device's: the gateway online, the radio connected, the
    contact removable. A document an earlier version retained, or one that is no JSON (an empty
    one clears the topic), reads as offline (home-assistant/tests)."""
    cmps = contact_payload(CONFIG, TOPICS, Tracked(node(ALICE, "Alice")), "alice")["cmps"]
    button = cmps["remove"]
    assert (button["p"], button["entity_category"]) == ("button", "config")
    assert button["command_topic"] == f"meshcore/radio1/contacts/{ALICE}/remove"
    assert button["availability_mode"] == "all"
    assert [a["topic"] for a in button["availability"]] == [
        "meshcore/radio1/availability", "meshcore/radio1/state",
        f"meshcore/radio1/contacts/{ALICE}",
    ]  # fmt: skip
    for entry in button["availability"][1:]:
        assert entry["value_template"].startswith("{{ 'online' if value_json is defined and ")
    assert "value_json.removable is true" in button["availability"][2]["value_template"]


def test_a_repeater_with_a_position_gets_a_tracker_only() -> None:
    """Only the tracker carries coordinates: the map shows every entity that has them."""
    repeater = node(HILL, "Hilltop", NodeType.REPEATER, position=Position(52.1, 9.2))
    mesh = mesh_of(repeater)
    cmps = contact_payload(CONFIG, TOPICS, mesh.contacts[HILL], "hilltop")["cmps"]
    assert set(cmps) == {"last_heard", "hops", "snr", "position", "remove"}
    tracker = cmps["position"]
    assert tracker["default_entity_id"] == "device_tracker.mesh_hilltop"
    assert tracker["json_attributes_topic"] == f"meshcore/radio1/contacts/{HILL}"
    assert tracker["availability_mode"] == "all"
    assert tracker["availability"][0] == {"topic": "meshcore/radio1/availability"}
    assert "gps_accuracy" not in tracker["json_attributes_template"]  # would make it `home`
    others = [c for k, c in cmps.items() if k != "position"]
    assert all("json_attributes_topic" not in c for c in others)
    unnamed = contact_payload(CONFIG, TOPICS, Tracked(node(HILL, "")), "x")
    assert unnamed["dev"]["name"] == HILL[:12]


def test_a_room_server_sends_messages_but_takes_none() -> None:
    cmps = contact_payload(CONFIG, TOPICS, Tracked(node(BOB, "Club", 3)), "club")["cmps"]
    assert "message" in cmps
    assert "send" not in cmps


def test_every_template_reads_a_key_of_its_document() -> None:
    mesh = mesh_of(node(ALICE, "Alice", position=Position(52.1, 9.2)))
    tracked = mesh.contacts[ALICE]
    cmps = contact_payload(CONFIG, TOPICS, tracked, "alice")["cmps"]
    documents = {
        TOPICS.contact(ALICE): set(contact_document(tracked)),
        TOPICS.contact_received(ALICE): set(_received()),
        TOPICS.state: STATE_KEYS,
    }
    for component in cmps.values():
        templates = [
            component.get("value_template", ""),
            component.get("json_attributes_template", ""),
            *(a.get("value_template", "") for a in component.get("availability", [])),
        ]
        read = {k for t in templates for k in template_keys(t)}
        sources = {component.get("state_topic"), component.get("json_attributes_topic")}
        sources |= {a["topic"] for a in component.get("availability", [])}
        known = set().union(*(documents.get(s, set()) for s in sources))
        assert read <= known, component["unique_id"]


def _received() -> dict[str, Any]:
    message = InboundMessage(MessageKind.CONTACT, "hi", int(NOW), NOW, sender_prefix=ALICE[:12])
    return received_payload(message, None, None)


# --- announcing and removing ---------------------------------------------------------------------


def test_a_contact_gets_its_device_once(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    ((sent_topic, document),) = bus.take()
    assert sent_topic == topic(ALICE)
    assert document["dev"]["name"] == "Alice"
    contacts.update(mesh)
    assert bus.take() == []  # unchanged


def test_a_renamed_contact_keeps_its_entity_ids(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    contacts.update(mesh_of(node(ALICE, "Alice")))
    bus.take()
    contacts.update(mesh_of(node(ALICE, "Alice phone")))
    ((_, document),) = bus.take()
    assert document["dev"]["name"] == "Alice phone"
    assert document["cmps"]["send"]["default_entity_id"] == "notify.mesh_alice"


def test_a_removed_contact_loses_its_device(tmp_path: Path) -> None:
    """Placeholders first: they bind what Home Assistant kept from before its start. They are
    enabled: removing a disabled entity makes Home Assistant clear the device's topic itself."""
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"), node(BOB, "Bob"))
    contacts.update(mesh)
    bus.take()
    contacts.update(mesh_of(node(BOB, "Bob"), mesh=mesh))
    (bind_topic, bind), (empty_topic, empty) = bus.take()
    assert bind_topic == empty_topic == topic(ALICE)
    assert empty is None
    assert set(bind["cmps"]) == {"last_heard", "hops", "snr", "message", "send", "remove"}
    assert all("enabled_by_default" not in c for c in bind["cmps"].values())
    assert bind["cmps"]["send"]["unique_id"] == f"{GATEWAY}_contact_{ALICE}_send"


def test_a_removal_is_sent_again_after_home_assistant_starts_then_done(tmp_path: Path) -> None:
    """Not known to run (no birth message seen since the connect): the removal waits for its
    start, is sent once more then, and is done."""
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"), node(BOB, "Bob"))
    contacts.update(mesh)
    contacts.update(mesh_of(node(BOB, "Bob"), mesh=mesh))
    bus.take()
    contacts.announce_all()  # the broker connected: Home Assistant's state is not known
    assert [t for t, _ in bus.take()] == [topic(BOB)]
    contacts.announce_all(birth=True)
    sent = bus.take()
    assert [t for t, _ in sent] == [topic(ALICE), topic(ALICE), topic(BOB)]
    assert sent[1][1] is None
    contacts.announce_all(birth=True)
    assert [t for t, _ in bus.take()] == [topic(BOB)]  # done


def test_a_removal_while_home_assistant_runs_is_not_repeated(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    contacts.announce_all(birth=True)
    mesh = mesh_of(node(ALICE, "Alice"), node(BOB, "Bob"))
    contacts.update(mesh)
    contacts.update(mesh_of(node(BOB, "Bob"), mesh=mesh))
    bus.take()
    contacts.announce_all(birth=True)
    assert [t for t, _ in bus.take()] == [topic(BOB)]
    contacts.ha_offline()  # stopped: a removal now waits for its next start
    contacts.update(mesh_of(mesh=mesh))
    bus.take()
    contacts.announce_all(birth=True)
    assert [t for t, d in bus.take()] == [topic(BOB), topic(BOB)]


def test_removals_and_slugs_survive_a_restart(tmp_path: Path) -> None:
    first, _ = devices(tmp_path)
    first.update(mesh_of(node(ALICE, "Alice"), node(BOB, "Bob")))
    first.update(mesh_of(node(ALICE, "Alice renamed"), node(BOB, "Bob")))
    second, bus = devices(tmp_path)  # Bob was removed while the gateway was stopped
    second.update(mesh_of(node(ALICE, "Alice renamed")))
    sent = bus.take()
    assert [t for t, _ in sent] == [topic(BOB), topic(BOB), topic(ALICE)]
    assert sent[2][1]["cmps"]["send"]["default_entity_id"] == "notify.mesh_alice"
    third, bus = devices(tmp_path)
    third.announce_all(birth=True)  # before the first table read: only the pending removal
    assert [t for t, _ in bus.take()] == [topic(BOB), topic(BOB)]


def test_a_contact_that_returns_keeps_its_entity_ids(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.update(mesh_of(mesh=mesh))
    restarted, bus = devices(tmp_path)
    restarted.update(mesh_of(node(ALICE, "Alice phone"), node(BOB, "Alice")))
    ids = {d["dev"]["name"]: d["cmps"]["send"]["default_entity_id"] for _, d in bus.take() if d}
    assert ids == {"Alice phone": "notify.mesh_alice", "Alice": f"notify.mesh_alice_{BOB[:4]}"}


def test_a_pending_removal_ends_after_a_month(tmp_path: Path) -> None:
    clock = Clock()
    contacts, bus = devices(tmp_path, clock=clock)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.update(mesh_of(mesh=mesh))
    clock.now += 31 * 86_400
    contacts.update(mesh)  # still gone
    bus.take()
    contacts.announce_all(birth=True)
    assert bus.take() == []


def test_equal_names_get_distinct_slugs(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    contacts.update(mesh_of(node(ALICE, "Alice"), node(BOB, "alice")))
    ids = {d["cmps"]["send"]["default_entity_id"] for _, d in bus.take()}
    assert ids == {"notify.mesh_alice", f"notify.mesh_alice_{BOB[:4]}"}


def test_a_name_without_letters_uses_the_key(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    contacts.update(mesh_of(node(ALICE, "🙂")))
    ((_, document),) = bus.take()
    assert document["cmps"]["send"]["default_entity_id"] == f"notify.mesh_{ALICE[:8]}"


def test_a_position_adds_the_tracker(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(HILL, "Hilltop", NodeType.REPEATER))
    contacts.update(mesh)
    assert "position" not in bus.take()[0][1]["cmps"]
    positioned = node(HILL, "Hilltop", NodeType.REPEATER, position=Position(52.1, 9.2))
    contacts.update(mesh_of(positioned, mesh=mesh))
    assert "position" in bus.take()[0][1]["cmps"]


def test_a_component_stays_when_its_data_goes(tmp_path: Path) -> None:
    """Never removed alone: Home Assistant would clear the whole device for a disabled one, and
    the gateway would take that for a deletion by the user."""
    contacts, bus = devices(tmp_path)
    positioned = node(ALICE, "Alice", position=Position(52.1, 9.2))
    contacts.update(mesh_of(positioned))
    bus.take()
    sensor = mesh_of(node(ALICE, "Alice", NodeType.SENSOR))  # flashed as a sensor, no position
    restarted, bus = devices(tmp_path)
    restarted.update(sensor)
    ((_, document),) = bus.take()
    assert {"send", "message", "position"} <= set(document["cmps"])
    assert document["cmps"]["send"]["command_topic"].endswith("/send")
    assert document["dev"]["mdl"] == "Sensor"
    assert all(set(c) != {"p"} for c in document["cmps"].values())


# --- limit ---------------------------------------------------------------------------------------


def test_the_household_comes_first_when_there_are_too_many(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path, limit=2, protected=frozenset({CAROL}))
    favourite = node(BOB, "Bob", flags=ContactFlag.FAVOURITE)
    mesh = mesh_of(node(ALICE, "Alice"), favourite, node(HILL, "Hill", NodeType.REPEATER),
                   node(CAROL, "Carol", last_advert=int(NOW)))  # fmt: skip
    contacts.update(mesh)
    assert {t for t, _ in bus.take()} == {topic(CAROL), topic(BOB)}


def test_a_protected_contact_gets_its_device_with_its_first_advert(tmp_path: Path) -> None:
    """The radio profile puts a protected contact on the radio before it was heard: a key and a
    configured name, nothing to show yet."""
    contacts, bus = devices(tmp_path, protected=frozenset({CAROL}))
    mesh = mesh_of(node(ALICE, "Alice"), node(CAROL, "Carol"))
    contacts.update(mesh)
    assert [t for t, _ in bus.take()] == [topic(ALICE)]
    contacts.update(mesh_of(node(ALICE, "Alice"), node(CAROL, "Carol", last_advert=int(NOW)),
                            mesh=mesh))  # fmt: skip
    assert [t for t, _ in bus.take()] == [topic(CAROL)]
    contacts.update(mesh_of(node(ALICE, "Alice"), node(CAROL, "Carol"), mesh=mesh))
    assert bus.take() == []  # once it has a device, it keeps it


def test_a_protected_contact_put_back_on_the_radio_gets_its_device_back(tmp_path: Path) -> None:
    """Deleted from the radio meanwhile (the phone app): the profile puts it back without an
    advert, and its device comes back at the same connect, with its entity IDs."""
    contacts, bus = devices(tmp_path, protected=frozenset({CAROL}))
    mesh = mesh_of(node(CAROL, "Carol", last_advert=int(NOW)))
    contacts.update(mesh)
    ((_, document),) = bus.take()
    contacts.update(mesh_of(mesh=mesh))  # the first table read of the connect: gone
    bus.take()
    contacts.update(mesh_of(node(CAROL, "sender-d4d4"), mesh=mesh))  # put back by the profile
    ((topic_back, again),) = bus.take()
    assert topic_back == topic(CAROL)

    def entity_ids(doc: dict[str, Any]) -> set[str]:
        return {c["default_entity_id"] for c in doc["cmps"].values() if "default_entity_id" in c}

    assert entity_ids(again) == entity_ids(document)
    assert all("mesh_carol" in e for e in entity_ids(again))


def test_a_device_keeps_its_place_and_the_recently_heard_come_next(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path, limit=2)
    mesh = mesh_of(node(ALICE, "Alice"), node(HILL, "Hill", NodeType.REPEATER))
    contacts.update(mesh)
    bus.take()
    mesh.contact_heard(BOB, Heard(NOW, "advert"))  # not yet a contact: no effect
    mesh = mesh_of(node(ALICE, "Alice"), node(HILL, "Hill", NodeType.REPEATER),
                   node(BOB, "Bob", flags=ContactFlag.FAVOURITE), mesh=mesh)  # fmt: skip
    contacts.update(mesh)
    assert bus.take() == []  # no slot free: Bob waits
    contacts.update(mesh_of(node(ALICE, "Alice"), node(BOB, "Bob", flags=ContactFlag.FAVOURITE),
                            node(CAROL, "Carol"), mesh=mesh))  # fmt: skip
    sent = bus.take()  # Hill left: Bob before Carol
    assert [t for t, _ in sent] == [topic(HILL), topic(HILL), topic(BOB)]


def test_no_devices_when_the_limit_is_zero(tmp_path: Path) -> None:
    contacts, _ = devices(tmp_path)
    contacts.update(mesh_of(node(ALICE, "Alice")))
    none, bus = devices(tmp_path, limit=0)
    assert not none.watching
    none.update(mesh_of(node(ALICE, "Alice")))
    assert [d for _, d in bus.take()][1:] == [None]  # removed


# --- deleted in Home Assistant --------------------------------------------------------------------


def test_a_device_deleted_in_home_assistant_stays_hidden(tmp_path: Path) -> None:
    clock = Clock()
    contacts, bus = devices(tmp_path, clock=clock)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    bus.take()
    contacts.discovery_message(topic(ALICE), b"{}", retained=False)  # its own document
    contacts.discovery_message(topic(ALICE), b"", retained=True)  # a left-over
    contacts.discovery_message(topic(ALICE), b"", retained=False)  # the user deleted it
    ((event_topic, event),) = bus.take()
    assert event_topic == "meshcore/radio1/contact"
    assert event == {"event_type": "hidden", "key": ALICE, "prefix": ALICE[:12], "name": "Alice",
                     "type": "chat", "request": None, "id": None, "reason": None}  # fmt: skip
    contacts.update(mesh)
    contacts.announce_all()
    assert bus.take() == []
    restarted, bus = devices(tmp_path, clock=clock)
    restarted.update(mesh)
    assert bus.take() == []


def test_its_own_late_echo_is_no_deletion(tmp_path: Path) -> None:
    """After a broker outage the queued removal arrives late, also after the contact came back:
    it is counted, not timed."""
    clock = Clock()
    contacts, bus = devices(tmp_path, clock=clock)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.update(mesh_of(mesh=mesh))
    contacts.update(mesh_of(node(ALICE, "Alice"), mesh=mesh))
    bus.take()
    clock.now += 60 * ECHO_S
    contacts.discovery_message(topic(ALICE), b"", retained=False)
    assert bus.take() == []
    contacts.discovery_message(topic(ALICE), b"", retained=False)
    assert bus.take()[0][1]["event_type"] == "hidden"


def test_its_own_removal_is_no_deletion(tmp_path: Path) -> None:
    clock = Clock()
    contacts, bus = devices(tmp_path, clock=clock)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.update(mesh_of(mesh=mesh))
    contacts.update(mesh_of(node(ALICE, "Alice"), mesh=mesh))  # back at once
    bus.take()
    contacts.discovery_message(topic(ALICE), b"", retained=False)  # the echo of the removal
    assert bus.take() == []
    clock.now += ECHO_S
    contacts.discovery_message(topic(ALICE), b"", retained=False)
    assert bus.take()[0][1]["event_type"] == "hidden"


def test_hidden_contacts_can_be_shown_again(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.discovery_message(topic(ALICE), b"", retained=False)
    bus.take()
    contacts.show_hidden()
    (event_topic, event), (device_topic, _) = bus.take()
    assert (event_topic, event["event_type"]) == ("meshcore/radio1/contact", "shown")
    assert device_topic == topic(ALICE)


def test_a_hidden_contact_that_returns_to_the_table_is_shown(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.discovery_message(topic(ALICE), b"", retained=False)
    contacts.update(mesh_of(mesh=mesh))
    bus.take()
    contacts.update(mesh_of(node(ALICE, "Alice"), mesh=mesh))
    assert [t for t, _ in bus.take()] == [topic(ALICE)]


def test_an_empty_document_of_a_device_it_does_not_know_is_ignored(tmp_path: Path) -> None:
    contacts, bus = devices(tmp_path)
    contacts.update(mesh_of(node(ALICE, "Alice")))
    bus.take()
    contacts.discovery_message(topic(BOB), b"", retained=False)
    contacts.discovery_message(f"homeassistant/device/{GATEWAY}/config", b"", retained=False)
    assert bus.take() == []
    assert set(ContactDeviceRecords(JsonStateFile(tmp_path / "state.json")).announced) == {ALICE}


def test_showing_hidden_contacts_before_the_first_table_read(tmp_path: Path) -> None:
    contacts, _ = devices(tmp_path)
    contacts.update(mesh_of(node(ALICE, "Alice")))
    contacts.discovery_message(topic(ALICE), b"", retained=False)
    restarted, bus = devices(tmp_path)
    restarted.show_hidden()
    assert [e["event_type"] for _, e in bus.take()] == ["shown"]
    assert ContactDeviceRecords(JsonStateFile(tmp_path / "state.json")).hidden == set()


def test_the_copies_home_assistant_sends_while_it_removes_are_no_deletion(tmp_path: Path) -> None:
    """Removing a device with an entity disabled by hand, Home Assistant clears the topic too."""
    contacts, bus = devices(tmp_path)
    mesh = mesh_of(node(ALICE, "Alice"))
    contacts.update(mesh)
    contacts.update(mesh_of(mesh=mesh))
    contacts.update(mesh_of(node(ALICE, "Alice"), mesh=mesh))  # back at once
    bus.take()
    for _ in range(3):  # its own echo, then Home Assistant's copies
        contacts.discovery_message(topic(ALICE), b"", retained=False)
    assert bus.take() == []
