"""`advert`: make the radio send its advert once to the radios in direct range, zero hop, through
the gateway's request on the bus (ADR 0033). The service holds the radio's only link: the command
never opens the radio."""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import (
    EX_FAILURE,
    EX_OK,
    PREFIX,
    _json_text,
    _safe_text,
    tr,
)
from meshcore_home_gateway.config import Config, load_config

_OUTCOMES = {"sent", "refused", "failed"}  # every request ends in one of them


class _AdvertSession(broker.GatewaySession):
    """A broker connection for `advert`: the gateway's availability, then the request and the
    `advert` event with its id."""

    def __init__(self, config: Config) -> None:
        self.watch: broker.EventWatch | None = None
        super().__init__(config, suffix="advert")

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        if message.topic == self.topics.advert_event and self.watch is not None:
            self.watch.on_message(message)
        else:
            super()._on_message(client, userdata, message)


def _advert(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session = _AdvertSession(config)
    try:
        error = session.open()
        if error is None and not session.online:
            error = tr("the gateway is offline")
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        return _request(session, args.wait)
    finally:
        session.close()


def _request(session: _AdvertSession, wait_s: float) -> int:
    request_id = f"cli-{os.urandom(4).hex()}"
    session.watch = broker.EventWatch(request_id, _OUTCOMES)
    topics = session.topics
    error = broker.subscribe(session.client, topics.advert_event) if wait_s > 0 else None
    body = json.dumps({"id": request_id}).encode("utf-8")
    error = error or broker.publish(session.client, topics.advert_request, body)
    if error is not None:
        print(PREFIX + error, file=sys.stderr)
        return EX_FAILURE
    if wait_s <= 0:
        return EX_OK
    if not session.watch.done.wait(wait_s) or session.watch.result is None:
        late = tr(
            "no result in time (request {request}; it may still be handled: {command})",
            request=request_id,
            command="meshcore-home-gateway listen --topic advert",
        )
        print(PREFIX + late, file=sys.stderr)
        return EX_FAILURE
    result = session.watch.result
    print(_json_text(result))
    kind, reason = result.get("event_type"), str(result.get("reason"))
    if kind == "sent":
        # Translators: name is the radio's name as the advert carried it
        sent = tr(
            "advert sent with the name {name!r}; radios in direct range that heard it can add "
            "this radio or show its new name",
            name=str(result.get("name")),
        )
        print(_safe_text(sent), file=sys.stderr)
        return EX_OK
    if kind == "refused":
        # Translators: reason is a code such as too_soon, which stays; document is a path
        refused = tr(
            "no advert sent ({reason}); {document} says why",
            reason=reason,
            document="docs/mqtt-api.md",
        )
        print(PREFIX + _safe_text(refused), file=sys.stderr)
    else:
        # Translators: reason is a code such as outcome_unknown, which stays
        failed = tr(
            "the radio did not confirm the advert ({reason}); it may have gone out", reason=reason
        )
        print(PREFIX + _safe_text(failed), file=sys.stderr)
    return EX_FAILURE
