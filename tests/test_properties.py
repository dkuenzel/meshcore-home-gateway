"""Property tests of the parsers that face the air, the radio and the bus (CODE-009): whatever
bytes or text come in, each returns, or refuses with its own error, never with any other, and
within the deadline of an example. Hypothesis makes up the inputs; a failure prints the smallest
it found and a blob that reproduces it (`@reproduce_failure`)."""

from __future__ import annotations

from contextlib import suppress

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meshcore_home_gateway import catalog, config
from meshcore_home_gateway.core import lpp
from meshcore_home_gateway.core.text import cut_utf8
from meshcore_home_gateway.protocol import codec, links, packet
from meshcore_home_gateway.protocol.codes import Response

# No example database in the checkout. The autouse fixtures (tests/conftest.py) keep the
# catalogs and the clock of the service, which these pure functions do not touch.
PARSER = settings(
    database=None,
    deadline=1000,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
FRAME = st.binary(max_size=300)  # a frame of the companion protocol has at most 172 bytes
MESSAGE_CODES = (
    Response.CONTACT_MSG_RECV,
    Response.CHANNEL_MSG_RECV,
    Response.CONTACT_MSG_RECV_V3,
    Response.CHANNEL_MSG_RECV_V3,
)


@PARSER
@given(FRAME)
def test_a_frame_of_the_radio_is_decoded_or_refused(payload: bytes) -> None:
    with suppress(codec.ProtocolError):
        codec.decode(payload)


@PARSER
@given(st.sampled_from(MESSAGE_CODES), FRAME)
def test_a_message_of_the_radio_is_decoded_or_refused(code: int, rest: bytes) -> None:
    with suppress(codec.ProtocolError):
        codec.decode_message(bytes([code]) + rest, 0.0)


@PARSER
@given(FRAME)
def test_a_logged_packet_is_parsed_or_none(raw: bytes) -> None:
    parsed = packet.parse_packet(raw)
    if parsed is not None:
        packet.parse_advert(parsed)


@PARSER
@given(st.integers(0, 3), st.integers(0, 3), st.lists(st.binary(min_size=1, max_size=3)), FRAME)
def test_an_advert_is_parsed_or_none(
    route: int, version: int, hashes: list[bytes], payload: bytes
) -> None:
    advert = packet.Packet(route, packet.PAYLOAD_TYPE_ADVERT, version, tuple(hashes), payload)
    packet.parse_advert(advert)


@PARSER
@given(FRAME)
def test_an_answer_of_a_sensor_is_read_without_an_error(data: bytes) -> None:
    payload = lpp.decode(data)
    lpp.position(payload)
    lpp.named(payload)


# a JSON escape or a command line argument can hold a lone surrogate, which st.text() leaves out
ANY_TEXT = st.text(st.one_of(st.characters(), st.integers(0xD800, 0xDFFF).map(chr)))
WELL_FORMED_LINK = st.builds(
    "{}{}?name={}&public_key={}&type={}".format,
    st.just(links.SCHEME),
    st.just(links.ADD_PATH),
    ANY_TEXT.filter(lambda name: not set(name) & set("&=;#%+")),
    st.binary(min_size=32, max_size=32).map(bytes.hex),
    st.integers(0, 5),
)
LINK_TEXT = st.one_of(
    ANY_TEXT,
    WELL_FORMED_LINK,
    st.builds(
        "{}{}".format, st.sampled_from([links.SCHEME, links.SCHEME + "contact/add?"]), ANY_TEXT
    ),
    st.builds("{}{}".format, st.just(links.SCHEME), FRAME.map(bytes.hex)),
    FRAME.map(bytes.hex),
)


@PARSER
@given(LINK_TEXT)
def test_a_key_link_or_card_is_read_or_refused(text: str) -> None:
    """What it reads goes to the radio, the bus and the history: a key of 32 bytes and text that
    UTF-8 can carry."""
    try:
        contact = links.parse_contact_text(text)
    except links.LinkError:
        return
    assert len(bytes.fromhex(contact.public_key)) == 32
    (contact.name or "").encode("utf-8")


@PARSER
@given(st.text(), st.integers(0, 200))
def test_a_cut_text_is_a_start_of_it_within_the_bytes(text: str, max_bytes: int) -> None:
    cut = cut_utf8(text, max_bytes)
    assert text.startswith(cut)
    assert len(cut.encode("utf-8")) <= max_bytes
    assert cut_utf8(cut, max_bytes) == cut
    if len(text.encode("utf-8")) <= max_bytes:
        assert cut == text


@PARSER
@given(st.text())
def test_a_message_catalog_is_read_or_refused(text: str) -> None:
    with suppress(catalog.PoError):
        catalog.read_po(text)


ANSWER = st.builds(
    "{}={}".format,
    st.one_of(st.sampled_from(sorted(config._ANSWER_TYPES)), st.text(max_size=20)),
    st.text(max_size=40).filter(lambda value: "\n" not in value),
)


@PARSER
@given(st.lists(ANSWER, max_size=12))
def test_installer_answers_give_a_valid_configuration_or_are_refused(answers: list[str]) -> None:
    try:
        text = config.render_config("\n".join(answers))
    except config.ConfigError:
        return
    config.parse_config(text)


@PARSER
@given(st.text())
def test_a_configuration_file_is_read_or_refused(text: str) -> None:
    with suppress(config.ConfigError):
        config.parse_config(text)
