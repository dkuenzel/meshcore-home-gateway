"""Command line: `meshcore-home-gateway <command>` (design §6.8, docs/configuration.md).

Exit codes: 0 success, 1 failure (radio, broker, delivery), 2 usage error (argparse), 78
configuration error (EX_CONFIG; the systemd unit does not restart on it).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import grp
import json
import logging
import math
import os
import signal
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import (
    Config,
    ConfigError,
    MqttConfig,
    config_answers,
    load_config,
    parse_config,
    read_text,
    render_config,
)

if TYPE_CHECKING:
    from meshcore_home_gateway.radio.transport import Transport

EX_OK, EX_FAILURE, EX_CONFIG = 0, 1, 78
CONNECT_TIMEOUT_S = 10.0
PUBLISH_TIMEOUT_S = 10.0
SUBSCRIBE_TIMEOUT_S = 5.0
DEFAULT_CONFIG = Path("/etc/meshcore-home-gateway/config.toml")
DEFAULT_STATE_DIR = Path("/var/lib/meshcore-home-gateway")
FINAL_STATES = {
    "confirmed",
    "failed",
    "expired",
    "duplicate",
    "rejected",
}  # plus "sent" for channels

log = logging.getLogger("meshcore_home_gateway")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except ConfigError as exc:
        print(f"meshcore-home-gateway: configuration error: {exc}", file=sys.stderr)
        return EX_CONFIG
    except KeyboardInterrupt:
        return EX_FAILURE


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshcore-home-gateway",
        description="Gateway between home automation (MQTT) and a MeshCore companion radio.",
    )
    parser.add_argument(
        "--version", action="version", version=f"meshcore-home-gateway {__version__}"
    )
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
        help="default: $STATE_DIRECTORY or /var/lib/meshcore-home-gateway",
    )

    check = command("check-config", "validate the configuration and print a summary", _check_config)
    check.add_argument(
        "--broker", action="store_true",
        help="also log in to the broker with the configured user name and password",
    )  # fmt: skip

    probe = command(
        "probe", "connect to a radio directly and list what it knows", _probe, config=False
    )
    where = probe.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--serial", metavar="DEVICE", help="serial device, e.g. /dev/serial/by-id/..."
    )
    where.add_argument(
        "--tcp", metavar="HOST[:PORT]", type=tcp_address,
        help="WiFi companion, port 5000 by default; IPv6 as [ADDRESS]:PORT",
    )  # fmt: skip
    probe.add_argument("--list", choices=["contacts", "channels"], help="tab-separated list only")
    probe.add_argument("--json", action="store_true", help="everything as JSON")

    send = command("send", "send a text to a configured target through the broker", _send)
    send.add_argument("alias")
    send.add_argument("text")
    send.add_argument(
        "--wait", type=_wait_seconds, default=0.0, metavar="SECONDS",
        help="wait for the delivery result; exit 1 unless sent or confirmed",
    )  # fmt: skip

    listen = command("listen", "print events from the broker as JSON lines", _listen)
    listen.add_argument(
        "--topic", choices=["received", "command", "delivery", "state", "all"], default="all"
    )
    listen.add_argument(
        "--count", type=_count, default=0, help="stop after this many events (0: never)"
    )

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
    log.info("meshcore-home-gateway %s starting, state in %s", __version__, state_dir)
    return asyncio.run(_serve(config, state_dir))


async def _serve(config: Config, state_dir: Path) -> int:
    from meshcore_home_gateway.bridge import Bridge  # noqa: PLC0415 - keep CLI start-up light
    from meshcore_home_gateway.mqtt.client import MqttBus  # noqa: PLC0415
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415
    from meshcore_home_gateway.state import (  # noqa: PLC0415
        AnnouncedComponents,
        CommandLedger,
        JsonStateFile,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    ha_status = config.homeassistant.status_topic if config.homeassistant.discovery else None
    bus = MqttBus(config.mqtt, topics, ha_status)
    store = JsonStateFile(state_dir / "state.json")
    bridge = Bridge(
        config, bus, lambda: transport_for(config), CommandLedger(store),
        components=AnnouncedComponents(store),
    )  # fmt: skip
    await bridge.run(stop)
    log.info("meshcore-home-gateway stopped")
    return EX_OK


def transport_for(config: Config) -> Transport:
    from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport  # noqa: PLC0415

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
    if mqtt.tls:  # the certificate and key files must load, as at start-up
        from meshcore_home_gateway.mqtt.client import tls_context  # noqa: PLC0415

        tls_context(mqtt)
    print(f"configuration {args.config}: valid")
    print(f"  radio      {radio.transport} {where}")
    login = f"user {mqtt.username!r}" if mqtt.username else "no user name"
    if mqtt.username:
        login += " with a password" if mqtt.password else " without a password"
    print(f"  broker     {mqtt.host}:{mqtt.port}{tls}, {login}, topics {mqtt.topic_root}/...")
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
    if not args.broker:
        return EX_OK
    client = _mqtt_client(mqtt, "check")  # another client id: the service may be connected
    connection = _Connection()
    try:
        error = connection.open(client, mqtt)
    finally:
        client.disconnect()
        client.loop_stop()
    if error is not None:
        print(f"meshcore-home-gateway: {error} ({login})", file=sys.stderr)
        if connection.refusal is not None:
            print(
                "meshcore-home-gateway: brokers such as Mosquitto refuse a wrong password, an "
                "unknown user and missing credentials alike; a standalone Mosquitto knows a user "
                "added with mosquitto_passwd only after 'systemctl reload mosquitto' "
                "(docs/deployment.md, troubleshooting)",
                file=sys.stderr,
            )
        return EX_FAILURE
    print(f"  login      accepted by {mqtt.host}:{mqtt.port}")
    return EX_OK


# --- probe ----------------------------------------------------------------------------------


def _probe(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport  # noqa: PLC0415

    if args.serial:
        transport: Any = SerialTransport(args.serial)
    else:
        transport = TcpTransport(*args.tcp)
    try:
        report = asyncio.run(_probe_radio(transport))
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        print(f"meshcore-home-gateway: probe failed: {exc}", file=sys.stderr)
        return EX_FAILURE
    if args.list == "channels":
        for channel in report["channels"]:
            print(f"{channel['index']}\t{_safe_text(channel['name'])}")
    elif args.list == "contacts":
        for contact in report["contacts"]:
            print(f"{contact['public_key']}\t{_safe_text(contact['name'])}\t{contact['type']}")
    elif args.json:
        print(_json_text(report, indent=2))
    else:
        _print_report(report)
    return EX_OK


NODE_TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}
DEFAULT_TCP_PORT = 5000


def tcp_address(text: str) -> tuple[str, int]:
    """HOST, HOST:PORT, [IPV6] or [IPV6]:PORT; a bare IPv6 address has no port."""
    host, port = text, ""
    if text.startswith("["):
        host, bracket, rest = text[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise argparse.ArgumentTypeError(f"expected [ADDRESS]:PORT, got {text!r}")
        port = rest[1:]
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    if not host:
        raise argparse.ArgumentTypeError(f"no host in {text!r}")
    if not port:
        return host, DEFAULT_TCP_PORT
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError(f"port must be 1..65535, got {port!r}")
    return host, int(port)


MAX_WAIT_S = 86_400.0


def _wait_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not 0 <= value <= MAX_WAIT_S:  # also refuses nan and inf
        raise argparse.ArgumentTypeError(f"expected 0 to {MAX_WAIT_S:.0f} seconds, got {text!r}")
    return value


def _count(text: str) -> int:
    if not text.isdecimal():
        raise argparse.ArgumentTypeError(f"expected a whole number, 0 or more, got {text!r}")
    return int(text)


async def _probe_radio(transport: Any) -> dict[str, Any]:
    from meshcore_home_gateway.radio.session import (  # noqa: PLC0415
        CompanionSession,
        RadioEvent,
        added_types,
        type_names,
    )

    events: asyncio.Queue[RadioEvent] = asyncio.Queue()
    session = CompanionSession(transport, events, set_clock=False)
    try:
        info = await session.start(fetch_messages=False)
    finally:
        await session.close()
    me, device, autoadd = info.self_info, info.device, session.autoadd
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
        "auto_add": {  # overwrite_oldest null: firmware 1.11 or older, no such setting
            "manual": me.manual_add_contacts,
            "types": type_names(added_types(me, autoadd)),
            "overwrite_oldest": None if autoadd is None else autoadd.overwrites_oldest,
        },
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
    auto = r["auto_add"]
    adding = "added manually" if auto["manual"] else "all added automatically"
    if auto["manual"] and auto["types"]:
        adding += f", but {', '.join(auto['types'])} automatically"
    if auto["types"] and auto["overwrite_oldest"]:
        adding += "; a full table overwrites the oldest"
    print(_safe_text(f"radio      {r['name']}  {r['public_key']}"))
    print(_safe_text(f"firmware   {firmware}"))
    print(f"lora       {lora}")
    print(f"contacts   {adding}")
    for c in r["contacts"]:
        print(_safe_text(f"  {c['public_key']}  {c['type']:<8} {c['name']}"))
    print("channels")
    for ch in r["channels"]:
        print(_safe_text(f"  {ch['index']:>3}  {ch['name']}"))


def _safe_text(text: str) -> str:
    """For tab-separated lists and dialogs: control characters (tabs, line breaks, terminal
    escapes), format characters (direction overrides) and Unicode line separators become
    spaces."""
    return "".join(" " if unicodedata.category(ch) in _UNSAFE else ch for ch in text)


def _json_text(value: Any, indent: int | None = None) -> str:
    """JSON for a terminal: readable letters stay, but the characters `_safe_text` replaces are
    written as \\u escapes, so texts from the mesh cannot steer the terminal. Still valid JSON:
    json.dumps escapes C0 controls itself, so a raw line break is indentation, and the rest
    only occurs inside strings."""
    text = json.dumps(value, ensure_ascii=False, indent=indent)
    return "".join(
        _escaped(ch) if ch != "\n" and unicodedata.category(ch) in _UNSAFE else ch for ch in text
    )


def _escaped(ch: str) -> str:
    code = ord(ch)
    if code <= 0xFFFF:
        return f"\\u{code:04x}"
    code -= 0x10000  # outside the basic plane: a UTF-16 surrogate pair, as JSON requires
    return f"\\u{0xD800 + (code >> 10):04x}\\u{0xDC00 + (code & 0x3FF):04x}"


_UNSAFE = frozenset({"Cc", "Cf", "Zl", "Zp"})


# --- send and listen ------------------------------------------------------------------------


def _mqtt_client(mqtt: MqttConfig, suffix: str) -> Any:
    import dataclasses  # noqa: PLC0415

    from meshcore_home_gateway.mqtt.client import make_paho_client, tls_context  # noqa: PLC0415

    client_id = f"meshcore-home-gateway-{suffix}-{os.getpid()}"
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
        try:
            client.connect(mqtt.host, mqtt.port, mqtt.keepalive_s)
        except OSError as exc:  # refused, unreachable, unknown host name, TLS failure
            return f"cannot connect to the broker {mqtt.host}:{mqtt.port}: {exc}"
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


class _DeliveryWatch:
    """Follows the delivery events of one send request. The bridge names the request in its first
    event (queued, duplicate or rejected) with its own id and the final text; the result is the
    final event with that id. Events of other requests to the same target are ignored."""

    def __init__(self, alias: str, wanted: str, final: set[str]) -> None:
        self._alias, self._wanted, self._final = alias, wanted, final
        self._id: str | None = None
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()

    def on_message(self, _c: Any, _u: Any, message: Any) -> None:
        try:
            event = json.loads(message.payload)
        except (ValueError, RecursionError):  # also nested too deeply; it would end paho's thread
            return
        if not isinstance(event, dict) or event.get("target") != self._alias:
            return
        if self._id is None:
            first = event.get("event_type") in {"queued", "duplicate", "rejected"}
            unnamed_rejection = event.get("event_type") == "rejected" and event.get("text") is None
            if not first or not (_matches(event.get("text"), self._wanted) or unnamed_rejection):
                return
            self._id = str(event.get("id"))
        if event.get("id") == self._id and event.get("event_type") in self._final:
            self.result = event
            self.done.set()


def _send(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.core.text import (  # noqa: PLC0415
        TextPolicy,
        TextRejectedError,
        prepare_text,
    )
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

    config = load_config(args.config)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    target = next((t for t in config.targets if t.alias == args.alias), None)
    if target is None:  # the bridge would reject it; also keeps wildcards out of the topic
        print(f"meshcore-home-gateway: no target {args.alias!r} in {args.config}", file=sys.stderr)
        return EX_FAILURE
    try:  # bytes that are not UTF-8 reach argv as surrogates (PEP 383)
        payload = args.text.encode("utf-8")
    except UnicodeEncodeError:
        print("meshcore-home-gateway: the text is not valid UTF-8", file=sys.stderr)
        return EX_FAILURE
    final = FINAL_STATES | ({"sent"} if target.channel_index is not None else set())
    try:  # the text as the bridge will report it, apart from shortening
        policy = TextPolicy(1 << 20, ascii_only=config.messages.ascii_only, truncate=False)
        wanted = prepare_text(args.text, policy)
    except TextRejectedError:
        wanted = args.text
    watch = _DeliveryWatch(args.alias, wanted, final)
    client = _mqtt_client(config.mqtt, "send")
    client.on_message = watch.on_message
    try:
        error = _Connection().open(client, config.mqtt)
        if error is None and args.wait > 0:
            error = _subscribe(client, topics.delivery)
        if error is None:
            error = _publish(client, topics.send(args.alias), payload)
        if error is not None:
            print(f"meshcore-home-gateway: {error}", file=sys.stderr)
            return EX_FAILURE
        if args.wait <= 0:
            return EX_OK
        if not watch.done.wait(args.wait) or watch.result is None:
            print("meshcore-home-gateway: no delivery result in time", file=sys.stderr)
            return EX_FAILURE
        print(_json_text(watch.result))
        return EX_OK if watch.result["event_type"] in {"sent", "confirmed"} else EX_FAILURE
    finally:
        client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged messages
        client.loop_stop()


def _subscribe(client: Any, topic: str) -> str | None:
    """Subscribe with QoS 1 and wait for the broker's answer; returns an error text."""
    answered = threading.Event()
    refused: list[str] = []

    def on_subscribe(_c: Any, _u: Any, _mid: Any, reason_codes: Any, _p: Any) -> None:
        refused.extend(str(code) for code in reason_codes if code.is_failure)
        answered.set()

    client.on_subscribe = on_subscribe
    client.subscribe(topic, qos=1)
    if not answered.wait(SUBSCRIBE_TIMEOUT_S):
        return "broker did not confirm the subscription"
    if refused:
        return f"broker refused the subscription to {topic}: {refused[0]}"
    return None


def _publish(client: Any, topic: str, payload: bytes) -> str | None:
    """Publish with QoS 1 and wait for the broker's acknowledgement; returns an error text."""
    info = client.publish(topic, payload, qos=1)
    try:
        info.wait_for_publish(PUBLISH_TIMEOUT_S)
    except (RuntimeError, ValueError) as exc:
        return f"request not sent: {exc}"
    return None if info.is_published() else "broker did not confirm the request"


def _listen(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

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
    failures: list[Exception] = []

    def on_message(_c: Any, _u: Any, message: Any) -> None:
        nonlocal seen
        if finished.is_set():  # paho may deliver a few more before the disconnect
            return
        try:
            payload: Any = json.loads(message.payload)
        except (ValueError, RecursionError):  # also nested too deeply; it would end paho's thread
            payload = message.payload.decode("utf-8", errors="replace")
        try:
            print(_json_text({"topic": message.topic, "payload": payload}), flush=True)
        except Exception as exc:  # noqa: BLE001 - e.g. a closed pipe: end, do not hang
            failures.append(exc)
            finished.set()
            return
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
            print(f"meshcore-home-gateway: {error}", file=sys.stderr)
            return EX_FAILURE
        finished.wait()
    finally:
        client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged messages
        client.loop_stop()
    if failures:
        if isinstance(failures[0], BrokenPipeError):  # the reader went away (`| head`): quietly
            with contextlib.suppress(OSError):
                os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        else:
            print(f"meshcore-home-gateway: {failures[0]}", file=sys.stderr)
        return EX_FAILURE
    return EX_OK


# --- config render and answers --------------------------------------------------------------


def _render(args: argparse.Namespace) -> int:
    text = render_config(read_text(args.answers))
    mqtt = parse_config(text).mqtt
    if mqtt.tls:  # the certificate and key files must load, as in check-config and at start-up
        from meshcore_home_gateway.mqtt.client import tls_context  # noqa: PLC0415

        tls_context(mqtt)
    try:
        gid = None if args.group is None else grp.getgrnam(args.group).gr_gid
    except KeyError:
        print(f"meshcore-home-gateway: no group {args.group!r}", file=sys.stderr)
        return EX_FAILURE
    try:
        write_private(args.output, text, gid)
    except OSError as exc:
        print(
            f"meshcore-home-gateway: cannot write {args.output}: {exc.strerror or exc}",
            file=sys.stderr,
        )
        return EX_FAILURE
    print(f"wrote {args.output}")
    return EX_OK


def _answers(args: argparse.Namespace) -> int:
    print(config_answers(read_text(args.config)), end="")
    return EX_OK


def write_private(path: Path, text: str, gid: int | None) -> None:
    """Write atomically and durably (fsync, rename, fsync of the directory) with mode 0640,
    readable by group `gid`; the file holds the broker password."""
    _make_directory(path.parent)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o640)
        if gid is not None:
            os.chown(tmp, -1, gid)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _make_directory(directory: Path) -> None:
    """Create `directory` and its missing parents with mode 0755 whatever the umask: the service
    user must reach the configuration. Existing directories keep their mode."""
    if directory.is_dir():
        return
    _make_directory(directory.parent)
    try:
        directory.mkdir()
    except FileExistsError:
        if directory.is_dir():  # created meanwhile
            return
        raise
    os.chmod(directory, 0o755)  # noqa: S103 - the file in it is 0640, the secret is there


if __name__ == "__main__":
    raise SystemExit(main())
