"""A Home Assistant device for every contact of the radio (ADR 0017, docs/home-assistant.md).

Each contact gets its own discovery document on `<prefix>/device/<gateway>/contact_<key>/config`,
one level below the gateway's device ID, so one subscription sees all of them and a removal touches
one contact only. Rules checked against Home Assistant 2026.10 (home-assistant/tests):

* Components only with data: a position tracker once the contact has a position, sending and
  messages only for node types that send texts. The button that removes the contact from the
  radio is on every one and available while a request would be taken (not for a protected
  contact, not with management off, not while the radio is away). Only the tracker carries
  coordinates, because the map shows every entity with `latitude` and `longitude` attributes.
  None is disabled by default:
  Home Assistant clears the discovery topic itself when it removes a device with a disabled
  entity, and warns about the echo.
* Entity IDs `<platform>.mesh_<slug>_<component>`, the slug taken from the contact's name when
  its device first appears and kept, so renaming a contact renames its device only, and recorder
  globs can match every contact's messages.
* Home Assistant removes a device on an empty document only if it discovered the device since its
  own start: each removal is sent as placeholders first, which bind what it kept, and once more
  after its next birth message if it may not have been running.
* An empty document the gateway did not send is the user deleting the device in Home Assistant:
  the contact stays hidden there until it leaves the radio's table or the user shows it again.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from collections.abc import Callable, Iterable, Mapping
from typing import TYPE_CHECKING, Any

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.mesh import Mesh, Tracked, type_name
from meshcore_home_gateway.mqtt.discovery import device_id, origin, placeholder
from meshcore_home_gateway.mqtt.payloads import encode
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codes import ContactFlag, NodeType
from meshcore_home_gateway.state import REMOVAL_KEEP_S, DeviceRecord, RemovedDevice
from meshcore_home_gateway.views import contact_event

if TYPE_CHECKING:
    from meshcore_home_gateway.observer import Publisher
    from meshcore_home_gateway.state import ContactDeviceRecords

log = logging.getLogger(__name__)

MODELS: dict[int, str] = {
    NodeType.CHAT: "Companion", NodeType.REPEATER: "Repeater",
    NodeType.ROOM_SERVER: "Room server", NodeType.SENSOR: "Sensor",
}  # fmt: skip
TEXT_TYPES = frozenset({NodeType.CHAT, NodeType.ROOM_SERVER})  # nodes that send texts
ECHO_S = 10.0  # an empty document seen this long after the gateway sent one is its own
SLUG_CHARS = 32
# letters that Unicode does not decompose into an ASCII letter and a mark
_LETTERS = str.maketrans({
    "ß": "ss", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE", "ø": "o", "Ø": "O", "ł": "l",
    "Ł": "L", "đ": "d", "Đ": "D", "þ": "th", "Þ": "TH",
})  # fmt: skip
_OBJECT = re.compile(r"contact_([0-9a-f]{64})")
POSITION_KNOWN = "value_json.latitude is number and value_json.longitude is number"
# `is defined`, `is true`: a document retained by an earlier version, without the key, reads as
# offline and logs no warning in Home Assistant (docs/platform-notes.md)
RADIO_CONNECTED = (
    "{{ 'online' if value_json is defined and value_json.radio == 'connected' else 'offline' }}"
)
REMOVABLE = (
    "{{ 'online' if value_json is defined and value_json.removable is true else 'offline' }}"
)
ACCURACY_KNOWN = (
    "value_json.position_accuracy_m is defined and value_json.position_accuracy_m is number"
)
_POSITION_KEYS = ("latitude", "longitude", "altitude_m", "position_at", "position_source")


def _position_attributes(extra: str = "") -> str:
    return "{" + ", ".join(f"'{k}': value_json.{k}" for k in _POSITION_KEYS) + extra + "}"


# the tracker's attributes; gps_accuracy only from telemetry (a number: null would count as 0
# with a warning), read only where the position is known, so the empty clearing message gives {}
POSITION_ATTRIBUTES = (
    "{{ (("
    + _position_attributes(", 'gps_accuracy': value_json.position_accuracy_m")
    + f" if {ACCURACY_KNOWN} else "
    + _position_attributes()
    + f") if {POSITION_KNOWN} else {{}}) | tojson }}}}"
)


def slugify(name: str) -> str:
    """The name as lower-case ASCII letters and digits, anything else as single underscores."""
    plain = unicodedata.normalize("NFKD", name.translate(_LETTERS))
    ascii_name = plain.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"[^a-z0-9]+", "_", ascii_name).strip("_")[:SLUG_CHARS].rstrip("_")


def contact_device_id(config: Config, key: str) -> str:
    return f"{device_id(config)}_contact_{key}"


def contact_discovery_topic(config: Config, key: str) -> str:
    return f"{config.homeassistant.prefix}/device/{device_id(config)}/contact_{key}/config"


def contact_discovery_subscription(config: Config) -> str:
    return f"{config.homeassistant.prefix}/device/{device_id(config)}/+/config"


def key_from_discovery_topic(config: Config, topic: str) -> str | None:
    prefix = f"{config.homeassistant.prefix}/device/{device_id(config)}/"
    if not (topic.startswith(prefix) and topic.endswith("/config")):
        return None
    match = _OBJECT.fullmatch(topic[len(prefix) : -len("/config")])
    return match.group(1) if match else None


def contact_payload(
    config: Config,
    topics: Topics,
    tracked: Tracked,
    slug: str,
    keep: Iterable[str] = (),
) -> dict[str, Any]:
    """The discovery document of one contact. Components it was announced with earlier (`keep`)
    stay, also without data: Home Assistant clears the whole device's topic when it removes a
    disabled entity, and the gateway would take that for a deletion by the user."""
    kept = frozenset(keep)
    node = tracked.node
    key = node.public_key
    uid = contact_device_id(config, key)
    document = topics.contact(key)

    def component(key: str, platform: str, suffix: str, **options: Any) -> dict[str, Any]:
        return {
            "p": platform, "unique_id": f"{uid}_{key}",
            "default_entity_id": f"{platform}.mesh_{slug}{suffix}", **options,
        }  # fmt: skip

    state = {"state_topic": document}
    components: dict[str, dict[str, Any]] = {
        "last_heard": component(
            "last_heard", "sensor", "_last_heard", name="Last heard", device_class="timestamp",
            value_template="{{ value_json.heard_at }}", **state,
        ),
        "hops": component(
            "hops", "sensor", "_hops", name="Hops", icon="mdi:transit-connection-variant",
            entity_category="diagnostic", value_template="{{ value_json.hops }}", **state,
        ),
        "snr": component(  # no name: Home Assistant names it after the class, translated
            "snr", "sensor", "_snr", device_class="signal_strength", unit_of_measurement="dB",
            state_class="measurement", suggested_display_precision=2,
            entity_category="diagnostic", value_template="{{ value_json.snr_db }}", **state,
        ),
    }  # fmt: skip
    if node.node_type in TEXT_TYPES or "message" in kept:  # the text is an event attribute
        components["message"] = component(
            "message", "event", "_message", name="Message",
            state_topic=topics.contact_received(key), event_types=["contact"],
        )  # fmt: skip
    if node.node_type == NodeType.CHAT or "send" in kept:
        components["send"] = component(  # no name: the device's main feature, named like it
            "send", "notify", "", name=None, command_topic=topics.contact_send(key)
        )
    components["remove"] = component(  # on every contact; available while it would be taken
        "remove", "button", "_remove", name="Remove from radio",
        command_topic=topics.contact_remove(key), payload_press="remove",
        entity_category="config", icon="mdi:account-remove",
        availability=[
            {"topic": topics.availability},
            {"topic": topics.state, "value_template": RADIO_CONNECTED},
            {"topic": document, "value_template": REMOVABLE},
        ],
        availability_mode="all",
    )  # fmt: skip
    if tracked.fix is not None or "position" in kept:
        components["position"] = component(
            "position", "device_tracker", "", name="Position", source_type="gps",
            json_attributes_topic=document, json_attributes_template=POSITION_ATTRIBUTES,
            # replaces the shared availability: also gone while no position is known
            availability=[
                {"topic": topics.availability},
                {"topic": document,
                 "value_template": f"{{{{ 'online' if {POSITION_KNOWN} else 'offline' }}}}"},
            ],
            availability_mode="all",
        )  # fmt: skip
    return {
        "dev": {
            "ids": [uid],
            "name": node.name or key[:12],
            "mf": "MeshCore",
            "mdl": MODELS.get(node.node_type, type_name(node.node_type)),
            "sn": key,
            "via_device": device_id(config),
        },
        "o": origin(),
        "availability": [{"topic": topics.availability}],
        "qos": 1,
        "cmps": components,
    }


def removal_payload(
    config: Config, topics: Topics, key: str, components: Mapping[str, str]
) -> dict[str, Any]:
    """Every component of a contact device as a placeholder: binds what Home Assistant kept from
    an earlier run, so that the empty document sent next removes the device."""
    uid = contact_device_id(config, key)
    stand_ins = {
        name: stand_in
        for name, platform in components.items()
        if (stand_in := placeholder(f"{uid}_{name}", platform, topics)) is not None
    }
    return {
        "dev": {"ids": [uid], "name": key[:12], "via_device": device_id(config)},
        "o": origin(),
        "availability": [{"topic": topics.availability}],
        "qos": 1,
        "cmps": stand_ins,
    }


def priority(tracked: Tracked, protected: frozenset[str]) -> tuple[Any, ...]:
    """Which contacts get a device first when there are more than the limit: protected ones,
    favourites, companions, then the most recently heard."""
    node = tracked.node
    return (
        node.public_key not in protected,
        not node.flags & ContactFlag.FAVOURITE,
        node.node_type != NodeType.CHAT,
        -(tracked.heard.at if tracked.heard else 0.0),
        node.name,
        node.public_key,
    )


class ContactDevices:
    """Announces, updates and removes the contact devices as the radio's table changes."""

    def __init__(
        self,
        config: Config,
        topics: Topics,
        bus: Publisher,
        records: ContactDeviceRecords,
        *,
        protected: frozenset[str] = frozenset(),
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._config = config
        self._topics = topics
        self._bus = bus
        self._records = records
        self._protected = protected
        self._wall = wall_clock
        self._limit = config.homeassistant.contact_devices
        self._mesh: Mesh | None = None  # None until the first table read
        self._published: dict[str, bytes] = {}  # key -> document last sent
        self._echoes: dict[str, int] = {}  # key -> empty documents sent, not seen back yet
        self._emptied: dict[str, float] = {}  # key -> when the gateway last sent it empty
        self._ha_running = False  # Home Assistant announced itself and has not gone offline
        self._without = 0  # contacts without a device because of the limit, as last logged

    @property
    def watching(self) -> bool:
        """Whether there can be contact devices (and the gateway watches their topics)."""
        return self._limit > 0

    def update(self, mesh: Mesh) -> None:
        """The table was read: announce new and changed contacts, remove those that left."""
        self._mesh = mesh
        records = self._records
        records.hidden &= set(mesh.contacts)  # a contact that left is shown again if it returns
        wanted = self._select(mesh)
        for key in [k for k in records.announced if k not in wanted]:
            self._remove(key)
        for key in wanted:
            self._announce(mesh.contacts[key])
        now = self._wall()
        records.removed = {
            k: r for k, r in records.removed.items()
            if k not in records.announced and now - r.since < REMOVAL_KEEP_S
        }  # fmt: skip
        records.save()

    def announce_all(self, *, birth: bool = False) -> None:
        """Send every document again: the broker connected, the radio became known, or (`birth`)
        Home Assistant started. After its start, the pending removals are sent once more and are
        then done: Home Assistant runs and takes them."""
        if birth:
            self._ha_running = True
            for key, removed in self._records.removed.items():
                self._send_removal(key, removed.components)
            self._records.removed.clear()
            self._records.save()
        self._published.clear()
        if self._mesh is not None:
            for key in list(self._records.announced):
                tracked = self._mesh.contacts.get(key)
                if tracked is not None:
                    self._announce(tracked)

    def ha_offline(self) -> None:
        """Home Assistant went offline (or the broker connection was lost, which hides it):
        removals from now on wait for its next start."""
        self._ha_running = False

    def discovery_message(self, topic: str, payload: bytes, retained: bool) -> None:
        """A message on one of the contact discovery topics (the gateway's own come back too)."""
        key = key_from_discovery_topic(self._config, topic)
        if key is None or payload or retained:
            return
        if self._echoes.get(key, 0) > 0:  # one of the gateway's own empty documents
            self._echoes[key] -= 1
            return
        if self._wall() - self._emptied.get(key, -ECHO_S) < ECHO_S:
            return  # a copy Home Assistant sends while it removes the device
        record = self._records.announced.pop(key, None)
        if record is None:
            return
        self._published.pop(key, None)
        self._records.hidden.add(key)
        self._records.save()
        log.info("Home Assistant: contact %s deleted by the user, hidden there", key[:12])
        self._event("hidden", key)

    def show_hidden(self) -> None:
        """Bring back the contacts the user deleted in Home Assistant."""
        hidden, self._records.hidden = sorted(self._records.hidden), set()
        for key in hidden:
            self._event("shown", key)
        if self._mesh is not None:
            self.update(self._mesh)
        else:
            self._records.save()

    # --- internals -------------------------------------------------------------------------------

    def _select(self, mesh: Mesh) -> list[str]:
        """Contacts with a device: those that have one keep it unless the limit went down."""
        records = self._records
        visible = [
            t for k, t in mesh.contacts.items()
            if k not in records.hidden and (k in records.announced or not self._unheard(t))
        ]  # fmt: skip

        def order(group: Iterable[Tracked]) -> list[str]:
            ranked = sorted(group, key=lambda t: priority(t, self._protected))
            return [t.node.public_key for t in ranked]

        kept = order(t for t in visible if t.node.public_key in records.announced)
        new = order(t for t in visible if t.node.public_key not in records.announced)
        chosen = (kept + new)[: self._limit]
        without = len(visible) - len(chosen)
        if without != self._without:
            self._without = without
            if without:
                log.info("Home Assistant: %d contacts without a device "
                         "(homeassistant.contact_devices = %d)", without, self._limit)  # fmt: skip
        return chosen

    def _unheard(self, tracked: Tracked) -> bool:
        """A protected contact the radio profile put on the radio that never had a device and has
        not advertised yet: only a key and a placeholder name, which would fix its entity IDs.
        Its device comes with its first advert; one that had a device gets it back at once."""
        key = tracked.node.public_key
        return (
            key in self._protected
            and not tracked.node.last_advert
            and key not in self._records.slugs
        )

    def _announce(self, tracked: Tracked) -> None:
        """Send the contact's document if it changed. A component it had keeps its place;
        one this version cannot build any more stays in the record for the device's removal."""
        records, key = self._records, tracked.node.public_key
        record = records.announced.get(key)
        if record is None:
            slug = records.slugs.get(key) or self._new_slug(tracked)
            records.slugs[key] = slug  # the entity IDs stay when the contact comes back
            record = DeviceRecord(slug, {})
            records.announced[key] = record
            records.removed.pop(key, None)
            log.info("Home Assistant: device for contact %r (%s)", tracked.node.name, key[:12])
        document = contact_payload(
            self._config, self._topics, tracked, record.slug, keep=record.components
        )
        record.components = {
            **record.components, **{n: c["p"] for n, c in document["cmps"].items()},
        }  # fmt: skip
        payload = encode(document)
        if self._published.get(key) != payload:
            self._published[key] = payload
            self._bus.publish(contact_discovery_topic(self._config, key), payload)

    def _remove(self, key: str) -> None:
        record = self._records.announced.pop(key)
        if not self._ha_running:  # it may not take the removal now: again after its start
            self._records.removed[key] = RemovedDevice(record.components, self._wall())
        self._published.pop(key, None)
        self._send_removal(key, record.components)
        log.info("Home Assistant: device of contact %s removed", key[:12])

    def _send_removal(self, key: str, components: Mapping[str, str]) -> None:
        """Placeholders, then the empty document: removes the device also when Home Assistant
        has not discovered it since its start."""
        topic, now = contact_discovery_topic(self._config, key), self._wall()
        payload = removal_payload(self._config, self._topics, key, components)
        self._bus.publish(topic, encode(payload))
        self._echoes[key] = self._echoes.get(key, 0) + 1
        self._emptied = {k: at for k, at in self._emptied.items() if now - at < ECHO_S}
        self._emptied[key] = now
        self._bus.publish(topic, b"")

    def _new_slug(self, tracked: Tracked) -> str:
        """The contact's name as slug, made unique with the key's start among every slug given
        (also to contacts that left: they get theirs back)."""
        key = tracked.node.public_key
        slug = slugify(tracked.node.name) or key[:8]
        taken = {s for k, s in self._records.slugs.items() if k != key}
        return f"{slug}_{key[:4]}" if slug in taken else slug

    def _event(self, kind: str, key: str) -> None:
        tracked = self._mesh.contacts.get(key) if self._mesh else None
        node = tracked.node if tracked else None
        self._bus.publish(self._topics.contact_event, encode(contact_event(kind, key, node)))
