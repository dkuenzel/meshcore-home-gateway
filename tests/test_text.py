"""Rules for outgoing text: cleaning, byte limit, optional ASCII transliteration."""

import pytest

from meshcore_bus.core.text import TextPolicy, TextRejectedError, prepare_text

UTF8 = TextPolicy(max_bytes=20)
ASCII = TextPolicy(max_bytes=40, ascii_only=True)


def test_plain_ascii_passes_unchanged() -> None:
    assert prepare_text("Gate open", UTF8) == "Gate open"


def test_whitespace_is_trimmed_and_line_endings_normalised() -> None:
    assert prepare_text("  a\r\nb\rc\t d  ", UTF8) == "a\nb\nc d"


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


def test_too_long_text_is_truncated_on_a_character_boundary() -> None:
    policy = TextPolicy(max_bytes=10)
    # nine two-byte characters: 7 bytes remain for content, a fourth "ä" would be split
    assert prepare_text("äääääääää", policy) == "äää..."


def test_truncation_keeps_as_much_as_fits() -> None:
    assert prepare_text("0123456789ABCDEF", TextPolicy(max_bytes=10)) == "0123456..."


def test_too_long_text_is_rejected_when_truncation_is_off() -> None:
    with pytest.raises(TextRejectedError, match="too long"):
        prepare_text("0123456789ABCDEF", TextPolicy(max_bytes=10, truncate=False))


def test_exact_limit_is_not_truncated() -> None:
    assert prepare_text("0123456789", TextPolicy(max_bytes=10)) == "0123456789"


def test_policy_rejects_limits_too_small_for_the_ellipsis() -> None:
    with pytest.raises(ValueError, match="max_bytes"):
        TextPolicy(max_bytes=3)
