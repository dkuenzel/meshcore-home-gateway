"""Frame layer of the companion protocol over USB serial and TCP (docs/protocol.md, "Framing").

To the radio: '<' (0x3C), payload length as uint16 little endian, payload.
From the radio: '>' (0x3E), same length field, payload.
MAX_FRAME_SIZE: MeshCore v1.18.0, src/helpers/BaseSerialInterface.h.
"""

from __future__ import annotations

TO_RADIO = 0x3C
FROM_RADIO = 0x3E
MAX_FRAME_SIZE = 176
_HEADER = 3


def encode_frame(payload: bytes) -> bytes:
    if not 0 < len(payload) <= MAX_FRAME_SIZE:
        raise ValueError(f"frame payload must be 1..{MAX_FRAME_SIZE} bytes, got {len(payload)}")
    return bytes([TO_RADIO]) + len(payload).to_bytes(2, "little") + payload


class FrameDecoder:
    """Incremental decoder for frames from the radio.

    Bytes before a start marker are discarded (and counted). A length of zero or above the
    maximum cannot be a frame: the marker is skipped and the search continues. A partial frame
    that never completes is dropped by `reset()`, which the session calls after a read timeout.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.discarded = 0

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def reset(self) -> None:
        self.discarded += len(self._buffer)
        self._buffer.clear()

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += data
        frames: list[bytes] = []
        while True:
            start = self._buffer.find(FROM_RADIO)
            if start < 0:
                self.reset()
                return frames
            if start:
                del self._buffer[:start]
                self.discarded += start
            if len(self._buffer) < _HEADER:
                return frames
            length = int.from_bytes(self._buffer[1:3], "little")
            if not 0 < length <= MAX_FRAME_SIZE:
                del self._buffer[:1]
                self.discarded += 1
                continue
            if len(self._buffer) < _HEADER + length:
                return frames
            frames.append(bytes(self._buffer[_HEADER : _HEADER + length]))
            del self._buffer[: _HEADER + length]
