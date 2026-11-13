"""Rules for outgoing message text.

The radio limits a message by bytes, not characters (see docs/protocol.md), and some handheld
devices can only display ASCII. `prepare_text` cleans a text, optionally transliterates it to
ASCII and enforces the byte limit by truncation or rejection.
"""

from __future__ import annotations

import re
import secrets
import unicodedata
from dataclasses import dataclass

ELLIPSIS = "..."
CONTACT_NAME_BYTES = 31  # a name in the radio's contact table, without its NUL
# hex digits of the random suffix of a direct message: 32 bits, as many as the code (SEC-001)
CONFIRM_NONCE_DIGITS = 8
CONFIRM_NONCE_BYTES = len(" #") + CONFIRM_NONCE_DIGITS
CHANNEL_NAME_SEPARATOR_BYTES = len(": ")  # after the radio's name, before a channel text
_NONCE = re.compile(rf" #[0-9a-f]{{{CONFIRM_NONCE_DIGITS}}}\Z")

# Characters with a conventional ASCII spelling. Everything else goes through Unicode
# decomposition (é -> e); what is still not ASCII becomes "?".
_TRANSLITERATION = str.maketrans(
    {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "Ä": "Ae",
        "Ö": "Oe",
        "Ü": "Ue",
        "ß": "ss",
        # double quotes and guillemets
        "\u201e": '"',
        "\u201c": '"',
        "\u201d": '"',
        "\u00ab": '"',
        "\u00bb": '"',
        # single quotes
        "\u201a": "'",
        "\u2018": "'",
        "\u2019": "'",
        "\u2039": "'",
        "\u203a": "'",
        # en dash, em dash, minus sign
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        # ellipsis, degree sign, euro sign, no-break space
        "\u2026": "...",
        "\u00b0": "deg",
        "\u20ac": "EUR",
        "\u00a0": " ",
    }
)


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


def prepare_text(text: str, policy: TextPolicy, *, prefix: str = "", suffix: str = "") -> str:
    """Return the text as it will be sent, or raise TextRejectedError. A target's `prefix` and
    `suffix` (EXT-017) go around a text that is not empty, count toward the limit and stay whole
    when the text is cut."""
    cleaned = _clean(unicodedata.normalize("NFC", text))
    if policy.ascii_only:
        cleaned = _clean(_to_ascii(cleaned))  # transliteration turns wide spaces into " "
        prefix, suffix = _to_ascii(prefix), _to_ascii(suffix)
    if not any(unicodedata.category(ch) != "Cf" and not ch.isspace() for ch in cleaned):
        raise TextRejectedError("empty", "text is empty after cleaning")
    room = _room(policy.max_bytes, prefix, suffix)
    encoded = cleaned.encode("utf-8")
    if len(encoded) <= room:
        return f"{prefix}{cleaned}{suffix}"
    if not policy.truncate:
        raise TextRejectedError(
            "too_long",
            f"text too long: {len(encoded) + policy.max_bytes - room} bytes, "
            f"limit {policy.max_bytes}",
        )
    return f"{prefix}{cut_utf8(cleaned, room - len(ELLIPSIS)).rstrip()}{ELLIPSIS}{suffix}"


def text_limit(max_bytes: int, *, channel: bool, radio_name: str | None, nonce: bool) -> int:
    """The bytes a text may take of the radio's `max_bytes`: a channel text leaves room for the
    radio's name and ": ", which the firmware puts before it (without the name, the room of the
    shortest one: such a text is limited again when it is sent); a direct text, with `nonce`, for
    the random suffix of `confirm_nonce` (SEC-001)."""
    if channel:
        return max_bytes - len((radio_name or "").encode("utf-8")) - CHANNEL_NAME_SEPARATOR_BYTES
    return max_bytes - CONFIRM_NONCE_BYTES if nonce else max_bytes


def room_bytes(policy: TextPolicy, *, prefix: str = "", suffix: str = "") -> int:
    """The bytes left to a text beside a target's prefix and suffix, as `prepare_text` counts
    them: transliterated where the policy asks for ASCII."""
    if policy.ascii_only:
        prefix, suffix = _to_ascii(prefix), _to_ascii(suffix)
    return _room(policy.max_bytes, prefix, suffix)


def _room(max_bytes: int, prefix: str, suffix: str) -> int:
    return max_bytes - len(prefix.encode("utf-8")) - len(suffix.encode("utf-8"))


def cut_chars(text: str, max_chars: int) -> str:
    """The longest start of `text` of at most `max_chars` characters (code points, as Home
    Assistant counts a text's length) that ends between two whole characters, as `cut_utf8`."""
    end = min(len(text), max(0, max_chars))
    while 0 < end < len(text) and _joined(text, end):
        end -= 1
    return text[:end]


def with_nonce(text: str) -> str:
    """The text with a random suffix, " #" and 8 hex digits. The acknowledgement of a direct
    message is a hash of its text that anyone who knows the text can compute: one who cannot
    guess the suffix cannot forge it (SEC-001). Its policy leaves CONFIRM_NONCE_BYTES free."""
    return f"{text} #{secrets.token_hex(CONFIRM_NONCE_DIGITS // 2)}"


def without_nonce(text: str) -> str:
    """The text without the suffix of `with_nonce`, if it ends in one."""
    return _NONCE.sub("", text, count=1)


def contact_name(text: str) -> str:
    """A name for the radio's contact table: cleaned like a text, on one line, at most the 31
    bytes the radio keeps (MAX_NAME_BYTES), cut between whole characters. Possibly empty."""
    one_line = _clean(unicodedata.normalize("NFC", text)).replace("\n", " ")
    return cut_utf8(one_line, CONTACT_NAME_BYTES).rstrip()


def cut_utf8(text: str, max_bytes: int) -> str:
    """The longest start of `text` within `max_bytes` UTF-8 bytes that ends between two whole
    characters as a reader sees them: a flag, joined emoji, an emoji with its skin tone, a
    letter with its marks.

    The standard library has no grapheme segmentation (Unicode UAX #29), so `_joined`
    approximates it for marks, joiners, variation selectors, emoji modifiers, tags, flags and the
    few signs of _SIGNS_AFTER. Not
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
# signs that extend the letter before them although they are no marks (UAX #29 Extend and
# SpacingMark): Thai and Lao sara am, the halfwidth katakana (semi-)voiced sound marks
_SIGNS_AFTER = frozenset("\u0e33\u0eb3\uff9e\uff9f")
_REGIONAL_INDICATORS = "".join(map(chr, range(0x1F1E6, 0x1F200)))  # a flag is a pair


def _joined(text: str, i: int) -> bool:
    """True if text[i] belongs to the same character as text[i - 1]."""
    before, after = text[i - 1], text[i]
    if after in _JOINERS or before == _ZWJ:  # a zero-width joiner also binds the next emoji
        return True
    if unicodedata.category(after) in _MARKS or after in _SIGNS_AFTER:
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
        ch
        for ch in text
        if ch == "\n" or (unicodedata.category(ch) != "Cc" and ch not in _DIRECTION)
    )
    return _SPACES_AROUND_NEWLINE.sub("\n", _SPACES.sub(" ", kept)).strip()


def clean_received(text: str) -> str:
    """A received text cleaned as an outgoing one is, for `[messages] clean_received`: without
    control and direction characters, which a terminal or a file could take for instructions
    (SEC-017). Received texts are data and stay as they came by default."""
    return _clean(text)


def clean_name(name: str) -> str:
    """A name from the mesh cleaned as a received text, for `[messages] clean_received` (#137):
    on one line, its line breaks single spaces. Neither normalised nor cut: the name as it came,
    without what a terminal or a file could take for instructions. Possibly empty."""
    return _SPACES.sub(" ", _clean(name).replace("\n", " "))


def _to_ascii(text: str) -> str:
    """Transliterate; invisible format characters (soft hyphen, zero-width space, joiners) are
    dropped instead of becoming a visible '?'."""
    decomposed = unicodedata.normalize("NFKD", text.translate(_TRANSLITERATION))
    base = "".join(
        ch
        for ch in decomposed
        if not unicodedata.combining(ch) and unicodedata.category(ch) != "Cf"
    )
    return "".join(ch if ch.isascii() else "?" for ch in base)
