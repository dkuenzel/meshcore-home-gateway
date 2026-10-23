"""Adapter between paho-mqtt and the asyncio event loop (ADR 0003, ADR 0007).

paho runs its network loop in its own thread (`loop_start`) and reconnects by itself. Callbacks
only hand data to the event loop with `call_soon_threadsafe`; `publish` is safe to call from the
event loop because paho guards its outgoing queue with a lock. While the broker is unreachable,
events stay queued in paho and go out after the reconnect, at most MAX_QUEUED_EVENTS of them;
retained documents are skipped then, because the reconnect publishes them again.
"""

from __future__ import annotations

import asyncio
import logging
import ssl
import sys
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion

from meshcore_home_gateway.config import ConfigError, MqttConfig
from meshcore_home_gateway.mqtt.topics import Topics

log = logging.getLogger(__name__)

ONLINE = b"online"
OFFLINE = b"offline"
MAX_QUEUED_EVENTS = 1000  # events kept for the broker while it is unreachable
RECONNECT_MIN_S, RECONNECT_MAX_S = 1, 60


@dataclass(frozen=True, slots=True)
class SendRequest:
    alias: str
    payload: bytes
    retained: bool


class PahoLike(Protocol):
    """The part of `paho.mqtt.client.Client` the adapter uses (lets tests pass a fake)."""

    on_connect: Any
    on_connect_fail: Any
    on_disconnect: Any
    on_message: Any

    def username_pw_set(self, username: str | None, password: str | None = None) -> None: ...
    def tls_set_context(self, context: ssl.SSLContext | None = None) -> None: ...
    def will_set(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None: ...
    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None: ...
    def max_queued_messages_set(self, queue_size: int) -> Any: ...
    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> None: ...
    def loop_start(self) -> Any: ...
    def loop_stop(self) -> Any: ...
    def subscribe(self, topic: list[tuple[str, int]]) -> Any: ...
    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> Any: ...
    def disconnect(self) -> Any: ...


def make_paho_client(config: MqttConfig) -> PahoLike:
    return paho.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=config.client_id or f"meshcore-home-gateway-{config.node_id}",
        protocol=MQTTProtocolVersion.MQTTv311,
        clean_session=True,
    )


def tls_context(config: MqttConfig) -> ssl.SSLContext:
    """TLS with certificate and host name verification. A missing or unreadable certificate or
    key file is a configuration error, so the service does not restart in a loop over it."""
    try:
        context = ssl.create_default_context(cafile=config.ca_file)
    except OSError as exc:  # also ssl.SSLError: not a certificate
        raise ConfigError(f"mqtt.ca_file: cannot load {config.ca_file}: {_reason(exc)}") from exc
    if config.cert_file:
        try:
            context.load_cert_chain(config.cert_file, config.key_file)
        except OSError as exc:
            files = " and ".join(f for f in (config.cert_file, config.key_file) if f)
            raise ConfigError(f"mqtt.cert_file: cannot load {files}: {_reason(exc)}") from exc
    return context


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


class MqttBus:
    def __init__(
        self,
        config: MqttConfig,
        topics: Topics,
        ha_status_topic: str | None,
        client_factory: Callable[[MqttConfig], PahoLike] = make_paho_client,
    ) -> None:
        self._config = config
        self._topics = topics
        self._ha_status = ha_status_topic
        self._client = client_factory(config)
        self._loop: asyncio.AbstractEventLoop | None = None
        self.requests: asyncio.Queue[SendRequest] = asyncio.Queue()
        self.connected = False
        self._queued = 0  # events published since the connection was lost
        self._dropped = 0
        self._stopping = False
        self.on_connected: list[Callable[[], None]] = []
        self.on_ha_online: list[Callable[[], None]] = []

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        client, config = self._client, self._config
        if config.username:
            client.username_pw_set(config.username, config.password)
        if config.tls:
            client.tls_set_context(tls_context(config))
        client.will_set(self._topics.availability, OFFLINE, qos=1, retain=True)
        client.reconnect_delay_set(RECONNECT_MIN_S, RECONNECT_MAX_S)
        # no limit inside paho: at a reconnect it would also refuse `online`, discovery and
        # state behind a full backlog; publish() bounds the events instead
        client.max_queued_messages_set(0)
        client.on_connect = self._paho_connected
        client.on_connect_fail = self._paho_connect_failed
        client.on_disconnect = self._paho_disconnected
        client.on_message = self._paho_message
        client.connect_async(config.host, config.port, config.keepalive_s)
        client.loop_start()
        log.info("MQTT: connecting to %s:%d", config.host, config.port)

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        """QoS 1 events are queued while the broker is unreachable; QoS 0 events (commands) are
        not, so they can never arrive late."""
        if not self.connected:
            if retain or qos == 0:  # retained: the reconnect publishes the current documents
                return
            if self._queued >= MAX_QUEUED_EVENTS:
                if not self._dropped:
                    log.warning("MQTT: broker unreachable, %d events queued; dropping events "
                                "until the connection is back", self._queued)  # fmt: skip
                self._dropped += 1
                return
            self._queued += 1
        info = self._client.publish(topic, payload, qos=qos, retain=retain)
        # NO_CONN: paho noticed the loss first and keeps a QoS 1 message for the reconnect
        if getattr(info, "rc", 0) not in (paho.MQTT_ERR_SUCCESS, paho.MQTT_ERR_NO_CONN):
            log.warning("MQTT: publish to %s refused (paho error %s)", topic, info.rc)

    async def stop(self, timeout_s: float = 2.0) -> None:
        self._stopping = True
        info = self._client.publish(self._topics.availability, OFFLINE, qos=1, retain=True)
        wait = getattr(info, "wait_for_publish", None)
        if self.connected and callable(wait):
            try:
                async with asyncio.timeout(timeout_s + 0.5):
                    await asyncio.to_thread(wait, timeout_s)  # returns after the timeout
                confirmed = bool(getattr(info, "is_published", lambda: True)())
            except (TimeoutError, RuntimeError, ValueError):
                confirmed = False
            if not confirmed:
                log.warning("MQTT: offline status not confirmed before shutdown")
        self._client.disconnect()
        self._client.loop_stop()

    # --- paho thread ---------------------------------------------------------------------------

    def _paho_connected(self, client: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
        if getattr(reason, "is_failure", False):
            log.warning("MQTT: connection refused: %s", reason)
            return
        subscriptions = [(self._topics.send_subscription, 1)]
        if self._ha_status:
            subscriptions.append((self._ha_status, 1))
        client.subscribe(subscriptions)
        client.publish(self._topics.availability, ONLINE, qos=1, retain=True)
        self._call(self._handle_connected)

    def _paho_connect_failed(self, _c: Any, _u: Any) -> None:
        # no answer from a broker (name, address, port, TLS): paho calls this while it handles
        # the attempt's OSError and retries with the reconnect delay
        exc = sys.exception()
        reason = _reason(exc) if isinstance(exc, OSError) else "no connection"
        log.warning("MQTT: cannot reach %s:%d: %s", self._config.host, self._config.port, reason)

    def _paho_disconnected(self, _c: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
        self._call(self._handle_disconnected, str(reason))

    def _paho_message(self, _c: Any, _u: Any, message: Any) -> None:
        self._call(
            self._handle_message, message.topic, bytes(message.payload), bool(message.retain)
        )

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(callback, *args)

    # --- event loop ----------------------------------------------------------------------------

    def _handle_connected(self) -> None:
        self.connected = True
        log.info("MQTT: connected")
        if self._dropped:
            log.warning("MQTT: %d events were dropped while the broker was unreachable",
                        self._dropped)  # fmt: skip
        self._queued = self._dropped = 0
        for callback in self.on_connected:
            callback()

    def _handle_disconnected(self, reason: str) -> None:
        if self.connected and not self._stopping:
            log.warning("MQTT: disconnected (%s), reconnecting", reason)
        self.connected = False

    def _handle_message(self, topic: str, payload: bytes, retained: bool) -> None:
        if self._ha_status and topic == self._ha_status:
            if payload == ONLINE:
                for callback in self.on_ha_online:
                    callback()
            return
        alias = self._topics.alias_from_send_topic(topic)
        if alias is not None:
            self.requests.put_nowait(SendRequest(alias, payload, retained))
