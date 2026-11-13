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
        self.pending = f"{self.root}/pending"  # the messages accepted and not final yet
        self.settings = f"{self.root}/settings"  # the managed radio profile (ADR 0020)
        self.config = f"{self.root}/config"  # the settings in effect, by an allowlist
        self.refused_commands = f"{self.root}/refused_commands"  # the latest refused (#141)
        # events
        self.contact_event = f"{self.root}/contact"  # added, removed; hidden or shown in HA
        self.node_event = f"{self.root}/node"  # a node heard for the first time
        self.setting_event = f"{self.root}/setting"  # a radio setting written or found changed
        self.telemetry_event = f"{self.root}/telemetry"  # a companion answers again or stops
        # requests
        self.show_hidden = f"{self.root}/homeassistant/show_hidden"  # contacts deleted in HA
        self.add_node = f"{self.root}/homeassistant/add_node"  # a heard node by its label (HA)
        self.add_contact = f"{self.root}/add_contact"  # a key, contact link or card (ADR 0017)
        # the drafts of Home Assistant's chats: `draft/<kind>/<id>`, kind `target` (its alias) or
        # `contact` (its key); retained {"text": …}, and the requests below each (design §6.7)
        self._draft = f"{self.root}/homeassistant/draft/"
        self.draft_subscription = self._draft + "+/+"
        self.draft_request_subscription = self._draft + "+/+/request"
        self._send = f"{self.root}/send/"
        self.send_subscription = self._send + "+"
        self.contact_send_subscription = f"{self.contacts}/+/send"
        self.contact_remove_subscription = f"{self.contacts}/+/remove"
        self.contact_telemetry_subscription = f"{self.contacts}/+/telemetry"

    def contact(self, key: str) -> str:
        """The document of one contact (full key, lower-case hex)."""
        return f"{self.contacts}/{key}"

    def contact_send(self, key: str) -> str:
        """Texts to send to one contact."""
        return f"{self.contacts}/{key}/send"

    def contact_received(self, key: str) -> str:
        """Messages from one contact (events, as on `received`)."""
        return f"{self.contacts}/{key}/received"

    def contact_remove(self, key: str) -> str:
        """Requests to remove one contact from the radio."""
        return f"{self.contacts}/{key}/remove"

    def contact_telemetry(self, key: str) -> str:
        """Requests to ask one companion for its telemetry, or not (ADR 0018)."""
        return f"{self.contacts}/{key}/telemetry"

    def key_from_contact_topic(self, topic: str, action: str) -> str | None:
        """The `<key>` level of `contacts/<key>/<action>`, whatever it holds (the bridge rejects
        one that is no contact's key); None for any other topic."""
        prefix, suffix = self.contacts + "/", "/" + action
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

    def target_draft(self, alias: str) -> str:
        """The draft of a target's chat in Home Assistant (retained)."""
        return f"{self._draft}target/{alias}"

    def target_draft_request(self, alias: str) -> str:
        """Home Assistant's requests for that draft: a text to keep, or the press of Send."""
        return self.target_draft(alias) + "/request"

    def contact_draft(self, key: str) -> str:
        """The draft of a companion's chat in Home Assistant (retained)."""
        return f"{self._draft}contact/{key}"

    def contact_draft_request(self, key: str) -> str:
        return self.contact_draft(key) + "/request"

    def draft_from_topic(self, topic: str) -> tuple[str, str] | None:
        """The kind and id levels of `homeassistant/draft/<kind>/<id>`, whatever they hold; None
        for any other topic."""
        return _levels(topic, self._draft, "")

    def draft_from_request_topic(self, topic: str) -> tuple[str, str] | None:
        """The kind and id levels of `homeassistant/draft/<kind>/<id>/request`."""
        return _levels(topic, self._draft, "/request")


def _levels(topic: str, prefix: str, suffix: str) -> tuple[str, str] | None:
    if not (topic.startswith(prefix) and topic.endswith(suffix)):
        return None
    kind, sep, ident = topic[len(prefix) : len(topic) - len(suffix)].partition("/")
    if not (sep and kind and ident) or "/" in ident:
        return None
    return kind, ident
