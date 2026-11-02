"""Experiment of the review (SEC-010, SEC-011, RES-005): what an advert flood with invented keys
does to the observer's memory, the history database and the bus.

Feeds NEW_ADVERT records to the observer the way the bridge does, with a real History on a
temporary SQLite file. Run from the repository root with `PYTHONPATH=src`:

    python3 docs/audit/2026-11-security-architecture-review/experiments/advert_flood.py 5000
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

from meshcore_home_gateway.history import History, Retention
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.radio.session import AdvertHeard


class CountingBus:
    """A bus that counts what the observer publishes."""

    def __init__(self) -> None:
        self.publishes = 0
        self.bytes = 0

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        self.publishes += 1
        self.bytes += len(payload)


def advert_record(i: int) -> tuple[bytes, codec.ContactRecord]:
    """The record of a NEW_ADVERT from an invented key, as the codec decodes it."""
    key = i.to_bytes(4, "big") + os.urandom(28)
    name = f"Node{i}".encode().ljust(32, b"\0")
    raw = (
        key
        + bytes([1, 0, 0xFF])
        + bytes(64)
        + name
        + (1_800_000_000 + i).to_bytes(4, "little")
        + bytes(8)
        + bytes(4)
    )
    return key, codec._contact(bytes([0x8A]) + raw)


def main(count: int) -> None:
    tmp = Path(tempfile.mkdtemp())
    history = History.open(tmp / "history.sqlite3", Retention())
    bus = CountingBus()
    observer = Observer(bus, Topics("meshcore", "radio1"), history=history, wall_clock=time.time)
    observer.radio_connected(350, None)
    started = time.time()
    for i in range(count):
        key, record = advert_record(i)
        advert = codec.Advert(key, True, record)
        observer.radio_event(AdvertHeard(advert, time.time() + i * 3, i + 1, 1))
    elapsed = time.time() - started
    db = sqlite3.connect(tmp / "history.sqlite3")
    rows = db.execute("select count(*) from node").fetchone()[0]
    size = sum(os.path.getsize(p) for p in tmp.glob("history.sqlite3*"))
    sys.stdout.write(
        f"adverts fed: {count}; node rows in history: {rows}; db bytes: {size}; "
        f"heard nodes in memory: {len(observer.mesh.nodes)}; publishes: {bus.publishes} "
        f"({bus.bytes} B); {elapsed:.2f} s\n"
    )


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 5000)
