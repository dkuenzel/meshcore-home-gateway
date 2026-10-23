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

from meshcore_home_gateway.config import MqttConfig
from meshcore_home_gateway.mqtt import client as client_module
from meshcore_home_gateway.mqtt.client import MqttBus, SendRequest
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
            [("meshcore/radio1/send/+", 1), ("homeassistant/status", 1)],
        )
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


def test_refused_connection_is_only_logged() -> None:
    async def scenario() -> None:
        bus, fake = make_bus()
        await bus.start()
        fake.on_connect(fake, None, None, SimpleNamespace(is_failure=True), None)
        await asyncio.sleep(0)
        assert not bus.connected
        assert "subscribe" not in names(fake)

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

        await asyncio.to_thread(deliver, "meshcore/radio1/send/alerts", b"Gate open")
        await asyncio.to_thread(deliver, "meshcore/radio1/send/alerts", b"old", True)
        await asyncio.to_thread(deliver, "homeassistant/status", b"online")
        await asyncio.to_thread(deliver, "homeassistant/status", b"offline")
        first = await asyncio.wait_for(bus.requests.get(), 1)
        second = await asyncio.wait_for(bus.requests.get(), 1)
        assert first == SendRequest("alerts", b"Gate open", retained=False)
        assert second == SendRequest("alerts", b"old", retained=True)
        assert births == [1]

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
