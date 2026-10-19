"""Rules for outgoing message text.

The radio limits a message by bytes, not characters (see docs/protocol.md), and some handheld
devices can only display ASCII. `prepare_text` cleans a text, optionally transliterates it to
ASCII and enforces the byte limit by truncation or rejection.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ELLIPSIS = "..."

# Characters with a conventional ASCII spelling. Everything else goes through Unicode
# decomposition (é -> e); what is still not ASCII becomes "?".
_TRANSLITERATION = str.maketrans(
    {
        "ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss",
        # double quotes and guillemets
        "\u201e": '"', "\u201c": '"', "\u201d": '"', "\u00ab": '"', "\u00bb": '"',
        # single quotes
        "\u201a": "'", "\u2018": "'", "\u2019": "'", "\u2039": "'", "\u203a": "'",
        # en dash, em dash, minus sign
        "\u2013": "-", "\u2014": "-", "\u2212": "-",
        # ellipsis, degree sign, euro sign, no-break space
        "\u2026": "...", "\u00b0": "deg", "\u20ac": "EUR", "\u00a0": " ",
    }
)  # fmt: skip


class TextRejectedError(ValueError):
    """The text cannot be sent under the policy; `reason` is a short machine-readable code."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class TextPolicy:
    """How outgoing text is shaped.

    max_bytes: limit of the UTF-8 encoded text.
    ascii_only: transliterate to ASCII for devices without Unicode fonts.
    truncate: cut texts that are too long (marked with "...") instead of rejecting them.
    """

    max_bytes: int
    ascii_only: bool = False
    truncate: bool = True

    def __post_init__(self) -> None:
        if self.max_bytes <= len(ELLIPSIS):
            raise ValueError(f"max_bytes must exceed {len(ELLIPSIS)}, got {self.max_bytes}")


def prepare_text(text: str, policy: TextPolicy) -> str:
    """Return the text as it will be sent, or raise TextRejectedError."""
    cleaned = _clean(unicodedata.normalize("NFC", text))
    if policy.ascii_only:
        cleaned = _to_ascii(cleaned)
    if not cleaned:
        raise TextRejectedError("empty", "text is empty after cleaning")
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= policy.max_bytes:
        return cleaned
    if not policy.truncate:
        raise TextRejectedError(
            "too_long", f"text too long: {len(encoded)} bytes, limit {policy.max_bytes}"
        )
    keep = encoded[: policy.max_bytes - len(ELLIPSIS)].decode("utf-8", errors="ignore")
    return keep.rstrip() + ELLIPSIS


_SPACES = re.compile(r" {2,}")
_SPACES_AROUND_NEWLINE = re.compile(r" *\n *")


def _clean(text: str) -> str:
    """Normalise line endings, drop control characters, collapse spaces (bytes are scarce)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    kept = "".join(ch for ch in text if ch == "\n" or unicodedata.category(ch)[0] != "C")
    return _SPACES_AROUND_NEWLINE.sub("\n", _SPACES.sub(" ", kept)).strip()


def _to_ascii(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATION))
    base = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch if ch.isascii() else "?" for ch in base)
