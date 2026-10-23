"""Rules for outgoing text: cleaning, byte limit, optional ASCII transliteration."""

import pytest

from meshcore_home_gateway.core.text import TextPolicy, TextRejectedError, prepare_text

UTF8 = TextPolicy(max_bytes=20)
ASCII = TextPolicy(max_bytes=40, ascii_only=True)


def test_plain_ascii_passes_unchanged() -> None:
    assert prepare_text("Gate open", UTF8) == "Gate open"


def test_whitespace_is_trimmed_and_line_endings_normalised() -> None:
    assert prepare_text("  a\r\nb\rc\t d  ", UTF8) == "a\nb\nc d"


def test_spaces_around_line_breaks_are_removed() -> None:
    assert prepare_text("a  \n  b", UTF8) == "a\nb"


def test_control_characters_are_removed() -> None:
    assert prepare_text("a\x00b\x07c\x1bd", UTF8) == "abcd"


def test_empty_after_cleaning_is_rejected() -> None:
    with pytest.raises(TextRejectedError, match="empty"):
        prepare_text(" \x00\t ", UTF8)


def test_umlauts_are_transliterated_in_ascii_mode() -> None:
    assert prepare_text("Tür offen, Größe Übermaß", ASCII) == "Tuer offen, Groesse Uebermass"


def test_typography_and_accents_become_ascii() -> None:
    # typographic quotes, en dash, degree sign, ellipsis
    text = "\u201eCaf\u00e9\u201c \u2013 5 \u00b0C \u2026 \u2019ok\u2019"
    assert prepare_text(text, ASCII) == "\"Cafe\" - 5 degC ... 'ok'"


def test_unknown_characters_become_question_marks_in_ascii_mode() -> None:
    assert prepare_text("Alarm 🚨 ✓", ASCII) == "Alarm ? ?"


def test_combining_characters_are_normalised_before_transliteration() -> None:
    decomposed = "Tür"  # u + combining diaeresis
    assert prepare_text(decomposed, ASCII) == "Tuer"


def test_utf8_mode_keeps_unicode() -> None:
    assert prepare_text("Tür 🚪", UTF8) == "Tür 🚪"


def test_truncation_does_not_split_a_utf8_sequence() -> None:
    policy = TextPolicy(max_bytes=10)
    # nine two-byte characters: 7 bytes remain for content, a fourth "ä" would be split
    assert prepare_text("äääääääää", policy) == "äää..."


@pytest.mark.parametrize(
    ("text", "max_bytes", "expected"),
    [
        # a flag is two regional indicators
        ("Status 12345 \U0001f1e9\U0001f1ea more", 20, "Status 12345..."),
        ("\U0001f1e9\U0001f1ea\U0001f1eb\U0001f1f7 ok", 15, "\U0001f1e9\U0001f1ea..."),
        # emoji joined by U+200D, a skin tone modifier, a keycap (variation selector and mark)
        ("Home \U0001f468\u200d\U0001f469\u200d\U0001f467 is back", 16, "Home..."),
        ("OK \U0001f44d\U0001f3fd thanks", 10, "OK..."),
        ("OK \U0001f44d\U0001f3fd thanks", 14, "OK \U0001f44d\U0001f3fd..."),
        ("Code 1\ufe0f\u20e3 now", 9, "Code..."),
        ("Hi \u0915\u093e\u0915\u093e", 9, "Hi..."),  # Devanagari vowel sign
    ],
)
def test_truncation_keeps_characters_with_their_marks_and_joiners(
    text: str, max_bytes: int, expected: str
) -> None:
    """A cut must not leave half a flag, an emoji without its modifier or a dangling joiner."""
    assert prepare_text(text, TextPolicy(max_bytes)) == expected


def test_truncation_keeps_as_much_as_fits() -> None:
    assert prepare_text("0123456789ABCDEF", TextPolicy(max_bytes=10)) == "0123456..."


def test_truncation_drops_the_space_before_the_ellipsis() -> None:
    assert prepare_text("0123 56789ABC", TextPolicy(max_bytes=8)) == "0123..."


def test_too_long_text_is_rejected_when_truncation_is_off() -> None:
    with pytest.raises(TextRejectedError, match="too long"):
        prepare_text("0123456789ABCDEF", TextPolicy(max_bytes=10, truncate=False))


def test_exact_limit_is_not_truncated() -> None:
    assert prepare_text("0123456789", TextPolicy(max_bytes=10)) == "0123456789"


def test_policy_rejects_limits_too_small_for_the_ellipsis() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        TextPolicy(max_bytes=3)


def test_only_control_and_direction_characters_are_removed() -> None:
    """Joiners and variation selectors are part of the text (emoji sequences, scripts); direction
    overrides could make a text read differently than it is; line and page breaks separate."""
    roomy = TextPolicy(max_bytes=160)
    family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"  # one emoji: joined by U+200D
    assert prepare_text(f"Home {family} \u2764\ufe0f", roomy) == f"Home {family} \u2764\ufe0f"
    assert prepare_text("abc\u202edef\u2066x\u2069\ufeff", roomy) == "abcdefx"
    assert prepare_text("one\x0btwo\x0cthree\x85four\u2028five", roomy) == (
        "one\ntwo\nthree\nfour\nfive"
    )


def test_spaces_from_transliteration_are_collapsed() -> None:
    policy = TextPolicy(max_bytes=160, ascii_only=True)
    assert prepare_text("a \u00a0 b\u2003c", policy) == "a b c"


def test_ascii_mode_drops_invisible_format_characters() -> None:
    """Soft hyphens and zero-width spaces from copied text would become visible '?'."""
    policy = TextPolicy(160, ascii_only=True)
    assert prepare_text("Temperatur\u00adf\u00fchler", policy) == "Temperaturfuehler"
    assert prepare_text("a\u200bb\u200ec\u2060d", policy) == "abcd"


@pytest.mark.parametrize("text", ["\u200b", "\u00ad \u2060", "\u200d"])
def test_a_text_without_visible_characters_is_rejected(text: str) -> None:
    with pytest.raises(TextRejectedError) as rejected:
        prepare_text(text, TextPolicy(160))
    assert rejected.value.reason == "empty"
