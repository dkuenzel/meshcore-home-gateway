"""The radio's contact table, used to resolve 6-byte key prefixes to full public keys.

Messages and send commands of the companion protocol identify a contact by the first 6 bytes of
its public key. Somebody can generate a key with the same 6-byte prefix, so permissions and
targets are bound to the full key and a prefix must resolve to exactly one contact (ADR 0009).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

from meshcore_home_gateway.texts import TextError

KEY_HEX_DIGITS = 64
PREFIX_HEX_DIGITS = 12
# what the gateway publishes and keeps of the nodes it heard but did not add (`[contacts]
# heard_nodes`): everything; their keys and types only; nothing but the look-alikes' keys
HeardNodes = Literal["full", "keys", "off"]
HEARD_NODES: tuple[HeardNodes, ...] = ("full", "keys", "off")
_HEX = re.compile(r"[0-9a-f]+")
_LOWER_HEX = re.compile(r"[0-9a-f]{64}")


def is_key(text: str) -> bool:
    """A full public key as the gateway writes it: 64 lower-case hex digits."""
    return len(text) == KEY_HEX_DIGITS and _LOWER_HEX.fullmatch(text) is not None


def normalise_key(key: str) -> str:
    """Lower-case a full public key (32 bytes, 64 hex digits) or raise ValueError (a TextError)."""
    value = key.strip().lower()
    if len(value) != KEY_HEX_DIGITS or not _HEX.fullmatch(value):
        raise TextError(
            "public key must be {digits} hex digits, got {key!r}", digits=KEY_HEX_DIGITS, key=key
        )
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
    key: str,
    node_type: int,
    *,
    table: Iterable[str],
    protected: Iterable[str],
    own_key: str,
    max_contacts: int,
) -> str | None:
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


# --- labels: a node for a person to pick (Home Assistant's select, the command line) ----------

LABEL_SUFFIX_HEX_DIGITS = 8
LABEL_TYPES = frozenset({1, 3, 4})  # chat, room server, sensor: the heard nodes offered by label
_LABEL_TYPE = {3: " (room)", 4: " (sensor)"}
NAME_TAKEN = " (name taken)"  # after a label whose node has a name another has (SEC-007)
_LABEL = re.compile(r"([0-9a-fA-F]{12})(?:…|\.\.\.)([0-9a-fA-F]{8})(?: |\Z)")
_UNSAFE = frozenset({"Cc", "Cf", "Zl", "Zp"})  # control, format (direction), line breaks
# Unicode's Default_Ignorable_Code_Point (DerivedCoreProperties.txt, 15.0): drawn as nothing,
# whatever their category: format characters, but also combining marks (the variation selectors,
# the grapheme joiner), letters (the Hangul fillers) and code points kept for more of them. One
# table for names and copies: names compare without all of them (SEC-007, #89); a copied key,
# link or card loses COPY_INVISIBLE, those a copy may carry and none holds (protocol.links). The
# rest belongs to a name's text without changing its look: the joiners and variation selectors
# of an emoji, the fillers of Hangul.
_INVISIBLE_SPANS = (
    (0x00AD, 0x00AD),
    (0x034F, 0x034F),
    (0x061C, 0x061C),
    (0x115F, 0x1160),
    (0x17B4, 0x17B5),
    (0x180B, 0x180F),
    (0x200B, 0x200F),
    (0x202A, 0x202E),
    (0x2060, 0x206F),
    (0x3164, 0x3164),
    (0xFE00, 0xFE0F),
    (0xFEFF, 0xFEFF),
    (0xFFA0, 0xFFA0),
    (0xFFF0, 0xFFF8),
    (0x1BCA0, 0x1BCA3),
    (0x1D173, 0x1D17A),
    (0xE0000, 0xE0FFF),
)
INVISIBLE = frozenset(chr(c) for first, last in _INVISIBLE_SPANS for c in range(first, last + 1))
# the soft hyphen, the Arabic letter mark, the zero-width space (the dashboard's breaks in long
# words), the direction marks, embeddings, overrides and isolates, the word joiner and the
# invisible operators, the byte order mark
COPY_INVISIBLE = "".join(
    chr(c)
    for first, last in (
        (0x00AD, 0x00AD),
        (0x061C, 0x061C),
        (0x200B, 0x200B),
        (0x200E, 0x200F),
        (0x202A, 0x202E),
        (0x2060, 0x2064),
        (0x2066, 0x2069),
        (0xFEFF, 0xFEFF),
    )
    for c in range(first, last + 1)
)
_NAME_INVISIBLE = str.maketrans(dict.fromkeys(INVISIBLE))
_BLANKS = frozenset("\u2800")  # the braille pattern blank (So): drawn as a space
# the key of a name that is shown, but drawn as nothing (a Hangul filler, a braille blank): the
# braille blank, which no other key holds (`name_key` makes it a space)
_BLANK_NAME = "\u2800"
# Cyrillic and Greek letters that look like Latin ones (SEC-007): a small table, not Unicode's
# confusables (UTS #39), against the common imitations of a name; mapped before case folding
_LOOK_ALIKE_LETTERS = str.maketrans(
    "АВЕКМНОРСТХУЅІЈаеорсухѕіјһԁԛԝΑΒΕΖΗΙΚΜΝΟΡΤΥΧαοικνρυχ",
    "ABEKMHOPCTXYSIJaeopcyxsijhdqwABEZHIKMNOPTYXaoikvpux",
)


def name_key(name: str) -> str:
    """A name as compared with others: NFKC, case-folded, Cyrillic and Greek look-alikes as the
    Latin letters, without the invisible characters (`INVISIBLE` and every format character:
    zero-width, direction, variation selectors, fillers), control characters, line breaks and
    the braille blank as spaces, single spaces (SEC-007). Empty for a name that is none, as a
    label shows none; a name shown but drawn as nothing, such as a Hangul filler, is one blank
    name (#89)."""
    # dropped before NFKC, which maps nothing into the table: a grapheme joiner would keep a
    # mark from composing with its letter
    folded = unicodedata.normalize("NFKC", name.translate(_NAME_INVISIBLE))
    plain = "".join(_compared(ch) for ch in folded)
    key = " ".join(plain.translate(_LOOK_ALIKE_LETTERS).casefold().split())
    return key if key or not _shown(name) else _BLANK_NAME


def shows_a_name(name: str) -> bool:
    """Whether a name reads as one where it stands alone, as a device's: not empty, not only
    invisible characters, not drawn as nothing (a Hangul filler, the braille blank, #89)."""
    return name_key(name) not in ("", _BLANK_NAME)


def _compared(ch: str) -> str:
    """A character of a name as `name_key` compares it: a format character is dropped, a control
    character, a line break or a blank is a space."""
    kind = unicodedata.category(ch)
    if kind == "Cf":
        return ""
    return " " if kind in _UNSAFE or ch in _BLANKS else ch


def node_label(key: str, name: str, node_type: int, *, name_taken: bool = False) -> str:
    """`<first 12 digits>…<last 8 digits> <name>`, with ` (room)` or ` (sensor)`, and ` (name
    taken)` for a name another node or the gateway has: the key comes
    first, so no name can push it aside or imitate it. A person compares both parts with the
    node's own screen; an attacker can copy the first 6 bytes of a key, not the rest (ADR 0009)."""
    plain = _shown(name)
    named = f" {plain}" if plain else ""
    start, end = key[:PREFIX_HEX_DIGITS], key[-LABEL_SUFFIX_HEX_DIGITS:]
    mark = NAME_TAKEN if name_taken else ""
    return f"{start}…{end}{named}{_LABEL_TYPE.get(node_type, '')}{mark}"


def _shown(name: str) -> str:
    """The name as a label shows it: control, format and line break characters as spaces, single
    spaces."""
    return " ".join(
        "".join(" " if unicodedata.category(ch) in _UNSAFE else ch for ch in name).split()
    )


def key_from_label(label: str, keys: Iterable[str]) -> str | None:
    """The one key whose first 12 and last 8 digits a label names (the name after them is not
    compared: it may have changed since); None if there is none or more than one."""
    match = _LABEL.match(label.strip())
    if match is None:
        return None
    start, end = match.group(1).lower(), match.group(2).lower()
    found = {k for k in keys if k.startswith(start) and k.endswith(end)}
    return found.pop() if len(found) == 1 else None
