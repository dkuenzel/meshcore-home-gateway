"""A playground on the simulated air (EXT-019): `make firmware-play` starts the household of the
hardware checklist on MeshCore's own firmware (radio A on the gateway, a repeater, Bob's
companion B with a GPS, set up as docs/companions.md says), a Mosquitto broker on 127.0.0.1 and
the gateway's service on A, and keeps them running for exploration with an MQTT client until
Ctrl-C. A line typed is a direct message from B to the gateway.

It is built from the firmware tests' parts (README.md) and is no test itself;
test_playground.py starts and stops it once.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import paho.mqtt.client as paho
from paho.mqtt.enums import CallbackAPIVersion

from tests.firmware.air import AirThread
from tests.firmware.app import App
from tests.firmware.conftest import BUILD, Firmware, Nodes, pinned_companion_version
from tests.firmware.household import Household, household_ready, start

NODE_ID = "play"
BASE = f"meshcore/{NODE_ID}"
READY_S = 60.0  # until the service reports the radio connected
STOP_S = 15.0


def configuration(household: Household, port: int) -> str:
    """The service on A: Bob's companion is a target, a command sender with answers, and asked
    for telemetry; the broker takes anyone (127.0.0.1 only), so `insecure` says so."""
    bob = household.bob_key.hex()
    return (
        f'[radio]\ntransport = "serial"\ndevice = "{household.alice.serial}"\n'
        f'[mqtt]\nhost = "127.0.0.1"\nport = {port}\nbase_topic = "meshcore"\n'
        f'node_id = "{NODE_ID}"\ninsecure = true\n'
        "[homeassistant]\ndiscovery = false\n"
        f'[targets.bob]\ncontact = "{bob}"\n'
        f'[commands]\nsenders = ["{bob}"]\nwords = ["status", "arm", "disarm"]\nreply = "all"\n'
        '[telemetry]\nask = "household"\n'
    )


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextlib.contextmanager
def broker(directory: Path, port: int) -> Iterator[None]:
    found = shutil.which("mosquitto") or shutil.which("mosquitto", path="/usr/sbin:/usr/local/sbin")
    if found is None:
        raise SystemExit("mosquitto not found: install it (apt install mosquitto)")
    conf = directory / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous true\npersistence false\n", encoding="utf-8"
    )
    with (directory / "mosquitto.log").open("ab") as log:
        process = subprocess.Popen([found, "-c", str(conf)], stdout=log, stderr=subprocess.STDOUT)  # noqa: S603 - fixed arguments
    try:
        deadline = time.monotonic() + 5.0
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                if time.monotonic() > deadline or process.poll() is not None:
                    raise SystemExit(
                        f"mosquitto did not start: {directory / 'mosquitto.log'}"
                    ) from None
                time.sleep(0.05)
        yield
    finally:
        process.terminate()
        process.wait(STOP_S)


@contextlib.contextmanager
def service(directory: Path, text: str) -> Iterator[subprocess.Popen[bytes]]:
    """`meshcore-home-gateway run` as the systemd service runs it; its log in service.log."""
    config = directory / "config.toml"
    config.write_text(text, encoding="utf-8")
    environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")}
    with (directory / "service.log").open("ab") as log:
        process = subprocess.Popen(  # noqa: S603 - this interpreter, fixed arguments
            [
                sys.executable,
                "-m",
                "meshcore_home_gateway",
                "run",
                "--config",
                str(config),
                "--state-dir",
                str(directory / "state"),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            env=environment,
        )
    try:
        yield process
    finally:
        process.terminate()
        try:
            process.wait(STOP_S)
        except subprocess.TimeoutExpired:
            process.kill()


def radio_connected(port: int, timeout_s: float = READY_S) -> bool:
    """Whether the service's retained `state` says the radio is connected within the time."""
    connected = threading.Event()

    def on_message(_client: object, _userdata: object, message: paho.MQTTMessage) -> None:
        with contextlib.suppress(ValueError):
            if json.loads(message.payload).get("radio") == "connected":
                connected.set()

    client = paho.Client(CallbackAPIVersion.VERSION2)
    client.on_message = on_message
    client.connect("127.0.0.1", port)
    client.subscribe(f"{BASE}/state", qos=1)
    client.loop_start()
    try:
        return connected.wait(timeout_s)
    finally:
        client.loop_stop()
        client.disconnect()


def instructions(directory: Path, port: int) -> str:
    return f"""
The playground is running (Ctrl-C stops it). Its files are in {directory}.

  mosquitto_sub -h 127.0.0.1 -p {port} -t '{BASE}/#' -v      everything the gateway publishes
  mosquitto_pub -h 127.0.0.1 -p {port} -t {BASE}/send/bob -m 'Hello Bob'      a message to B
  type a line here                                            B sends it to the gateway
                                                              ("status" is a command)

Logs: service.log (the gateway), air.log (every packet on the air), A/, R1/ and B/node.log.
"""


async def chat(household: Household, seconds: float | None, *, typed: bool) -> None:
    """Until Ctrl-C or `seconds`: each line typed goes from B to the gateway. Standard input is
    read in a daemon thread, which a stop does not wait for."""
    loop = asyncio.get_running_loop()
    lines: asyncio.Queue[str] = asyncio.Queue()

    def read() -> None:
        for line in sys.stdin:
            loop.call_soon_threadsafe(lines.put_nowait, line)

    if typed:
        threading.Thread(target=read, name="stdin", daemon=True).start()
    async with App(household.bob) as bob:
        await bob.start()

        async def forward() -> None:
            while True:
                if text := (await lines.get()).strip():
                    sent = await bob.send_text(household.alice_key, text)
                    print(f"B sent {text!r}, acknowledged as {sent.expected_ack.hex()}", flush=True)

        sending = asyncio.create_task(forward(), name="B")
        try:
            if seconds is None:
                await asyncio.Event().wait()
            else:
                await asyncio.sleep(seconds)
        finally:
            sending.cancel()


async def prepare(household: Household) -> None:
    async with App(household.alice) as alice, App(household.bob) as bob:
        await household_ready(household, alice, bob)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="make firmware-play", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--directory",
        type=Path,
        help="where the nodes, the broker and the service keep their files (default: new)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=1883,
        help="the broker's port on 127.0.0.1, 0 for any free one (default 1883)",
    )
    parser.add_argument(
        "--hops",
        type=int,
        default=1,
        choices=range(3),
        help="repeaters between A and B (default 1)",
    )
    parser.add_argument("--for", dest="seconds", type=float, help="stop after this many seconds")
    parser.add_argument("--no-input", action="store_true", help="read no lines from standard input")
    args = parser.parse_args(argv)
    firmware = Firmware(
        BUILD / "bin" / "companion", BUILD / "bin" / "repeater", pinned_companion_version()
    )
    for binary in (firmware.companion, firmware.repeater):
        if not os.access(binary, os.X_OK):
            print(f"{binary} is missing: `make firmware` builds it", file=sys.stderr)
            return 2
    directory = args.directory or Path(tempfile.mkdtemp(prefix="meshcore-play-"))
    directory.mkdir(parents=True, exist_ok=True)
    port = args.port or free_port()
    with AirThread(directory / "air.log") as air:
        nodes = Nodes(firmware, air, directory)
        try:
            household = start(nodes, air, hops=args.hops)
            asyncio.run(prepare(household))
            with broker(directory, port), service(directory, configuration(household, port)):
                if not radio_connected(port):
                    print(
                        f"the gateway did not connect to A: {directory / 'service.log'}",
                        file=sys.stderr,
                    )
                    return 1
                print(instructions(directory, port), flush=True)
                with contextlib.suppress(KeyboardInterrupt):
                    asyncio.run(chat(household, args.seconds, typed=not args.no_input))
        finally:
            nodes.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
