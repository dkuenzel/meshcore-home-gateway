"""The gateway's retained topics as the broker holds them (SEC-005, ADR 0015).

The documents and the availability are retained, so that a front end that starts later reads
them, and the broker keeps whatever was written last by any client allowed to write the topic.
The adapter subscribes to these topics and hands every live message on them here. The gateway's
own publishes come back unchanged (MQTT 3.1.1 has no option to leave them out); a write of another
client is undone: the topic gets the gateway's last payload again, or is cleared where the
gateway has none, at most once per topic every HEAL_INTERVAL_S, with a warning. The copies the
broker sends for a new subscription (with the retain flag) are not compared: the gateway
publishes all its documents at every connect, after the subscription. After a reconnect paho
sends again what the broker had not acknowledged (docs/platform-notes.md): those copies are the
gateway's too, and one that comes after the current payload's is followed by it again (#101).

This keeps a forged or stale document short-lived; it does not prevent one. That only the gateway
writes its topics is the broker's ACL's job (docs/mqtt-api.md, Securing the broker).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.mqtt.topics import Topics

log = logging.getLogger(__name__)

HEAL_INTERVAL_S = 10.0  # a topic another client keeps writing is restored at most this often
WARNING_INTERVAL_S = 60.0  # per topic; the next warning counts the writes in between
ECHOES = 32  # the gateway's payloads per topic that went out and have not come back yet
# writes to topics the gateway has no payload for are bounded together: there is no end of them
CLEARS_PER_S, CLEARS_BURST = 1.0, 10.0
HINT = "only the gateway may write its topics: docs/mqtt-api.md, Securing the broker"


class Cancellable(Protocol):
    def cancel(self) -> None: ...


@dataclass
class _Topic:
    payload: bytes  # what the gateway wrote last
    echoes: deque[bytes] = field(default_factory=lambda: deque(maxlen=ECHOES))
    # out before the last reconnect and not back: paho sends them again (QoS 1)
    resent: deque[bytes] = field(default_factory=lambda: deque(maxlen=ECHOES))
    restored_at: float | None = None
    restore: Cancellable | None = None  # a restore that waits for the interval
    warned_at: float | None = None
    writes: int = 0  # by other clients, since the last warning


class RetainedTopics:
    def __init__(
        self,
        topics: Topics,
        publish: Callable[[str, bytes], None],
        call_later: Callable[[float, Callable[[], None]], Cancellable],
        monotonic: Callable[[], float],
        *,
        drafts: bool = False,
    ) -> None:
        """`publish`: writes a retained payload, if the broker is connected; `drafts`: the drafts
        of Home Assistant's chats are the gateway's too (`homeassistant/draft/<kind>/<id>`). The
        broker's copies of those reach the drafts part first (mqtt.client), which deletes those
        of no current chat."""
        self._fixed = frozenset(
            {
                topics.availability,
                topics.state,
                topics.stats,
                topics.contacts,
                topics.nodes,
                topics.history,
                topics.pending,
                topics.settings,
                topics.config,
                topics.refused_commands,
            }
        )
        self._contact_prefix = topics.contacts + "/"
        self._topics_layout = topics
        self._drafts = drafts
        self.filters = (*sorted(self._fixed), topics.contact("+"))
        if drafts:
            self.filters += (topics.draft_subscription,)
        self._publish = publish
        self._call_later = call_later
        self._now = monotonic
        self._topics: dict[str, _Topic] = {}
        self._clears = RateLimit(CLEARS_PER_S, CLEARS_BURST, now=monotonic())
        self._cleared_warned_at: float | None = None
        self._closed = False

    def covers(self, topic: str) -> bool:
        """One of the gateway's retained topics, or a topic where a contact's document or a draft
        would be."""
        if topic in self._fixed:
            return True
        if self._drafts and self._topics_layout.draft_from_topic(topic) is not None:
            return True
        key = topic.removeprefix(self._contact_prefix)
        return key != topic and bool(key) and "/" not in key

    def sent(self, topic: str, payload: bytes, *, published: bool) -> None:
        """The gateway's new payload of a topic; `published`: it went out (else the next connect
        publishes it). A restore still waiting is no longer needed."""
        entry = self._topics.get(topic)
        if entry is None:
            entry = self._topics[topic] = _Topic(payload)
        else:
            entry.payload = payload
            self._cancel(entry)
        if published:
            entry.echoes.append(payload)

    def connected(self) -> None:
        """A new connection: the connect publishes every payload again. The payloads in flight
        arrive as copies for the new subscription or once more, as paho sends them again."""
        for entry in self._topics.values():
            entry.resent.extend(entry.echoes)
            entry.echoes.clear()
            self._cancel(entry)

    def close(self) -> None:
        """The gateway stops: no restore after its last payloads."""
        self._closed = True
        for entry in self._topics.values():
            self._cancel(entry)

    def received(self, topic: str, payload: bytes, retained: bool) -> None:
        """A message on a covered topic."""
        if retained or self._closed:  # the broker's copy for the subscription made at connect
            return
        entry = self._topics.get(topic)
        if entry is None:
            self._clear(topic, payload)
            return
        if payload in entry.echoes:  # the gateway's own, in the order it published them
            while entry.echoes.popleft() != payload:
                pass
            return
        if payload in entry.resent:  # the gateway's own, sent again by paho after a reconnect
            entry.resent.remove(payload)
            if payload != entry.payload and entry.payload not in entry.echoes:
                self._cancel(entry)  # it came after the current one: the broker keeps it
                self._restore(topic, entry)
            return
        if payload == entry.payload:
            return
        now = self._now()
        entry.writes += 1
        if entry.warned_at is None or not 0 <= now - entry.warned_at < WARNING_INTERVAL_S:
            times = f" ({entry.writes} times since the last warning)" if entry.writes > 1 else ""
            log.warning(
                "MQTT: another client wrote %s%s; restoring the gateway's payload (%s)",
                topic,
                times,
                HINT,
            )
            entry.warned_at, entry.writes = now, 0
        if entry.restore is not None:
            return
        wait = 0.0 if entry.restored_at is None else entry.restored_at + HEAL_INTERVAL_S - now
        if 0 < wait <= HEAL_INTERVAL_S:
            entry.restore = self._call_later(wait, lambda: self._restore(topic, entry))
        else:
            self._restore(topic, entry)

    def _restore(self, topic: str, entry: _Topic) -> None:
        entry.restore = None
        if self._closed:
            return
        entry.restored_at = self._now()
        entry.echoes.append(entry.payload)
        self._publish(topic, entry.payload)

    def _clear(self, topic: str, payload: bytes) -> None:
        """A topic the gateway has not written in this run: emptied, as nothing of the gateway
        belongs there now. Not remembered, as anyone can invent topics."""
        if not payload:  # empty already
            return
        now = self._now()
        if not self._clears.allow(now):
            return
        warned = self._cleared_warned_at
        if warned is None or not 0 <= now - warned < WARNING_INTERVAL_S:
            log.warning(
                "MQTT: another client wrote %s, where the gateway has published nothing "
                "in this run; clearing it (%s)",
                topic,
                HINT,
            )
            self._cleared_warned_at = now
        self._publish(topic, b"")

    @staticmethod
    def _cancel(entry: _Topic) -> None:
        if entry.restore is not None:
            entry.restore.cancel()
            entry.restore = None
