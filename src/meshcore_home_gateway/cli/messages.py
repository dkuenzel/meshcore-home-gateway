"""`send` and `listen`: a message through the gateway and the events of its delivery; the
gateway's events as JSON lines."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
from typing import Any

from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import EX_FAILURE, EX_OK, PREFIX, _json_text, tr
from meshcore_home_gateway.config import load_config

FINAL_STATES = {
    "confirmed",
    "failed",
    "expired",
    "duplicate",
    "rejected",
}  # plus "sent" for channels


def _matches(event_text: Any, wanted: str) -> bool:
    """Whether a delivery event carries the requested text, also when the bridge shortened it."""
    if not isinstance(event_text, str):
        return False
    if event_text == wanted:
        return True
    return event_text.endswith("...") and wanted.startswith(event_text[:-3])


class _DeliveryWatch:
    """Follows the delivery events of one send request. The bridge names the request in its first
    event (queued, duplicate or rejected) with its own id and the final text; with `nonce`, a
    bridge before 2.1 included the random suffix there, which it now shows in the final event
    only (#87). The result is the final event with that id. Events of other requests to the
    same target are ignored."""

    def __init__(self, alias: str, wanted: str, final: set[str], *, nonce: bool = False) -> None:
        self._alias, self._wanted, self._final, self._nonce = alias, wanted, final, nonce
        self._id: str | None = None
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()

    def _named(self, event_text: Any) -> bool:
        from meshcore_home_gateway.core.text import without_nonce  # noqa: PLC0415

        if _matches(event_text, self._wanted):
            return True
        return (
            self._nonce
            and isinstance(event_text, str)
            and (_matches(without_nonce(event_text), self._wanted))
        )

    def on_message(self, _c: Any, _u: Any, message: Any) -> None:
        try:
            event = json.loads(message.payload)
        except (ValueError, RecursionError):  # also nested too deeply; it would end paho's thread
            return
        if not isinstance(event, dict) or event.get("target") != self._alias:
            return
        if self._id is None:
            first = event.get("event_type") in {"queued", "duplicate", "rejected"}
            unnamed_rejection = event.get("event_type") == "rejected" and event.get("text") is None
            if not first or not (self._named(event.get("text")) or unnamed_rejection):
                return
            self._id = str(event.get("id"))
        if event.get("id") == self._id and event.get("event_type") in self._final:
            self.result = event
            self.done.set()


def _send(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.core.text import (  # noqa: PLC0415
        TextPolicy,
        TextRejectedError,
        prepare_text,
    )
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

    config = load_config(args.config)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    target = next((t for t in config.targets if t.alias == args.alias), None)
    if target is None:  # the bridge would reject it; also keeps wildcards out of the topic
        missing = tr("no target {alias!r} in {path}", alias=args.alias, path=args.config)
        print(PREFIX + missing, file=sys.stderr)
        return EX_FAILURE
    try:  # bytes that are not UTF-8 reach argv as surrogates (PEP 383)
        payload = args.text.encode("utf-8")
    except UnicodeEncodeError:
        print(PREFIX + tr("the text is not valid UTF-8"), file=sys.stderr)
        return EX_FAILURE
    final = FINAL_STATES | ({"sent"} if target.channel_index is not None else set())
    try:  # the text as the bridge will report it, apart from shortening
        policy = TextPolicy(1 << 20, ascii_only=config.messages.ascii_only, truncate=False)
        wanted = prepare_text(args.text, policy)
    except TextRejectedError:
        wanted = args.text
    watch = _DeliveryWatch(
        args.alias,
        wanted,
        final,
        nonce=config.messages.confirm_nonce and target.public_key is not None,
    )
    client = broker.mqtt_client(config.mqtt, "send")
    client.on_message = watch.on_message
    try:
        error = broker.Connection().open(client, config.mqtt)
        if error is None and args.wait > 0:
            error = broker.subscribe(client, topics.delivery)
        if error is None:
            error = broker.publish(client, topics.send(args.alias), payload)
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        if args.wait <= 0:
            return EX_OK
        if not watch.done.wait(args.wait) or watch.result is None:
            print(PREFIX + tr("no delivery result in time"), file=sys.stderr)
            return EX_FAILURE
        print(_json_text(watch.result))
        suspect = bool(watch.result.get("suspect"))
        if suspect:
            # Translators: security text (docs/translations.md): the recipient's
            # acknowledgement came sooner than the message's way through the mesh allows
            warning = tr("confirmed sooner than the path allows: the acknowledgement may be forged")
            print(PREFIX + warning, file=sys.stderr)
        delivered = watch.result["event_type"] in {"sent", "confirmed"} and not suspect
        return EX_OK if delivered else EX_FAILURE
    finally:
        client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged messages
        client.loop_stop()


def _listen(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

    config = load_config(args.config)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    wanted = {
        "received": [topics.received],
        "command": [topics.command],
        "delivery": [topics.delivery],
        "state": [topics.state],
        "contact": [topics.contact_event],
        "node": [topics.node_event],
        "telemetry": [topics.telemetry_event],
        "advert": [topics.advert_event],
        "all": [
            topics.received,
            topics.command,
            topics.delivery,
            topics.state,
            topics.contact_event,
            topics.node_event,
            topics.telemetry_event,
            topics.advert_event,
        ],
    }[args.topic]
    client = broker.mqtt_client(config.mqtt, "listen")
    seen = 0
    finished = threading.Event()
    failures: list[Exception] = []

    def on_message(_c: Any, _u: Any, message: Any) -> None:
        nonlocal seen
        if finished.is_set():  # paho may deliver a few more before the disconnect
            return
        try:
            payload: Any = json.loads(message.payload)
        except (ValueError, RecursionError):  # also nested too deeply; it would end paho's thread
            payload = message.payload.decode("utf-8", errors="replace")
        try:
            print(_json_text({"topic": message.topic, "payload": payload}), flush=True)
        except Exception as exc:  # noqa: BLE001 - e.g. a closed pipe: end, do not hang
            failures.append(exc)
            finished.set()
            return
        seen += 1
        if args.count and seen >= args.count:
            finished.set()

    def subscribe(c: Any) -> None:  # after every (re)connect: the session is clean
        c.subscribe([(topic, 1) for topic in wanted])

    connection = broker.Connection(on_ready=subscribe)
    client.on_message = on_message
    try:
        error = connection.open(client, config.mqtt)
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        finished.wait()
    finally:
        client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged messages
        client.loop_stop()
    if failures:
        if isinstance(failures[0], BrokenPipeError):  # the reader went away (`| head`): quietly
            with contextlib.suppress(OSError):
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        else:
            print(f"{PREFIX}{failures[0]}", file=sys.stderr)  # the system's text
        return EX_FAILURE
    return EX_OK
