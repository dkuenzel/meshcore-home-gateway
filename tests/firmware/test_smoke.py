"""The harness itself: the real firmware boots on the simulated air, every node on the EU/UK
Narrow preset, packets cross the air, and the gateway reads a companion through its own serial
transport."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeVar

import pytest

from meshcore_home_gateway import cli
from meshcore_home_gateway.radio.session import CompanionSession, RadioEvent
from meshcore_home_gateway.radio.transport import SerialTransport
from tests.firmware.air import AirThread, RadioSettings
from tests.firmware.conftest import Firmware, Nodes
from tests.firmware.node import Node

T = TypeVar("T")
# EU/UK Narrow with CR 4/8: frequency in kHz, bandwidth in Hz, spreading factor, coding rate 4/x
PRESET = (869_618, 62_500, 8, 8)
WAIT_S = 10.0


def wait_for(found: Callable[[], T | None], what: str, timeout: float = WAIT_S) -> T:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = found()
        if result:
            return result
        time.sleep(0.05)
    pytest.fail(f"no {what} within {timeout:g} s")


def probe(node: Node, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    status = cli.main(
        ["probe", "--serial", str(node.serial), "--state-dir", str(tmp_path / "state"), "--json"]
    )
    output = capsys.readouterr()
    assert status == 0, output.err
    report: dict[str, Any] = json.loads(output.out)
    return report


def test_the_probe_reads_the_companion_through_the_serial_transport(
    firmware: Firmware, companion: Node, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    report = probe(companion, tmp_path, capsys)
    assert report["firmware"] == firmware.version
    assert report["profile"]["firmware_tested"]  # the gateway knows the pinned firmware
    assert report["model"] == "Native Sim"
    lora = (report["frequency_mhz"], report["bandwidth_khz"], report["spreading_factor"])
    assert (*lora, report["coding_rate"]) == (869.618, 62.5, 8, 8)
    # the port opens again once the probe closed it, as a USB serial port does (also without root)
    assert probe(companion, tmp_path, capsys)["public_key"] == report["public_key"]


def test_every_node_is_on_the_eu_uk_narrow_preset(nodes: Nodes, air: AirThread) -> None:
    started = [nodes.companion("A", wait=False), nodes.repeater("R", wait=False)]
    nodes.ready()

    def all_heard() -> dict[int, RadioSettings] | None:
        settings = air.settings()
        return settings if all(n.node_id in settings for n in started) else None

    settings = wait_for(all_heard, "hello of every node")
    for node in started:
        heard = settings[node.node_id]
        assert (heard.frequency_khz, heard.bandwidth_hz, heard.spreading_factor) == PRESET[:3]
        assert heard.coding_rate == PRESET[3], node.name


def test_a_channel_message_crosses_the_air(nodes: Nodes, air: AirThread) -> None:
    alice, bob = nodes.companion("A", wait=False), nodes.companion("B", wait=False)
    nodes.ready()
    air.link(alice.node_id, bob.node_id, snr=8.0)

    async def send() -> None:
        events: asyncio.Queue[RadioEvent] = asyncio.Queue()
        session = CompanionSession(SerialTransport(str(alice.serial)), events, set_clock=False)
        try:
            await session.start(fetch_messages=False)
            await session.send_channel_text(0, "Hello from Alice", int(time.time()))
        finally:
            await session.close()

    asyncio.run(send())
    heard = wait_for(
        lambda: [
            t
            for t in air.transmissions()
            if t.sender == alice.node_id and t.kind.startswith("GRP_TXT/")
            and t.outcomes.get(bob.node_id) == "received"
        ],
        "channel message from A received by B",
    )  # fmt: skip
    assert len(heard) == 1
    # the packet log goes to the node's log, never into its serial port
    assert wait_for(lambda: "RX, len=" in bob.log_text(), "packet log line of B")
