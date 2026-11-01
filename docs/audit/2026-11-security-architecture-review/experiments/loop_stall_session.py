"""Experiment of the review (CODE-001, RES-009): the real CompanionSession over a socket pair to
a thread that plays a radio answering after a delay; the event loop is stalled while the session
waits for the answer. A stall longer than the command timeout ends the session although the
answer arrived during the stall. Run from the repository root with `PYTHONPATH=src:tests`:

    python3 docs/audit/2026-11-security-architecture-review/experiments/loop_stall_session.py
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
import threading
import time

from meshcore_home_gateway.protocol.framing import FrameDecoder
from meshcore_home_gateway.radio.session import CompanionSession, RadioError
from radio_emulator import EmulatedRadio

logging.basicConfig(level=logging.ERROR)


class SocketTransport:
    """A TCP-like transport on one end of a socket pair (the Transport protocol)."""

    needs_flush = False
    description = "socketpair"

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(sock=self._sock)

    async def read(self) -> bytes:
        assert self._reader is not None  # noqa: S101 - an experiment, not the program
        data = await self._reader.read(4096)
        if not data:
            raise ConnectionError("closed")
        return data

    async def write(self, data: bytes) -> None:
        assert self._writer is not None  # noqa: S101
        self._writer.write(data)
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()


def radio_thread(
    sock: socket.socket, radio: EmulatedRadio, slow_from: threading.Event, delay_s: float
) -> None:
    """Feed frames to the emulator synchronously and write its answers; delay them once asked."""
    loop = asyncio.new_event_loop()
    loop.run_until_complete(radio.open())
    FrameDecoder()
    sock.settimeout(0.05)
    while True:
        try:
            data = sock.recv(4096)
        except TimeoutError:
            data = b""
        except OSError:
            return
        if data:
            loop.run_until_complete(radio.write(data))
        try:
            out = loop.run_until_complete(asyncio.wait_for(radio.read(), 0.01))
        except TimeoutError:
            continue
        if slow_from.is_set():
            time.sleep(delay_s)
        try:
            sock.sendall(out)
        except OSError:
            return


async def run(stall_s: float, answer_after_s: float) -> str:
    a, b = socket.socketpair()
    radio = EmulatedRadio(clock=int(time.time()))
    slow = threading.Event()
    threading.Thread(
        target=radio_thread, args=(b, radio, slow, answer_after_s), daemon=True
    ).start()
    session = CompanionSession(SocketTransport(a), asyncio.Queue(), flush_settle_s=0.0)
    await session.start()
    slow.set()
    loop = asyncio.get_running_loop()
    loop.call_later(0.2, time.sleep, stall_s)  # the stall while the session waits for the answer
    try:
        battery = await session.get_battery()
        result = f"battery {battery.millivolts} mV; closed {session.closed}"
    except RadioError as exc:
        result = f"SESSION FAILED: {exc}"
    await session.close()
    a.close()
    b.close()
    return f"stall {stall_s} s, answer after {answer_after_s} s: {result}"


if __name__ == "__main__":
    for stall, answer in ((0.0, 1.0), (6.0, 1.0), (4.0, 1.0), (6.0, 4.0)):
        sys.stdout.write(asyncio.run(run(stall, answer)) + "\n")
