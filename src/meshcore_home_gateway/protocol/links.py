"""What a person hands over to add a contact (ADR 0017): a public key, a contact link
(`meshcore://contact/add?…`, the firmware's docs/qr_codes.md) or a contact card
(`meshcore://` and the hex of an advert packet, as the MeshCore app exports it).

Pure parsing. Name and type of a link or card are claims: a card's signature is not checked
(the standard library has no Ed25519), and the radio replaces both with the node's next signed
advert.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode

from meshcore_home_gateway.core.contacts import NODE_TYPES_ADDED
from meshcore_home_gateway.protocol.packet import parse_advert, parse_packet
from meshcore_home_gateway.texts import TextError

SCHEME = "meshcore://"
ADD_PATH = "contact/add"
_KEY = re.compile(r"[0-9a-fA-F]{64}")
_HEX = re.compile(r"(?:[0-9a-fA-F]{2})+")
_ONCE = ("public_key", "name", "type")


class LinkError(TextError):
    """The text is no key, link or card the gateway takes; `reason` as in the `contact` event,
    the text for people (str() English, `.text` for the command line) after it."""

    def __init__(self, reason: str, msgid: str, /, **values: object) -> None:
        super().__init__(msgid, **values)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class LinkContact:
    public_key: str  # 64 lower-case hex digits
    name: str | None  # claimed; None: none given
    node_type: int  # claimed; 1 for a bare key
    source: str  # "key", "link" or "card"; "heard" for a heard node picked by its label


def parse_contact_text(text: str) -> LinkContact:
    """A key, contact link or contact card; LinkError with the reason otherwise."""
    value = text.strip()
    try:  # a lone surrogate (a JSON escape, a command line argument): no text
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise LinkError("invalid_link", "the text is not valid Unicode") from None
    if _KEY.fullmatch(value):
        return LinkContact(value.lower(), None, 1, "key")
    if value[: len(SCHEME)].lower() != SCHEME:
        raise LinkError("invalid_link", "not a public key, contact link or contact card")
    rest = value[len(SCHEME) :]
    if rest.startswith(ADD_PATH + "?"):
        return _link(rest[len(ADD_PATH) + 1 :])
    if _HEX.fullmatch(rest):
        return _card(bytes.fromhex(rest))
    if "/" in rest or "?" in rest:
        raise LinkError("unsupported_link", "a MeshCore link, but not one that adds a contact")
    raise LinkError("invalid_link", "not a contact link or contact card")


def contact_link(name: str, public_key: str) -> str:
    """The link that adds this node as a companion, for a QR code (no position: ADR 0016)."""
    query = urlencode({"name": name, "public_key": public_key, "type": 1})
    return f"{SCHEME}{ADD_PATH}?{query}"


def _link(query: str) -> LinkContact:
    try:
        fields = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError:
        raise LinkError("invalid_link", "the contact link is malformed") from None
    if any(len(fields.get(name, ())) > 1 for name in _ONCE):
        raise LinkError("invalid_link", "the contact link repeats a parameter")
    (key,) = fields.get("public_key", [""])
    if not _KEY.fullmatch(key):
        raise LinkError("invalid_link", "the contact link has no valid public key")
    (name,) = fields.get("name", [""])
    (kind,) = fields.get("type", ["1"])
    node_type = int(kind) if kind.isascii() and kind.isdigit() else -1
    if node_type not in NODE_TYPES_ADDED:
        raise LinkError(
            "unsupported_type", "the contact link has type {kind!r}, not 1 to 4", kind=kind
        )
    return LinkContact(key.lower(), name or None, node_type, "link")


def _card(raw: bytes) -> LinkContact:
    packet = parse_packet(raw)
    advert = parse_advert(packet) if packet is not None else None
    if advert is None:
        raise LinkError("invalid_link", "the contact card holds no advert")
    if advert.node_type not in NODE_TYPES_ADDED:
        raise LinkError(
            "unsupported_type", "the contact card has type {node_type}", node_type=advert.node_type
        )
    return LinkContact(advert.public_key.hex(), advert.name, advert.node_type, "card")
