"""Data model shared by the core, the radio adapter and the bus adapter."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class MessageKind(StrEnum):
    CONTACT = "contact"  # direct message from one contact, end-to-end encrypted
    CHANNEL = "channel"  # group message; anyone with the channel secret can write


class TextType(IntEnum):
    """Text types of the companion protocol (docs/protocol.md)."""

    PLAIN = 0
    CLI_DATA = 1
    SIGNED_PLAIN = 2  # room server post: a 4-byte author prefix precedes the text
    CLI_COMMAND = 3  # CLI command from a contact without remote-CLI permission


@dataclass(frozen=True, slots=True)
class InboundMessage:
    """A message the radio received.

    sender_timestamp: the sender's clock (Unix seconds), set by the sending device.
    received_at: the bridge's wall clock (Unix seconds) when the bridge got the message.
    sender_prefix: first 6 bytes of the sender's public key as 12 hex digits (contact messages).
    hops: repeaters a flooded message passed; None if it arrived by direct route (count unknown).
    """

    kind: MessageKind
    text: str
    sender_timestamp: int
    received_at: float
    text_type: TextType = TextType.PLAIN
    sender_prefix: str | None = None
    channel_index: int | None = None
    snr_db: float | None = None
    hops: int | None = None


class DeliveryState(StrEnum):
    """Lifecycle of an outgoing message, published on the delivery topic."""

    QUEUED = "queued"  # accepted, waiting for airtime or for the radio
    SENT = "sent"  # handed to the radio; final for channel messages (no acknowledgement)
    CONFIRMED = "confirmed"  # the recipient acknowledged a direct message
    FAILED = "failed"  # no acknowledgement after all attempts, or the radio refused
    EXPIRED = "expired"  # waited longer than the queue time limit
    DUPLICATE = "duplicate"  # same target and text within the deduplication window
    REJECTED = "rejected"  # unknown target, empty text, too long, queue full


@dataclass(frozen=True, slots=True)
class Target:
    """A configured destination. Only configured targets can be sent to (ADR 0008)."""

    alias: str
    kind: MessageKind
    public_key: str | None = None  # contact targets: full key, 64 hex digits
    channel_index: int | None = None  # channel targets

    def __post_init__(self) -> None:
        if self.kind is MessageKind.CONTACT and self.public_key is None:
            raise ValueError(f"contact target {self.alias!r} needs a public key")
        if self.kind is MessageKind.CHANNEL and self.channel_index is None:
            raise ValueError(f"channel target {self.alias!r} needs a channel index")


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    """One step in the life of an outgoing message (MQTT delivery topic)."""

    id: str
    target: str
    state: DeliveryState
    text: str | None = None
    attempt: int = 0
    reason: str | None = None
    rtt_ms: int | None = None
    airtime_ms: int | None = None


_NAME_PREFIX_MAX = 32


def split_sender_name(text: str) -> tuple[str | None, str]:
    """Split "Name: text" of a channel message into the claimed name and the text.

    The firmware writes the sender's node name in front of channel texts. It is unauthenticated:
    anyone with the channel secret can write any name.
    """
    name, sep, rest = text.partition(": ")
    if sep and 0 < len(name) <= _NAME_PREFIX_MAX and "\n" not in name:
        return name, rest
    return None, text
