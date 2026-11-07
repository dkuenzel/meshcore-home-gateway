"""Adapter between paho-mqtt and the asyncio event loop (ADR 0003, ADR 0007).

paho runs its network loop in its own thread (`loop_start`) and reconnects by itself. Callbacks
only hand data to the event loop with `call_soon_threadsafe`; `publish` is safe to call from the
event loop because paho guards its outgoing queue with a lock. While the broker is unreachable,
events stay queued in paho and go out after the reconnect, at most MAX_QUEUED_EVENTS of them;
retained documents are skipped then, because the reconnect publishes them again. The adapter
subscribes to the gateway's retained topics too, and undoes what other clients write there
(`mqtt.retained`). With `[mqtt] protocol = "5"` every message carries the gateway's name, its
version and the time as user properties, events an expiry, and the delivery events of a send
request its correlation data (ARC-014).
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import ssl
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion, MQTTProtocolVersion
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import ConfigError, MqttConfig
from meshcore_home_gateway.health import MinuteCounts
from meshcore_home_gateway.mqtt.retained import RetainedTopics
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import Bus, ContactRequest, SendRequest

log = logging.getLogger(__name__)

ONLINE = b"online"
OFFLINE = b"offline"
MAX_QUEUED_EVENTS = 1000  # events kept for the broker while it is unreachable
RECONNECT_MIN_S, RECONNECT_MAX_S = 1, 60
ECHO_WAIT_S = 10  # the gateway's `online` comes back within this, or it cannot read its topics


class PahoLike(Protocol):
    """The part of `paho.mqtt.client.Client` the adapter uses (lets tests pass a fake)."""

    on_connect: Any
    on_connect_fail: Any
    on_disconnect: Any
    on_message: Any
    on_subscribe: Any

    def username_pw_set(self, username: str | None, password: str | None = None) -> None: ...
    def tls_set_context(self, context: ssl.SSLContext | None = None) -> None: ...
    def will_set(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None: ...
    def reconnect_delay_set(self, min_delay: int = 1, max_delay: int = 120) -> None: ...
    def max_queued_messages_set(self, queue_size: int) -> Any: ...
    def connect_async(self, host: str, port: int = 1883, keepalive: int = 60) -> None: ...
    def loop_start(self) -> Any: ...
    def loop_stop(self) -> Any: ...
    def subscribe(self, topic: list[tuple[str, int]]) -> Any: ...
    def publish(
        self,
        topic: str,
        payload: bytes,
        qos: int = 0,
        retain: bool = False,
        properties: Properties | None = None,
    ) -> Any: ...
    def disconnect(self) -> Any: ...


def make_paho_client(config: MqttConfig) -> PahoLike:
    client_id = config.client_id or default_client_id(config.node_id)
    if config.protocol == "5":
        # no session expiry interval: the broker ends the session at the disconnect, as a clean
        # session of 3.1.1 does
        return paho.Client(
            callback_api_version=CallbackAPIVersion.VERSION2,
            client_id=client_id,
            protocol=MQTTProtocolVersion.MQTTv5,
        )
    return paho.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=MQTTProtocolVersion.MQTTv311,
        clean_session=True,
    )


def default_client_id(node_id: str) -> str:
    """A broker disconnects a client when another connects with its id (MQTT 3.1.1 section
    3.1.4): one that anyone can read in the topics would let any client of the broker take the
    gateway offline. With `clean_session` the id names no session, so a random part per start
    costs nothing (SEC-006)."""
    return f"meshcore-home-gateway-{node_id}-{secrets.token_hex(4)}"


def tls_context(config: MqttConfig) -> ssl.SSLContext:
    """TLS with certificate and host name verification. A missing or unreadable certificate or
    key file is a configuration error, so the service does not restart in a loop over it."""
    try:
        context = ssl.create_default_context(cafile=config.ca_file)
    except OSError as exc:  # also ssl.SSLError: not a certificate
        # Translators: key is a setting, path a file, detail the system's or OpenSSL's reason.
        raise ConfigError(
            "{key}: cannot load {path}: {detail}",
            key="mqtt.ca_file",
            path=config.ca_file,
            detail=_reason(exc),
        ) from exc
    if config.cert_file:
        try:
            context.load_cert_chain(config.cert_file, config.key_file)
        except OSError as exc:
            if config.key_file:
                # Translators: key is the setting mqtt.cert_file; path and key_file are files.
                raise ConfigError(
                    "{key}: cannot load {path} and {key_file}: {detail}",
                    key="mqtt.cert_file",
                    path=config.cert_file,
                    key_file=config.key_file,
                    detail=_reason(exc),
                ) from exc
            raise ConfigError(
                "{key}: cannot load {path}: {detail}",
                key="mqtt.cert_file",
                path=config.cert_file,
                detail=_reason(exc),
            ) from exc
    return context


def _reason(exc: OSError) -> str:
    return exc.strerror or str(exc)


class MqttBus(Bus):
    """The bus port (ports.py) over paho-mqtt (CODE-010)."""

    def __init__(
        self,
        config: MqttConfig,
        topics: Topics,
        ha_status_topic: str | None,
        client_factory: Callable[[MqttConfig], PahoLike] = make_paho_client,
        *,
        ha_devices: Sequence[str] = (),
        event_expiry_s: int | None = None,
        command_expiry_s: int | None = None,
    ) -> None:
        """`ha_devices`: subscriptions to the gateway's discovery topics, to notice what Home
        Assistant clears there (with the request to show deleted contact devices again).
        `event_expiry_s`, `command_expiry_s`: with MQTT 5, how long the broker keeps a `delivery`
        or `received` event, and a `command` event, for a subscriber that is away."""
        self._config = config
        self._topics = topics
        self._ha_status = ha_status_topic
        self._ha_devices = tuple(ha_devices)
        self._expiry = {topics.delivery: event_expiry_s, topics.received: event_expiry_s}
        self._expiry[topics.command] = command_expiry_s
        self._client = client_factory(config)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._retained = RetainedTopics(
            topics,
            self._publish_retained,
            self._call_later,
            time.monotonic,
            drafts=bool(self._ha_devices),  # the chats' drafts come with discovery
        )
        self.requests: asyncio.Queue[SendRequest] = asyncio.Queue()
        self.contact_requests: asyncio.Queue[ContactRequest] = asyncio.Queue()
        self.connected = False
        self._queued = 0  # events published since the connection was lost
        self._dropped = 0
        self._drops = MinuteCounts()  # events dropped, for the health section
        self._stopping = False
        self._answered = False  # paho thread: the broker answered the current attempt
        # paho thread: the message id of the connect's subscription, and its topics
        self._subscribing: tuple[int | None, list[str]] = (None, [])
        self._heard_back = False  # a message on the retained topics came since the connect
        self._echo_check: asyncio.TimerHandle | None = None
        self.on_connected: list[Callable[[], None]] = []
        self.on_ha_online: list[Callable[[], None]] = []
        self.on_ha_offline: list[Callable[[], None]] = []
        self.on_ha_device: list[Callable[[str, bytes, bool], None]] = []
        self.on_show_hidden: list[Callable[[], None]] = []
        self.on_draft_request: list[Callable[[str, str, bytes, bool], None]] = []
        self.on_draft_retained: list[Callable[[str, str, bytes], None]] = []
        self.on_stopping: list[Callable[[], None]] = []

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
        client.on_subscribe = self._paho_subscribed
        client.connect_async(config.host, config.port, config.keepalive_s)
        client.loop_start()
        log.info("MQTT: connecting to %s:%d", config.host, config.port)

    def dropped_1h(self) -> int:
        """Events dropped in the last hour while the broker was unreachable (more than
        MAX_QUEUED_EVENTS waited): the health section tells them once the broker is back."""
        return self._drops.total(time.monotonic())

    def publish(
        self,
        topic: str,
        payload: bytes,
        retain: bool = False,
        qos: int = 1,
        *,
        correlation: bytes | None = None,
    ) -> None:
        """QoS 1 events are queued while the broker is unreachable; QoS 0 events (commands) are
        not, so they can never arrive late."""
        if retain and self._retained.covers(topic):
            self._retained.sent(topic, payload, published=self.connected)
        if not self.connected:
            if retain or qos == 0:  # retained: the reconnect publishes the current documents
                return
            if self._queued >= MAX_QUEUED_EVENTS:
                if not self._dropped:
                    log.warning(
                        "MQTT: broker unreachable, %d events queued; dropping events "
                        "until the connection is back",
                        self._queued,
                    )
                self._dropped += 1
                self._drops.add(time.monotonic())
                return
            self._queued += 1
        properties = self._properties(topic, correlation)
        info = self._client.publish(topic, payload, qos=qos, retain=retain, properties=properties)
        # NO_CONN: paho noticed the loss first and keeps a QoS 1 message for the reconnect
        if getattr(info, "rc", 0) not in (paho.MQTT_ERR_SUCCESS, paho.MQTT_ERR_NO_CONN):
            log.warning("MQTT: publish to %s refused (paho error %s)", topic, info.rc)

    def _properties(self, topic: str, correlation: bytes | None = None) -> Properties | None:
        """MQTT 5: who published it, and when; how long an event may wait for a subscriber that
        is away; the correlation data of the request it answers. None for MQTT 3.1.1."""
        if self._config.protocol != "5":
            return None
        properties = Properties(PacketTypes.PUBLISH)  # type: ignore[no-untyped-call]
        now = datetime.now(UTC).isoformat(timespec="seconds")
        properties.UserProperty = [
            ("gateway", self._config.node_id),
            ("version", __version__),
            ("published_at", now),
        ]
        expiry = self._expiry.get(topic)
        if expiry is None and self._topics.key_from_contact_topic(topic, "received") is not None:
            expiry = self._expiry[self._topics.received]
        if expiry:
            properties.MessageExpiryInterval = expiry
        if correlation is not None:
            properties.CorrelationData = correlation
        return properties

    async def stop(self, timeout_s: float = 2.0) -> None:
        for callback in self.on_stopping:  # before the offline status, in the order of QoS 1
            callback()
        self._stopping = True
        self._retained.close()
        self._cancel_echo_check()
        info = self._client.publish(
            self._topics.availability,
            OFFLINE,
            qos=1,
            retain=True,
            properties=self._properties(self._topics.availability),
        )
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
        self._answered = True
        if getattr(reason, "is_failure", False):
            # Mosquitto answers 'Not authorized' for a wrong password, an unknown user and
            # missing credentials alike: the user name tells the user what was sent
            user = f"user {self._config.username!r}" if self._config.username else "no user name"
            log.warning(
                "MQTT: connection refused: %s (%s; test the login with: "
                "meshcore-home-gateway check-config --broker)",
                reason,
                user,
            )
            return
        subscriptions = [
            (self._topics.send_subscription, 1),
            (self._topics.contact_send_subscription, 1),
            (self._topics.add_contact, 1),
            (self._topics.contact_remove_subscription, 1),
            (self._topics.contact_telemetry_subscription, 1),
        ]
        # the gateway's retained topics, to undo what other clients write there
        subscriptions += [(topic, 1) for topic in self._retained.filters]
        if self._ha_status:
            subscriptions.append((self._ha_status, 1))
        if self._ha_devices:  # the requests of Home Assistant's controls, with its topics
            ha_requests = (
                self._topics.show_hidden,
                self._topics.add_node,
                self._topics.draft_request_subscription,
            )
            subscriptions += [(topic, 1) for topic in (*self._ha_devices, *ha_requests)]
        info = client.subscribe(subscriptions)
        mid = info[1] if isinstance(info, tuple) else None  # its answer comes after this returns
        self._subscribing = (mid, [topic for topic, _ in subscriptions])
        online = self._properties(self._topics.availability)
        client.publish(self._topics.availability, ONLINE, qos=1, retain=True, properties=online)
        self._call(self._handle_connected)

    def _paho_subscribed(self, _c: Any, _u: Any, mid: int, reasons: Any, _p: Any) -> None:
        expected, topics = self._subscribing
        if mid != expected:
            return
        refused = [
            topic
            for topic, reason in zip(topics, reasons, strict=False)
            if getattr(reason, "is_failure", False)
        ]
        if refused:  # a broker that checks a SUBSCRIBE against its ACL (not an acl_file)
            log.warning(
                "MQTT: the broker refused the subscription to %s (the gateway's user "
                "needs read access to them: docs/mqtt-api.md, Securing the broker)",
                ", ".join(refused),
            )

    def _paho_connect_failed(self, _c: Any, _u: Any) -> None:
        # no answer from a broker (name, address, port, TLS): paho calls this while it handles
        # the attempt's OSError and retries with the reconnect delay
        exc = sys.exception()
        reason = _reason(exc) if isinstance(exc, OSError) else "no connection"
        log.warning("MQTT: cannot reach %s:%d: %s", self._config.host, self._config.port, reason)

    def _paho_disconnected(self, _c: Any, _u: Any, _f: Any, reason: Any, _p: Any) -> None:
        answered, self._answered = self._answered, False
        self._call(self._handle_disconnected, str(reason), answered)

    def _paho_message(self, _c: Any, _u: Any, message: Any) -> None:
        properties = getattr(message, "properties", None)  # MQTT 5 only
        correlation = getattr(properties, "CorrelationData", None)
        self._call(
            self._handle_message,
            message.topic,
            bytes(message.payload),
            bool(message.retain),
            bytes(correlation) if isinstance(correlation, bytes | bytearray) else None,
        )

    def _call(self, callback: Callable[..., None], *args: Any) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(callback, *args)

    # --- event loop ----------------------------------------------------------------------------

    def _call_later(self, delay_s: float, callback: Callable[[], None]) -> asyncio.TimerHandle:
        assert self._loop is not None  # noqa: S101 - messages arrive only after start()
        return self._loop.call_later(delay_s, callback)

    def _publish_retained(self, topic: str, payload: bytes) -> None:
        """A restore of a retained topic (`mqtt.retained`); the next connect publishes all."""
        if self.connected:
            properties = self._properties(topic)
            self._client.publish(topic, payload, qos=1, retain=True, properties=properties)

    def _handle_connected(self) -> None:
        self.connected = True
        log.info("MQTT: connected")
        self._retained.connected()
        # published by the paho thread before this ran, so before its copy can come back
        self._retained.sent(self._topics.availability, ONLINE, published=True)
        self._heard_back = False
        if self._loop is not None:
            self._cancel_echo_check()
            self._echo_check = self._loop.call_later(ECHO_WAIT_S, self._check_echo)
        if self._dropped:
            log.warning(
                "MQTT: %d events were dropped while the broker was unreachable", self._dropped
            )
        self._queued = self._dropped = 0
        for callback in self.on_connected:
            callback()

    def _handle_disconnected(self, reason: str, answered: bool = True) -> None:
        if self.connected and not self._stopping:
            log.warning("MQTT: disconnected (%s), reconnecting", reason)
        elif not answered and not self._stopping:  # e.g. TLS expected, or a refused feature
            log.warning(
                "MQTT: %s:%d closed the connection without answering (%s)",
                self._config.host,
                self._config.port,
                reason,
            )
        self.connected = False

    def _check_echo(self) -> None:
        """Mosquitto's `acl_file` grants every subscription and leaves out what the user may not
        read: then the gateway's own messages never come back, and nothing is restored."""
        self._echo_check = None
        if self.connected and not self._heard_back:
            log.warning(
                "MQTT: the gateway's `online` on %s did not come back within %d s: its "
                "user may not read its own topics, so what other clients write there is "
                "not undone (docs/mqtt-api.md, Securing the broker)",
                self._topics.availability,
                ECHO_WAIT_S,
            )

    def _cancel_echo_check(self) -> None:
        if self._echo_check is not None:
            self._echo_check.cancel()
            self._echo_check = None

    def _handle_message(
        self, topic: str, payload: bytes, retained: bool, correlation: bytes | None = None
    ) -> None:
        draft = self._topics.draft_from_topic(topic) if self._ha_devices else None
        if draft is not None and retained and not self._stopping:
            # the broker's copy of a draft, for the subscription: the drafts part deletes those
            # of no current chat; the retained guard takes no copy
            for draft_callback in self.on_draft_retained:
                draft_callback(*draft, payload)
        if self._retained.covers(topic):
            self._heard_back = True
            if not self._stopping:
                self._retained.received(topic, payload, retained)
        elif self._ha_status and topic == self._ha_status:
            for callback in {ONLINE: self.on_ha_online, OFFLINE: self.on_ha_offline}.get(
                payload, []
            ):
                callback()
        elif not (self._ha_devices and self._ha_message(topic, payload, retained)):
            self._request(topic, payload, retained, correlation)

    def _request(
        self, topic: str, payload: bytes, retained: bool, correlation: bytes | None = None
    ) -> None:
        """A request on one of the gateway's request topics; any other topic is ignored. A send
        request keeps its correlation data for its delivery events; a contact request names
        itself by the `id` of its JSON form, which its events carry."""
        alias = self._topics.alias_from_send_topic(topic)
        if alias is not None:
            self.requests.put_nowait(SendRequest(alias, payload, retained, False, correlation))
            return
        key = self._topics.key_from_contact_topic(topic, "send")
        if key is not None:
            self.requests.put_nowait(SendRequest(key, payload, retained, True, correlation))
            return
        if topic == self._topics.add_contact:
            self.contact_requests.put_nowait(ContactRequest("add", payload, retained))
            return
        for kind in ("remove", "telemetry"):
            key = self._topics.key_from_contact_topic(topic, kind)
            if key is not None:
                self.contact_requests.put_nowait(ContactRequest(kind, payload, retained, key))
                return

    def _ha_message(self, topic: str, payload: bytes, retained: bool) -> bool:
        """The gateway's discovery topics and the requests of Home Assistant's controls; False
        for any other topic."""
        if any(_matches(subscription, topic) for subscription in self._ha_devices):
            for device_callback in self.on_ha_device:
                device_callback(topic, payload, retained)
        elif topic == self._topics.show_hidden:
            if not retained:  # a request, not a left-over
                for callback in self.on_show_hidden:
                    callback()
        elif topic == self._topics.add_node:  # a retained one is refused, with an event
            self.contact_requests.put_nowait(
                ContactRequest("add", payload, retained, node_label=True)
            )
        elif (draft := self._topics.draft_from_request_topic(topic)) is not None:
            # a chat's text or Send, handled here on the event loop in the order they came: the
            # press takes the text stored before it (drafts.py)
            for draft_callback in self.on_draft_request:
                draft_callback(*draft, payload, retained)
        else:
            return False
        return True


def _matches(subscription: str, topic: str) -> bool:
    """MQTT topic filter matching for filters with single-level wildcards ('+')."""
    pattern, levels = subscription.split("/"), topic.split("/")
    return len(pattern) == len(levels) and all(
        p in ("+", t) for p, t in zip(pattern, levels, strict=True)
    )
