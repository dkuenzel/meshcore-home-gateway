"""`retire`: the gateway leaves the broker and Home Assistant before its container goes (IMP-016,
OPS-022). Every device and component it announced to Home Assistant is removed, every retained
document under its topics and every retained message of its Homie device is cleared (also with
`[homie]` off now: an earlier run may have had it on, but not while a connection holds the
device), and its availability says `offline`; then it looks again, and what is left is an error.
It runs with the service stopped, while Home Assistant runs; without `--yes` it only says what
it would do."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import DEFAULT_STATE_DIR, EX_FAILURE, EX_OK, PREFIX, tr, trn
from meshcore_home_gateway.config import Config, load_config

LIVE = frozenset({b"init", b"ready", b"alert", b"sleeping"})  # Homie 4.0.0: a device connected


class _Sweep(broker.GatewaySession):
    """The gateway's availability, every retained document under its topics, and every retained
    message of its Homie device."""

    def __init__(self, config: Config) -> None:
        from meshcore_home_gateway.mqtt.homie import ROOT, device_id  # noqa: PLC0415

        self.found: dict[str, bytes] = {}  # paho's thread writes it while a sweep reads
        self.heard = 0.0  # time.monotonic() of the latest retained message
        self.homie = f"{ROOT}/{device_id(config.mqtt.node_id)}"
        super().__init__(config, (), "retire")

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        super()._on_message(client, userdata, message)
        if message.retain:
            self.heard = time.monotonic()
            if message.payload:
                self.found[message.topic] = bytes(message.payload)

    @property
    def root(self) -> str:
        return self.config.mqtt.topic_root

    def sweep(self) -> tuple[dict[str, bytes], str | None]:
        """The retained messages under the gateway's topics and its Homie device's, and an error
        text or None. They come with QoS 0, which Mosquitto passes on in full, until none has come
        for a while; the subscriptions end before anything is cleared, as each cleared
        topic would come back to them. With `[homie]` off, a broker that refuses the device's
        topics to the gateway's user has nothing of it to clear: a broker may check
        subscriptions against its ACL, and a gateway that never had Homie needs no grant for
        them."""
        self.found = {}
        subscribed = []
        error = broker.subscribe(self.client, f"{self.root}/#", qos=0)
        if error is None:
            subscribed.append(f"{self.root}/#")
            homie = broker.subscribe(self.client, f"{self.homie}/#", qos=0)
            if homie is None:
                subscribed.append(f"{self.homie}/#")
            elif self.config.homie.enabled:
                error = homie
        if error is None:
            self._quiet()
        for topic in subscribed:
            error = error or broker.unsubscribe(self.client, topic)
        return dict(self.found), error

    def _quiet(self) -> None:
        """Until no retained message has come for RETAINED_QUIET_S, at most RETAINED_MAX_S: a
        broker sends them after its answer, as fast as the connection takes them."""
        start = self.heard = time.monotonic()
        while (now := time.monotonic()) < (
            end := min(self.heard + broker.RETAINED_QUIET_S, start + broker.RETAINED_MAX_S)
        ):
            time.sleep(end - now)

    def documents(self, found: dict[str, bytes]) -> list[str]:
        """The gateway's retained documents, its availability aside."""
        mine = (t for t in found if t.startswith(self.root + "/"))
        return sorted(t for t in mine if t != self.topics.availability)

    def homie_topics(self, found: dict[str, bytes]) -> list[str]:
        """The Homie device's retained topics, `$state` first: the order in which Homie 5.0.0
        removes a device (4.0.0 names none), so that a controller drops it before its parts."""
        state = f"{self.homie}/$state"
        mine = sorted(t for t in found if t.startswith(self.homie + "/"))
        return sorted(mine, key=lambda topic: topic != state)

    def live(self, found: dict[str, bytes]) -> str | None:
        """The Homie device's `$state` when it says that a connection holds the device. With this
        gateway stopped, that is another gateway's: node IDs that differ only in `_` and `-`
        share a Homie device (homie.device_id)."""
        state = found.get(f"{self.homie}/$state")
        return state.decode() if state in LIVE else None


def _retire(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    session = _Sweep(config)
    try:
        found: dict[str, bytes] = {}
        error = session.open()
        if error is None:
            found, error = session.sweep()
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        if session.online:
            running = tr(
                "the gateway is online: stop its service first "
                "(systemctl stop meshcore-home-gateway), then retire it"
            )
            print(PREFIX + running, file=sys.stderr)
            return EX_FAILURE
        removals = home_assistant_removals(config, state_dir)
        cleared = session.documents(found)
        live = session.live(found)
        homie = [] if live else session.homie_topics(found)
        print(
            trn(
                "Home Assistant: {count} device to remove",
                "Home Assistant: {count} devices to remove",
                len({topic for topic, _ in removals}),
            )
        )
        print(
            trn(
                "broker: {count} retained document to clear under {root}/",
                "broker: {count} retained documents to clear under {root}/",
                len(cleared),
                root=session.root,
            )
        )
        if live:
            print(
                # Translators: {root} is the Homie device's topic; {state} its state, such as ready
                tr(
                    "broker: Homie device {root} not cleared: a gateway is connected to it "
                    "(state {state})",
                    root=session.homie,
                    state=live,
                )
            )
        elif homie:  # also with `[homie]` off now
            print(
                # Translators: {root} is the Homie device's topic, such as homie/meshcore-radio1
                trn(
                    "broker: {count} retained Homie message to clear under {root}/",
                    "broker: {count} retained Homie messages to clear under {root}/",
                    len(homie),
                    root=session.homie,
                )
            )
        if not args.yes:
            print(tr("nothing done: add --yes to retire the gateway"))
            return EX_OK
        for topic, payload in removals:
            error = error or broker.publish(session.client, topic, payload)
        for topic in [*cleared, *homie]:
            error = error or broker.publish(session.client, topic, b"", retain=True)
        availability = session.topics.availability
        error = error or broker.publish(session.client, availability, b"offline", retain=True)
        if error is None:  # a broker acknowledges what its ACL drops (MQTT 3.1.1)
            left, error = session.sweep()
            remaining = session.documents(left) + ([] if live else session.homie_topics(left))
            if error is None and remaining:
                error = trn(
                    "broker: {count} retained message left after clearing, such as {topic}",
                    "broker: {count} retained messages left after clearing, such as {topic}",
                    len(remaining),
                    topic=remaining[0],
                )
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        print(tr("retired: the container can go now"))
        return EX_OK
    finally:
        session.close()


def home_assistant_removals(config: Config, state_dir: Path) -> list[tuple[str, bytes]]:
    """The messages that remove the gateway's devices in Home Assistant, in order: for each
    device, a document that binds every component it had (placeholders under their unique IDs,
    also those of earlier runs), then the empty document that removes the device with them
    (the bridge does the same for a single component, mqtt/discovery.py)."""
    from meshcore_home_gateway.mqtt.contact_devices import (  # noqa: PLC0415
        contact_discovery_topic,
        removal_payload,
    )
    from meshcore_home_gateway.mqtt.discovery import (  # noqa: PLC0415
        discovery_payload,
        discovery_topic,
    )
    from meshcore_home_gateway.mqtt.manage_entities import (  # noqa: PLC0415
        OBJECT_ID,
        manage_payload,
        manage_topic,
    )
    from meshcore_home_gateway.mqtt.payloads import encode  # noqa: PLC0415
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415
    from meshcore_home_gateway.state import (  # noqa: PLC0415
        AnnouncedComponents,
        ContactDeviceRecords,
        JsonStateFile,
    )

    store = JsonStateFile(state_dir / "state.json", read_only=True)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    messages: list[tuple[str, bytes]] = []
    gateway = AnnouncedComponents(store).announced()
    controls = AnnouncedComponents(store, OBJECT_ID).announced()
    if config.homeassistant.discovery or gateway:
        document = discovery_payload(config, topics)
        extra = {k: p for k, p in gateway.items() if k not in document["cmps"]}
        bind = discovery_payload(config, topics, None, extra, bind=True)
        messages += [(discovery_topic(config), encode(bind)), (discovery_topic(config), b"")]
    if config.homeassistant.discovery or controls:
        document = manage_payload(config, topics, [])
        extra = {k: p for k, p in controls.items() if k not in document["cmps"]}
        bind = manage_payload(config, topics, [], extra, bind=True)
        messages += [(manage_topic(config), encode(bind)), (manage_topic(config), b"")]
    records = ContactDeviceRecords(store)
    devices = {key: r.components for key, r in records.removed.items()}
    devices |= {key: r.components for key, r in records.announced.items()}
    for key, components in sorted(devices.items()):
        topic = contact_discovery_topic(config, key)
        messages += [
            (topic, encode(removal_payload(config, topics, key, components))),
            (topic, b""),
        ]
    return messages
