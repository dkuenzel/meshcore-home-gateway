"""Transports: serial through a pseudo terminal, TCP through a local server."""

from __future__ import annotations

import asyncio
import errno
import gc
import os
import termios
from collections.abc import Iterator
from pathlib import Path

import pytest

from meshcore_home_gateway.radio import transport as transport_module
from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport


@pytest.fixture
def pty() -> Iterator[tuple[int, str]]:
    controller, terminal = os.openpty()
    path = os.ttyname(terminal)
    try:
        yield controller, path
    finally:
        os.close(controller)
        os.close(terminal)


def open_descriptors() -> int:
    """After a collection: garbage of earlier tests could otherwise close its descriptors while a
    test counts."""
    gc.collect()
    return len(os.listdir("/proc/self/fd"))


def test_serial_reads_and_writes_raw_bytes(pty: tuple[int, str]) -> None:
    controller, path = pty

    async def scenario() -> None:
        transport = SerialTransport(path)
        await transport.open()
        try:
            await transport.write(b"<\x01\x00\x16\n\r")
            await asyncio.sleep(0.05)
            assert os.read(controller, 100) == b"<\x01\x00\x16\n\r"  # raw: no CR/LF translation
            os.write(controller, b">\x01\x00\x0a\r\n")
            assert await asyncio.wait_for(transport.read(), 1) == b">\x01\x00\x0a\r\n"
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_serial_port_is_configured_raw_8n1_without_echo(pty: tuple[int, str]) -> None:
    _, path = pty

    async def scenario() -> None:
        transport = SerialTransport(path)
        await transport.open()
        try:
            fd = transport._fd  # white-box check of the terminal settings
            assert fd is not None
            iflag, oflag, cflag, lflag, ispeed, ospeed, cc = termios.tcgetattr(fd)
            assert not lflag & (termios.ECHO | termios.ICANON | termios.ISIG)
            assert cflag & termios.CSIZE == termios.CS8
            assert not cflag & (termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
            assert (ispeed, ospeed) == (termios.B115200, termios.B115200)
            assert not oflag & termios.OPOST
            assert not iflag & (termios.ICRNL | termios.IXON)
            assert (cc[termios.VMIN], cc[termios.VTIME]) == (0, 0)
        finally:
            await transport.close()

    asyncio.run(scenario())


def test_serial_open_is_exclusive(pty: tuple[int, str]) -> None:
    _, path = pty
    if os.geteuid() == 0:
        pytest.skip("root bypasses TIOCEXCL")

    async def scenario() -> None:
        first = SerialTransport(path)
        await first.open()
        try:
            with pytest.raises(OSError, match="busy"):
                await SerialTransport(path).open()
        finally:
            await first.close()

    asyncio.run(scenario())


def test_serial_refuses_1200_baud() -> None:
    with pytest.raises(ValueError, match="bootloader"):
        SerialTransport("/dev/null", baudrate=1200)


def test_serial_missing_device_raises() -> None:
    async def scenario() -> None:
        with pytest.raises(FileNotFoundError):
            await SerialTransport("/dev/serial/by-id/does-not-exist").open()

    asyncio.run(scenario())


def test_serial_hangup_is_reported_as_connection_error() -> None:
    controller, terminal = os.openpty()
    path = os.ttyname(terminal)
    os.close(terminal)

    async def scenario() -> None:
        transport = SerialTransport(path)
        await transport.open()
        os.close(controller)  # the other end disappears, like an unplugged USB device
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(transport.read(), 1)
        await transport.close()
        with pytest.raises(ConnectionError, match="not open"):
            await transport.write(b"x")

    asyncio.run(scenario())


def test_tcp_roundtrip_and_remote_close() -> None:
    async def scenario() -> None:
        received: list[bytes] = []

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            received.append(await reader.read(100))
            writer.write(b">\x01\x00\x0a")
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = TcpTransport("127.0.0.1", port)
        await transport.open()
        await transport.write(b"<\x01\x00\x16")
        assert await asyncio.wait_for(transport.read(), 1) == b">\x01\x00\x0a"
        with pytest.raises(ConnectionError, match="closed"):
            await asyncio.wait_for(transport.read(), 1)
        await transport.close()
        server.close()
        await server.wait_closed()
        assert received == [b"<\x01\x00\x16"]

    asyncio.run(scenario())


def test_tcp_requires_open() -> None:
    async def scenario() -> None:
        transport = TcpTransport("127.0.0.1", 9)
        with pytest.raises(ConnectionError):
            await transport.read()
        with pytest.raises(ConnectionError):
            await transport.write(b"x")
        await transport.close()

    asyncio.run(scenario())


def test_serial_asserts_dtr_and_clears_rts(
    pty: tuple[int, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """nRF52 radios only send while DTR is set; RTS may reset ESP32 boards (docs/protocol.md)."""
    import fcntl
    import struct

    calls: list[tuple[int, bytes | int | None]] = []
    real_ioctl = fcntl.ioctl

    def recording_ioctl(fd: int, request: int, arg: bytes | int = 0) -> object:
        calls.append((request, arg))
        if request in (termios.TIOCMBIS, termios.TIOCMBIC):
            return arg
        return real_ioctl(fd, request, arg)

    monkeypatch.setattr(fcntl, "ioctl", recording_ioctl)
    _, path = pty

    async def scenario() -> None:
        transport = SerialTransport(path)
        await transport.open()
        await transport.close()

    asyncio.run(scenario())
    dtr, rts = struct.pack("I", termios.TIOCM_DTR), struct.pack("I", termios.TIOCM_RTS)
    assert (termios.TIOCEXCL, 0) in calls
    assert (termios.TIOCMBIS, dtr) in calls
    assert (termios.TIOCMBIC, rts) in calls


def _fails_on(path: str, real: object) -> object:
    """os.read/os.write replacement that fails with EIO on the terminal `path` only."""

    def call(fd: int, arg: object) -> object:
        if os.isatty(fd) and os.ttyname(fd) == path:
            raise OSError(errno.EIO, "Input/output error")
        return real(fd, arg)  # type: ignore[operator]

    return call


def test_serial_read_error_is_reported_as_connection_error(
    pty: tuple[int, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    controller, path = pty

    async def scenario() -> None:
        transport = SerialTransport(path)
        await transport.open()
        monkeypatch.setattr(os, "read", _fails_on(path, os.read))
        os.write(controller, b"x")  # the port becomes readable; reading it fails
        with pytest.raises(ConnectionError, match="Input/output error"):
            await asyncio.wait_for(transport.read(), 1)
        monkeypatch.undo()
        await transport.close()

    asyncio.run(scenario())


def test_serial_write_error_is_reported_as_connection_error(
    pty: tuple[int, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, path = pty

    async def scenario() -> None:
        transport = SerialTransport(path)
        await transport.open()
        monkeypatch.setattr(os, "write", _fails_on(path, os.write))
        with pytest.raises(ConnectionError, match="Input/output error"):
            await transport.write(b"<\x01\x00\x16")
        monkeypatch.undo()
        await transport.close()

    asyncio.run(scenario())


def test_serial_open_of_something_else_than_a_terminal_fails_cleanly(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-tty"
    plain.write_bytes(b"")
    before = open_descriptors()

    async def scenario() -> None:
        with pytest.raises(OSError, match="Inappropriate ioctl"):
            await SerialTransport(str(plain)).open()

    asyncio.run(scenario())
    assert open_descriptors() == before  # the descriptor was closed


def test_serial_terminal_setup_failure_is_an_os_error_and_closes_the_descriptor(
    pty: tuple[int, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """termios.error is no OSError: unconverted, the bridge would log a traceback, and the
    descriptor would keep the exclusive lock until the service restarts."""
    _, path = pty

    def hung_up(*_: object) -> None:  # the terminal went away after TIOCEXCL
        raise termios.error(errno.EIO, "Input/output error")

    monkeypatch.setattr(termios, "tcsetattr", hung_up)
    before = open_descriptors()

    async def scenario() -> None:
        with pytest.raises(OSError, match=f"Input/output error: '{path}'"):
            await SerialTransport(path).open()

    asyncio.run(scenario())
    assert open_descriptors() == before  # the descriptor was closed


def test_tcp_connect_timeout_names_the_radio(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(*_: object) -> None:
        await asyncio.sleep(3600)

    monkeypatch.setattr(asyncio, "open_connection", never)
    monkeypatch.setattr(transport_module, "CONNECT_TIMEOUT_S", 0.01)
    with pytest.raises(TimeoutError, match=r"tcp 192\.0\.2\.10:5000: no connection within"):
        asyncio.run(TcpTransport("192.0.2.10", 5000).open())
