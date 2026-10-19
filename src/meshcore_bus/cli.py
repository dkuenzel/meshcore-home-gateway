"""Command line: `meshcore-bus <command>` (design §6.8, docs/configuration.md).

Exit codes: 0 success, 1 failure (radio, broker, delivery), 78 configuration error (EX_CONFIG;
the systemd unit does not restart on it).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import json
import logging
import os
import signal
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from meshcore_bus import __version__
from meshcore_bus.config import (
    Config,
    ConfigError,
    MqttConfig,
    config_answers,
    load_config,
    render_config,
)

EX_OK, EX_FAILURE, EX_CONFIG = 0, 1, 78
CONNECT_TIMEOUT_S = 10.0
PUBLISH_TIMEOUT_S = 10.0
SUBSCRIBE_TIMEOUT_S = 5.0
DEFAULT_CONFIG = Path("/etc/meshcore-bus/config.toml")
DEFAULT_STATE_DIR = Path("/var/lib/meshcore-bus")
FINAL_STATES = {
    "confirmed",
    "failed",
    "expired",
    "duplicate",
    "rejected",
}  # plus "sent" for channels

log = logging.getLogger("meshcore_bus")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"meshcore-bus: configuration error: {exc}", file=sys.stderr)
        return EX_CONFIG
    except KeyboardInterrupt:
        return EX_FAILURE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshcore-bus",
        description="Bridge between a MeshCore companion radio and an MQTT message bus.",
    )
    parser.add_argument("--version", action="version", version=f"meshcore-bus {__version__}")
    commands = parser.add_subparsers(required=True, metavar="command")

    def command(
        name: str, help_text: str, handler: Any, config: bool = True
    ) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        if config:
            sub.add_argument(
                "--config", type=Path, default=DEFAULT_CONFIG, help="configuration file"
            )
        return sub

    run = command("run", "run the bridge (the systemd service does this)", _run)
    run.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="default: $STATE_DIRECTORY or /var/lib/meshcore-bus",
    )

    command("check-config", "validate the configuration and print a summary", _check_config)

    probe = command(
        "probe", "connect to a radio directly and list what it knows", _probe, config=False
    )
    where = probe.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--serial", metavar="DEVICE", help="serial device, e.g. /dev/serial/by-id/..."
    )
    where.add_argument("--tcp", metavar="HOST[:PORT]", help="WiFi companion, port 5000 by default")
    probe.add_argument("--list", choices=["contacts", "channels"], help="tab-separated list only")
    probe.add_argument("--json", action="store_true", help="everything as JSON")

    send = command("send", "send a text to a configured target through the broker", _send)
    send.add_argument("alias")
    send.add_argument("text")
    send.add_argument(
        "--wait", type=float, default=0.0, metavar="SECONDS",
        help="wait for the delivery result; exit 1 unless sent or confirmed",
    )  # fmt: skip

    listen = command("listen", "print events from the broker as JSON lines", _listen)
    listen.add_argument(
        "--topic", choices=["received", "command", "delivery", "state", "all"], default="all"
    )
    listen.add_argument("--count", type=int, default=0, help="stop after this many events")

    config = commands.add_parser("config", help="configuration helpers")
    config_commands = config.add_subparsers(required=True, metavar="action")
    render = config_commands.add_parser(
        "render", help="write a validated configuration from key=value answers"
    )
    render.set_defaults(handler=_render)
    render.add_argument("--answers", type=Path, required=True, help="file with key=value lines")
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--group", default=None, help="group that may read the file (mode 0640)")
    answers = config_commands.add_parser(
        "answers",
        help="print a configuration as key=value answers (the inverse of render; shows secrets)",
    )
    answers.set_defaults(handler=_answers)
    answers.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


# --- run ------------------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.diagnostics.log_level)
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    log.info("meshcore-bus %s starting, state in %s", __version__, state_dir)
    return asyncio.run(_serve(config, state_dir))


async def _serve(config: Config, state_dir: Path) -> int:
    from meshcore_bus.bridge import Bridge  # noqa: PLC0415 - keep CLI start-up light
    from meshcore_bus.mqtt.client import MqttBus  # noqa: PLC0415
    from meshcore_bus.mqtt.topics import Topics  # noqa: PLC0415
    from meshcore_bus.state import CommandLedger, JsonStateFile  # noqa: PLC0415

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    ha_status = f"{config.homeassistant.prefix}/status" if config.homeassistant.discovery else None
    bus = MqttBus(config.mqtt, topics, ha_status)
    ledger = CommandLedger(JsonStateFile(state_dir / "state.json"))
    bridge = Bridge(config, bus, lambda: transport_for(config), ledger)
    await bridge.run(stop)
    log.info("meshcore-bus stopped")
    return EX_OK


def transport_for(config: Config) -> Any:
    from meshcore_bus.radio.transport import SerialTransport, TcpTransport  # noqa: PLC0415

    radio = config.radio
    if radio.transport == "serial":
        assert radio.device is not None  # noqa: S101 - validated by the configuration
        return SerialTransport(radio.device)
    assert radio.host is not None  # noqa: S101 - validated by the configuration
    return TcpTransport(radio.host, radio.port)


def setup_logging(level: str) -> None:
    under_journal = "JOURNAL_STREAM" in os.environ  # the journal adds its own time stamps
    fmt = "%(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt if under_journal else "%(asctime)s " + fmt,
        stream=sys.stderr,
        force=True,
    )


# --- check-config ---------------------------------------------------------------------------


def _check_config(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    radio, mqtt, ha = config.radio, config.mqtt, config.homeassistant
    where = radio.device if radio.transport == "serial" else f"{radio.host}:{radio.port}"
    tls = " (TLS)" if mqtt.tls else ""
    discovery = f"on, prefix {ha.prefix}" if ha.discovery else "off"
    print(f"configuration {args.config}: valid")
    print(f"  radio      {radio.transport} {where}")
    print(f"  broker     {mqtt.host}:{mqtt.port}{tls}, topics {mqtt.topic_root}/...")
    print(f"  discovery  {discovery}")
    for target in config.targets:
        if target.channel_index is not None:
            print(f"  target     {target.alias}: channel {target.channel_index}")
        else:
            print(f"  target     {target.alias}: contact {target.public_key}")
    words, senders = sorted(config.commands.words), len(config.commands.senders)
    if words:
        print(f"  commands   {', '.join(words)} from {senders} sender(s)")
    airtime = config.airtime
    print(f"  airtime    {airtime.budget_s_per_hour:g} s per hour, burst {airtime.burst_s:g} s")
    return EX_OK


# --- probe ----------------------------------------------------------------------------------


def _probe(args: argparse.Namespace) -> int:
    from meshcore_bus.radio.transport import SerialTransport, TcpTransport  # noqa: PLC0415

    if args.serial:
        transport: Any = SerialTransport(args.serial)
    else:
        host, _, port = args.tcp.partition(":")
        transport = TcpTransport(host, int(port or 5000))
    try:
        report = asyncio.run(_probe_radio(transport))
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        print(f"meshcore-bus: probe failed: {exc}", file=sys.stderr)
        return EX_FAILURE
    if args.list == "channels":
        for channel in report["channels"]:
            print(f"{channel['index']}\t{_tab_safe(channel['name'])}")
    elif args.list == "contacts":
        for contact in report["contacts"]:
            print(f"{contact['public_key']}\t{_tab_safe(contact['name'])}\t{contact['type']}")
    elif args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        _print_report(report)
    return EX_OK


NODE_TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}


async def _probe_radio(transport: Any) -> dict[str, Any]:
    from meshcore_bus.radio.session import CompanionSession, RadioEvent  # noqa: PLC0415

    events: asyncio.Queue[RadioEvent] = asyncio.Queue()
    session = CompanionSession(transport, events, set_clock=False)
    try:
        info = await session.start()
    finally:
        await session.close()
    me, device = info.self_info, info.device
    return {
        "name": me.name,
        "public_key": info.public_key,
        "firmware": device.version,
        "firmware_code": device.firmware_code,
        "model": device.model,
        "build_date": device.build_date,
        "frequency_mhz": me.frequency_khz / 1000,
        "bandwidth_khz": me.bandwidth_hz / 1000,
        "spreading_factor": me.spreading_factor,
        "coding_rate": me.coding_rate,
        "tx_power_dbm": me.tx_power_dbm,
        "manual_add_contacts": me.manual_add_contacts,
        "battery_mv": session.battery.millivolts if session.battery else None,
        "contacts": [
            {
                "public_key": key,
                "name": rec.name,
                "type": NODE_TYPES.get(rec.node_type, str(rec.node_type)),
            }
            for key, rec in sorted(
                session.contact_records.items(), key=lambda kv: kv[1].name.lower()
            )
        ],
        "channels": [{"index": i, "name": n} for i, n in sorted(session.channels.items())],
    }


def _print_report(r: dict[str, Any]) -> None:
    firmware = f"{r['firmware']} (code {r['firmware_code']}), {r['model']}, built {r['build_date']}"
    lora = (
        f"{r['frequency_mhz']:.3f} MHz, {r['bandwidth_khz']:g} kHz, SF {r['spreading_factor']}, "
        f"CR 4/{r['coding_rate']}, {r['tx_power_dbm']} dBm"
    )
    adding = "added manually" if r["manual_add_contacts"] else "added automatically"
    print(f"radio      {r['name']}  {r['public_key']}")
    print(f"firmware   {firmware}")
    print(f"lora       {lora}")
    print(f"contacts   {adding}")
    for c in r["contacts"]:
        print(f"  {c['public_key']}  {c['type']:<8} {c['name']}")
    print("channels")
    for ch in r["channels"]:
        print(f"  {ch['index']:>3}  {ch['name']}")


def _tab_safe(text: str) -> str:
    """For tab-separated lists and dialogs: control characters (tabs, line breaks, terminal
    escapes) and Unicode line separators become spaces."""
    return "".join(" " if unicodedata.category(ch) in _UNSAFE else ch for ch in text)


_UNSAFE = frozenset({"Cc", "Zl", "Zp"})


# --- send and listen ------------------------------------------------------------------------


def _mqtt_client(mqtt: MqttConfig, suffix: str) -> Any:
    import dataclasses  # noqa: PLC0415

    from meshcore_bus.mqtt.client import make_paho_client, tls_context  # noqa: PLC0415

    client_id = f"meshcore-bus-{suffix}-{os.getpid()}"
    client = make_paho_client(dataclasses.replace(mqtt, client_id=client_id))
    if mqtt.username:
        client.username_pw_set(mqtt.username, mqtt.password)
    if mqtt.tls:
        client.tls_set_context(tls_context(mqtt))
    return client


class _Connection:
    """Tracks the broker's answers to CONNECT; `on_ready` runs after each accepted one."""

    def __init__(self, on_ready: Callable[[Any], None] | None = None) -> None:
        self.answered = threading.Event()
        self.refusal: str | None = None
        self._on_ready = on_ready

    def on_connect(
        self, client: Any, _userdata: Any, _flags: Any, reason_code: Any, _props: Any
    ) -> None:
        if reason_code.is_failure:
            self.refusal = str(reason_code)
        elif self._on_ready is not None:
            self._on_ready(client)
        self.answered.set()

    def open(self, client: Any, mqtt: MqttConfig) -> str | None:
        """Connect and wait for the answer; returns an error text, or None when connected."""
        client.on_connect = self.on_connect
        client.connect(mqtt.host, mqtt.port, mqtt.keepalive_s)
        client.loop_start()
        if not self.answered.wait(CONNECT_TIMEOUT_S):
            return "broker did not answer"
        if self.refusal is not None:
            return f"broker refused the connection: {self.refusal}"
        return None


def _matches(event_text: Any, wanted: str) -> bool:
    """Whether a delivery event carries the requested text, also when the bridge shortened it."""
    if not isinstance(event_text, str):
        return False
    if event_text == wanted:
        return True
    return event_text.endswith("...") and wanted.startswith(event_text[:-3])


def _send(args: argparse.Namespace) -> int:
    from meshcore_bus.core.text import TextPolicy, TextRejectedError, prepare_text  # noqa: PLC0415
    from meshcore_bus.mqtt.topics import Topics  # noqa: PLC0415

    config = load_config(args.config)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    target = next((t for t in config.targets if t.alias == args.alias), None)
    final = FINAL_STATES | ({"sent"} if target and target.channel_index is not None else set())
    try:  # the text as the bridge will report it, apart from shortening
        policy = TextPolicy(1 << 20, ascii_only=config.messages.ascii_only, truncate=False)
        wanted = prepare_text(args.text, policy)
    except TextRejectedError:
        wanted = args.text
    own_id: list[str] = []  # the bridge's id for this request, from its first event
    results: list[dict[str, Any]] = []
    done, subscribed = threading.Event(), threading.Event()

    def on_message(_c: Any, _u: Any, message: Any) -> None:
        try:
            event = json.loads(message.payload)
        except ValueError:
            return
        if not isinstance(event, dict) or event.get("target") != args.alias:
            return
        if not own_id:
            first = event.get("event_type") in {"queued", "duplicate", "rejected"}
            unnamed_rejection = event.get("event_type") == "rejected" and event.get("text") is None
            if not first or not (_matches(event.get("text"), wanted) or unnamed_rejection):
                return
            own_id.append(str(event.get("id")))
        if event.get("id") == own_id[0] and event.get("event_type") in final:
            results.append(event)
            done.set()

    client = _mqtt_client(config.mqtt, "send")
    client.on_message = on_message
    client.on_subscribe = lambda *_: subscribed.set()
    try:
        error = _Connection().open(client, config.mqtt)
        if error is None and args.wait > 0:
            client.subscribe(topics.delivery, qos=1)
            if not subscribed.wait(SUBSCRIBE_TIMEOUT_S):
                error = "broker did not confirm the subscription"
        if error is None:
            error = _publish(client, topics.send(args.alias), args.text.encode("utf-8"))
        if error is not None:
            print(f"meshcore-bus: {error}", file=sys.stderr)
            return EX_FAILURE
        if args.wait <= 0:
            return EX_OK
        if not done.wait(args.wait):
            print("meshcore-bus: no delivery result in time", file=sys.stderr)
            return EX_FAILURE
        event = results[0]
        print(json.dumps(event, ensure_ascii=False))
        return EX_OK if event["event_type"] in {"sent", "confirmed"} else EX_FAILURE
    finally:
        client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged messages
        client.loop_stop()


def _publish(client: Any, topic: str, payload: bytes) -> str | None:
    """Publish with QoS 1 and wait for the broker's acknowledgement; returns an error text."""
    info = client.publish(topic, payload, qos=1)
    try:
        info.wait_for_publish(PUBLISH_TIMEOUT_S)
    except (RuntimeError, ValueError) as exc:
        return f"request not sent: {exc}"
    return None if info.is_published() else "broker did not confirm the request"


def _listen(args: argparse.Namespace) -> int:
    from meshcore_bus.mqtt.topics import Topics  # noqa: PLC0415

    config = load_config(args.config)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    wanted = {
        "received": [topics.received], "command": [topics.command],
        "delivery": [topics.delivery], "state": [topics.state],
        "all": [topics.received, topics.command, topics.delivery, topics.state],
    }[args.topic]  # fmt: skip
    client = _mqtt_client(config.mqtt, "listen")
    seen = 0
    finished = threading.Event()

    def on_message(_c: Any, _u: Any, message: Any) -> None:
        nonlocal seen
        try:
            payload: Any = json.loads(message.payload)
        except ValueError:
            payload = message.payload.decode("utf-8", errors="replace")
        print(
            json.dumps({"topic": message.topic, "payload": payload}, ensure_ascii=False), flush=True
        )
        seen += 1
        if args.count and seen >= args.count:
            finished.set()

    def subscribe(c: Any) -> None:  # after every (re)connect: the session is clean
        c.subscribe([(topic, 1) for topic in wanted])

    connection = _Connection(on_ready=subscribe)
    client.on_message = on_message
    try:
        error = connection.open(client, config.mqtt)
        if error is not None:
            print(f"meshcore-bus: {error}", file=sys.stderr)
            return EX_FAILURE
        finished.wait()
    finally:
        client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged messages
        client.loop_stop()
    return EX_OK


# --- config render and answers --------------------------------------------------------------


def _render(args: argparse.Namespace) -> int:
    try:
        answers = args.answers.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {args.answers}: {exc.strerror}") from exc
    text = render_config(answers)
    write_private(args.output, text, args.group)
    print(f"wrote {args.output}")
    return EX_OK


def _answers(args: argparse.Namespace) -> int:
    try:
        text = args.config.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {args.config}: {exc.strerror}") from exc
    print(config_answers(text), end="")
    return EX_OK


def write_private(path: Path, text: str, group: str | None) -> None:
    """Write atomically with mode 0640; the file holds the broker password."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o640)
        if group is not None:
            os.chown(tmp, -1, grp.getgrnam(group).gr_gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
