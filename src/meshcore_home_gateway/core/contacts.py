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
