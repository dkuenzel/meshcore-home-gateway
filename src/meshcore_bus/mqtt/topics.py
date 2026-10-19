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
        self._send = f"{self.root}/send/"
        self.send_subscription = self._send + "+"

    def send(self, alias: str) -> str:
        return self._send + alias

    def alias_from_send_topic(self, topic: str) -> str | None:
        if not topic.startswith(self._send):
            return None
        alias = topic[len(self._send) :]
        return alias if alias and "/" not in alias else None
