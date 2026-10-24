"""Contact book: resolution of 6-byte key prefixes to full public keys."""

import pytest

from meshcore_home_gateway.core.contacts import (
    Contact,
    ContactBook,
    add_refusal,
    look_alike,
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
