"""The gateway's announcements to Home Assistant (design §6.7, ARC-009): its device with the
discovery document, the controls that add contacts, and the contacts' devices; again after the
broker connected, after Home Assistant's birth message and after Home Assistant cleared the
device, at most every ANNOUNCE_MIN_INTERVAL_S (SEC-006)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.health import FAILURES
from meshcore_home_gateway.mqtt.contact_devices import (
    ContactDevices,
    contact_discovery_subscription,
)
from meshcore_home_gateway.mqtt.discovery import discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.manage_entities import ManageEntities, manage_topic
from meshcore_home_gateway.mqtt.payloads import encode
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import observe
from meshcore_home_gateway.ports import Bus, RadioInfo
from meshcore_home_gateway.radio_link import RadioLink
from meshcore_home_gateway.state import AnnouncedComponents

log = logging.getLogger(__name__)
log.addFilter(FAILURES)  # caught bugs counted, their tracebacks thinned out

DISCOVERY_DELAY_S = (0.5, 2.0)  # random delay after Home Assistant's birth message
ANNOUNCE_MIN_INTERVAL_S = 30  # announcements after births and clears, at most this often
BIRTHS_PER_MINUTE = 5  # more come from another client than Home Assistant: logged


@dataclass
class _Announcing:
    """When the gateway announces itself to Home Assistant again (SEC-006)."""

    due: asyncio.TimerHandle | None = None  # a scheduled announcement
    birth: bool = False  # it follows Home Assistant's birth message
    last: float | None = None  # monotonic time of the last announcement
    births: deque[float] = field(default_factory=deque)  # of the last minute, monotonic
    warned: float | None = None  # monotonic time of the last warning about births


class Announcer:
    """Announces the gateway to Home Assistant: at once, or after a random delay (`schedule`)."""

    def __init__(
        self,
        config: Config,
        topics: Topics,
        bus: Bus,
        radio: RadioLink,
        *,
        publish_state: Callable[[], None],
        components: AnnouncedComponents | None = None,
        manage: ManageEntities | None = None,
        devices: ContactDevices | None = None,
        monotonic: Callable[[], float],
        wall_clock: Callable[[], float],
    ) -> None:
        """`publish_state`: the state document follows every announcement; `components`: what
        earlier runs announced; `manage`: the controls that add contacts; `devices`: the
        contacts' devices."""
        self.config = config
        self._topics, self._bus, self._radio = topics, bus, radio
        self._publish_state = publish_state
        self._components, self._manage, self._devices = components, manage, devices
        self._mono, self._wall = monotonic, wall_clock
        self._announced: tuple[str, ...] | None = None  # radio identity in the discovery
        self._removed: dict[str, str] | None = None  # earlier components, removed in this run
        self._state = _Announcing()

    def radio_changed(self) -> bool:
        """The radio connected is not the one the discovery document shows."""
        return _identity(self._radio.info) != self._announced

    def connected(self) -> None:
        """The broker connected: Home Assistant may have restarted unseen meanwhile."""
        if self._devices is not None:
            observe(self._devices.ha_offline)
        self.announce()

    def device_message(self, topic: str, payload: bytes, retained: bool) -> None:
        """A message on a discovery topic of the gateway. An empty one on a topic of its own
        device (the main document, the controls that add contacts) comes from Home Assistant,
        which clears it when it removes a disabled entity (or the user deletes the device): the
        entities come back with the next announcement."""
        if topic not in (discovery_topic(self.config), manage_topic(self.config)):
            if self._devices is not None:
                observe(self._devices.discovery_message, topic, payload, retained)
            return
        if payload or retained or self._state.due is not None:
            return
        log.info("Home Assistant cleared the gateway's device; announcing it again")
        self.schedule()

    def announce(self, *, birth: bool = False) -> None:
        if self.config.homeassistant.discovery:
            if self._removed is None:  # first announcement: what earlier runs had and this lacks
                current = discovery_payload(self.config, self._topics)["cmps"]
                platforms = {key: component["p"] for key, component in current.items()}
                self._removed = (
                    self._components.replace(platforms, now=self._wall())
                    if self._components
                    else {}
                )
                if self._removed:
                    log.info("Home Assistant: removing %s", ", ".join(sorted(self._removed)))
            topic, info = discovery_topic(self.config), self._radio.info
            if self._removed:  # Home Assistant removes only what it discovered since its start
                bind = discovery_payload(self.config, self._topics, info, self._removed, bind=True)
                self._bus.publish(topic, encode(bind))
            document = discovery_payload(self.config, self._topics, info, self._removed)
            self._bus.publish(topic, encode(document))
            if (manage := self._manage) is not None:
                observe(manage.announce)
            self._announced = _identity(self._radio.info)
            self._state.last = self._mono()
            if (devices := self._devices) is not None:
                observe(lambda: devices.announce_all(birth=birth))
        self._publish_state()

    def schedule(self, *, birth: bool = False) -> None:
        """Announce after a random delay. One already due is never postponed (a storm of birth
        messages would keep it from ever coming); it only learns that a birth came (SEC-006)."""
        if birth:
            self._count_birth()
        state = self._state
        if state.due is not None:
            state.birth = state.birth or birth
            return
        state.birth = birth
        delay = random.uniform(*DISCOVERY_DELAY_S)  # noqa: S311 - spreading load only
        state.due = asyncio.get_running_loop().call_later(delay, self._due)

    def _due(self) -> None:
        """At most one announcement every ANNOUNCE_MIN_INTERVAL_S: a later one waits for the
        rest of the interval, with whatever came meanwhile."""
        state = self._state
        wait = 0.0 if state.last is None else state.last + ANNOUNCE_MIN_INTERVAL_S - self._mono()
        if wait > 0:
            state.due = asyncio.get_running_loop().call_later(wait, self._due)
            return
        birth, state.due = state.birth, None
        self.announce(birth=birth)

    def _count_birth(self) -> None:
        """Home Assistant sends one birth message per start: many in a minute come from another
        client, which the log names once a minute."""
        now, state = self._mono(), self._state
        births = state.births
        births.append(now)
        while births and now - births[0] > 60:
            births.popleft()
        if len(births) > BIRTHS_PER_MINUTE and (state.warned is None or now - state.warned > 60):
            state.warned = now
            log.warning(
                "%d birth messages on %s in a minute: another client writes there; the gateway "
                "announces itself at most every %d s",
                len(births),
                self.config.homeassistant.status_topic,
                ANNOUNCE_MIN_INTERVAL_S,
            )


def ha_device_topics(config: Config) -> list[str]:
    """The gateway's discovery topics it watches, to notice what Home Assistant clears there:
    its own, and either the contact devices' (whose subscription takes the controls' topic, one
    level below the gateway's device too) or the controls' alone."""
    if not config.homeassistant.discovery:
        return []
    below = contact_discovery_subscription(config) if config.homeassistant.contact_devices else None
    return [discovery_topic(config), below or manage_topic(config)]


def _identity(info: RadioInfo | None) -> tuple[str, ...] | None:
    """What the announcement depends on of the radio: the device page's model, firmware and key,
    and the name, which takes room from every channel text."""
    if info is None:
        return None
    return (info.device.model, info.device.version, info.public_key, info.name)
