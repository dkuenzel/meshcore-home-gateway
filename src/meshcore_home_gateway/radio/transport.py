"""Byte transports to the companion radio: USB serial (termios) and TCP (asyncio).

Both deliver raw bytes; framing is done by `meshcore_home_gateway.protocol.framing`.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import os
import struct
import termios
from typing import Protocol

BAUDRATE = 115200  # MeshCore companion firmware, USB serial
FORBIDDEN_BAUDRATE = 1200  # "1200 baud touch": nRF52 boards reboot into the bootloader
CONNECT_TIMEOUT_S = 10.0


class Transport(Protocol):
    description: str
    needs_flush: bool  # the radio's parser may hold a partial frame from an earlier client

    async def open(self) -> None: ...
    async def read(self) -> bytes: ...
    async def write(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


class SerialTransport:
    """Raw 8N1 serial port without flow control, opened exclusively (TIOCEXCL).

    DTR is asserted and RTS cleared, as meshcore_py and meshcore-cli do: nRF52 radios only send
    while DTR is set, and RTS can reset ESP32 boards with an auto-reset circuit. The port is never
    set to 1200 baud (docs/protocol.md, section 1).
    """

    # the serial parser keeps its state across reconnects and ignores zeros while idle
    needs_flush = True

    def __init__(self, device: str, baudrate: int = BAUDRATE) -> None:
        if baudrate == FORBIDDEN_BAUDRATE:
            raise ValueError("1200 baud would put nRF52 radios into the bootloader")
        self.device = device
        self.description = f"serial {device}"
        self._speed = getattr(termios, f"B{baudrate}")
        self._fd: int | None = None
        self._data: asyncio.Queue[bytes | Exception] = asyncio.Queue()

    async def open(self) -> None:
        fd = os.open(self.device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            fcntl.ioctl(fd, termios.TIOCEXCL)
            _make_raw(fd, self._speed)
            _set_modem_lines(fd)
            termios.tcflush(fd, termios.TCIOFLUSH)
        except OSError:
            os.close(fd)
            raise
        self._fd = fd
        asyncio.get_running_loop().add_reader(fd, self._readable)

    def _readable(self) -> None:
        if self._fd is None:
            return
        try:
            data = os.read(self._fd, 4096)
        except BlockingIOError:
            return
        except OSError as exc:  # device unplugged: EIO
            self._data.put_nowait(ConnectionError(f"{self.device}: {exc.strerror}"))
            asyncio.get_running_loop().remove_reader(self._fd)
            return
        if data:
            self._data.put_nowait(data)
        else:  # end of file: the device went away
            self._data.put_nowait(ConnectionError(f"{self.device}: closed"))
            asyncio.get_running_loop().remove_reader(self._fd)

    async def read(self) -> bytes:
        item = await self._data.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def write(self, data: bytes) -> None:
        if self._fd is None:
            raise ConnectionError(f"{self.device}: not open")
        view = memoryview(data)
        while view:
            try:
                written = os.write(self._fd, view)
            except BlockingIOError:
                await asyncio.sleep(0.005)
                continue
            except OSError as exc:
                raise ConnectionError(f"{self.device}: {exc.strerror}") from exc
            view = view[written:]

    async def close(self) -> None:
        if self._fd is None:
            return
        fd, self._fd = self._fd, None
        with contextlib.suppress(Exception):
            asyncio.get_running_loop().remove_reader(fd)
        with contextlib.suppress(OSError):
            os.close(fd)


def _set_modem_lines(fd: int) -> None:
    """Assert DTR, clear RTS. Pseudo terminals have no modem lines; that is not an error."""
    try:
        fcntl.ioctl(fd, termios.TIOCMBIS, struct.pack("I", termios.TIOCM_DTR))
        fcntl.ioctl(fd, termios.TIOCMBIC, struct.pack("I", termios.TIOCM_RTS))
    except OSError as exc:
        if exc.errno not in (errno.ENOTTY, errno.EINVAL):
            raise


def _make_raw(fd: int, speed: int) -> None:
    iflag, oflag, cflag, lflag, _, _, cc = termios.tcgetattr(fd)
    iflag &= ~(
        termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP | termios.INLCR
        | termios.IGNCR | termios.ICRNL | termios.IXON | termios.IXOFF | termios.IXANY
    )  # fmt: skip
    oflag &= ~termios.OPOST
    lflag &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
    cflag &= ~(termios.CSIZE | termios.PARENB | termios.CSTOPB | termios.CRTSCTS)
    cflag |= termios.CS8 | termios.CREAD | termios.CLOCAL
    cc[termios.VMIN] = 0
    cc[termios.VTIME] = 0
    termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc])


class TcpTransport:
    """TCP connection to a companion radio with WiFi firmware. The interface has no
    authentication: use it only on a trusted, isolated network (docs/design.md, Security)."""

    # every connection starts the WiFi parser afresh, and zeros would misalign its 3-byte
    # header reads (docs/protocol.md, section 1)
    needs_flush = False

    def __init__(self, host: str, port: int) -> None:
        self.description = f"tcp {host}:{port}"
        self._host, self._port = host, port
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None

    async def open(self) -> None:
        async with asyncio.timeout(CONNECT_TIMEOUT_S):
            self._reader, self._writer = await asyncio.open_connection(self._host, self._port)

    async def read(self) -> bytes:
        if self._reader is None:
            raise ConnectionError(f"{self.description}: not open")
        data = await self._reader.read(4096)
        if not data:
            raise ConnectionError(f"{self.description}: closed by the radio")
        return data

    async def write(self, data: bytes) -> None:
        if self._writer is None:
            raise ConnectionError(f"{self.description}: not open")
        try:
            self._writer.write(data)
            await self._writer.drain()
        except OSError as exc:
            raise ConnectionError(f"{self.description}: {exc}") from exc

    async def close(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer is not None:
            writer.close()
            with contextlib.suppress(OSError, ConnectionError):
                await writer.wait_closed()


def is_disconnect(exc: BaseException) -> bool:
    """Errors that mean the radio went away (reconnect) rather than a bug."""
    return isinstance(exc, ConnectionError | TimeoutError) or (
        isinstance(exc, OSError)
        and exc.errno in (errno.EIO, errno.ENODEV, errno.ENXIO, errno.EBADF)
    )
