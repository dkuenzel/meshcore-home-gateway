"""Contact book: resolution of 6-byte key prefixes to full public keys."""

import pytest

from meshcore_home_gateway.core.contacts import (
    COPY_INVISIBLE,
    INVISIBLE,
    Contact,
    ContactBook,
    add_refusal,
    key_from_label,
    look_alike,
    name_key,
    node_label,
    normalise_key,
    remove_refusal,
)

A = "a1" * 32
A_TWIN = "a1" * 6 + "00" * 26
B = "b2" * 32


def test_unique_prefix_resolves_to_the_full_key() -> None:
    book = ContactBook([Contact(A, "Alice"), Contact(B, "Bob")])
    assert book.resolve(A[:12]) == [Contact(A, "Alice")]
    assert book.by_key(B) == Contact(B, "Bob")


def test_shared_prefix_returns_all_candidates() -> None:
    book = ContactBook([Contact(A, "Alice"), Contact(A_TWIN, "Twin")])
    assert {c.public_key for c in book.resolve(A[:12])} == {A, A_TWIN}


def test_unknown_prefix_resolves_to_nothing() -> None:
    assert ContactBook([]).resolve("00" * 6) == []
    assert ContactBook([]).by_key(A) is None


def test_keys_are_normalised_and_validated() -> None:
    assert normalise_key(A.upper()) == A
    with pytest.raises(ValueError, match="public key"):
        normalise_key("a1b2")
    with pytest.raises(ValueError, match="public key"):
        normalise_key("zz" * 32)


def test_prefix_of_a_contact() -> None:
    assert Contact(A, "Alice").prefix == A[:12]


A, B, C = "a1" * 32, "b2" * 32, "c3" * 32
A_TWIN = "a1" * 6 + "ee" * 26  # the same 6 bytes as A


def test_look_alikes_share_the_first_six_bytes() -> None:
    assert look_alike(A, [A]) == ()
    assert look_alike(A_TWIN, [A, B]) == (A,)
    assert look_alike("a1" * 6 + "ef" + "00" * 25, ["a1" * 6 + "ee" + "00" * 25]) == (
        "a1" * 6 + "ee" + "00" * 25,
    )
    assert look_alike("a1" * 5 + "a2" + "ee" * 26, [A]) == ()  # the 6th byte differs


def test_the_rules_of_an_add_in_their_order() -> None:
    def refusal(
        key: str, node_type: int = 1, table: tuple[str, ...] = (B,), **kw: object
    ) -> object:
        options: dict[str, object] = {"protected": (C,), "own_key": "ff" * 32, "max_contacts": 3}
        options.update(kw)
        return add_refusal(key, node_type, table=table, **options)  # type: ignore[arg-type]

    assert refusal("ff" * 32, node_type=0) == "own_key"
    assert refusal(A, node_type=0) == "unsupported_type"
    assert refusal(B, node_type=5) == "unsupported_type"
    assert refusal(B) == "already_contact"
    assert refusal("b2" * 6 + "00" * 26) == "look_alike"  # of a contact
    assert refusal("c3" * 6 + "00" * 26) == "look_alike"  # of a protected key not on the radio
    assert refusal(C) is None  # a protected contact put back by hand
    assert refusal(A, table=(B, C, "dd" * 32)) == "table_full"
    assert refusal(A) is None


def test_only_a_contact_on_the_radio_can_be_removed() -> None:
    assert remove_refusal(A, table=[A, B]) is None
    assert remove_refusal(C, table=[A, B]) == "unknown_contact"


# --- labels ------------------------------------------------------------------------------------


def test_a_label_starts_with_the_key_and_ends_with_the_name() -> None:
    assert node_label(A, "Alice", 1) == f"{A[:12]}…{A[-8:]} Alice"
    assert node_label(B, "Hilltop", 3) == f"{B[:12]}…{B[-8:]} Hilltop (room)"
    assert node_label(B, "Weather", 4).endswith(" Weather (sensor)")
    assert node_label(A, "", 1) == f"{A[:12]}…{A[-8:]}"


@pytest.mark.parametrize(
    "imitation",
    [
        "Alice",
        "ALICE",
        " alice ",
        "\uff21\uff4c\uff49\uff43\uff45",
        "\u0410lice",
        "Al\u0456c\u0435",
        "\u0391lice",
        "Ali\u200bce",
        "Alice\u202e",
    ],
    ids=[
        "same",
        "upper",
        "spaces",
        "fullwidth",
        "cyrillic-a",
        "cyrillic-i-e",
        "greek-alpha",
        "zero-width-space",
        "direction",
    ],
)
def test_names_are_compared_as_a_person_reads_them(imitation: str) -> None:
    """SEC-007: NFKC, case folding, Cyrillic and Greek look-alikes, without the invisible
    format characters."""
    assert name_key(imitation) == name_key("Alice") == "alice"


def test_other_names_stay_apart() -> None:
    assert name_key("Alice 2") != name_key("Alice")
    assert name_key("Bob") == "bob"
    assert name_key("") == name_key("\u200b") == ""  # no name
    assert name_key("\u039d\u0399\u039a\u039f") == name_key("NIKO")  # Greek capitals


@pytest.mark.parametrize(
    "imitation",
    [
        "Bob" + chr(0xFE0F),  # variation selector (Mn)
        "Bo" + chr(0x034F) + "b",  # combining grapheme joiner (Mn)
        "B" + chr(0x3164) + "ob",  # Hangul filler (Lo)
        "Bob" + chr(0x115F),  # Hangul choseong filler (Lo)
        "Bob" + chr(0xFFA0),  # halfwidth Hangul filler (Lo)
        "Bob" + chr(0x17B4),  # Khmer vowel inherent (Mn)
        "Bob" + chr(0x180B),  # Mongolian free variation selector (Mn)
        "Bob" + chr(0xE0100),  # variation selector 17 (Mn)
        "Bob" + chr(0x2800),  # braille pattern blank (So): a space at the end
        "Bob" + chr(0x200B),  # zero width space (Cf), dropped before too
    ],
    ids=lambda name: f"U+{ord(name.strip('Bob')[0]):04X}",
)
def test_names_compare_without_the_invisible_characters_of_any_category(imitation: str) -> None:
    """SEC-007: Unicode's default ignorable characters are drawn as nothing, not only the format
    characters (Cf): variation selectors, the combining grapheme joiner, the Hangul fillers. The
    braille blank looks like a space (#89)."""
    assert name_key(imitation) == name_key("Bob") == "bob"


def test_a_braille_blank_inside_a_name_is_a_space_and_visible_marks_stay() -> None:
    """The blank separates words as a space does; a combining mark that shows (U+0301, an
    acute accent) makes another name, and the grapheme joiner before one changes nothing."""
    assert name_key("B" + chr(0x2800) + "ob") == name_key("B ob") == "b ob"
    assert name_key("Bob" + chr(0x0301)) != name_key("Bob")
    assert name_key("Jos" + "e" + chr(0x034F) + chr(0x0301)) == name_key("Jos\u00e9")


def test_the_invisible_characters_of_a_copy_are_part_of_the_one_table() -> None:
    """protocol.links drops from a copied key, link or card the invisible characters a copy may
    carry, which are among those names compare without: one table for both."""
    assert set(COPY_INVISIBLE) < INVISIBLE
    assert "\u200c" not in COPY_INVISIBLE  # the joiners belong to a name
    assert "\ufe0f" not in COPY_INVISIBLE  # as do the variation selectors of an emoji


def test_a_label_marks_a_name_that_is_taken() -> None:
    label = node_label(A, "Alice", 1, name_taken=True)
    assert label == f"{A[:12]}…{A[-8:]} Alice (name taken)"
    assert node_label(B, "", 3, name_taken=True).endswith(" (room) (name taken)")
    assert key_from_label(label, [A, B]) == A  # the digits still name it


def test_a_label_keeps_a_hostile_name_on_one_plain_line() -> None:
    """Line breaks, direction overrides and other invisible characters cannot move or imitate
    the key digits a person compares."""
    hostile = "\u202e" + "Bob\nb2b2b2b2b2b2…b2b2b2b2\u200b  x"
    label = node_label(A, hostile, 1)
    assert label == f"{A[:12]}…{A[-8:]} Bob b2b2b2b2b2b2…b2b2b2b2 x"
    assert key_from_label(label, [A, B]) == A  # the leading digits name the node, not the name


@pytest.mark.parametrize(
    "label",
    [
        f"{A[:12]}…{A[-8:]} Alice",
        f"{A[:12]}…{A[-8:]}",
        f"{A[:12]}...{A[-8:]} Alice",
        f"  {A[:12].upper()}…{A[-8:].upper()} renamed since  ",
    ],
)
def test_a_label_names_its_key(label: str) -> None:
    assert key_from_label(label, [A, A_TWIN, B]) == A


@pytest.mark.parametrize(
    "label",
    [
        "",
        "Alice",
        f"{A[:12]}…{A[-8:]}x",
        f"{A[:12]}…{A[-7:]} Alice",
        f"x{A[:12]}…{A[-8:]}",
        f"{A[:12]}…{A[-8:]}\nAlice",
        f"{B[:12]}…{A[-8:]}",
    ],
)
def test_a_label_without_its_key_names_nothing(label: str) -> None:
    assert key_from_label(label, [A, B]) is None


def test_a_label_that_two_keys_share_names_neither() -> None:
    twin = A[:12] + "00" * 22 + A[-8:]
    assert key_from_label(node_label(A, "Alice", 1), [A, twin]) is None
