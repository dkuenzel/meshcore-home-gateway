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
        # events
        self.contact_event = f"{self.root}/contact"  # a contact added, removed, renamed
        self.node_event = f"{self.root}/node"  # a node heard for the first time
        self._send = f"{self.root}/send/"
        self.send_subscription = self._send + "+"

    def contact(self, key: str) -> str:
        """The document of one contact (full key, lower-case hex)."""
        return f"{self.contacts}/{key}"

    def send(self, alias: str) -> str:
        return self._send + alias

    def alias_from_send_topic(self, topic: str) -> str | None:
        if not topic.startswith(self._send):
            return None
        alias = topic[len(self._send) :]
        return alias if alias and "/" not in alias else None
