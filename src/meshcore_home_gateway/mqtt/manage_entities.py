"""The controls that add contacts in Home Assistant (ADR 0017, docs/home-assistant.md): a select
of the heard nodes and a text for a key or contact link, on the gateway's device.

They have a discovery document of their own, `<prefix>/device/<gateway>/manage_contacts/config`,
whose components join the gateway's device: a select's options exist only in its discovery, and
the heard nodes change often, so the gateway's main document is not sent again for them. Rules
checked against Home Assistant 2026.10 (docs/platform-notes.md):

* A select announced again with other options keeps its entity. Its state stays `unknown`
  (`value_template` "None"), so every choice is a change and publishes the label.
* A text's state stays empty, which its pattern must allow. The pattern keeps contact cards out:
  the frontend cuts a paste at 255 characters, and a cut card still parses, with a cut name.
* Both are available while the gateway is online, the radio connected and management on.
* Components that a later configuration or version no longer has are removed as in the main
  document: placeholders first, which bind what Home Assistant kept, then the document without
  them.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any, Protocol

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.core.contacts import LABEL_TYPES, node_label
from meshcore_home_gateway.core.mesh import Mesh
from meshcore_home_gateway.mqtt.contact_devices import RADIO_CONNECTED
from meshcore_home_gateway.mqtt.discovery import device_id, origin, with_removed
from meshcore_home_gateway.mqtt.payloads import encode
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.views import look_alike_nodes

if TYPE_CHECKING:
    from meshcore_home_gateway.observer import Publisher
    from meshcore_home_gateway.state import AnnouncedComponents

log = logging.getLogger(__name__)

OBJECT_ID = "manage_contacts"
NODE_OPTIONS = 20  # heard nodes offered: the most recently heard
OPTIONS_INTERVAL_S = 30.0  # the options are sent at most this often, but when a contact changed
# and at most 10 changes at once, then 1 a minute (70 in any hour), announcements included: Home
# Assistant warns from the 101st change of an entity's capabilities within an hour, and it keeps
# counting while the gateway restarts, so a start begins with the 2 it needs (the announcement at
# the connect, the heard nodes of the first table read) (docs/platform-notes.md)
OPTIONS_PER_S, OPTIONS_BURST, OPTIONS_AT_START = 1 / 60, 10.0, 2.0
# Home Assistant checks a text against these before it publishes it (at most 255 characters);
# spaces around it, as a copied key often has, are stripped by the gateway
TEXT_PATTERN = r"^$|^\s*(?:[0-9A-Fa-f]{64}|meshcore://contact/add\?\S+)\s*$"
MANAGE_ON = "{{ 'online' if value_json is defined and value_json.manage is true else 'offline' }}"


class _Handle(Protocol):
    def cancel(self) -> None: ...


def manage_topic(config: Config) -> str:
    return f"{config.homeassistant.prefix}/device/{device_id(config)}/{OBJECT_ID}/config"


def node_options(mesh: Mesh, protected: Iterable[str] = ()) -> list[str]:
    """The labels of the heard nodes to offer: companions, room servers and sensors, no
    look-alikes (they would be refused, or block the node they imitate), the most recently heard,
    ordered by name. A label names one node: look-alikes share their first 12 digits."""
    flagged = look_alike_nodes(mesh, protected)
    offered = [
        t.node for t in reversed(mesh.nodes.values())
        if t.node.node_type in LABEL_TYPES and t.node.public_key not in flagged
    ][:NODE_OPTIONS]  # fmt: skip
    ordered = sorted(offered, key=lambda n: (n.name.casefold(), n.public_key))
    return [node_label(n.public_key, n.name, n.node_type) for n in ordered]


def manage_payload(
    config: Config,
    topics: Topics,
    options: list[str],
    removed: dict[str, str] | None = None,
    *,
    bind: bool = False,
) -> dict[str, Any]:
    """The document; `removed` (key -> platform) as in `discovery_payload`."""
    uid = device_id(config)

    def component(key: str, platform: str, name: str, **options: Any) -> dict[str, Any]:
        return {
            "p": platform, "unique_id": f"{uid}_{key}",
            "default_entity_id": f"{platform}.{uid}_{key}", "name": name,
            "state_topic": topics.contacts, "entity_category": "config",
            "icon": "mdi:account-plus", **options,
        }  # fmt: skip

    components = {
        "add_node": component(
            "add_node", "select", "Add heard node", command_topic=topics.add_node,
            options=options, value_template="None",
        ),
        "add_contact": component(
            "add_contact", "text", "Add contact (key or link)", command_topic=topics.add_contact,
            value_template="{{ '' }}", min=0, max=255, pattern=TEXT_PATTERN,
        ),
    }  # fmt: skip
    with_removed(components, uid, topics, removed, bind=bind)
    return {
        "dev": {"ids": [uid], "name": config.homeassistant.device_name},
        "o": origin(),
        "availability": [
            {"topic": topics.availability},
            {"topic": topics.state, "value_template": RADIO_CONNECTED},
            {"topic": topics.contacts, "value_template": MANAGE_ON},
        ],
        "availability_mode": "all",
        "qos": 1,
        "cmps": components,
    }


def _call_later(delay_s: float, callback: Callable[[], None]) -> _Handle:
    return asyncio.get_running_loop().call_later(delay_s, callback)


class ManageEntities:
    """Announces the controls and keeps the select's options current."""

    def __init__(
        self,
        config: Config,
        topics: Topics,
        bus: Publisher,
        mesh: Mesh,
        *,
        records: AnnouncedComponents | None = None,
        protected: frozenset[str] = frozenset(),
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        call_later: Callable[[float, Callable[[], None]], _Handle] = _call_later,
        connected: Callable[[], bool] = lambda: True,
    ) -> None:
        """`mesh`: the observer's; `records`: the components announced in earlier runs;
        `connected`: the broker connection, without which new options wait for the announcement
        that follows the reconnect, or for the next token after it (they would only take places
        of events in the backlog)."""
        self._config = config
        self._topics = topics
        self._bus = bus
        self._mesh = mesh
        self._records = records
        self._protected = protected
        self._mono = monotonic
        self._wall = wall_clock
        self._call_later = call_later
        self._connected = connected
        self._removed: dict[str, str] | None = None  # earlier components, removed in this run
        self._options: list[str] | None = None  # as last sent; None: not sent yet
        self._sent_at = 0.0
        self._changes = RateLimit(
            OPTIONS_PER_S, OPTIONS_BURST, now=monotonic(), tokens=OPTIONS_AT_START
        )
        self._timer: _Handle | None = None
        self._due = 0.0  # when the timer fires
        self._urgent = False  # the change it waits for is one of the table

    def announce(self) -> None:
        """Send the document again: the broker connected, Home Assistant started, or it cleared
        the topic (the user deleted the gateway's device)."""
        if self._removed is None:  # first announcement: what earlier runs had and this lacks
            current = manage_payload(self._config, self._topics, [])["cmps"]
            platforms = {key: component["p"] for key, component in current.items()}
            records = self._records
            self._removed = records.replace(platforms, now=self._wall()) if records else {}
            if self._removed:
                log.info("Home Assistant: removing %s", ", ".join(sorted(self._removed)))
        options = self._announced_options()
        if self._removed:  # Home Assistant removes only what it discovered since its start
            bind = manage_payload(self._config, self._topics, options, self._removed, bind=True)
            self._bus.publish(manage_topic(self._config), encode(bind))
        self._send(options)
        self.nodes_changed(read=False)  # a change held back waits for its token, still as urgent

    def _announced_options(self) -> list[str]:
        """The current options, or those sent last when there is no token for a change: Home
        Assistant counts a change that comes with an announcement too."""
        options = node_options(self._mesh, self._protected)
        held = self._options is not None and not self._changes.ready(self._mono())
        return self._options if held and self._options is not None else options

    def nodes_changed(self, read: bool) -> None:
        """The heard nodes may have changed. When a contact joined or left the table (`read`),
        as after a request to add or remove one, changed options are sent at once."""
        if self._options is None:
            return  # not announced yet
        if node_options(self._mesh, self._protected) == self._options:
            self._urgent = False  # also while the broker is away: nothing waits any more
            return
        if not self._connected():
            return  # the announcement after the reconnect sends them
        read = read or self._urgent  # a table change that waits stays urgent
        now = self._mono()
        interval_s = 0.0 if read else self._sent_at + OPTIONS_INTERVAL_S - now
        wait_s = max(interval_s, self._changes.wait_s(now))
        if wait_s <= 0:
            self._send()
            return
        self._urgent = read
        if self._timer is None or now + wait_s < self._due:  # none due, or a later one
            if self._timer is not None:
                self._timer.cancel()
            self._timer, self._due = self._call_later(wait_s, self._timer_due), now + wait_s

    def _timer_due(self) -> None:
        self._timer = None
        self.nodes_changed(self._urgent)  # sent now, or later if the change budget is spent

    def _send(self, options: list[str] | None = None) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        now, current = self._mono(), node_options(self._mesh, self._protected)
        options = current if options is None else options
        if options != self._options:  # a change for Home Assistant: it counts, and is the last
            self._changes.allow(now)
            self._sent_at = now
        if options == current:
            self._urgent = False
        document = manage_payload(self._config, self._topics, options, self._removed)
        self._bus.publish(manage_topic(self._config), encode(document))
        self._options = options
