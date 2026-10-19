"""Frame layer of the companion protocol: '<' + length (uint16 LE) + payload, '>' from the radio."""

import pytest

from meshcore_bus.protocol.framing import MAX_FRAME_SIZE, FrameDecoder, encode_frame


def test_frames_to_the_radio_start_with_less_than_and_little_endian_length() -> None:
    assert encode_frame(b"\x01\x02\x03") == b"<\x03\x00\x01\x02\x03"
    assert encode_frame(bytes(MAX_FRAME_SIZE))[:3] == b"<\xb0\x00"


@pytest.mark.parametrize("size", [0, MAX_FRAME_SIZE + 1])
def test_frames_outside_the_size_limits_are_refused(size: int) -> None:
    with pytest.raises(ValueError, match="frame"):
        encode_frame(bytes(size))


def test_decoder_returns_complete_frames_from_the_radio() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b">\x02\x00\xaa\xbb>\x01\x00\xcc") == [b"\xaa\xbb", b"\xcc"]


def test_decoder_reassembles_frames_split_across_reads() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b">") == []
    assert decoder.feed(b"\x03") == []
    assert decoder.feed(b"\x00\x01\x02") == []
    assert decoder.feed(b"\x03>\x01") == [b"\x01\x02\x03"]
    assert decoder.feed(b"\x00\x09") == [b"\x09"]


def test_decoder_skips_noise_and_counts_it() -> None:
    decoder = FrameDecoder()
    assert decoder.feed(b"boot log\r\n>\x01\x00\x05") == [b"\x05"]
    assert decoder.discarded == len(b"boot log\r\n")


def test_decoder_resynchronises_after_an_impossible_length() -> None:
    decoder = FrameDecoder()
    oversize = (MAX_FRAME_SIZE + 1).to_bytes(2, "little")
    assert decoder.feed(b">" + oversize + b">\x01\x00\x07") == [b"\x07"]
    assert decoder.feed(b">\x00\x00>\x01\x00\x08") == [b"\x08"]


def test_reset_drops_a_stale_partial_frame() -> None:
    decoder = FrameDecoder()
    decoder.feed(b">\x05\x00\x01")
    assert decoder.pending == 4
    decoder.reset()
    assert decoder.pending == 0
    assert decoder.feed(b">\x01\x00\x02") == [b"\x02"]
