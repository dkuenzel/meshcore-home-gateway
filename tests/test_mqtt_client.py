"""MQTT adapter: unit tests with a fake paho client, integration tests with mosquitto."""

from __future__ import annotations

import asyncio
import socket
import threading
from types import SimpleNamespace
from typing import Any

import paho.mqtt.client as paho
import pytest
from paho.mqtt.enums import CallbackAPIVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from meshcore_home_gateway.config import MqttConfig
from meshcore_home_gateway.mqtt import client as client_module
from meshcore_home_gateway.mqtt.client import ContactRequest, MqttBus, SendRequest
from meshcore_home_gateway.mqtt.topics import Topics
from tests.conftest import Broker

TOPICS = Topics("meshcore", "radio1")


class FakePaho:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.on_connect: Any = None
        self.on_disconnect: Any = None
        self.on_message: Any = None

    def __getattr__(self, name: str) -> Any:
        def record(*args: Any, **kwargs: Any) -> None:
            self.calls.append((name, args + tuple(kwargs.values())))

        return record

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any:
        self.published.append((topic, payload, qos, retain))
        return SimpleNamespace()


def make_bus(config: MqttConfig | None = None) -> tuple[MqttBus, FakePaho]:
    fake = FakePaho()
    cfg = config or MqttConfig(
        host="broker.example.org", node_id="radio1", username="u", password="p"
    )
    return MqttBus(cfg, TOPICS, "homeassistant/status", client_factory=lambda _: fake), fake


def names(fake: FakePaho) -> list[str]:
    return [name for name, _ in fake.calls]


def test_start_configures_will_credentials_and_reconnects() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        calls = dict(fake.calls)
        assert calls["username_pw_set"] == ("u", "p")
        assert calls["will_set"] == ("meshcore/radio1/availability", b"offline", 1, True)
        assert calls["reconnect_delay_set"] == (1, 60)
        assert calls["connect_async"] == ("broker.example.org", 1883, 60)
        assert "loop_start" in names(fake)
        assert "tls_set_context" not in names(fake)

    asyncio.run(scenario())


def test_tls_uses_a_verifying_context() -> None:
    async def scenario() -> None:
        bus, fake = make_bus(MqttConfig(host="h", node_id="radio1", tls=True))
        await bus.start()
        (context,) = dict(fake.calls)["tls_set_context"]
        assert context.check_hostname is True
        assert "username_pw_set" not in names(fake)

    asyncio.run(scenario())


def test_connection_subscribes_and_announces_availability() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        reason = SimpleNamespace(is_failure=False)
        await asyncio.to_thread(fake.on_connect, fake, None, None, reason, None)
        await asyncio.wait_for(connected.wait(), 1)
        assert dict(fake.calls)["subscribe"] == (
            [("meshcore/radio1/send/+", 1), ("meshcore/radio1/contacts/+/send", 1),
             ("meshcore/radio1/add_contact", 1), ("meshcore/radio1/contacts/+/remove", 1),
             ("homeassistant/status", 1)],
        )  # fmt: skip
        assert fake.published[-1] == ("meshcore/radio1/availability", b"online", 1, True)
        assert bus.connected

    asyncio.run(scenario())


def test_every_reconnect_subscribes_and_announces_again() -> None:
    """With a clean session the broker forgets the subscriptions with the connection."""

    async def scenario() -> None:
        bus, fake = make_bus()
        connections: list[int] = []
        bus.on_connected.append(lambda: connections.append(1))
        await bus.start()
        reason = SimpleNamespace(is_failure=False)
        for _ in range(2):
            await asyncio.to_thread(fake.on_connect, fake, None, None, reason, None)
            await asyncio.sleep(0)
            assert bus.connected
            fake.on_disconnect(fake, None, None, "lost", None)
            await asyncio.sleep(0)
            assert not bus.connected
        assert names(fake).count("subscribe") == 2
        online = [p for p in fake.published if p[:2] == (TOPICS.availability, b"online")]
        assert len(online) == 2
        assert connections == [1, 1]

    asyncio.run(scenario())


def test_refused_connection_is_only_logged(caplog: pytest.LogCaptureFixture) -> None:
    """With the user name: Mosquitto gives the same reason for every credentials problem."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        refused = ReasonCode(PacketTypes.CONNACK, identifier=135)
        fake.on_connect(fake, None, None, refused, None)
        fake.on_disconnect(fake, None, None, "Unspecified error", None)  # paho closes after it
        await asyncio.sleep(0)
        assert not bus.connected
        assert "subscribe" not in names(fake)
        assert "connection refused: Not authorized (user 'u'; test the login with" in caplog.text
        assert "check-config --broker" in caplog.text
        assert "'p'" not in caplog.text
        assert "without answering" not in caplog.text  # the refusal was the answer

    asyncio.run(scenario())


def test_a_connection_closed_without_an_answer_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A plain connection to a TLS port, or a broker that refuses a feature, sends no CONNACK."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.on_disconnect(fake, None, None, "Unspecified error", None)
        await asyncio.sleep(0)
        assert (
            "MQTT: broker.example.org:1883 closed the connection without answering "
            "(Unspecified error)"
        ) in caplog.text

    asyncio.run(scenario())


def test_a_broker_that_cannot_be_reached_is_logged_with_the_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A wrong name or address gets no answer from a broker: paho reports the attempt only."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        try:  # paho calls on_connect_fail while it handles the attempt's error
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        except OSError:
            fake.on_connect_fail(fake, None)
        fake.on_connect_fail(fake, None)  # without an error at hand
        assert "cannot reach broker.example.org:1883: Name or service not known" in caplog.text
        assert "cannot reach broker.example.org:1883: no connection" in caplog.text
        assert not bus.connected

    asyncio.run(scenario())


def test_with_paho_a_closed_port_is_logged_at_every_attempt(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real client: no broker listens, so no answer ever comes."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = int(sock.getsockname()[1])

    async def scenario() -> None:
        config = MqttConfig(host="127.0.0.1", port=port, node_id="radio1")
        bus = MqttBus(config, TOPICS, None)
        await bus.start()
        try:
            for _ in range(100):
                if "cannot reach" in caplog.text:
                    break
                await asyncio.sleep(0.05)
        finally:
            await bus.stop()
        assert f"MQTT: cannot reach 127.0.0.1:{port}: Connection refused" in caplog.text

    asyncio.run(scenario())


def test_send_requests_and_home_assistant_birth_reach_the_loop() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        births: list[int] = []
        bus.on_ha_online.append(lambda: births.append(1))
        await bus.start()

        def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
            message = SimpleNamespace(topic=topic, payload=payload, retain=retain)
            fake.on_message(fake, None, message)

        key = "a1" * 32
        await asyncio.to_thread(deliver, "meshcore/radio1/send/alerts", b"Gate open")
        await asyncio.to_thread(deliver, "meshcore/radio1/send/alerts", b"old", True)
        await asyncio.to_thread(deliver, "homeassistant/status", b"online")
        await asyncio.to_thread(deliver, "homeassistant/status", b"offline")
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/send", b"Hi")
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key.upper()}/send", b"no")
        await asyncio.to_thread(deliver, "meshcore/radio1/contacts", b"{}")
        first = await asyncio.wait_for(bus.requests.get(), 1)
        second = await asyncio.wait_for(bus.requests.get(), 1)
        third = await asyncio.wait_for(bus.requests.get(), 1)
        assert first == SendRequest("alerts", b"Gate open", retained=False)
        assert second == SendRequest("alerts", b"old", retained=True)
        assert third == SendRequest(key, b"Hi", retained=False, contact=True)
        fourth = await asyncio.wait_for(bus.requests.get(), 1)  # rejected by the bridge
        assert fourth == SendRequest(key.upper(), b"no", retained=False, contact=True)
        await asyncio.sleep(0.05)
        assert bus.requests.empty()
        assert births == [1]

    asyncio.run(scenario())


def test_contact_requests_reach_their_own_queue() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()

        def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
            fake.on_message(
                fake, None, SimpleNamespace(topic=topic, payload=payload, retain=retain)
            )

        key = "a1" * 32
        await asyncio.to_thread(deliver, "meshcore/radio1/add_contact", key.encode())
        await asyncio.to_thread(deliver, "meshcore/radio1/add_contact", b"old", True)
        await asyncio.to_thread(deliver, f"meshcore/radio1/contacts/{key}/remove", b"remove")
        await asyncio.to_thread(deliver, "meshcore/radio1/contacts/x/y/remove", b"")
        requests = [await asyncio.wait_for(bus.contact_requests.get(), 1) for _ in range(3)]
        assert requests == [
            ContactRequest("add", key.encode(), False), ContactRequest("add", b"old", True),
            ContactRequest("remove", b"remove", False, key),
        ]  # fmt: skip
        await asyncio.sleep(0.05)
        assert bus.contact_requests.empty()  # a level with a "/" names no contact
        assert bus.requests.empty()

    asyncio.run(scenario())


def test_discovery_topics_and_the_show_request_reach_the_loop() -> None:
    """The gateway watches its discovery topics, to notice what Home Assistant clears there (a
    contact device the user deleted, the gateway's own device), its offline status, and the
    request to show deleted contact devices again."""

    async def scenario() -> None:
        fake = FakePaho()
        config = MqttConfig(host="broker.example.org", node_id="radio1")
        own = "homeassistant/device/meshcore_home_gateway_radio1/config"
        watch = "homeassistant/device/meshcore_home_gateway_radio1/+/config"
        bus = MqttBus(
            config, TOPICS, "homeassistant/status", lambda _: fake, ha_devices=[own, watch]
        )
        seen: list[tuple[str, bytes, bool]] = []
        shown: list[int] = []
        offline: list[int] = []
        bus.on_ha_device.append(lambda *message: seen.append(message))
        bus.on_show_hidden.append(lambda: shown.append(1))
        bus.on_ha_offline.append(lambda: offline.append(1))
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        subscriptions = dict(fake.calls)["subscribe"][0]
        assert subscriptions[-4:] == [
            (own, 1), (watch, 1), ("meshcore/radio1/homeassistant/show_hidden", 1),
            ("meshcore/radio1/homeassistant/add_node", 1),
        ]  # fmt: skip

        def deliver(topic: str, payload: bytes, retain: bool = False) -> None:
            fake.on_message(
                fake, None, SimpleNamespace(topic=topic, payload=payload, retain=retain)
            )

        device = "homeassistant/device/meshcore_home_gateway_radio1/contact_x/config"
        await asyncio.to_thread(deliver, device, b"")
        await asyncio.to_thread(deliver, device, b"", True)
        await asyncio.to_thread(deliver, own, b"")
        await asyncio.to_thread(deliver, "homeassistant/device/other/config", b"")
        await asyncio.to_thread(deliver, "homeassistant/status", b"offline")
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/show_hidden", b"show")
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/show_hidden", b"x", True)
        label = "b0b0b0b0b0b0…12121212 Bob".encode()  # the select's choice
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/add_node", label)
        await asyncio.to_thread(deliver, "meshcore/radio1/homeassistant/add_node", b"x", True)
        await asyncio.sleep(0.05)
        assert seen == [(device, b"", False), (device, b"", True), (own, b"", False)]
        assert (shown, offline) == ([1], [1])
        assert bus.requests.empty()
        picked = [bus.contact_requests.get_nowait() for _ in range(2)]
        assert picked == [
            ContactRequest("add", label, False, node_label=True),
            ContactRequest("add", b"x", True, node_label=True),  # refused there, with an event
        ]  # fmt: skip

    asyncio.run(scenario())


def test_without_contact_devices_their_topics_are_not_watched() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await asyncio.to_thread(fake.on_connect, fake, None, None, SimpleNamespace(), None)
        topics = [topic for topic, _ in dict(fake.calls)["subscribe"][0]]
        assert "meshcore/radio1/homeassistant/show_hidden" not in topics
        assert "meshcore/radio1/homeassistant/add_node" not in topics
        assert not any(topic.startswith("homeassistant/device") for topic in topics)

    asyncio.run(scenario())


def test_disconnect_is_tracked() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=False), None)
        await asyncio.sleep(0)
        fake.on_disconnect(fake, None, None, "lost", None)
        await asyncio.sleep(0)
        assert not bus.connected

    asyncio.run(scenario())


def test_events_are_bounded_while_the_broker_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """paho's own queue limit would also drop `online`, discovery and state at the reconnect,
    after a long outage; the bus bounds only the events and skips retained documents, which
    are published again when the connection is back."""
    monkeypatch.setattr(client_module, "MAX_QUEUED_EVENTS", 2)

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        assert dict(fake.calls)["max_queued_messages_set"] == (0,)  # no limit inside paho
        for n in range(4):
            bus.publish("meshcore/radio1/received", b"%d" % n)
        bus.publish("meshcore/radio1/state", b"{}", retain=True)
        assert [p[1] for p in fake.published] == [b"0", b"1"]
        assert caplog.text.count("dropping events") == 1
        bus._handle_connected()  # the connection is back: the bound starts again
        bus.publish("meshcore/radio1/state", b"{}", retain=True)
        bus.publish("meshcore/radio1/received", b"4")
        assert [p[1] for p in fake.published][-2:] == [b"{}", b"4"]
        assert "2 events were dropped" in caplog.text

    asyncio.run(scenario())


def test_stop_warns_when_offline_is_not_confirmed(caplog: pytest.LogCaptureFixture) -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        fake.publish = lambda *a, **k: SimpleNamespace(  # type: ignore[method-assign]
            wait_for_publish=lambda timeout: None, is_published=lambda: False
        )
        await bus.start()
        bus.connected = True
        await bus.stop()
        bus._handle_disconnected("Normal disconnection")  # paho reports the clean disconnect
        assert "offline status not confirmed" in caplog.text
        assert "reconnecting" not in caplog.text

    asyncio.run(scenario())


def test_stop_announces_offline_and_disconnects() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        await bus.stop()
        assert fake.published[-1] == ("meshcore/radio1/availability", b"offline", 1, True)
        assert names(fake)[-2:] == ["disconnect", "loop_stop"]

    asyncio.run(scenario())


# --- integration with a real broker -----------------------------------------------------------


class Observer:
    """A plain paho client that records what arrives on the bridge's topics."""

    def __init__(self, broker: Broker) -> None:
        self.messages: list[tuple[str, bytes, bool]] = []
        self.event = threading.Event()
        self.client = paho.Client(callback_api_version=CallbackAPIVersion.VERSION2)
        self.client.on_message = self._message
        self.client.connect(broker.host, broker.port)
        self.client.subscribe("meshcore/#", qos=1)
        self.client.loop_start()

    def _message(self, _c: Any, _u: Any, message: Any) -> None:
        self.messages.append((message.topic, bytes(message.payload), bool(message.retain)))
        self.event.set()

    def wait_for(self, topic: str, payload: bytes, timeout: float = 5.0) -> None:
        deadline = threading.Event()
        timer = threading.Timer(timeout, deadline.set)
        timer.start()
        try:
            while not deadline.is_set():
                if any(t == topic and p == payload for t, p, _ in self.messages):
                    return
                self.event.wait(0.05)
                self.event.clear()
        finally:
            timer.cancel()
        raise AssertionError(f"{topic} {payload!r} not seen; got {self.messages}")

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


@pytest.mark.broker
def test_with_broker_availability_requests_and_last_will(broker: Broker) -> None:
    async def scenario() -> None:
        observer = Observer(broker)
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", keepalive_s=10)
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        try:
            await asyncio.wait_for(connected.wait(), 5)
            await asyncio.to_thread(observer.wait_for, TOPICS.availability, b"online")

            observer.client.publish(TOPICS.send("alerts"), b"Hello", qos=1)
            request = await asyncio.wait_for(bus.requests.get(), 5)
            assert request == SendRequest("alerts", b"Hello", retained=False)

            # Abrupt loss of the connection: the broker publishes the last will.
            client: Any = bus._client  # test reaches into the adapter
            client.loop_stop()
            sock = client.socket()
            assert isinstance(sock, socket.socket)
            sock.shutdown(socket.SHUT_RDWR)
            sock.close()
            await asyncio.to_thread(observer.wait_for, TOPICS.availability, b"offline")
        finally:
            observer.close()

    asyncio.run(scenario())


@pytest.mark.broker
def test_with_broker_clean_stop_leaves_offline_retained(broker: Broker) -> None:
    async def scenario() -> None:
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1")
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        await asyncio.wait_for(connected.wait(), 5)
        await bus.stop()
        late = Observer(broker)  # subscribes after the fact: sees the retained value
        try:
            await asyncio.to_thread(late.wait_for, TOPICS.availability, b"offline")
            assert (TOPICS.availability, b"offline", True) in late.messages
        finally:
            late.close()

    asyncio.run(scenario())


@pytest.mark.broker
def test_with_broker_a_retained_request_is_flagged_only_when_replayed(broker: Broker) -> None:
    """MQTT 3.1.1 clears the retain flag of a live message: a retained request published while
    the bridge is connected arrives unflagged and is sent once. The copy the broker keeps arrives
    flagged after every reconnect, so the bridge rejects it (docs/mqtt-api.md, Sending)."""

    async def scenario() -> None:
        observer = Observer(broker)
        config = MqttConfig(host=broker.host, port=broker.port, node_id="radio1", keepalive_s=10)
        bus = MqttBus(config, TOPICS, None)
        connected = asyncio.Event()
        bus.on_connected.append(connected.set)
        await bus.start()
        try:
            await asyncio.wait_for(connected.wait(), 5)
            await asyncio.to_thread(observer.wait_for, TOPICS.availability, b"online")
            observer.client.publish(TOPICS.send("alerts"), b"Gate open", qos=1, retain=True)
            live = await asyncio.wait_for(bus.requests.get(), 5)
            assert live == SendRequest("alerts", b"Gate open", retained=False)

            connected.clear()
            client: Any = bus._client  # drop the connection; paho reconnects by itself
            client.socket().shutdown(socket.SHUT_RDWR)
            await asyncio.wait_for(connected.wait(), 10)
            replayed = await asyncio.wait_for(bus.requests.get(), 5)
            assert replayed == SendRequest("alerts", b"Gate open", retained=True)
            assert bus.requests.empty()
        finally:
            await bus.stop()
            observer.close()

    asyncio.run(scenario())


def test_events_queued_for_the_reconnect_are_not_logged_as_refused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """paho keeps a QoS 1 message for the reconnect and still answers MQTT_ERR_NO_CONN."""

    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.publish = lambda *a, **k: SimpleNamespace(rc=paho.MQTT_ERR_NO_CONN)  # type: ignore[method-assign]
        bus.publish("meshcore/radio1/received", b"queued")
        assert "refused" not in caplog.text
        fake.publish = lambda *a, **k: SimpleNamespace(rc=paho.MQTT_ERR_QUEUE_SIZE)  # type: ignore[method-assign]
        bus.publish("meshcore/radio1/received", b"lost")
        assert "refused" in caplog.text

    asyncio.run(scenario())


def test_the_quality_of_service_reaches_paho() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        bus._handle_connected()
        bus.publish("meshcore/radio1/command", b"{}", qos=0)
        bus.publish("meshcore/radio1/received", b"{}")
        assert [(p[0], p[2]) for p in fake.published][-2:] == [
            ("meshcore/radio1/command", 0), ("meshcore/radio1/received", 1),
        ]  # fmt: skip
        bus.connected = False
        before = len(fake.published)
        bus.publish("meshcore/radio1/command", b"{}", qos=0)  # nothing to keep for later
        assert len(fake.published) == before

    asyncio.run(scenario())
