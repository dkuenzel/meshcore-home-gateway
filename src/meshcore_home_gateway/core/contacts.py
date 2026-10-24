"""The radio's contact table, used to resolve 6-byte key prefixes to full public keys.

Messages and send commands of the companion protocol identify a contact by the first 6 bytes of
its public key. Somebody can generate a key with the same 6-byte prefix, so permissions and
targets are bound to the full key and a prefix must resolve to exactly one contact (ADR 0009).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

KEY_HEX_DIGITS = 64
PREFIX_HEX_DIGITS = 12
_HEX = re.compile(r"[0-9a-f]+")
_LOWER_HEX = re.compile(r"[0-9a-f]{64}")


def is_key(text: str) -> bool:
    """A full public key as the gateway writes it: 64 lower-case hex digits."""
    return len(text) == KEY_HEX_DIGITS and _LOWER_HEX.fullmatch(text) is not None


def normalise_key(key: str) -> str:
    """Lower-case a full public key (32 bytes, 64 hex digits) or raise ValueError."""
    value = key.strip().lower()
    if len(value) != KEY_HEX_DIGITS or not _HEX.fullmatch(value):
        raise ValueError(f"public key must be {KEY_HEX_DIGITS} hex digits, got {key!r}")
    return value


@dataclass(frozen=True, slots=True)
class Contact:
    public_key: str
    name: str

    @property
    def prefix(self) -> str:
        return self.public_key[:PREFIX_HEX_DIGITS]


class ContactBook:
    def __init__(self, contacts: Iterable[Contact]) -> None:
        self._by_key = {c.public_key: c for c in contacts}

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self) -> Iterator[Contact]:
        return iter(self._by_key.values())

    def by_key(self, public_key: str) -> Contact | None:
        return self._by_key.get(public_key)

    def resolve(self, prefix: str) -> list[Contact]:
        """All contacts whose key starts with `prefix`; more than one means ambiguous."""
        return [c for key, c in self._by_key.items() if key.startswith(prefix.lower())]


# --- managing the table (ADR 0017) --------------------------------------------------------------

NODE_TYPES_ADDED = frozenset({1, 2, 3, 4})  # chat, repeater, room server, sensor (NodeType)


def look_alike(key: str, keys: Iterable[str]) -> tuple[str, ...]:
    """The other keys with the same 6-byte prefix: the radio could not tell them apart in a
    message or a send (ADR 0009)."""
    prefix = key[:PREFIX_HEX_DIGITS]
    return tuple(sorted(k for k in set(keys) if k != key and k[:PREFIX_HEX_DIGITS] == prefix))


def add_refusal(
    key: str, node_type: int, *, table: Iterable[str], protected: Iterable[str], own_key: str,
    max_contacts: int,
) -> str | None:  # fmt: skip
    """Why the key must not be added to the radio's table as read, or None. A protected key may
    be added by hand (it was removed meanwhile); one with another's prefix may not."""
    keys = set(table)
    if key == own_key:
        return "own_key"
    if node_type not in NODE_TYPES_ADDED:
        return "unsupported_type"  # type 0 lands in a slot the radio never lists or saves
    if key in keys:
        return "already_contact"  # the command would overwrite every field of it
    if look_alike(key, keys | set(protected)):
        return "look_alike"
    if len(keys) >= max_contacts:
        return "table_full"  # the radio would refuse it, or evict the oldest
    return None


def remove_refusal(key: str, *, table: Iterable[str]) -> str | None:
    """Why the key cannot be removed from the radio's table as read, or None."""
    return None if key in set(table) else "unknown_contact"
