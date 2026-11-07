"""`retire`: the gateway leaves the broker and Home Assistant before its container goes (IMP-016,
OPS-022). Every device and component it announced to Home Assistant is removed, every retained
document under its topics and every retained message of its Homie device is cleared (also with
`[homie]` off now: an earlier run may have had it on), and its availability says `offline`. It
runs with the service stopped, while Home Assistant runs; without `--yes` it only says what it
would do."""

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


class _Sweep(broker.GatewaySession):
    """The gateway's availability, every retained document under its topics, and every retained
    message of its Homie device."""

    def __init__(self, config: Config) -> None:
        from meshcore_home_gateway.mqtt.homie import ROOT, device_id  # noqa: PLC0415

        self.found: dict[str, bytes] = {}
        self.homie = f"{ROOT}/{device_id(config.mqtt.node_id)}"
        super().__init__(config, (), "retire")

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        super()._on_message(client, userdata, message)
        if message.retain and message.payload:
            self.found[message.topic] = bytes(message.payload)

    @property
    def root(self) -> str:
        return self.config.mqtt.topic_root

    def sweep(self) -> str | None:
        """Collect the retained messages; an error text, or None. With `[homie]` off, a broker
        that refuses the device's topics to the gateway's user has nothing of it to clear: a
        broker may check subscriptions against its ACL, and a gateway that never had Homie
        needs no grant for them."""
        error = broker.subscribe(self.client, f"{self.root}/#")
        if error is None:
            homie = broker.subscribe(self.client, f"{self.homie}/#")
            error = homie if self.config.homie.enabled else None
        if error is None:
            time.sleep(broker.RETAINED_WAIT_S)  # retained messages come on SUBACK
        return error

    def homie_topics(self) -> list[str]:
        """The Homie device's retained topics, `$state` first: the order in which Homie 5.0.0
        removes a device (4.0.0 names none), so that a controller drops it before its parts."""
        state = f"{self.homie}/$state"
        found = sorted(t for t in self.found if t.startswith(self.homie + "/"))
        return sorted(found, key=lambda topic: topic != state)


def _retire(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    session = _Sweep(config)
    try:
        error = session.open() or session.sweep()
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
        documents = (t for t in session.found if t.startswith(session.root + "/"))
        cleared = sorted(t for t in documents if t != session.topics.availability)
        homie = session.homie_topics()
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
        if homie:  # also with `[homie]` off now
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
