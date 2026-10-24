"""Topic layout below `<base_topic>/<node_id>` (docs/mqtt-api.md)."""

from __future__ import annotations


class Topics:
    def __init__(self, base_topic: str, node_id: str) -> None:
        self.root = f"{base_topic}/{node_id}"
        self.availability = f"{self.root}/availability"  # retained, last will: online/offline
        self.state = f"{self.root}/state"  # retained JSON: radio link, battery, airtime
        self.received = f"{self.root}/received"  # every received message
        self.command = f"{self.root}/command"  # authorised commands only
        self.delivery = f"{self.root}/delivery"  # life cycle of outgoing messages
        self.rx = f"{self.root}/rx"  # optional radio diagnostics per packet
        # documents of the providers (ADR 0015), retained
        self.stats = f"{self.root}/stats"  # the radio's own measurements
        self.contacts = f"{self.root}/contacts"  # index of the radio's contact table
        self.nodes = f"{self.root}/nodes"  # nodes heard, not contacts
        self.history = f"{self.root}/history"  # the latest messages in and out
        self.settings = f"{self.root}/settings"  # the managed radio profile (ADR 0020)
        # events
        self.contact_event = f"{self.root}/contact"  # added, removed; hidden or shown in HA
        self.node_event = f"{self.root}/node"  # a node heard for the first time
        self.setting_event = f"{self.root}/setting"  # a radio setting written or found changed
        # requests
        self.show_hidden = f"{self.root}/homeassistant/show_hidden"  # contacts deleted in HA
        self._send = f"{self.root}/send/"
        self.send_subscription = self._send + "+"
        self.contact_send_subscription = f"{self.contacts}/+/send"

    def contact(self, key: str) -> str:
        """The document of one contact (full key, lower-case hex)."""
        return f"{self.contacts}/{key}"

    def contact_send(self, key: str) -> str:
        """Texts to send to one contact."""
        return f"{self.contacts}/{key}/send"

    def contact_received(self, key: str) -> str:
        """Messages from one contact (events, as on `received`)."""
        return f"{self.contacts}/{key}/received"

    def key_from_contact_send_topic(self, topic: str) -> str | None:
        """The `<key>` level of `contacts/<key>/send`, whatever it holds (the bridge rejects one
        that is no contact's key); None for any other topic."""
        prefix, suffix = self.contacts + "/", "/send"
        if not (topic.startswith(prefix) and topic.endswith(suffix)):
            return None
        key = topic[len(prefix) : -len(suffix)]
        return key if key and "/" not in key else None

    def send(self, alias: str) -> str:
        return self._send + alias

    def alias_from_send_topic(self, topic: str) -> str | None:
        if not topic.startswith(self._send):
            return None
        alias = topic[len(self._send) :]
        return alias if alias and "/" not in alias else None
