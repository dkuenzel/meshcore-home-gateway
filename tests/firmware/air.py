"""The simulated LoRa air for the firmware nodes, in asyncio (board/SimRadio.h has the datagrams).

A node greets the air with its radio settings and sends each transmission with its time on air.
The air tells every node linked to the sender at once that the channel is busy (the receiver's
channel activity detection), and hands it the packet when the time on air is over, with the
link's SNR and RSSI. A reception that overlaps another at the same node is lost, and so is the
other (a collision). A node on another frequency, bandwidth or spreading factor hears nothing;
the coding rate is in the packet's header, so a node hears any rate. Links are one-way;
`link()` makes both directions by default. Two knobs per link stand in for a worse mesh: `loss`,
the share of packets the receiver misses (it hears nothing of them), and `delay_s`, how much
later than the sender's time on air a packet reaches the receiver (as if the mesh held it).

`Air` runs in the caller's event loop. `AirThread` runs one in a thread of its own, for the
synchronous fixtures and for tests that run the gateway with `asyncio.run` beside it.
"""

from __future__ import annotations

import asyncio
import random
import struct
import threading
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, TextIO, TypeVar

T = TypeVar("T")
Outcome = Literal["received", "collided", "lost"]

# src/Packet.h: the route in bits 0-1 of the header, the payload type in bits 2-5
ROUTES = {0: "TF", 1: "F", 2: "D", 3: "TD"}  # (transport) flood, (transport) direct
TYPES = {
    0: "REQ", 1: "RESPONSE", 2: "TXT_MSG", 3: "ACK", 4: "ADVERT", 5: "GRP_TXT", 6: "GRP_DATA",
    7: "ANON_REQ", 8: "PATH", 9: "TRACE", 10: "MULTIPART", 11: "CONTROL", 15: "RAW_CUSTOM",
}  # fmt: skip
HELLO = struct.Struct("<cBIIBBb")  # 'H', node, frequency kHz, bandwidth Hz, SF, CR, dBm
TRANSMIT = struct.Struct("<cBI")  # 'T', node, airtime in ms; the packet follows
WAIT_S = 5.0  # for an answer of the air's thread


@dataclass(frozen=True)
class RadioSettings:
    frequency_khz: int
    bandwidth_hz: int
    spreading_factor: int
    coding_rate: int  # 5 to 8: 4/5 to 4/8
    tx_power_dbm: int

    def hears(self, other: RadioSettings) -> bool:
        return (self.frequency_khz, self.bandwidth_hz, self.spreading_factor) == (
            other.frequency_khz, other.bandwidth_hz, other.spreading_factor,
        )  # fmt: skip


@dataclass(frozen=True)
class Link:
    snr: float  # dB
    rssi: int  # dBm
    loss: float = 0.0  # the share of packets the receiver misses, 0 to 1
    delay_s: float = 0.0  # the packet reaches the receiver this much later


@dataclass
class Transmission:
    at: float  # seconds since the air started
    sender: int
    airtime_ms: int
    packet: bytes
    # by receiver, once the time on air is over
    outcomes: dict[int, Outcome] = field(default_factory=dict)
    ended: dict[int, float] = field(default_factory=dict)  # by receiver: when it was over

    def heard_by(self, receiver: int) -> bool:
        return self.outcomes.get(receiver) == "received"

    @property
    def kind(self) -> str:
        """Payload type and route, such as `ADVERT/F`."""
        if not self.packet:
            return "empty"
        header = self.packet[0]
        return f"{TYPES.get((header >> 2) & 0x0F, '?')}/{ROUTES[header & 0x03]}"


@dataclass
class _Reception:
    end: float  # loop time
    collided: bool = False


class Air:
    def __init__(self, log_path: Path | None = None, *, seed: int = 1) -> None:
        """`seed`: the losses of `Link.loss` repeat from run to run."""
        self.port = 0
        self.settings: dict[int, RadioSettings] = {}  # by node, from its hello
        self.links: dict[tuple[int, int], Link] = {}  # (sender, receiver)
        self.transmissions: list[Transmission] = []
        self._addresses: dict[int, tuple[str, int]] = {}
        self._receiving: dict[int, list[_Reception]] = {}
        self._timers: set[asyncio.TimerHandle] = set()
        self._transport: asyncio.DatagramTransport | None = None
        self._closed: asyncio.Event | None = None
        self._log_path = log_path
        self._log: TextIO | None = None
        self._start = 0.0
        self._random = random.Random(seed)  # noqa: S311 - a simulation, not a secret

    async def start(self) -> int:
        loop = asyncio.get_running_loop()
        self._closed = asyncio.Event()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _AirProtocol(self), local_addr=("127.0.0.1", 0)
        )
        self.port = int(self._transport.get_extra_info("sockname")[1])
        self._start = time.monotonic()  # the clock of asyncio's loop
        if self._log_path is not None:
            self._log = self._log_path.open("a", encoding="utf-8")
        self._write(f"air on 127.0.0.1:{self.port}")
        return self.port

    async def close(self) -> None:
        for timer in self._timers:
            timer.cancel()
        self._timers.clear()
        if self._transport is not None and self._closed is not None:
            self._transport.close()
            await self._closed.wait()
            self._transport = None
        if self._log is not None:
            self._log.close()
            self._log = None

    async def __aenter__(self) -> Air:
        await self.start()
        return self

    async def __aexit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    def link(
        self, a: int, b: int, snr: float = 8.0, rssi: int | None = None, *, both: bool = True,
        loss: float = 0.0, delay_s: float = 0.0,
    ) -> None:  # fmt: skip
        """Let b hear a (and a hear b) with this SNR; the RSSI follows from it unless given. A
        packet already on its way keeps the link it left with."""
        if not 0.0 <= loss <= 1.0 or delay_s < 0.0:
            raise ValueError(f"loss is 0 to 1 and the delay not negative: {loss}, {delay_s}")
        found = Link(snr, rssi if rssi is not None else round(-110 + snr), loss, delay_s)
        self.links[(a, b)] = found
        if both:
            self.links[(b, a)] = found
        knobs = (f" loss={loss:g}" if loss else "") + (f" delay={delay_s:g}s" if delay_s else "")
        self._write(f"link {a} {'<->' if both else '->'} {b} snr={snr:g} rssi={found.rssi}{knobs}")

    def unlink(self, a: int, b: int, *, both: bool = True) -> None:
        self.links.pop((a, b), None)
        if both:
            self.links.pop((b, a), None)
        self._write(f"unlink {a} {'<->' if both else '->'} {b}")

    # --- datagrams ------------------------------------------------------------------------------

    def on_datagram(self, data: bytes, address: tuple[str, int]) -> None:
        if data[:1] == b"H" and len(data) >= HELLO.size:
            _, node, frequency, bandwidth, sf, cr, dbm = HELLO.unpack_from(data)
            settings = RadioSettings(frequency, bandwidth, sf, cr, dbm)
            if self._addresses.get(node) != address or self.settings.get(node) != settings:
                self._write(f"node {node} at port {address[1]}: {_describe(settings)}")
            self._addresses[node] = address
            self.settings[node] = settings
        elif data[:1] == b"T" and len(data) > TRANSMIT.size:
            _, node, airtime_ms = TRANSMIT.unpack_from(data)
            self._addresses[node] = address
            self._transmit(node, airtime_ms, data[TRANSMIT.size :])

    def _transmit(self, sender: int, airtime_ms: int, packet: bytes) -> None:
        loop = asyncio.get_running_loop()
        now = loop.time()
        sent = Transmission(round(now - self._start, 3), sender, airtime_ms, packet)
        self.transmissions.append(sent)
        self._write(f"{sender} TX {airtime_ms} ms {sent.kind} len={len(packet)} {packet.hex()}")
        self._timers = {t for t in self._timers if t.when() > now}
        own = self.settings.get(sender)
        for (a, b), link in list(self.links.items()):
            if a != sender or b not in self._addresses:
                continue
            theirs = self.settings.get(b)
            if own is not None and theirs is not None and not own.hears(theirs):
                continue
            if link.loss and self._random.random() < link.loss:
                sent.outcomes[b] = "lost"
                self._write(f"  {sender} -> {b} lost {sent.kind}")
            elif link.delay_s:
                self._timers.add(loop.call_later(link.delay_s, self._arrive, sent, b, link))
            else:
                self._arrive(sent, b, link)

    def _arrive(self, sent: Transmission, receiver: int, link: Link) -> None:
        """The packet starts to reach the receiver: its channel is busy for the time on air."""
        loop = asyncio.get_running_loop()
        now, airtime_s = loop.time(), sent.airtime_ms / 1000
        self._send(receiver, b"B" + struct.pack("<I", sent.airtime_ms))
        reception = _Reception(now + airtime_s)
        ongoing = [r for r in self._receiving.get(receiver, []) if r.end > now]
        for other in ongoing:
            other.collided = reception.collided = True
        self._receiving[receiver] = [*ongoing, reception]
        timer = loop.call_later(airtime_s, self._deliver, sent, receiver, link, reception)
        self._timers.add(timer)

    def _deliver(
        self, sent: Transmission, receiver: int, link: Link, reception: _Reception
    ) -> None:
        outcome: Outcome = "collided" if reception.collided else "received"
        sent.outcomes[receiver] = outcome
        sent.ended[receiver] = round(asyncio.get_running_loop().time() - self._start, 3)
        if outcome == "received":
            snr = round(link.snr * 4)  # the radio reports a quarter dB
            self._send(receiver, b"R" + struct.pack("<hh", snr, link.rssi) + sent.packet)
        else:
            self._send(receiver, b"X")
        self._write(f"  {sent.sender} -> {receiver} {outcome} {sent.kind} snr={link.snr:g}")

    @property
    def started(self) -> float:
        """time.monotonic() when the air started: `Transmission.at` counts from it."""
        return self._start

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def on_closed(self) -> None:
        if self._closed is not None:
            self._closed.set()

    def _send(self, node: int, data: bytes) -> None:
        if self._transport is not None:
            self._transport.sendto(data, self._addresses[node])

    def _write(self, line: str) -> None:
        if self._log is not None:
            elapsed = time.monotonic() - self._start
            self._log.write(f"{elapsed:9.3f} {line}\n")
            self._log.flush()


def _describe(settings: RadioSettings) -> str:
    return (
        f"{settings.frequency_khz / 1e3:.3f} MHz, {settings.bandwidth_hz / 1e3:g} kHz, "
        f"SF {settings.spreading_factor}, CR 4/{settings.coding_rate}, {settings.tx_power_dbm} dBm"
    )


class _AirProtocol(asyncio.DatagramProtocol):
    def __init__(self, air: Air) -> None:
        self._air = air

    def datagram_received(self, data: bytes, addr: tuple[str | int, ...]) -> None:
        self._air.on_datagram(data, (str(addr[0]), int(addr[1])))

    def connection_lost(self, exc: Exception | None) -> None:
        self._air.on_closed()


class AirThread:
    """An `Air` in an event loop of its own thread. Every call waits for the air's answer, so the
    test reads a consistent state."""

    def __init__(self, log_path: Path | None = None, *, seed: int = 1) -> None:
        self.air = Air(log_path, seed=seed)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="air", daemon=True)

    @property
    def port(self) -> int:
        return self.air.port

    def start(self) -> int:
        self._thread.start()
        return self._wait(self.air.start())

    def stop(self) -> None:
        if self._thread.is_alive():
            self._wait(self.air.close())
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(WAIT_S)
        self._loop.close()

    def __enter__(self) -> AirThread:
        self.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()

    def call(self, function: Callable[[Air], T]) -> T:
        """`function(air)` in the air's thread."""

        async def run() -> T:
            return function(self.air)

        return self._wait(run())

    def link(
        self, a: int, b: int, snr: float = 8.0, rssi: int | None = None, *, both: bool = True,
        loss: float = 0.0, delay_s: float = 0.0,
    ) -> None:  # fmt: skip
        self.call(lambda air: air.link(a, b, snr, rssi, both=both, loss=loss, delay_s=delay_s))

    def unlink(self, a: int, b: int, *, both: bool = True) -> None:
        self.call(lambda air: air.unlink(a, b, both=both))

    def settings(self) -> dict[int, RadioSettings]:
        return self.call(lambda air: dict(air.settings))

    @property
    def started(self) -> float:
        return self.air.started

    def elapsed(self) -> float:
        return self.air.elapsed()

    def sent_by(self, sender: int, kind: str = "") -> list[Transmission]:
        """The transmissions of `sender` whose kind starts with `kind` (such as `REQ/`)."""
        return [t for t in self.transmissions() if t.sender == sender and t.kind.startswith(kind)]

    async def next_sent(
        self, sender: int, kind: str, *, after: int, timeout_s: float = 60.0
    ) -> Transmission:
        """The first transmission of `sender` whose kind starts with `kind`, after the first
        `after` transmissions of the air; for a test in its own event loop."""
        deadline = time.monotonic() + timeout_s
        while True:
            for found in self.transmissions()[after:]:
                if found.sender == sender and found.kind.startswith(kind):
                    return found
            if time.monotonic() > deadline:
                raise AssertionError(f"node {sender} sent no {kind} within {timeout_s:g} s")
            await asyncio.sleep(0.05)

    def again(self, sent: Transmission) -> Transmission:
        """A transmission as the air knows it now: with the outcomes that came since."""
        (found,) = [t for t in self.transmissions() if (t.sender, t.at) == (sent.sender, sent.at)]
        return found

    def count(self) -> int:
        """The number of transmissions so far, for `next_sent`."""
        return self.call(lambda air: len(air.transmissions))

    def transmissions(self) -> list[Transmission]:
        """A copy of the transmissions so far, with the outcomes known so far."""
        return self.call(
            lambda air: [
                Transmission(
                    t.at, t.sender, t.airtime_ms, t.packet, dict(t.outcomes), dict(t.ended)
                )
                for t in air.transmissions
            ]
        )

    def _wait(self, coroutine: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(WAIT_S)
