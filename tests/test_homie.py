"""The Homie adapter (EXT-011): the contacts as the nodes of one Homie 4 device, from the
documents, on a connection of its own with the device's last will."""

from __future__ import annotations

import asyncio
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion

from meshcore_home_gateway.config import MqttConfig
from meshcore_home_gateway.core.telemetry import STATES
from meshcore_home_gateway.mqtt import homie, homie_client
from meshcore_home_gateway.mqtt.homie import HOLD_S, SETTLE_S, HomieDevice, device_id
from meshcore_home_gateway.mqtt.homie_client import HomieConnection
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import ContactsRead, SendRequest
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import default_session_factory
from meshcore_home_gateway.state import CommandLedger, JsonStateFile
from meshcore_home_gateway.wiring import gateway
from tests.bridge_harness import FakeBus, make_config
from tests.conftest import Broker
from tests.radio_emulator import EmulatedRadio

TOPICS = Topics("meshcore", "radio1")
BASE = "homie/meshcore-radio1"
GATEWAY = f"{BASE}/gateway"
ALICE, BOB, CAROL = "a1" * 32, "b2" * 32, "c3" * 32
HOMIE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?")


def document(key: str, name: str, **fields: Any) -> dict[str, Any]:
    """A contact's document with the keys the adapter reads."""
    return {
        "key": key,
        "name": name,
        "type": "chat",
        "protected": False,
        "favourite": False,
        "heard_at": "2026-11-04T10:00:00+00:00",
        "battery_v": 3.91,
        "latitude": 47.5,
        "longitude": 8.25,
        "telemetry_state": "answering",
    } | fields


@dataclass
class Timer:
    delay_s: float
    callback: Callable[[], None]
    cancelled: bool = False

    def cancel(self) -> None:
        self.cancelled = True


class Timers:
    """The event loop's timers, fired when the test says."""

    def __init__(self) -> None:
        self.pending: list[Timer] = []

    def call_later(self, delay_s: float, callback: Callable[[], None]) -> Timer:
        timer = Timer(delay_s, callback)
        self.pending.append(timer)
        return timer

    def delays(self) -> list[float]:
        return [t.delay_s for t in self.pending if not t.cancelled]

    def fire(self, delay_s: float | None = None) -> None:
        """Those due after `delay_s` (all: None), and those they start."""
        while due := [t for t in self.pending if not t.cancelled and delay_s in (None, t.delay_s)]:
            for timer in due:
                self.pending.remove(timer)
                if not timer.cancelled:
                    timer.callback()


class Recorder:
    def __init__(self, limit: int = 50) -> None:
        self.published: list[tuple[str, bytes, bool]] = []
        self.sent: list[SendRequest] = []
        self.timers = Timers()
        self.device = HomieDevice(
            "radio1",
            "MeshCore radio",
            limit,
            TOPICS,
            publish=lambda t, p, r: self.published.append((t, p, r)),
            send=self.sent.append,
            call_later=self.timers.call_later,
        )

    def latest(self) -> dict[str, bytes]:
        """The retained value of each topic as the broker holds it (an empty one: none)."""
        held = {topic: payload for topic, payload, retained in self.published if retained}
        return {t: p for t, p in held.items() if p}

    def take(self) -> list[tuple[str, bytes, bool]]:
        published, self.published = self.published, []
        return published


def contact(device: HomieDevice, key: str, name: str, **fields: Any) -> None:
    device.document(TOPICS.contact(key), document(key, name, **fields))


def radio(device: HomieDevice, state: str) -> None:
    device.document(TOPICS.state, {"radio": state, "queue": 0})


def started(recorder: Recorder) -> None:
    """A start as the gateway runs it: the connection, then the radio's first contact table (here
    without contacts), and the device announced after it."""
    recorder.device.connected()
    recorder.device.contacts_read()
    recorder.timers.fire()


def nodes(*keys: str) -> bytes:
    """`$nodes`: the gateway's node first, then the contacts'."""
    return ",".join(["gateway", *keys]).encode()


def gateway_node(radio: bytes = b"disconnected") -> list[tuple[str, bytes, bool]]:
    """The messages of the gateway's node, as an announcement publishes them."""
    return [
        (f"{GATEWAY}/$name", b"Gateway", True),
        (f"{GATEWAY}/$type", b"gateway", True),
        (f"{GATEWAY}/$properties", b"radio", True),
        (f"{GATEWAY}/radio/$name", b"Radio", True),
        (f"{GATEWAY}/radio/$datatype", b"enum", True),
        (f"{GATEWAY}/radio/$format", b"connected,disconnected", True),
        (f"{GATEWAY}/radio", radio, True),
    ]


def states(published: list[tuple[str, bytes, bool]]) -> list[bytes]:
    return [payload for topic, payload, _ in published if topic == f"{BASE}/$state"]


@pytest.mark.parametrize(
    ("node_id", "expected"),
    [("radio1", "meshcore-radio1"), ("home_2", "meshcore-home-2"), ("a-b_c", "meshcore-a-b-c")],
)
def test_the_device_id_is_a_homie_id_of_the_node_id(node_id: str, expected: str) -> None:
    assert device_id(node_id) == expected
    assert HOMIE_ID.fullmatch(expected)


def test_the_device_announces_itself_and_its_state_follows_the_radio() -> None:
    """Homie 4.0.0: `init` first, which a start keeps until the radio's first contact table has
    come; then every device attribute (`$extensions` empty) and the state: `alert` while the
    radio is not connected, `ready` once it is. Everything retained."""
    recorder = Recorder()
    recorder.device.connected()
    assert recorder.take() == [(f"{BASE}/$state", b"init", True)]
    recorder.device.contacts_read()  # a radio without contacts
    assert recorder.take() == []
    assert recorder.timers.delays() == [SETTLE_S]  # the table's documents come first
    recorder.timers.fire()
    assert recorder.take() == [
        (f"{BASE}/$homie", b"4.0.0", True),
        (f"{BASE}/$name", b"MeshCore radio", True),
        (f"{BASE}/$extensions", b"", True),
        (f"{BASE}/$implementation", b"meshcore-home-gateway", True),
        (f"{BASE}/$nodes", nodes(), True),
        *gateway_node(),
        (f"{BASE}/$state", b"alert", True),
    ]
    radio(recorder.device, "connected")
    assert recorder.take() == [
        (f"{GATEWAY}/radio", b"connected", True),
        (f"{BASE}/$state", b"ready", True),
    ]
    radio(recorder.device, "connected")  # unchanged: nothing
    assert recorder.take() == []
    radio(recorder.device, "disconnected")
    assert recorder.take() == [
        (f"{GATEWAY}/radio", b"disconnected", True),
        (f"{BASE}/$state", b"alert", True),
    ]


def test_a_companion_is_a_node_with_its_values_and_a_settable_send() -> None:
    recorder = Recorder()
    radio(recorder.device, "connected")
    recorder.device.contacts_read()
    contact(recorder.device, ALICE, "Alice")
    recorder.device.connected()  # after the first table: at once
    node = f"{BASE}/{ALICE}"
    assert recorder.latest() == {t: p for t, p, _ in gateway_node(b"connected")} | {
        f"{BASE}/$state": b"ready",
        f"{BASE}/$homie": b"4.0.0",
        f"{BASE}/$name": b"MeshCore radio",
        f"{BASE}/$implementation": b"meshcore-home-gateway",
        f"{BASE}/$nodes": nodes(ALICE),
        f"{node}/$name": b"Alice",
        f"{node}/$type": b"chat",
        f"{node}/$properties": b"battery,position,last-heard,telemetry-state,send",
        f"{node}/battery/$name": b"Battery",
        f"{node}/battery/$datatype": b"float",
        f"{node}/battery/$unit": b"V",
        f"{node}/battery": b"3.91",
        f"{node}/position/$name": b"Position",
        f"{node}/position/$datatype": b"string",
        f"{node}/position": b"47.5,8.25",
        f"{node}/last-heard/$name": b"Last heard",
        f"{node}/last-heard/$datatype": b"string",
        f"{node}/last-heard": b"2026-11-04T10:00:00+00:00",
        f"{node}/telemetry-state/$name": b"Telemetry",
        f"{node}/telemetry-state/$datatype": b"enum",
        f"{node}/telemetry-state/$format": ",".join(STATES).encode(),
        f"{node}/telemetry-state": b"answering",
        f"{node}/send/$name": b"Send",
        f"{node}/send/$datatype": b"string",
        f"{node}/send/$settable": b"true",
        f"{node}/send/$retained": b"false",
    }
    assert all(HOMIE_ID.fullmatch(level) for level in node.split("/")[1:])


def test_a_sensor_has_no_send_and_an_unknown_value_is_cleared() -> None:
    recorder = Recorder()
    started(recorder)
    contact(recorder.device, BOB, "Bob", type="sensor", battery_v=None, latitude=None)
    recorder.timers.fire()
    node = f"{BASE}/{BOB}"
    held = recorder.latest()
    assert held[f"{node}/$properties"] == b"battery,position,last-heard,telemetry-state"
    assert not any(t.startswith(f"{node}/send") for t in held)
    assert (f"{node}/battery", b"", True) in recorder.published  # no value on the broker
    assert f"{node}/position" not in held
    recorder.take()
    contact(recorder.device, BOB, "Bob", type="sensor", battery_v=4, latitude=None)
    assert recorder.take() == [(f"{node}/battery", b"4.0", True)]  # only what changed
    contact(recorder.device, BOB, "Bob", type="sensor", battery_v=4, longitude=None)
    assert recorder.take() == []  # a position needs both


def test_a_change_of_the_nodes_goes_out_between_init_and_ready() -> None:
    """A new node, or one whose contact left the table: the device again, between `init` and
    the state, and the topics of a node that is gone are cleared."""
    recorder = Recorder()
    radio(recorder.device, "connected")
    started(recorder)
    recorder.take()
    contact(recorder.device, ALICE, "Alice")
    recorder.timers.fire()
    published = recorder.take()
    assert published[0] == (f"{BASE}/$state", b"init", True)
    assert published[1] == (f"{BASE}/$nodes", nodes(ALICE), True)
    assert published[-1] == (f"{BASE}/$state", b"ready", True)
    contact(recorder.device, BOB, "Bob")
    recorder.timers.fire()
    assert (f"{BASE}/$nodes", nodes(ALICE, BOB), True) in recorder.take()
    recorder.device.document(TOPICS.contact(ALICE), None)
    recorder.timers.fire()
    published = recorder.take()
    assert published[0] == (f"{BASE}/$state", b"init", True)
    assert published[1] == (f"{BASE}/$nodes", nodes(BOB), True)
    assert published[-1] == (f"{BASE}/$state", b"ready", True)
    cleared = {t for t, p, _ in published if t.startswith(f"{BASE}/{ALICE}/")}
    assert len(cleared) == 21  # $name, $type, $properties, 14 attributes, 4 values
    assert all(p == b"" for t, p, _ in published if t in cleared)
    assert not any(t.startswith(f"{BASE}/{ALICE}") for t in recorder.latest())


FIVE = [f"{n}{n}" * 32 for n in "12345"]


def test_a_start_announces_the_device_once_with_its_nodes() -> None:
    """The connection comes up before the radio's contacts (#97): the device stays `init` until
    the first table has come, then goes out once, with all its nodes, each topic once, instead of
    once more for every contact."""
    recorder = Recorder()
    device = recorder.device
    device.connected()
    radio(device, "connected")
    device.contacts_read()
    for n, key in enumerate(FIVE):
        contact(device, key, f"Contact {n}")
    assert recorder.published == [(f"{BASE}/$state", b"init", True)]
    recorder.timers.fire()
    assert states(recorder.published) == [b"init", b"ready"]
    announced = [p for t, p, _ in recorder.published if t == f"{BASE}/$nodes"]
    assert announced == [nodes(*FIVE)]
    topics = [t for t, _, _ in recorder.published if t != f"{BASE}/$state"]
    assert len(topics) == len(set(topics)) == 5 + 7 + 5 * 21  # attributes, gateway, contacts


def test_changes_of_the_nodes_go_out_together_after_a_while() -> None:
    """A table read changes the nodes once for all its contacts, and those that follow within
    SETTLE_S join it: one more announcement. The values of the nodes there go out at once."""
    recorder = Recorder()
    device = recorder.device
    radio(device, "connected")
    started(recorder)
    contact(device, ALICE, "Alice")
    recorder.timers.fire()
    recorder.take()
    device.contacts_read()
    contact(device, ALICE, "Alice", battery_v=4)
    contact(device, BOB, "Bob")
    contact(device, CAROL, "Carol")
    assert recorder.take() == [(f"{BASE}/{ALICE}/battery", b"4.0", True)]
    assert recorder.timers.delays() == [SETTLE_S]
    device.document(TOPICS.contact(CAROL), None)  # gone again before the settle
    assert recorder.timers.delays() == [SETTLE_S]
    recorder.timers.fire()
    published = recorder.take()
    assert states(published) == [b"init", b"ready"]
    assert [p for t, p, _ in published if t == f"{BASE}/$nodes"] == [nodes(ALICE, BOB)]
    assert not any(t.startswith(f"{BASE}/{CAROL}") for t, _, _ in published)
    recorder.timers.fire()
    assert recorder.take() == []  # nothing changed since


def test_without_a_contact_table_the_device_goes_out_after_hold_s() -> None:
    """A radio that does not connect: the device is announced after HOLD_S as it is, `alert`,
    with the documents it has (those of the last run that a start publishes again); a reconnect
    meanwhile does not start the wait again. The first table then changes the nodes as any."""
    recorder = Recorder()
    device = recorder.device
    device.connected()
    radio(device, "disconnected")
    contact(device, ALICE, "Alice")
    device.disconnected()
    device.connected()
    assert states(recorder.take()) == [b"init", b"init"]
    assert recorder.timers.delays() == [HOLD_S]
    recorder.timers.fire()
    assert recorder.latest()[f"{BASE}/$nodes"] == nodes(ALICE)
    assert recorder.latest()[f"{BASE}/$state"] == b"alert"
    recorder.take()
    device.contacts_read()
    contact(device, BOB, "Bob")
    recorder.timers.fire()
    published = recorder.take()
    assert states(published) == [b"init", b"alert"]
    assert (f"{BASE}/$nodes", nodes(ALICE, BOB), True) in published

    away = Recorder()  # the wait ends while the connection is away: at the next one, at once
    away.device.connected()
    away.device.disconnected()
    away.take()
    away.timers.fire()
    assert away.take() == []
    away.device.connected()
    assert states(away.take()) == [b"init", b"alert"]


def test_a_node_whose_contact_left_takes_no_text_until_the_nodes_change() -> None:
    recorder = Recorder()
    device = recorder.device
    started(recorder)
    contact(device, ALICE, "Alice")
    recorder.timers.fire()
    device.document(TOPICS.contact(ALICE), None)
    device.message(f"{BASE}/{ALICE}/send/set", b"hello", False)
    assert recorder.sent == []


def test_other_documents_are_not_nodes() -> None:
    recorder = Recorder()
    started(recorder)
    recorder.take()
    for topic in (TOPICS.contacts, TOPICS.contact_received(ALICE), TOPICS.stats, TOPICS.nodes):
        recorder.device.document(topic, document(ALICE, "Alice"))
    recorder.timers.fire()
    assert recorder.take() == []


def test_at_most_the_limit_chosen_as_for_home_assistant_and_kept() -> None:
    """Protected contacts first, then favourites, companions and the most recently heard; a
    node stays while its contact does, also when one that would come first arrives."""
    recorder = Recorder(limit=2)
    device = recorder.device
    device.contacts_read()
    contact(device, ALICE, "Alice", heard_at="2026-11-04T09:00:00+00:00")
    contact(device, BOB, "Bob", heard_at="2026-11-04T10:00:00+00:00")
    contact(device, CAROL, "Carol", type="repeater", heard_at="2026-11-04T11:00:00+00:00")
    device.connected()
    assert recorder.latest()[f"{BASE}/$nodes"] == nodes(BOB, ALICE)
    contact(device, CAROL, "Carol", type="repeater", protected=True)
    recorder.timers.fire()
    assert recorder.latest()[f"{BASE}/$nodes"] == nodes(BOB, ALICE)
    device.document(TOPICS.contact(ALICE), None)
    recorder.timers.fire()
    assert recorder.latest()[f"{BASE}/$nodes"] == nodes(BOB, CAROL)

    fresh = Recorder(limit=3)
    fresh.device.contacts_read()
    contact(fresh.device, ALICE, "Alice", favourite=True, heard_at=None)
    contact(fresh.device, BOB, "Bob")
    contact(fresh.device, CAROL, "Carol", type="sensor", protected=True, heard_at=None)
    fresh.device.connected()
    assert fresh.latest()[f"{BASE}/$nodes"] == nodes(CAROL, ALICE, BOB)


def test_a_reconnect_publishes_everything_and_clears_what_went_meanwhile() -> None:
    recorder = Recorder()
    device = recorder.device
    started(recorder)
    contact(device, ALICE, "Alice")
    contact(device, BOB, "Bob")
    recorder.timers.fire()
    device.disconnected()
    device.document(TOPICS.contact(ALICE), None)  # while the connection is away
    recorder.take()
    device.connected()  # after the first table: at once, without waiting
    published = recorder.take()
    assert (f"{BASE}/$homie", b"4.0.0", True) in published  # again, for a broker that lost it
    assert (f"{BASE}/$nodes", nodes(BOB), True) in published
    assert (f"{BASE}/{ALICE}/battery", b"", True) in published
    assert published[-1] == (f"{BASE}/$state", b"alert", True)
    assert not any(t.startswith(f"{BASE}/{ALICE}") for t in recorder.latest())


def test_the_gateway_is_a_node_so_that_nodes_is_never_empty() -> None:
    """openHAB's Homie binding takes `$nodes` and a node's `$properties` as mandatory and skips an
    empty message, and an empty retained message is none at all: a device without nodes never
    comes up there (#97). The gateway is a node of its own, with the radio's state, also with
    `[homie] contacts = 0`; it takes no texts."""
    recorder = Recorder(limit=0)
    started(recorder)
    contact(recorder.device, ALICE, "Alice")
    recorder.timers.fire()
    held = recorder.latest()
    assert held[f"{BASE}/$nodes"] == nodes()
    assert {t: p for t, p in held.items() if t.startswith(GATEWAY + "/")} == {
        f"{GATEWAY}/$name": b"Gateway",
        f"{GATEWAY}/$type": b"gateway",
        f"{GATEWAY}/$properties": b"radio",
        f"{GATEWAY}/radio/$name": b"Radio",
        f"{GATEWAY}/radio/$datatype": b"enum",
        f"{GATEWAY}/radio/$format": b"connected,disconnected",
        f"{GATEWAY}/radio": b"disconnected",
    }
    assert all(HOMIE_ID.fullmatch(level) for level in GATEWAY.split("/")[1:])
    recorder.take()
    radio(recorder.device, "connected")
    assert recorder.take() == [
        (f"{GATEWAY}/radio", b"connected", True),
        (f"{BASE}/$state", b"ready", True),
    ]
    recorder.device.message(f"{GATEWAY}/send/set", b"no", False)
    assert recorder.sent == []
    assert recorder.take() == []


def test_a_table_just_before_hold_s_ends_announces_the_device_settle_s_later() -> None:
    """The first contact table ends the wait of a start and starts the one for its documents:
    the device stays `init` until SETTLE_S after the table, at the latest 65 s after the
    connection, as mqtt-api.md says."""
    recorder = Recorder()
    recorder.device.connected()
    assert recorder.timers.delays() == [HOLD_S]
    recorder.device.contacts_read()  # just before HOLD_S ends
    contact(recorder.device, ALICE, "Alice")
    assert recorder.timers.delays() == [SETTLE_S]
    assert recorder.latest()[f"{BASE}/$state"] == b"init"
    recorder.timers.fire()
    assert recorder.latest()[f"{BASE}/$state"] == b"alert"
    assert HOLD_S + SETTLE_S == 65.0


def test_send_set_hands_the_text_over_as_contacts_send_and_echoes_it() -> None:
    """As `contacts/<key>/send`: a retained message is handed over too and refused there, so
    its delivery event tells why; the echo Homie asks for is not retained."""
    recorder = Recorder()
    device = recorder.device
    started(recorder)
    contact(device, ALICE, "Alice")
    contact(device, BOB, "Bob", type="sensor")
    recorder.timers.fire()
    recorder.take()
    device.message(f"{BASE}/{ALICE}/send/set", b"hello", False)
    assert recorder.sent == [SendRequest(ALICE, b"hello", False, contact=True)]
    assert recorder.take() == [(f"{BASE}/{ALICE}/send", b"hello", False)]
    device.message(f"{BASE}/{ALICE}/send/set", b"kept", True)
    assert recorder.sent[-1] == SendRequest(ALICE, b"kept", True, contact=True)
    assert recorder.take() == []
    for topic in (
        f"{BASE}/{BOB}/send/set",  # a sensor takes no texts
        f"{BASE}/{CAROL}/send/set",  # no node
        f"{BASE}/{ALICE}/x/send/set",
        f"{BASE}/{ALICE}/send",
        f"homie/other/{ALICE}/send/set",
    ):
        device.message(topic, b"no", False)
    assert len(recorder.sent) == 2
    assert recorder.take() == []


class FakePaho:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.on_connect: Any = None
        self.on_connect_fail: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None
        self.on_subscribe: Any = None

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self.calls.append((name, args + tuple(kwargs.values())))

    def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        properties: Any = None,
    ) -> Any:
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace()


def test_the_connection_is_its_own_with_the_devices_last_will() -> None:
    """Homie 4.0.0, section 2.4: `$state` `lost` as the will, which the gateway's connection
    cannot carry beside `availability`; the broker settings of `[mqtt]`, another client ID."""
    fake, given = FakePaho(), []
    config = MqttConfig(host="broker.example.org", node_id="radio1", username="u", password="p")

    def factory(own: MqttConfig) -> FakePaho:
        given.append(own)
        return fake

    async def scenario() -> None:
        sent: list[SendRequest] = []
        connection = homie_client.homie_adapter(
            config, 50, "MeshCore radio", TOPICS, send=sent.append, client_factory=factory
        )
        await connection.start()
        calls = dict(fake.calls)
        assert calls["will_set"] == (f"{BASE}/$state", b"lost", 1, True)
        assert calls["username_pw_set"] == ("u", "p")
        assert calls["connect_async"] == ("broker.example.org", 1883, 60)
        assert re.fullmatch(
            r"meshcore-home-gateway-radio1-[0-9a-f]{8}-homie", str(given[0].client_id)
        )
        connection.publish("homie/x", b"1", True)  # not connected: dropped
        assert fake.published == []
        connection.device.contacts_read()
        contact(connection.device, ALICE, "Alice")
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=False), None)
        await asyncio.sleep(0)
        assert calls_of(fake, "subscribe") == [([(f"{BASE}/+/send/set", 1)],)]
        assert fake.published[0] == (f"{BASE}/$state", b"init", 1, True)
        assert (f"{BASE}/{ALICE}/$name", b"Alice", 1, True) in fake.published
        message = SimpleNamespace(topic=f"{BASE}/{ALICE}/send/set", payload=b"hi", retain=False)
        fake.on_message(fake, None, message)  # on paho's thread: to the event loop
        assert sent == []
        await asyncio.sleep(0)
        assert sent == [SendRequest(ALICE, b"hi", False, contact=True)]
        assert fake.published[-1] == (f"{BASE}/{ALICE}/send", b"hi", 1, False)
        fake.on_disconnect(fake, None, None, None, None)
        await asyncio.sleep(0)
        assert not connection.connected
        await connection.stop()
        assert fake.published[-1] == (f"{BASE}/$state", b"disconnected", 1, True)
        assert [n for n, _ in fake.calls][-2:] == ["disconnect", "loop_stop"]

    asyncio.run(scenario())
    HomieConnection(
        MqttConfig(host="h", node_id="radio1", client_id="fixed"), Recorder().device, factory
    )
    assert given[-1].client_id == "fixed-homie"


def calls_of(fake: FakePaho, name: str) -> list[tuple[Any, ...]]:
    return [args for called, args in fake.calls if called == name]


def test_a_stop_publishes_nothing_after_disconnected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A change of the nodes that waits for SETTLE_S when the service stops does not go out
    after `$state` `disconnected`."""
    monkeypatch.setattr(homie, "SETTLE_S", 0.01)
    fake = FakePaho()

    async def scenario() -> None:
        connection = homie_client.homie_adapter(
            MqttConfig(host="h", node_id="radio1"),
            50,
            "MeshCore radio",
            TOPICS,
            send=lambda _: None,
            client_factory=lambda _: fake,
        )
        await connection.start()
        connection.device.contacts_read()
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=False), None)
        await asyncio.sleep(0)
        contact(connection.device, ALICE, "Alice")  # its node waits for SETTLE_S
        await connection.stop()
        await asyncio.sleep(0.05)
        assert fake.published[-1] == (f"{BASE}/$state", b"disconnected", 1, True)

    asyncio.run(scenario())


def test_a_refused_connection_announces_nothing() -> None:
    fake = FakePaho()

    async def scenario() -> None:
        recorder = Recorder()
        connection = HomieConnection(
            MqttConfig(host="h", node_id="r"), recorder.device, lambda _: fake
        )
        await connection.start()
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=True), None)
        await asyncio.sleep(0)
        assert not connection.connected
        assert fake.published == []

    asyncio.run(scenario())


def test_the_wiring_feeds_the_device_the_documents(tmp_path: Path) -> None:
    """The observer's contact documents, also cleared ones, and the state document reach the
    device."""
    recorder = Recorder()
    bridge = gateway(
        make_config(),
        FakeBus(),
        EmulatedRadio,
        CommandLedger(JsonStateFile(tmp_path / "state.json")),
        session_factory=default_session_factory,
        notifier=lambda _: True,
        state_dir=tmp_path,
        homie=recorder.device,
    )
    recorder.device.connected()
    bridge.observer.radio_connected(350, codec.TuningParams(0, 9000))
    alice = codec.ContactRecord(bytes.fromhex(ALICE), 1, 0, 2, "Alice", 100, 100)
    bob = codec.ContactRecord(bytes.fromhex(BOB), 1, 0, 2, "Bob", 100, 100)
    bridge.observer.radio_event(ContactsRead({ALICE: alice, BOB: bob}, 200.0))
    assert recorder.timers.delays() == [SETTLE_S]  # the table read ended the hold
    recorder.timers.fire(SETTLE_S)
    assert recorder.latest()[f"{BASE}/{ALICE}/$name"] == b"Alice"
    assert recorder.timers.delays() == []
    bridge.observer.radio_event(ContactsRead({ALICE: alice}, 201.0))  # Bob left the table
    recorder.timers.fire()
    assert recorder.latest()[f"{BASE}/$nodes"] == nodes(ALICE)
    assert not any(t.startswith(f"{BASE}/{BOB}") for t in recorder.latest())
    bridge.radio.ready.set()
    bridge.status.publish()
    assert recorder.latest()[f"{BASE}/$state"] == b"ready"  # the state: the radio connected


def test_the_adapter_is_built_with_the_gateways_name_and_limit() -> None:
    connection = homie_client.homie_adapter(
        MqttConfig(host="h", node_id="radio1"),
        7,
        "Radio",
        TOPICS,
        send=lambda _: None,
        client_factory=lambda _: FakePaho(),
    )
    assert connection.device.base == BASE
    assert connection.device._limit == 7
    assert connection.device._name == "Radio"


def retained_under(broker: Broker, base: str) -> dict[str, bytes]:
    """What a controller that subscribes now gets: the messages under `base` the broker
    retained (those it passes on as they come have the retain flag cleared)."""
    found: dict[str, bytes] = {}
    subscribed = threading.Event()

    def message(_c: Any, _u: Any, message: Any) -> None:
        if message.retain:
            found[message.topic] = bytes(message.payload)

    client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
    client.on_subscribe = lambda *_: subscribed.set()
    client.on_message = message
    client.connect(broker.host, broker.port)
    client.loop_start()
    try:
        client.subscribe(f"{base}/#", qos=1)
        assert subscribed.wait(5)
        time.sleep(0.3)  # the broker sends the retained messages after the SUBACK
    finally:
        client.disconnect()  # first: loop_stop() alone waits for the network loop's timeout
        client.loop_stop()
    return found


@pytest.mark.broker
def test_with_broker_a_late_subscriber_sees_the_device_as_announced(
    broker: Broker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through Mosquitto: before the radio's first contact table a controller that subscribes
    sees the device `init`, not `ready` without its nodes (#97); after it, every device attribute
    but `$extensions`. That one is empty, and an empty retained message removes the topic's
    retained message (MQTT 3.1.1, section 3.3.1.3): only the controllers subscribed at the time
    get it. Homie 4.0.0 has no other form of an empty value (mqtt-api.md)."""
    monkeypatch.setattr(homie, "SETTLE_S", 0.05)

    async def wait_for(check: Callable[[dict[str, bytes]], bool]) -> dict[str, bytes]:
        deadline = time.monotonic() + 5
        while not check(held := await asyncio.to_thread(retained_under, broker, BASE)):
            assert time.monotonic() < deadline, held
        return held

    async def scenario() -> None:
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", keepalive_s=10)
        connection = homie_client.homie_adapter(
            config, 50, "MeshCore radio", TOPICS, send=lambda _: None
        )
        await connection.start()
        try:
            device = connection.device
            await wait_for(lambda held: f"{BASE}/$state" in held)  # connected
            radio(device, "connected")
            await asyncio.sleep(0.1)
            held = await asyncio.to_thread(retained_under, broker, BASE)
            assert held == {f"{BASE}/$state": b"init"}
            device.contacts_read()  # a radio without contacts
            await wait_for(lambda held: held.get(f"{BASE}/$state") == b"ready")
            held = await asyncio.to_thread(retained_under, broker, BASE)  # a controller later
            assert held[f"{BASE}/$nodes"] == b"gateway"  # never empty: openHAB requires it
            assert held[f"{BASE}/gateway/$properties"] == b"radio"
            device.contacts_read()
            contact(device, ALICE, "Alice")
            await wait_for(lambda held: held.get(f"{BASE}/$nodes") == f"gateway,{ALICE}".encode())
            await wait_for(lambda held: held.get(f"{BASE}/$state") == b"ready")
            held = await asyncio.to_thread(retained_under, broker, BASE)  # a controller later
            attributes = {t: p for t, p in held.items() if t.count("/") == 2}
            assert attributes == {
                f"{BASE}/$homie": b"4.0.0",
                f"{BASE}/$name": b"MeshCore radio",
                f"{BASE}/$implementation": b"meshcore-home-gateway",
                f"{BASE}/$nodes": f"gateway,{ALICE}".encode(),
                f"{BASE}/$state": b"ready",
            }
            assert held[f"{BASE}/{ALICE}/position"] == b"47.5,8.25"
        finally:
            await connection.stop()

    asyncio.run(scenario())
