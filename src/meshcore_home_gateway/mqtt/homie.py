"""The Homie adapter (EXT-011, ADR 0015): the contacts as the nodes of one Homie 4 device, which
openHAB and the other Homie controllers discover without configuration (docs/mqtt-api.md, Homie).
It is fed by the documents that the other front ends read: `state` and `contacts/<key>`.

Homie wants the device's `$state` as the last will of its connection, `lost` (Homie 4.0.0, section
2.4), and a connection has one will, which carries `availability` on the gateway's: the adapter
has a connection of its own (mqtt/homie_client.py). Every message is retained with QoS 1, the
echo of `send` aside. The first node is the gateway, with the radio's state: `$nodes` is never
empty, which openHAB requires and an empty retained message cannot carry (#97). The other nodes
are the contacts that come first as for Home Assistant's devices, at most `[homie] contacts`,
and a contact keeps its node while it stays in the table; a change of the nodes goes out between
`$state` `init` and `ready`, so that a controller reads the device again. A node's property
without a value (`null` in the document) is cleared: an empty retained message removes the
value from the broker.

The connection comes up before the radio's contacts, whose documents then arrive one by one: a
start keeps the device `init` until SETTLE_S after the radio's first contact table (at most
HOLD_S + SETTLE_S), and a change of the nodes waits SETTLE_S for those that follow it, so that a
controller reads the device once per table read, not once per contact (#97).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from meshcore_home_gateway.core.telemetry import STATES
from meshcore_home_gateway.mqtt.retained import Cancellable
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import SendRequest

log = logging.getLogger(__name__)

HOLD_S = 60.0  # a start waits this long at most for the radio's first contact table
SETTLE_S = 5.0  # a change of the nodes waits this long for those that follow it
HOMIE = "4.0.0"
ROOT = "homie"
IMPLEMENTATION = "meshcore-home-gateway"
TEXTS = "chat"  # the type of the contacts that take texts, the companions: their nodes `send`
GATEWAY = "gateway"  # the gateway's own node; a contact's ID is its key, 64 hex digits


def device_id(node_id: str) -> str:
    """The Homie ID of the gateway's device: Homie's IDs are `[a-z0-9-]`, a node ID can hold `_`
    (which neither starts nor ends it, config.ID_RULE)."""
    return f"meshcore-{node_id.replace('_', '-')}"


@dataclass(frozen=True, slots=True)
class Property:
    name: str
    datatype: str
    unit: str = ""
    format: str = ""
    settable: bool = False
    retained: bool = True

    def attributes(self) -> dict[str, str]:
        found = {"$name": self.name, "$datatype": self.datatype}
        if self.unit:
            found["$unit"] = self.unit
        if self.format:
            found["$format"] = self.format
        if self.settable:
            found["$settable"] = "true"
        if not self.retained:
            found["$retained"] = "false"
        return found


PROPERTIES: dict[str, Property] = {
    "battery": Property("Battery", "float", unit="V"),
    "position": Property("Position", "string"),  # latitude,longitude in degrees
    "last-heard": Property("Last heard", "string"),  # ISO 8601 in UTC
    "telemetry-state": Property("Telemetry", "enum", format=",".join(STATES)),
    "send": Property("Send", "string", settable=True, retained=False),
}
RADIO = Property("Radio", "enum", format="connected,disconnected")  # the gateway node's


def values(document: Mapping[str, Any]) -> dict[str, bytes | None]:
    """A node's property values from its contact's document; None: unknown."""
    battery, state = document.get("battery_v"), document.get("telemetry_state")
    latitude, longitude = document.get("latitude"), document.get("longitude")
    heard = document.get("heard_at")
    return {
        "battery": None if battery is None else repr(float(battery)).encode(),
        "position": (
            None if latitude is None or longitude is None else f"{latitude},{longitude}".encode()
        ),
        "last-heard": None if heard is None else str(heard).encode(),
        "telemetry-state": None if state is None else str(state).encode(),
    }


class HomieDevice:
    """What the adapter publishes, from the documents: `publish(topic, payload, retain)` sends a
    message, `send` hands over a text to send (as `contacts/<key>/send` does), `call_later` is
    the event loop's timers."""

    def __init__(
        self,
        node_id: str,
        name: str,
        limit: int,
        topics: Topics,
        *,
        publish: Callable[[str, bytes, bool], None],
        send: Callable[[SendRequest], None],
        call_later: Callable[[float, Callable[[], None]], Cancellable],
    ) -> None:
        self.base = f"{ROOT}/{device_id(node_id)}"
        self.will = f"{self.base}/$state"
        self.set_subscription = f"{self.base}/+/send/set"
        self._name = name
        self._limit = limit
        self._topics = topics
        self._publish = publish
        self._send = send
        self._call_later = call_later
        self._documents: dict[str, dict[str, Any]] = {}  # key -> its latest document
        self._nodes: list[str] = []  # the keys with a node, in their order
        self._radio = False  # the state document says the radio is connected
        self._retained: dict[str, bytes] = {}  # topic -> payload published on this connection
        self._before: set[str] = set()  # topics published on earlier connections of this run
        self._online = False
        self._announced = False  # the device went out on this connection
        self._held = True  # until the radio's first contact table, at most HOLD_S
        self._hold: Cancellable | None = None
        self._settle: Cancellable | None = None

    def document(self, topic: str, document: Mapping[str, Any] | None) -> None:
        """A document was published (None: cleared)."""
        if topic == self._topics.state:
            radio = document is not None and document.get("radio") == "connected"
            if radio != self._radio:
                self._radio = radio
                if self._announced:
                    self._put(f"{GATEWAY}/radio", self._radio_value())
                    self._put("$state", self._state())
            return
        key = self._contact(topic)
        if key is None:
            return
        if document is None:
            self._documents.pop(key, None)
        else:
            self._documents[key] = dict(document)
        if not self._announced:
            return  # the announcement takes the documents as they are then
        if key in self._nodes and key in self._documents:
            self._node(key)  # its values at once
        if self._chosen() != self._nodes:
            self._later()

    def contacts_read(self) -> None:
        """The radio's contact table was read; its documents follow at once. The first ends the
        wait of a start."""
        if self._hold is not None:
            self._hold.cancel()
            self._hold = None
        self._held = False
        self._later()

    def connected(self) -> None:
        """The connection is up: everything again, and the topics of nodes gone meanwhile are
        cleared; at a start, once the radio's first contact table has come (at most HOLD_S)."""
        self._online = True
        self._before |= self._retained.keys()
        self._retained = {}
        if not self._held:
            self._announce()
            return
        self._put("$state", b"init")  # coming online (Homie 4.0.0, Device Lifecycle)
        if self._hold is None:
            self._hold = self._call_later(HOLD_S, self._release)

    def disconnected(self) -> None:
        self._online = self._announced = False
        if self._settle is not None:
            self._settle.cancel()
            self._settle = None

    def message(self, topic: str, payload: bytes, retained: bool) -> None:
        """A message on `<node>/send/set`: a text for that companion, refused as on
        `contacts/<key>/send` (a retained one, one too long); the echo Homie asks for. None is
        taken while the device is not online: a stop silences it first, once the bridge that
        would send the text has stopped."""
        if not self._online:
            log.info("Homie: not connected; message dropped")
            return
        prefix, suffix = self.base + "/", "/send/set"
        key = topic[len(prefix) : -len(suffix)]
        if not (topic.startswith(prefix) and topic.endswith(suffix)) or "/" in key:
            return
        if (
            key not in self._nodes
            or key not in self._documents
            or "send" not in self._properties(key)
        ):
            log.info("Homie: %s is no companion's node; message dropped", key[:12] or "''")
            return
        self._send(SendRequest(key, payload, retained, contact=True))
        if not retained:
            self._publish(f"{self.base}/{key}/send", payload, False)

    # --- internals -------------------------------------------------------------------------------

    def _contact(self, topic: str) -> str | None:
        prefix = self._topics.contacts + "/"
        key = topic.removeprefix(prefix)
        return key if topic.startswith(prefix) and key and "/" not in key else None

    def _state(self) -> bytes:
        """`alert` while the radio is not connected: the device's values do not change then."""
        return b"ready" if self._radio else b"alert"

    def _radio_value(self) -> bytes:
        return b"connected" if self._radio else b"disconnected"

    def _chosen(self) -> list[str]:
        """The nodes: those that have one keep it; then protected contacts, favourites,
        companions, the most recently heard (contact_devices.priority, from the documents)."""
        kept = [k for k in self._nodes if k in self._documents]
        new = sorted(k for k in self._documents if k not in kept)
        new.sort(key=lambda k: str(self._documents[k].get("name") or ""))
        new.sort(key=lambda k: str(self._documents[k].get("heard_at") or ""), reverse=True)
        new.sort(key=self._rank)
        return (kept + new)[: self._limit]

    def _rank(self, key: str) -> tuple[bool, bool, bool]:
        document = self._documents[key]
        return (
            not document.get("protected"),
            not document.get("favourite"),
            document.get("type") != TEXTS,
        )

    def _release(self) -> None:
        """HOLD_S after the first connection, without a contact table: the device as it is."""
        self._hold = None
        self._held = False
        if self._online:
            self._announce()

    def _later(self) -> None:
        """The nodes may change: the device again after SETTLE_S, once for every change then."""
        if self._online and self._settle is None:
            self._settle = self._call_later(SETTLE_S, self._settled)

    def _settled(self) -> None:
        self._settle = None
        if self._announced:
            self._update()
        else:
            self._announce()

    def _announce(self) -> None:
        """Every attribute and node, between `init` and the state; then the topics published on
        an earlier connection that are not again are cleared."""
        self._nodes = self._chosen()
        self._put("$state", b"init")
        self._put("$homie", HOMIE.encode())
        self._put("$name", self._name.encode())
        self._put("$extensions", b"")
        self._put("$implementation", IMPLEMENTATION.encode())
        self._put("$nodes", ",".join([GATEWAY, *self._nodes]).encode())
        self._gateway()
        for key in self._nodes:
            self._node(key)
        for topic in sorted(self._before - self._retained.keys()):
            self._publish(topic, b"", True)
        self._before = set()
        self._put("$state", self._state())
        self._announced = True

    def _update(self) -> None:
        nodes = self._chosen()
        if nodes == self._nodes:
            return
        gone = [k for k in self._nodes if k not in nodes]
        self._nodes = nodes
        self._put("$state", b"init")
        self._put("$nodes", ",".join([GATEWAY, *nodes]).encode())
        for each in gone:
            self._clear(each)
        for each in nodes:
            self._node(each)
        self._put("$state", self._state())

    def _properties(self, key: str) -> list[str]:
        texts = self._documents[key].get("type") == TEXTS
        return [p for p in PROPERTIES if texts or p != "send"]

    def _gateway(self) -> None:
        self._put(f"{GATEWAY}/$name", b"Gateway")
        self._put(f"{GATEWAY}/$type", b"gateway")
        self._put(f"{GATEWAY}/$properties", b"radio")
        for attribute, text in RADIO.attributes().items():
            self._put(f"{GATEWAY}/radio/{attribute}", text.encode())
        self._put(f"{GATEWAY}/radio", self._radio_value())

    def _node(self, key: str) -> None:
        document, names = self._documents[key], self._properties(key)
        self._put(f"{key}/$name", str(document.get("name") or key[:12]).encode())
        self._put(f"{key}/$type", str(document.get("type") or "").encode())
        self._put(f"{key}/$properties", ",".join(names).encode())
        for name in names:
            for attribute, text in PROPERTIES[name].attributes().items():
                self._put(f"{key}/{name}/{attribute}", text.encode())
        for name, value in values(document).items():
            self._put(f"{key}/{name}", value or b"")

    def _clear(self, key: str) -> None:
        prefix = f"{self.base}/{key}/"
        for topic in [t for t in self._retained if t.startswith(prefix)]:
            del self._retained[topic]
            self._publish(topic, b"", True)

    def _put(self, path: str, payload: bytes) -> None:
        topic = f"{self.base}/{path}"
        if self._retained.get(topic) == payload:
            return
        self._retained[topic] = payload
        self._publish(topic, payload, True)
