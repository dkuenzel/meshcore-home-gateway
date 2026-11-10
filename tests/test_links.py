"""What a person hands over to add a contact: keys, contact links and contact cards
(protocol/links.py; the firmware's docs/qr_codes.md)."""

import struct

import pytest

from meshcore_home_gateway.protocol.links import (
    LinkContact,
    LinkError,
    contact_link,
    parse_contact_text,
)

KEY = "9cd8fcf22a47333b591d96a2b848b73f457b1bb1a3ea2453a885f9e5787765b1"


def card(flags: int = 0x81, name: bytes = b"Bob", header: int = 0x11, path: bytes = b"") -> str:
    """`meshcore://` and the hex of an advert packet, as the app exports a contact."""
    app = bytes([flags]) + (struct.pack("<ii", 1, 2) if flags & 0x10 else b"") + name
    advert = bytes.fromhex(KEY) + struct.pack("<I", 1_760_000_000) + bytes(64) + app
    route = b"\x01\x02\x03\x04" if header & 0x03 in (0, 3) else b""
    return "meshcore://" + (bytes([header]) + route + bytes([len(path)]) + path + advert).hex()


@pytest.mark.parametrize("text", [KEY, KEY.upper(), f"  {KEY}\n"])
def test_a_key_in_any_case_with_spaces(text: str) -> None:
    assert parse_contact_text(text) == LinkContact(KEY, None, 1, "key")


# what the dashboard drops from the texts it shows, and so what a copy from it may carry
INVISIBLE = (
    "\u00ad\u061c\u200b\u200e\u200f\u202a\u202b\u202c\u202d\u202e"
    "\u2060\u2061\u2062\u2063\u2064\u2066\u2067\u2068\u2069\ufeff"
)


def broken(text: str, mark: str, every: int = 20) -> str:
    """The text with `mark` after every `every` characters, as the dashboard breaks long words."""
    return mark.join(text[i : i + every] for i in range(0, len(text), every))


@pytest.mark.parametrize("mark", list(INVISIBLE), ids=lambda m: f"U+{ord(m):04X}")
def test_invisible_characters_of_a_copy_are_dropped(mark: str) -> None:
    """A key copied from a chat on the dashboard carries its word breaks (U+200B every 20
    characters), a copy from elsewhere direction marks or a byte order mark: none of them is
    part of a key, link or card."""
    assert parse_contact_text(broken(KEY, mark)) == LinkContact(KEY, None, 1, "key")
    link = broken(f"meshcore://contact/add?name=Bob&public_key={KEY}&type=1", mark)
    assert parse_contact_text(mark + link + "\ufeff") == LinkContact(KEY, "Bob", 1, "link")
    assert parse_contact_text(broken(card(), mark)).public_key == KEY


@pytest.mark.parametrize("joiner", ["\u200c", "\u200d"])
def test_the_joiners_of_a_name_stay(joiner: str) -> None:
    """Zero-width non-joiner and joiner belong to the text they are in."""
    claim = parse_contact_text(f"meshcore://contact/add?name=Ali{joiner}ce&public_key={KEY}")
    assert claim.name == f"Ali{joiner}ce"
    with pytest.raises(LinkError):
        parse_contact_text(broken(KEY, joiner))


@pytest.mark.parametrize(
    ("query", "name", "node_type"),
    [
        (f"name=Example+Contact&public_key={KEY}&type=1", "Example Contact", 1),
        (f"name=J%C3%BCrgen&public_key={KEY.upper()}&type=3", "Jürgen", 3),
        (f"public_key={KEY}", None, 1),  # no type: a companion
        (f"public_key={KEY}&name=&type=4&region=x", None, 4),  # an empty name is none
    ],
)
def test_a_contact_link(query: str, name: str | None, node_type: int) -> None:
    claim = parse_contact_text("meshcore://contact/add?" + query)
    assert claim == LinkContact(KEY, name, node_type, "link")


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "invalid_link"),
        ("hello", "invalid_link"),
        (KEY[:-1], "invalid_link"),
        ("meshcore://contact/add?name=Bob", "invalid_link"),
        (f"meshcore://contact/add?public_key={KEY[:-2]}", "invalid_link"),
        (f"meshcore://contact/add?public_key={KEY}&public_key={KEY}", "invalid_link"),
        (f"meshcore://contact/add?public_key={KEY}&name=a&name=b", "invalid_link"),
        (f"meshcore://contact/add?public_key={KEY}&", "invalid_link"),  # strict parsing
        (f"meshcore://contact/add?public_key={KEY}&type=0", "unsupported_type"),
        (f"meshcore://contact/add?public_key={KEY}&type=5", "unsupported_type"),
        (f"meshcore://contact/add?public_key={KEY}&type=x", "unsupported_type"),
        (f"meshcore://contact/add?public_key={KEY}&type=%C2%B2", "unsupported_type"),  # "²"
        ("meshcore://channel/add?name=Public&secret=" + "00" * 16, "unsupported_link"),
        ("meshcore://abc", "invalid_link"),  # odd hex
        ("meshcore://00", "invalid_link"),  # no packet
    ],
)
def test_what_is_refused(text: str, reason: str) -> None:
    with pytest.raises(LinkError) as caught:
        parse_contact_text(text)
    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("flags", "header", "path"),
    [(0x81, 0x11, b""), (0x91, 0x11, b"\xaa\xbb"), (0x83, 0x10, b"")],  # position; transport
)
def test_a_contact_card(flags: int, header: int, path: bytes) -> None:
    claim = parse_contact_text(card(flags, header=header, path=path))
    assert claim == LinkContact(KEY, "Bob", flags & 0x0F, "card")


def test_a_card_is_refused_when_it_holds_no_advert() -> None:
    text = card()
    for broken in (text[:40], "meshcore://" + "11" + "00" + "02" * 8):  # cut; another payload
        with pytest.raises(LinkError) as caught:
            parse_contact_text(broken)
        assert caught.value.reason == "invalid_link"


def test_the_gateways_link_round_trips() -> None:
    link = contact_link("My gateway", KEY)
    assert link == f"meshcore://contact/add?name=My+gateway&public_key={KEY}&type=1"
    assert parse_contact_text(link) == LinkContact(KEY, "My gateway", 1, "link")
