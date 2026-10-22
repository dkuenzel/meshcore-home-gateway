"""Contact book: resolution of 6-byte key prefixes to full public keys."""

import pytest

from meshcore_home_gateway.core.contacts import Contact, ContactBook, normalise_key

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
