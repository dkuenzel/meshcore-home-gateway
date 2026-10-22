"""JSON payloads of the MQTT API (docs/mqtt-api.md).

Event payloads carry `event_type`, so Home Assistant event entities can use the topics directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from meshcore_home_gateway.core.commands import Command, CommandDecision
from meshcore_home_gateway.core.contacts import Contact
from meshcore_home_gateway.core.model import (
    DeliveryEvent,
    InboundMessage,
    MessageKind,
    split_sender_name,
)


def encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat(timespec="seconds")


def received_payload(
    message: InboundMessage,
    sender: Contact | None,
    target: str | None,
    decision: CommandDecision | None = None,
) -> dict[str, Any]:
    text = message.text
    sender_name = sender.name if sender else None
    if message.kind is MessageKind.CHANNEL:
        sender_name, text = split_sender_name(text)  # claimed, not authenticated
    command = None
    if decision is not None:
        command = {
            "word": decision.word,
            "accepted": decision.command is not None,
            "reason": decision.rejection.value if decision.rejection else None,
        }
    return {
        "event_type": message.kind.value,
        "text": text,
        "text_type": message.text_type.name.lower(),
        "sender_key": sender.public_key if sender else None,
        "sender_prefix": message.sender_prefix,
        "sender_name": sender_name,
        "target": target,
        "channel_index": message.channel_index,
        "sent_at": iso(message.sender_timestamp),
        "received_at": iso(message.received_at),
        "snr_db": message.snr_db,
        "hops": message.hops,
        "command": command,
    }


def command_payload(
    command: Command, sender_name: str | None, received_at: float
) -> dict[str, Any]:
    return {
        "event_type": command.word,
        "args": command.args,
        "sender_key": command.sender_key,
        "sender_name": sender_name,
        "sent_at": iso(command.sender_timestamp),
        "received_at": iso(received_at),
    }


def delivery_payload(event: DeliveryEvent) -> dict[str, Any]:
    return {
        "event_type": event.state.value,
        "id": event.id,
        "target": event.target,
        "text": event.text,
        "attempt": event.attempt,
        "reason": event.reason,
        "rtt_ms": event.rtt_ms,
        "airtime_ms": event.airtime_ms,
    }


def rx_payload(snr_db: float, rssi_dbm: int, raw: bytes, received_at: float) -> dict[str, Any]:
    return {
        "snr_db": snr_db,
        "rssi_dbm": rssi_dbm,
        "length": len(raw),
        "raw": raw.hex(),
        "received_at": iso(received_at),
    }
