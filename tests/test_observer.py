"""The observer through the bridge, with the emulated radio: the providers' documents, events and
the history (ADR 0014-0016)."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.bridge import protected_keys
from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.telemetry import Answer
from meshcore_home_gateway.history import History
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import Push
from meshcore_home_gateway.radio.session import AdvertHeard, ContactsRead, PathReset
from meshcore_home_gateway.state import JsonStateFile, RetainedKeys
from meshcore_home_gateway.views import iso
from tests.radio_emulator import LPP_FULL, EmulatedContact, EmulatedRadio
from tests.test_bridge import ALICE, BOB, NOW, FakeBus, make_config, running, scenario

ROOT = "meshcore/radio1"
TOPICS = Topics("meshcore", "radio1")
CAROL = EmulatedContact(bytes.fromhex("c3" * 32), "Carol", node_type=2, latitude_e6=53_550_000,
                        longitude_e6=9_990_000, last_advert=int(NOW) - 5)  # fmt: skip


def retained(bus: FakeBus, topic: str) -> list[bytes]:
    return [p for t, p, keep in bus.published if t == topic and keep]


def last(bus: FakeBus, topic: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(retained(bus, topic)[-1])
    return document


def advert_log(contact: EmulatedContact, hops: int, snr_q: int = 22, rssi: int = -95) -> bytes:
    """LOG_RX_DATA with the flooded advert the radio heard (src/Packet.cpp:65-85)."""
    app = bytes([0x80 | contact.node_type]) + contact.name.encode()
    payload = contact.public_key + struct.pack("<I", contact.last_advert) + bytes(64) + app
    raw = bytes([0x11, hops]) + bytes(range(hops)) + payload
    return bytes([Push.LOG_RX_DATA, snr_q & 0xFF, rssi & 0xFF]) + raw


def test_contacts_are_published_one_document_each_with_an_index(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            index = last(bus, f"{ROOT}/contacts")
            assert index["total"] == 2
            assert index["max"] == 100
            assert {c["key"] for c in index["contacts"]} == {
                ALICE.public_key.hex(),
                BOB.public_key.hex(),
            }
            alice = last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")
            assert (alice["name"], alice["type"], alice["protected"]) == ("Alice", "chat", True)
            assert alice["path_known"] is False
            bob = last(bus, f"{ROOT}/contacts/{BOB.public_key.hex()}")
            assert (bob["path_hops"], bob["protected"]) == (2, True)
            stats = last(bus, f"{ROOT}/stats")
            assert (stats["uptime_s"], stats["noise_floor_dbm"]) == (3600, -118)
            assert stats["duty_cycle_percent"] == 50.0  # factor 1: the factory default

    scenario(main)


def test_a_removed_contact_clears_its_document_and_is_announced(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.overwrite(BOB)  # the radio dropped it, and reads the table again
            event = await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "removed")
            assert (event["key"], event["name"], event["type"]) == (
                BOB.public_key.hex(), "Bob", "chat",
            )  # fmt: skip
            assert len(bus.payloads(f"{ROOT}/contact")) == 1  # the start announced nothing
            topic = f"{ROOT}/contacts/{BOB.public_key.hex()}"
            await asyncio.sleep(0.05)
            assert retained(bus, topic)[-1] == b""  # the retained document is cleared
            assert last(bus, f"{ROOT}/contacts")["total"] == 1

    scenario(main)


def test_contacts_gone_while_the_gateway_was_stopped_are_cleared(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    RetainedKeys(store, "contacts").replace({ALICE.public_key.hex(), "dd" * 32})
    radio = EmulatedRadio(contacts=[ALICE])

    def observer(bus: FakeBus) -> Observer:
        return Observer(bus, TOPICS, retained=RetainedKeys(store, "contacts"),
                        wall_clock=lambda: NOW)  # fmt: skip

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, observer=observer) as (_, bus, _n):
            await bus.wait_for(f"{ROOT}/contacts", lambda d: d["total"] == 1)
            assert retained(bus, f"{ROOT}/contacts/{'dd' * 32}") == [b""]
            assert retained(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")[-1] != b""
            events = bus.payloads(f"{ROOT}/contact")  # only what changed while stopped
            assert events == [{"event_type": "removed", "key": "dd" * 32, "prefix": "dd" * 6,
                               "name": None, "type": None, "request": None, "id": None,
                               "reason": None}]  # fmt: skip

    scenario(main)
    assert RetainedKeys(store, "contacts").keys == {ALICE.public_key.hex()}


def test_a_new_node_is_heard_with_its_signal_but_without_its_position(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.emit(advert_log(CAROL, hops=3))
            radio.advertise(CAROL)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert (event["key"], event["name"], event["type"]) == (
                CAROL.public_key.hex(), "Carol", "repeater",
            )  # fmt: skip
            assert (event["hops"], event["snr_db"], event["rssi_dbm"]) == (3, 5.5, -95)
            assert event["look_alike"] is False
            nodes = await bus.wait_for(f"{ROOT}/nodes", lambda d: d["total"] == 1)
            assert "latitude" not in json.dumps(nodes)  # strangers' positions are not kept
            radio.advertise(CAROL)  # heard again: no second event
            await asyncio.sleep(0.1)
            assert len(bus.payloads(f"{ROOT}/node")) == 1

    scenario(main)


def test_contacts_heard_by_advert_and_message_and_the_history(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            key = ALICE.public_key.hex()
            radio.emit(advert_log(ALICE, hops=1))
            radio.advertise(ALICE)
            doc = await bus.wait_for(f"{ROOT}/contacts/{key}", lambda d: d["heard_via"] == "advert")
            assert doc["hops"] == 1
            radio.queue_direct_message(ALICE.public_key[:6], "hi there", int(NOW) - 10)
            doc = await bus.wait_for(
                f"{ROOT}/contacts/{key}", lambda d: d["heard_via"] == "message"
            )
            radio.queue_channel_message(0, "Carol: on my way", int(NOW) - 5)
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 2)
            channel, direct = history["messages"]
            assert (direct["direction"], direct["text"], direct["contact_key"]) == (
                "in", "hi there", key,
            )  # fmt: skip
            assert (channel["kind"], channel["name"], channel["text"]) == (
                "channel", "Carol", "on my way",
            )  # fmt: skip
            bus.send("alice", "Back soon")
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 3)
            out = history["messages"][0]
            assert (out["direction"], out["state"], out["name"]) == ("out", "confirmed", "Alice")

    scenario(main)


def test_the_history_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    config = make_config()

    def observer(bus: FakeBus) -> Observer:
        history = History.open(path)
        assert history is not None
        observers.append(history)
        return Observer(bus, TOPICS, history=history, protected=protected_keys(config),
                        wall_clock=lambda: NOW)  # fmt: skip

    observers: list[History] = []

    async def first() -> None:
        radio = EmulatedRadio(contacts=[ALICE, CAROL])  # one per event loop
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            radio.queue_channel_message(0, "Dave: remember me", int(NOW) - 5)
            await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)

    async def second() -> None:
        radio = EmulatedRadio(contacts=[ALICE, CAROL])
        async with running(config, radio, tmp_path, observer=observer) as (_, bus, _notes):
            history = await bus.wait_for(f"{ROOT}/history", lambda d: d["total"] == 1)
            assert history["messages"][0]["text"] == "remember me"
            carol = last(bus, f"{ROOT}/contacts/{CAROL.public_key.hex()}")
            assert (carol["latitude"], carol["position_source"]) == (53.55, "advert")

    scenario(first)
    scenario(second)
    for history in observers:
        history.close()
    stored = History.open(path)
    assert stored is not None
    assert list(stored.last_fixes()) == [CAROL.public_key.hex()]  # stored once, not per run
    assert len(stored.trace(CAROL.public_key.hex(), 0)) == 1
    stored.close()


def test_a_failing_observer_does_not_stop_messages(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    def broken(*_: object) -> None:
        raise RuntimeError("bug in a view")

    monkeypatch.setattr(Observer, "message", broken)
    monkeypatch.setattr(Observer, "delivery", broken)
    caplog.set_level(logging.ERROR)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "status", int(NOW) - 10)
            await bus.wait_for(f"{ROOT}/command", lambda e: e["event_type"] == "status")
            bus.send("alice", "still works")
            await bus.wait_for(f"{ROOT}/delivery", lambda e: e["event_type"] == "confirmed")

    scenario(main)
    assert "observer failed" in caplog.text


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def contact_record(contact: EmulatedContact) -> codec.ContactRecord:
    decoded = codec.decode(contact.record())
    assert isinstance(decoded, codec.ContactRecord)
    return decoded


def test_heard_nodes_come_back_after_a_restart_and_expire(tmp_path: Path) -> None:
    path, clock = tmp_path / "history.sqlite3", Clock(NOW)
    history = History.open(path)
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=clock)
    observer.radio_event(ContactsRead({}, NOW))
    carol = contact_record(CAROL)
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    history.close()

    history = History.open(path)
    assert history is not None
    clock.now = NOW + 3600
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=clock)
    observer.radio_event(ContactsRead({}, clock.now))
    nodes = last(bus, f"{ROOT}/nodes")
    assert [(n["name"], n["first_heard_at"]) for n in nodes["nodes"]] == [("Carol", iso(NOW))]
    clock.now = NOW + 8 * 86_400  # beyond the memory of heard nodes, and a purge is due
    observer.tick()
    assert last(bus, f"{ROOT}/nodes")["total"] == 0
    assert [row.name for row in history.nodes()] == ["Carol"]  # the history keeps 30 days
    clock.now = NOW + 31 * 86_400
    observer.tick()  # the next purge is due an hour after the first
    assert history.nodes() == []
    history.close()


def test_documents_are_published_again_when_the_broker_reconnects() -> None:
    """The MQTT adapter drops retained messages while the broker is away: a reconnect sends the
    current documents again, and the clearing of a removed contact's topic too."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    alice, bob = contact_record(ALICE), contact_record(BOB)
    keys = (ALICE.public_key.hex(), BOB.public_key.hex())
    observer.radio_event(ContactsRead(dict(zip(keys, (alice, bob), strict=True)), NOW))
    observer.radio_event(ContactsRead({keys[0]: alice}, NOW + 1))  # Bob was removed
    current = {t: p for t, p, keep in bus.published if keep}
    bus.published.clear()
    observer.publish_all()
    again = {t: p for t, p, keep in bus.published if keep}
    assert again == current | {f"{ROOT}/history": again[f"{ROOT}/history"]}
    assert again[f"{ROOT}/contacts/{keys[1]}"] == b""  # cleared again
    assert again[f"{ROOT}/contacts/{keys[0]}"] != b""


def test_a_new_advert_of_a_contact_counts_for_the_contact() -> None:
    """Firmware 1.11 also sends NEW_ADVERT for a contact it has just added."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    alice = contact_record(ALICE)
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW))
    observer.radio_event(AdvertHeard(codec.Advert(alice.public_key, True, alice), NOW + 1))
    assert last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")["heard_via"] == "advert"
    assert not bus.payloads(f"{ROOT}/node")
    assert last(bus, f"{ROOT}/nodes")["total"] == 0


def test_node_listeners_learn_of_every_change_and_cannot_stop_the_documents(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Home Assistant's select of the heard nodes follows them (mqtt/manage_entities.py): at
    once after a table read, else at a bounded rate."""
    bus = FakeBus()
    clock = [NOW]
    observer = Observer(bus, TOPICS, wall_clock=lambda: clock[0])
    calls: list[bool] = []
    observer.on_nodes.append(calls.append)

    def broken(_read: bool) -> None:
        raise RuntimeError("bug")

    observer.on_nodes.append(broken)
    alice, bob, carol = contact_record(ALICE), contact_record(BOB), contact_record(CAROL)
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW))
    assert calls
    assert set(calls) == {True}
    calls.clear()
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW + 1))
    assert calls == [False]
    assert last(bus, f"{ROOT}/nodes")["total"] == 1  # published although a listener failed
    # the radio reads its table after every advert: unchanged, it is no reason to hurry
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW + 3))
    observer.radio_event(ContactsRead({k.public_key.hex(): r for k, r in
                                       ((ALICE, alice), (BOB, bob))}, NOW + 4))  # fmt: skip
    assert calls == [False, False, True]  # Bob joined
    clock[0] = NOW + 8 * 86_400  # Carol is forgotten
    observer.tick()
    assert calls == [False, False, True, False]
    assert "node listener failed" in caplog.text


def test_the_switch_the_worker_applies_is_published() -> None:
    """Untested firmware takes no request (ADR 0020's firmware gate): `manage` and
    `removable` say so after the first table read; before it, the last run's documents stay."""
    bus = FakeBus()
    observer = Observer(bus, TOPICS, manage=True, wall_clock=lambda: NOW)
    observer.set_manage(False)
    assert bus.published == []
    alice = contact_record(ALICE)
    observer.radio_event(ContactsRead({ALICE.public_key.hex(): alice}, NOW))
    assert last(bus, f"{ROOT}/contacts")["manage"] is False
    observer.set_manage(True)
    assert last(bus, f"{ROOT}/contacts")["manage"] is True
    assert last(bus, f"{ROOT}/contacts/{ALICE.public_key.hex()}")["removable"] is True
    count = len(bus.published)
    observer.set_manage(True)
    assert len(bus.published) == count


def test_the_history_keeps_the_record_of_the_newest_advert(tmp_path: Path) -> None:
    """A replayed older advert is heard, but its name does not replace the newer one's, not even
    after a restart."""
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead({}, NOW))
    for name, advert in (("Carol", int(NOW) - 5), ("Mallory", int(NOW) - 500)):
        record = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), name, last_advert=advert))
        observer.radio_event(AdvertHeard(codec.Advert(record.public_key, True, record), NOW))
    assert observer.mesh.nodes["c3" * 32].node.name == "Carol"
    assert [row.name for row in history.nodes()] == ["Carol"]
    history.close()
    history = History.open(path)  # a restart: the advert time comes back with the record
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW + 60)
    observer.radio_event(ContactsRead({}, NOW + 60))
    record = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), "Mallory", node_type=1,
                                            last_advert=int(NOW) - 500))  # fmt: skip
    observer.radio_event(AdvertHeard(codec.Advert(record.public_key, True, record), NOW + 60))
    assert observer.mesh.nodes["c3" * 32].node.name == "Carol"
    assert [(row.name, row.last_advert) for row in history.nodes()] == [("Carol", int(NOW) - 5)]
    history.close()


def test_a_replay_heard_before_the_first_table_read_does_not_win(tmp_path: Path) -> None:
    """At a start, the radio's pushes can come before the first table read, while the nodes from
    the history are not in the view yet: the newer advert's record stays, in the history too, and
    the node keeps the time it was heard in this run."""
    path = tmp_path / "history.sqlite3"
    history = History.open(path)
    assert history is not None
    carol = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), "Carol", node_type=2,
                                           last_advert=int(NOW) - 5))  # fmt: skip
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW)
    observer.radio_event(ContactsRead({}, NOW))
    observer.radio_event(AdvertHeard(codec.Advert(carol.public_key, True, carol), NOW))
    history.close()
    history = History.open(path)
    assert history is not None
    observer = Observer(FakeBus(), TOPICS, history=history, wall_clock=lambda: NOW + 30)
    replay = contact_record(EmulatedContact(bytes.fromhex("c3" * 32), "Mallory", node_type=1,
                                            last_advert=int(NOW) - 500))  # fmt: skip
    observer.radio_event(AdvertHeard(codec.Advert(replay.public_key, True, replay), NOW + 30))
    observer.radio_event(ContactsRead({}, NOW + 30))  # the first table read of this run
    tracked = observer.mesh.nodes["c3" * 32]
    assert (tracked.node.name, tracked.node.node_type) == ("Carol", 2)
    assert tracked.heard is not None
    assert tracked.heard.at == NOW + 30
    assert [(row.name, row.last_advert) for row in history.nodes()] == [("Carol", int(NOW) - 5)]
    history.close()


def answer(at: float, data: bytes = LPP_FULL) -> Answer:
    return Answer(lpp.decode(data), at, False, 0, False)


def test_telemetry_answers_publish_the_position_and_store_only_changes(tmp_path: Path) -> None:
    """ADR 0018: the position goes to the contact's document (and its tracker); the history
    gets a position row when it moved and a node row at most hourly from answers alone."""
    history = History.open(tmp_path / "history.sqlite3")
    assert history is not None
    bus = FakeBus()
    observer = Observer(bus, TOPICS, history=history, wall_clock=lambda: NOW)
    tracker_updates: list[bool] = []
    observer.on_contacts.append(lambda _mesh: tracker_updates.append(True))
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead({key: contact_record(ALICE)}, NOW))
    tracker_updates.clear()
    observer.telemetry(key, answer(NOW + 10))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["latitude"], document["longitude"], document["altitude_m"]) == (
        50.12345, 8.56775, 120.5,
    )  # fmt: skip
    assert (document["position_source"], document["position_accuracy_m"]) == ("telemetry", 15.0)
    assert document["heard_via"] == "telemetry"
    assert tracker_updates == [True]  # its device gets a tracker at once
    observer.telemetry(key, answer(NOW + 100))  # resting: nothing new but the time received
    assert tracker_updates == [True]
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert document["position_at"] == iso(NOW + 10)
    assert document["position_received_at"] == iso(NOW + 100)
    assert document["heard_at"] == iso(NOW + 10)  # moves hourly from answers alone
    assert len(history.trace(key, 0)) == 1
    assert [row.heard.at for row in history.nodes() if row.key == key] == [NOW + 10]
    observer.telemetry(key, answer(NOW + 3610))
    assert [row.heard.at for row in history.nodes() if row.key == key] == [NOW + 3610]
    observer.telemetry("e5" * 32, answer(NOW + 3620))  # not a contact (any more): nothing
    assert not retained(bus, f"{ROOT}/contacts/{'e5' * 32}")
    history.close()


def test_an_answer_without_position_keeps_the_last_one() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    key = ALICE.public_key.hex()
    observer.radio_event(ContactsRead({key: contact_record(ALICE)}, NOW))
    observer.telemetry(key, answer(NOW + 10))
    observer.telemetry(key, answer(NOW + 20, bytes.fromhex("0174018b016700d9")))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["latitude"], document["position_received_at"]) == (50.12345, iso(NOW + 10))


def test_a_path_reset_is_published_before_the_next_table_read() -> None:
    bus = FakeBus()
    observer = Observer(bus, TOPICS, wall_clock=lambda: NOW)
    key = BOB.public_key.hex()
    observer.radio_event(ContactsRead({key: contact_record(BOB)}, NOW))
    assert last(bus, f"{ROOT}/contacts/{key}")["path_known"] is True
    observer.radio_event(PathReset(BOB.public_key))
    document = last(bus, f"{ROOT}/contacts/{key}")
    assert (document["path_known"], document["path_hops"]) == (False, None)
    published = len(bus.published)
    observer.radio_event(PathReset(BOB.public_key))  # nothing changed
    observer.radio_event(PathReset(ALICE.public_key))  # no contact
    assert len(bus.published) == published


def test_the_radios_own_time_on_air_needs_two_readings(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, _bus, _notes):
            assert bridge.observer.radio_tx_1h_s() is None  # one reading, at the connect

    scenario(main)
