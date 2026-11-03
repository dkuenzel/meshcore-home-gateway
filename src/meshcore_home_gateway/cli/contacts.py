"""`contacts`: add, remove, list and link the radio's contacts and switch their telemetry, through
the gateway's requests on the bus (ADR 0017)."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
from collections.abc import Sequence
from typing import Any

from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import (
    EX_FAILURE,
    EX_OK,
    EX_USAGE,
    PREFIX,
    _json_text,
    _safe_text,
    tr,
    trn,
)
from meshcore_home_gateway.config import Config, load_config
from meshcore_home_gateway.i18n import N_

RETAINED_WAIT_S = 2.0  # for the gateway's retained documents; brokers send them on SUBACK


_FULL_KEY = re.compile(r"[0-9a-fA-F]{64}")


# a heard node as Home Assistant's select lists it, the name after the digits optional
_LABEL = re.compile(r"([0-9a-fA-F]{12})(?:…|\.\.\.)([0-9a-fA-F]{8})(?: .*)?")


class _Retained:
    """The retained messages of some topics (paho thread); JSON where they are JSON."""

    def __init__(self, topics: Sequence[str]) -> None:
        self.topics = tuple(topics)
        self.messages: dict[str, Any] = {}
        self.complete = threading.Event()

    def on_message(self, message: Any) -> None:
        if message.topic not in self.topics or not message.retain or not message.payload:
            return
        try:
            self.messages[message.topic] = json.loads(message.payload)
        except (ValueError, RecursionError):
            self.messages[message.topic] = message.payload.decode("utf-8", errors="replace")
        if set(self.messages) == set(self.topics):
            self.complete.set()


class _ContactWatch:
    """The result of one contact request: the `contact` event with its id (paho thread)."""

    def __init__(self, request_id: str, final: set[str]) -> None:
        self._id, self._final = request_id, final
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()

    def on_message(self, message: Any) -> None:
        try:
            event = json.loads(message.payload)
        except (ValueError, RecursionError):  # also nested too deeply; it would end paho's thread
            return
        if not isinstance(event, dict) or event.get("id") != self._id:
            return
        if event.get("event_type") in self._final:
            self.result = event
            self.done.set()


class _ContactSession:
    """A broker connection for the `contacts` commands: the gateway's availability and some of
    its retained documents (`documents`: "contacts", "nodes", "state"), then perhaps a request."""

    def __init__(self, config: Config, documents: Sequence[str] = ()) -> None:
        from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

        self.config = config
        self.topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
        names = [self.topics.availability, *(getattr(self.topics, d) for d in documents)]
        self.retained = _Retained(names)
        self.watch: _ContactWatch | None = None
        self.client = broker.mqtt_client(config.mqtt, "contacts")
        self.client.on_message = self._on_message

    def _on_message(self, _c: Any, _u: Any, message: Any) -> None:
        if message.topic == self.topics.contact_event and self.watch is not None:
            self.watch.on_message(message)
        else:
            self.retained.on_message(message)

    def open(self) -> str | None:
        """Connect and read the retained documents; an error text, or None."""
        error = broker.Connection().open(self.client, self.config.mqtt)
        for topic in () if error else self.retained.topics:
            error = error or broker.subscribe(self.client, topic)
        if error is None:
            self.retained.complete.wait(RETAINED_WAIT_S)
        return error

    def document(self, name: str) -> Any:
        return self.retained.messages.get(getattr(self.topics, name))

    @property
    def online(self) -> bool:
        return bool(self.document("availability") == "online")

    def request(
        self,
        topic: str,
        payload: dict[str, Any],
        wait_s: float,
        final: set[str],
        ok: frozenset[str] = frozenset({"added", "removed"}),
    ) -> int:
        """Publish the request and print its result: success is an event of `ok` without a
        reason."""
        request_id = f"cli-{os.urandom(4).hex()}"
        self.watch = _ContactWatch(request_id, final)
        error = broker.subscribe(self.client, self.topics.contact_event) if wait_s > 0 else None
        body = json.dumps({**payload, "id": request_id}).encode("utf-8")
        error = error or broker.publish(self.client, topic, body)
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        if wait_s <= 0:
            return EX_OK
        if not self.watch.done.wait(wait_s) or self.watch.result is None:
            late = tr(
                "no result in time (request {request}; it may still be handled: {command})",
                request=request_id,
                command="meshcore-home-gateway listen --topic contact",
            )
            print(PREFIX + late, file=sys.stderr)
            return EX_FAILURE
        result = self.watch.result
        print(_json_text(result))
        return EX_OK if result["event_type"] in ok and result.get("reason") is None else EX_FAILURE

    def close(self) -> None:
        self.client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged
        self.client.loop_stop()


def _opened(session: _ContactSession) -> int | None:
    """None when connected and the gateway is online; else the exit status, after saying why."""
    error = session.open()
    if error is None and not session.online:
        error = tr("the gateway is offline")
    if error is None:
        return None
    print(PREFIX + error, file=sys.stderr)
    return EX_FAILURE


def _entries(document: Any, where: str) -> list[dict[str, Any]]:
    items = document.get(where) if isinstance(document, dict) else None
    return [e for e in items or [] if isinstance(e, dict) and isinstance(e.get("key"), str)]


def _resolve_label(label: re.Match[str], document: Any, where: str) -> dict[str, Any] | None:
    """The one entry of a document's list with the label's first 12 and last 8 digits, or None
    after telling why."""
    start, end = label.group(1).lower(), label.group(2).lower()
    found = [
        e
        for e in _entries(document, where)
        if e["key"].startswith(start) and e["key"].endswith(end)
    ]
    if len(found) == 1:
        return found[0]
    if found:
        keys = ", ".join(sorted(e["key"] for e in found))
        matches = trn(
            "{count} key matches: {keys}", "{count} keys match: {keys}", len(found), keys=keys
        )
        print(PREFIX + matches, file=sys.stderr)
    else:
        cut = isinstance(document, dict) and document.get("truncated") is True
        print(PREFIX + tr(_NOT_LISTED[where, cut], label=label.group(0)), file=sys.stderr)
    return None


# Translators: a contact or heard node given as KEY12…KEY8 that the gateway's list lacks
_NOT_LISTED = {  # the list; cut at its bound
    ("nodes", False): N_("no node {label}"),
    ("nodes", True): N_("no node {label} (the published list is cut: give the full key)"),
    ("contacts", False): N_("no contact {label}"),
    ("contacts", True): N_("no contact {label} (the published list is cut: give the full key)"),
}


def _looks_alike(key: str, nodes: Any) -> bool:
    """The gateway flags the key, also beyond the bounded list, or a heard node shares its first
    12 digits (the key itself not heard yet)."""
    flagged = nodes.get("look_alike") if isinstance(nodes, dict) else None
    if isinstance(flagged, list) and key in flagged:
        return True
    return any(
        (e["key"] == key and e.get("look_alike") is True)
        or (e["key"] != key and e["key"][:12] == key[:12])
        for e in _entries(nodes, "nodes")
    )


def _print_key(key: str, name: str | None, *, claimed: bool = False) -> None:
    """The full key, to compare with the node's own screen: an attacker can copy the first
    digits of a key, not all of them."""
    groups = " ".join(key[i : i + 4] for i in range(0, len(key), 4))
    if name is None:
        line = tr("key {groups}", groups=groups)
    elif claimed:
        # Translators: the name a contact link or card gives itself, which nobody checked
        line = tr("key {groups}  {name!r} (claimed)", groups=groups, name=name)
    else:
        line = tr("key {groups}  {name!r}", groups=groups, name=name)
    print(_safe_text(line), file=sys.stderr)


def _contacts_add(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.protocol.links import LinkError, parse_contact_text  # noqa: PLC0415

    config = load_config(args.config)
    text = args.contact.strip()
    label = _LABEL.fullmatch(text)
    key, name, claimed = None, None, False
    if _FULL_KEY.fullmatch(text):
        key = text.lower()
    elif label is None:  # checked before connecting; the gateway parses it again
        try:
            claim = parse_contact_text(text)
        except LinkError as exc:
            # Translators: why the text is no contact, then the code of the reason (stays)
            refused = tr("{detail} ({reason})", detail=exc.text, reason=exc.reason)
            print(PREFIX + refused, file=sys.stderr)
            return EX_USAGE
        key, name, claimed = claim.public_key, claim.name, True
    session = _ContactSession(config, ["nodes"])
    try:
        if (status := _opened(session)) is not None:
            return status
        nodes = session.document("nodes")
        if label is not None:
            entry = _resolve_label(label, nodes, "nodes")
            if entry is None:
                return EX_USAGE
            key, name = entry["key"], entry.get("name")
        assert key is not None  # noqa: S101 - one of the branches above
        _print_key(key, name if isinstance(name, str) else None, claimed=claimed)
        if _looks_alike(key, nodes):
            # Translators: security text (docs/translations.md): translate all of it, add nothing
            print(
                tr(
                    "warning: another node has the same first 12 key digits: compare the full "
                    "key above with the node's own screen; the one added first keeps them"
                ),
                file=sys.stderr,
            )
        contact = key if claimed is False else text  # a card's text: the gateway parses it
        return session.request(
            session.topics.add_contact,
            {"contact": contact},
            args.wait,
            {"added", "refused", "failed"},
        )
    finally:
        session.close()


def _contact_given(text: str) -> tuple[str, re.Match[str] | None] | None:
    """A contact given as a full key or KEY12…KEY8: the text and its label; None after saying
    what is wanted."""
    text = text.strip()
    label = _LABEL.fullmatch(text)
    if label is None and not _FULL_KEY.fullmatch(text):
        print(
            PREFIX + tr("give the contact's full key (64 hex digits) or KEY12…KEY8"),
            file=sys.stderr,
        )
        return None
    return text, label


def _indexed(text: str, label: re.Match[str] | None, index: Any) -> dict[str, Any] | None:
    """The index's entry of the contact (by label or key); a key it lacks as `{"key": key}`;
    None after saying why a label names none."""
    if label is not None:
        return _resolve_label(label, index, "contacts")
    key = text.lower()
    return next((e for e in _entries(index, "contacts") if e["key"] == key), {"key": key})


def _contacts_remove(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if (given := _contact_given(args.contact)) is None:
        return EX_USAGE
    session = _ContactSession(config, ["contacts"])
    try:
        if (status := _opened(session)) is not None:
            return status
        entry = _indexed(*given, session.document("contacts"))
        if entry is None:
            return EX_USAGE
        name = entry.get("name")
        _print_key(entry["key"], name if isinstance(name, str) else None)
        print(
            tr("the radio drops its direct messages from then on; messages queued to it fail"),
            file=sys.stderr,
        )
        return session.request(
            session.topics.contact_remove(entry["key"]),
            {},
            args.wait,
            {"removed", "refused", "failed"},
        )
    finally:
        session.close()


def _contacts_telemetry(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.forget and args.state == "on":
        # Translators: {state} is the command's word off, which stays
        print(PREFIX + tr("--forget goes with {state} only", state="off"), file=sys.stderr)
        return EX_USAGE
    if (given := _contact_given(args.contact)) is None:
        return EX_USAGE
    session = _ContactSession(config, ["contacts"])
    try:
        if (status := _opened(session)) is not None:
            return status
        index = session.document("contacts")
        entry = _indexed(*given, index)
        if entry is None:
            return EX_USAGE
        key, name = entry["key"], entry.get("name")
        _print_key(key, name if isinstance(name, str) else None)
        if args.state == "off":
            # Translators: {readings} and {positions} are the names of two settings, which stay
            stops = (
                tr(
                    "the gateway stops asking it and forgets its battery, readings and position "
                    "and deletes them from its history"
                )
                if args.forget
                else tr(
                    "the gateway stops asking it and forgets its battery, readings and "
                    "position; its history keeps them for {readings} and {positions}",
                    readings="readings_days",
                    positions="positions_days",
                )
            )
            print(stops, file=sys.stderr)
            listed = any(e["key"] == key for e in _entries(index, "contacts"))
            cut = isinstance(index, dict) and index.get("truncated") is True
            if not listed and not cut:  # also before it is added: asked within a minute then
                print(tr("not on the radio now: excluded in advance"), file=sys.stderr)
        request = {"telemetry": args.state, **({"forget": True} if args.forget else {})}
        return session.request(
            session.topics.contact_telemetry(key),
            request,
            args.wait,
            {"excluded", "included", "refused", "failed"},
            frozenset({"excluded", "included"}),
        )
    finally:
        session.close()


def _contacts_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session = _ContactSession(config, ["contacts", "nodes"])
    try:
        error = session.open()
    finally:
        session.close()
    if error is not None:
        print(PREFIX + error, file=sys.stderr)
        return EX_FAILURE
    index, nodes = session.document("contacts"), session.document("nodes")
    if not isinstance(index, dict):
        print(PREFIX + tr("the gateway has published no contacts"), file=sys.stderr)
        return EX_FAILURE
    if not session.online:
        offline = tr("the gateway is offline: the lists are as it last saw them")
        print(PREFIX + offline, file=sys.stderr)
    if args.json:
        print(_json_text({"contacts": index, "nodes": nodes}, indent=2))
        return EX_OK
    manage = index.get("manage") is True
    for e in _entries(index, "contacts"):
        mark = "protected" if e.get("protected") else "removable" if manage else "-"
        print(_list_line("contact", e, mark))
    for e in _entries(nodes, "nodes"):
        print(_list_line("node", e, "look_alike" if e.get("look_alike") else "-"))
    for name, document in (("contacts", index), ("nodes", nodes)):
        if isinstance(document, dict) and document.get("truncated") is True:
            shown, total = len(_entries(document, name)), document.get("total")
            print(PREFIX + _bounded(name, shown, total), file=sys.stderr)
    reason = index.get("manage_reason")
    if isinstance(reason, str):  # an older gateway publishes none
        text = _REFUSING.get(reason, N_("the gateway takes no add or remove requests ({reason})"))
        print(PREFIX + tr(text, reason=reason), file=sys.stderr)
    return EX_OK


# Translators: after the lists of `contacts`: why the gateway refuses to add or remove contacts
# now (OPS-007); the settings stay as they are
_REFUSING = {
    "management_off": N_("the gateway takes no add or remove requests: [contacts] manage = false"),
    "no_radio": N_("the gateway takes no add or remove requests while its radio is not connected"),
    "untested_firmware": N_(
        "the gateway takes no add or remove requests: it is not tested with the radio's "
        'firmware (radio.profile.manage = "always" allows them; docs/deployment.md, '
        "Troubleshooting)"
    ),
}


def _bounded(name: str, shown: int, total: Any) -> str:
    """A note that the gateway's document of its contacts or nodes lists only some of them."""
    n = total if isinstance(total, int) else 0  # chooses the form; {count} shows what was sent
    values = {"shown": shown, "count": total}
    if name == "contacts":
        return trn(
            "the gateway lists {shown} of {count} contact (its document is bounded); give the "
            "others by full key",
            "the gateway lists {shown} of {count} contacts (its document is bounded); give the "
            "others by full key",
            n,
            **values,
        )
    return trn(
        "the gateway lists {shown} of {count} node (its document is bounded); give the others "
        "by full key",
        "the gateway lists {shown} of {count} nodes (its document is bounded); give the others "
        "by full key",
        n,
        **values,
    )


def _list_line(kind: str, entry: dict[str, Any], mark: str) -> str:
    fields = [kind, entry["key"], str(entry.get("type")), mark, str(entry.get("heard_at") or "-")]
    return "\t".join([*fields, _safe_text(str(entry.get("name") or ""))])


def _contacts_link(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session = _ContactSession(config, ["state"])
    try:
        error = session.open()
    finally:
        session.close()
    state = session.document("state")
    link = state.get("contact_link") if isinstance(state, dict) else None
    if error is not None or not isinstance(link, str):
        print(PREFIX + (error or tr("the gateway has published no contact link")), file=sys.stderr)
        return EX_FAILURE
    print(_safe_text(link))
    return EX_OK
