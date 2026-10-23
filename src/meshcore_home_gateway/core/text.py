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
        cleaned = _clean(_to_ascii(cleaned))  # transliteration turns wide spaces into " "
    if not any(unicodedata.category(ch) != "Cf" and not ch.isspace() for ch in cleaned):
        raise TextRejectedError("empty", "text is empty after cleaning")
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= policy.max_bytes:
        return cleaned
    if not policy.truncate:
        raise TextRejectedError(
            "too_long", f"text too long: {len(encoded)} bytes, limit {policy.max_bytes}"
        )
    return _cut(cleaned, policy.max_bytes - len(ELLIPSIS)).rstrip() + ELLIPSIS


def _cut(text: str, max_bytes: int) -> str:
    """The longest start of `text` within `max_bytes` UTF-8 bytes that ends between two whole
    characters as a reader sees them: a flag, joined emoji, an emoji with its skin tone, a
    letter with its marks.

    The standard library has no grapheme segmentation (Unicode UAX #29), so `_joined`
    approximates it for marks, joiners, variation selectors, emoji modifiers, tags and flags. Not
    covered are Indic conjuncts across a virama, prepended marks and Hangul jamo that NFC left
    uncomposed: there a cut can still fall inside what reads as one character.
    """
    end = len(text.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore"))
    while 0 < end < len(text) and _joined(text, end):
        end -= 1
    return text[:end]


_ZWJ = "\u200d"
_JOINERS = "\u200c\u200d"  # zero-width non-joiner and joiner: part of the character before
_MARKS = frozenset({"Mn", "Mc", "Me"})  # also variation selectors and the keycap mark
_REGIONAL_INDICATORS = "".join(map(chr, range(0x1F1E6, 0x1F200)))  # a flag is a pair


def _joined(text: str, i: int) -> bool:
    """True if text[i] belongs to the same character as text[i - 1]."""
    before, after = text[i - 1], text[i]
    if after in _JOINERS or before == _ZWJ:  # a zero-width joiner also binds the next emoji
        return True
    if unicodedata.category(after) in _MARKS:
        return True
    if "\U0001f3fb" <= after <= "\U0001f3ff" or "\U000e0020" <= after <= "\U000e007f":
        return True  # emoji modifiers (skin tones), tags (flags of regions like Scotland)
    if before in _REGIONAL_INDICATORS and after in _REGIONAL_INDICATORS:
        run = i - len(text[:i].rstrip(_REGIONAL_INDICATORS))
        return run % 2 == 1  # an odd run ends in the first half of a flag
    return False


_SPACES = re.compile(r" {2,}")
_SPACES_AROUND_NEWLINE = re.compile(r" *\n *")
# vertical tab, form feed, next line, line and paragraph separator: breaks between words
_BREAKS = str.maketrans(dict.fromkeys("\x0b\x0c\x85\u2028\u2029", "\n"))
# direction embeddings, overrides and isolates make a text read differently than it is stored;
# the byte order mark is noise. Joiners and variation selectors stay: they belong to the text.
_DIRECTION = frozenset("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\ufeff")


def _clean(text: str) -> str:
    """Normalise line endings, drop control and direction characters, collapse spaces (bytes are
    scarce)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ").translate(_BREAKS)
    kept = "".join(
        ch for ch in text
        if ch == "\n" or (unicodedata.category(ch) != "Cc" and ch not in _DIRECTION)
    )  # fmt: skip
    return _SPACES_AROUND_NEWLINE.sub("\n", _SPACES.sub(" ", kept)).strip()


def _to_ascii(text: str) -> str:
    """Transliterate; invisible format characters (soft hyphen, zero-width space, joiners) are
    dropped instead of becoming a visible '?'."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATION))
    base = "".join(
        ch for ch in decomposed
        if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf"
    )  # fmt: skip
    return "".join(ch if ch.isascii() else "?" for ch in base)
