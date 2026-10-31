"""Command line: `meshcore-home-gateway <command>` (design §6.8, docs/configuration.md).

Exit codes: 0 success, 1 failure (radio, broker, delivery), 2 usage error (argparse), 78
configuration error (EX_CONFIG; the systemd unit does not restart on it).

Texts for people follow the language of the environment, except those of `run`, which go to the
journal (ADR 0019); every text is a whole message marked with tr(), tr_help() or trn(). Machine
output (lists, JSON, `config answers`, `--version`) is the same in every language; the dashboard
follows [homeassistant] language, as the names in Home Assistant do.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import dataclasses
import grp
import json
import logging
import math
import os
import re
import signal
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.config import (
    Config,
    ConfigError,
    MqttConfig,
    ProfileConfig,
    config_answers,
    load_config,
    parse_config,
    read_text,
    render_config,
)
from meshcore_home_gateway.i18n import N_
from meshcore_home_gateway.protocol.codes import ContactFlag, TelemetryMode

if TYPE_CHECKING:
    from meshcore_home_gateway.core.profile import Plan
    from meshcore_home_gateway.radio.transport import Transport
    from meshcore_home_gateway.state import ProfileRecords

EX_OK, EX_FAILURE, EX_USAGE, EX_CONFIG = 0, 1, 2, 78
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

PREFIX = "meshcore-home-gateway: "  # before a message, never inside one: the installer strips it

log = logging.getLogger("meshcore_home_gateway")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # the service writes into the journal: English whatever the environment asks for, its
    # usage errors too, so decided before parsing (docs/configuration.md)
    language = "en" if arguments[:1] == ["run"] else i18n.choose(os.environ)
    with _speaking(i18n.translator(language)):
        args = _parser().parse_args(arguments)
        try:
            return int(args.handler(args))
        except ConfigError as exc:
            print(PREFIX + tr("configuration error: {detail}", detail=exc.text), file=sys.stderr)
            return EX_CONFIG
        except KeyboardInterrupt:
            return EX_FAILURE


@dataclasses.dataclass
class _Language:
    """The language of the command line's texts: one per process, so that texts made in paho's
    network thread follow it too."""

    translator: i18n.Translator


_LANGUAGE = _Language(i18n.ENGLISH)  # main() sets it for one call; English outside main()
_ARGPARSE_WORDS = ("_", "ngettext")  # argparse's gettext functions, which it reads as globals


@contextlib.contextmanager
def _speaking(translator: i18n.Translator) -> Iterator[None]:
    """tr(), tr_help(), trn() and argparse's own words in this language until the block ends,
    also when argparse ends it with SystemExit. A Python whose argparse lacks the module
    attributes keeps argparse's words English (ADR 0019)."""
    names = vars(argparse)
    saved = {name: names[name] for name in _ARGPARSE_WORDS if name in names}
    before, _LANGUAGE.translator = _LANGUAGE.translator, translator
    if len(saved) == len(_ARGPARSE_WORDS):
        words, plurals = translator.argparse_words()
        names.update(_=words, ngettext=plurals)
    try:
        yield
    finally:
        _LANGUAGE.translator = before
        names.update(saved)


def tr(msgid: str, /, **values: object) -> str:
    """A text for people in the command line's language, with its values; English where the
    catalog has no usable translation."""
    return _LANGUAGE.translator.text(msgid, **values)


def tr_help(msgid: str, /) -> str:
    """A help or description text: argparse %-formats it, so it is never filled, and its
    translation is used only with the %-directives of the English text."""
    return _LANGUAGE.translator.argparse_text(msgid)


def trn(singular: str, plural: str, n: int, /, **values: object) -> str:
    """The form of a text for the count n, which fills {count}."""
    return _LANGUAGE.translator.plural(singular, plural, n, **values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meshcore-home-gateway",
        description=tr_help(
            "Gateway between home automation (MQTT) and a MeshCore companion radio."
        ),
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
                "--config", type=Path, default=DEFAULT_CONFIG, help=tr_help("configuration file")
            )
        return sub

    run = command("run", tr_help("run the bridge (the systemd service does this)"), _run)
    run.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=tr_help("default: $STATE_DIRECTORY or /var/lib/meshcore-home-gateway"),
    )

    check = command(
        "check-config", tr_help("validate the configuration and print a summary"), _check_config
    )
    check.add_argument(
        "--broker", action="store_true",
        help=tr_help("also log in to the broker with the configured user name and password"),
    )  # fmt: skip

    probe = command(
        "probe", tr_help("connect to a radio directly and list what it knows"), _probe,
        config=False,
    )  # fmt: skip
    where = probe.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--serial", metavar="DEVICE", help=tr_help("serial device, e.g. /dev/serial/by-id/...")
    )
    where.add_argument(
        "--tcp", metavar="HOST[:PORT]", type=tcp_address,
        help=tr_help("WiFi companion, port 5000 by default; IPv6 as [ADDRESS]:PORT"),
    )  # fmt: skip
    probe.add_argument(
        "--list", choices=["contacts", "channels", "profile"],
        help=tr_help("tab-separated list only"),
    )  # fmt: skip
    probe.add_argument("--json", action="store_true", help=tr_help("everything as JSON"))
    probe.add_argument(
        "--config", type=Path, default=None,
        help=tr_help(
            "show what the [radio.profile] of this configuration changes (default: the "
            "observer profile without a name)"
        ),
    )  # fmt: skip
    probe.add_argument(
        "--state-dir", type=Path, default=None,
        help=tr_help(
            "the service's state, read only, for its exact plan (default: $STATE_DIRECTORY or "
            "/var/lib/meshcore-home-gateway)"
        ),
    )  # fmt: skip

    send = command("send", tr_help("send a text to a configured target through the broker"), _send)
    send.add_argument("alias")
    send.add_argument("text")
    send.add_argument(
        "--wait", type=_wait_seconds, default=0.0, metavar="SECONDS",
        help=tr_help("wait for the delivery result; exit 1 unless sent or confirmed"),
    )  # fmt: skip

    listen = command("listen", tr_help("print events from the broker as JSON lines"), _listen)
    listen.add_argument(
        "--topic", default="all",
        choices=["received", "command", "delivery", "state", "contact", "node", "telemetry", "all"],
    )  # fmt: skip
    listen.add_argument(
        "--count", type=_count, default=0, help=tr_help("stop after this many events (0: never)")
    )

    _contacts_parser(commands)

    dashboard = command(
        "dashboard",
        tr_help("write a Home Assistant dashboard for this gateway (YAML, to standard output)"),
        _dashboard,
    )
    dashboard.add_argument(
        "--state-dir", type=Path, default=None,
        help=tr_help(
            "the service's state, read only, for the contacts on the map (default: "
            "$STATE_DIRECTORY or /var/lib/meshcore-home-gateway)"
        ),
    )  # fmt: skip

    config = commands.add_parser("config", help=tr_help("configuration helpers"))
    config_commands = config.add_subparsers(required=True, metavar="action")
    render = config_commands.add_parser(
        "render", help=tr_help("write a validated configuration from key=value answers")
    )
    render.set_defaults(handler=_render)
    render.add_argument(
        "--answers", type=Path, required=True, help=tr_help("file with key=value lines")
    )
    render.add_argument("--output", type=Path, required=True)
    render.add_argument(
        "--group", default=None, help=tr_help("group that may read the file (mode 0640)")
    )
    answers = config_commands.add_parser(
        "answers",
        help=tr_help(
            "print a configuration as key=value answers (the inverse of render; shows secrets)"
        ),
    )
    answers.set_defaults(handler=_answers)
    answers.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    return parser


def _contacts_parser(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    contacts = commands.add_parser(
        "contacts",
        help=tr_help(
            "list, add and remove the radio's contacts, and exclude companions from telemetry, "
            "through the broker"
        ),
    )
    contacts_commands = contacts.add_subparsers(required=True, metavar="action")

    def contacts_command(name: str, help_text: str, handler: Any) -> argparse.ArgumentParser:
        sub = contacts_commands.add_parser(name, help=help_text, description=help_text)
        sub.set_defaults(handler=handler)
        sub.add_argument(
            "--config", type=Path, default=DEFAULT_CONFIG, help=tr_help("configuration file")
        )
        return sub

    listing = contacts_command(
        "list", tr_help("list the contacts and the nodes heard"), _contacts_list
    )
    listing.add_argument("--json", action="store_true", help=tr_help("both documents as JSON"))
    wait_help = tr_help(
        "wait this long for the result; 0: only send the request (default 30: the gateway reads "
        "the radio's table before and after the change, several seconds for a large table)"
    )
    add = contacts_command("add", tr_help("add a contact to the radio"), _contacts_add)
    add.add_argument(
        "contact",
        help=tr_help(
            "a public key (64 hex digits), a heard node as KEY12…KEY8 (the first 12 and the last "
            "8 digits of its key, the name after them optional, as Home Assistant lists it), a "
            "contact link or a contact card"
        ),
    )
    add.add_argument("--wait", type=_wait_seconds, default=30.0, metavar="SECONDS", help=wait_help)
    remove = contacts_command(
        "remove", tr_help("remove a contact from the radio"), _contacts_remove
    )
    remove.add_argument(
        "contact", help=tr_help("its public key, or KEY12…KEY8 (a name may follow)")
    )
    remove.add_argument(
        "--wait", type=_wait_seconds, default=30.0, metavar="SECONDS", help=wait_help
    )
    contacts_command(
        "link", tr_help("print the gateway's contact link (for a QR code)"), _contacts_link
    )
    telemetry = contacts_command(
        "telemetry",
        tr_help("ask a companion for its telemetry, or not (exclude it)"),
        _contacts_telemetry,
    )
    telemetry.add_argument(
        "contact",
        help=tr_help(
            "its public key, also of a contact not on the radio yet, or KEY12…KEY8 (a name may "
            "follow)"
        ),
    )
    telemetry.add_argument(
        "state", choices=["on", "off"], help=tr_help("off: excluded; on: asked again")
    )
    telemetry.add_argument(
        "--forget", action="store_true",
        help=tr_help(
            "with off: also delete its readings and its positions from telemetry from the "
            "gateway's history"
        ),
    )  # fmt: skip
    telemetry.add_argument(
        "--wait", type=_wait_seconds, default=30.0, metavar="SECONDS",
        help=tr_help("wait this long for the result; 0: only send the request (default 30)"),
    )  # fmt: skip


# --- run ------------------------------------------------------------------------------------


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.diagnostics.log_level)
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    log.info("meshcore-home-gateway %s starting, state in %s", __version__, state_dir)
    return asyncio.run(_serve(config, state_dir))


async def _serve(config: Config, state_dir: Path) -> int:
    from meshcore_home_gateway.bridge import (  # noqa: PLC0415 - light start
        Bridge,
        ha_device_topics,
        protected_keys,
        protected_names,
    )
    from meshcore_home_gateway.history import History, Retention  # noqa: PLC0415
    from meshcore_home_gateway.mqtt.client import MqttBus  # noqa: PLC0415
    from meshcore_home_gateway.mqtt.contact_devices import ContactDevices  # noqa: PLC0415
    from meshcore_home_gateway.mqtt.manage_entities import OBJECT_ID  # noqa: PLC0415
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415
    from meshcore_home_gateway.observer import Observer  # noqa: PLC0415
    from meshcore_home_gateway.radio.profile import RadioProfile  # noqa: PLC0415
    from meshcore_home_gateway.state import (  # noqa: PLC0415
        EXCLUSIONS_FILE,
        AdvertChecks,
        AnnouncedComponents,
        CommandLedger,
        ContactDeviceRecords,
        CurrentDocuments,
        JsonStateFile,
        ProfileRecords,
        RetainedKeys,
        TelemetryExclusions,
        TelemetryRecords,
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    ha = config.homeassistant
    ha_status = ha.status_topic if ha.discovery else None
    bus = MqttBus(config.mqtt, topics, ha_status, ha_devices=ha_device_topics(config))
    store = JsonStateFile(state_dir / "state.json")
    history = None
    if config.history.enabled:
        days = config.history
        retention = Retention(
            days.messages_days, days.positions_days, readings_days=days.readings_days
        )
        history = History.open(state_dir / "history.sqlite3", retention)
    protected = protected_keys(config)
    exclusions = TelemetryExclusions(state_dir / EXCLUSIONS_FILE)  # also while telemetry is off
    observer = Observer(
        bus, topics, history=history, retained=RetainedKeys(store, "contacts"),
        protected=protected, exclusions=exclusions, current=CurrentDocuments(store),
        advert_checks=AdvertChecks(store),
    )  # fmt: skip
    profile = RadioProfile(
        config.radio.profile, ProfileRecords(store), protected=protected_names(config)
    )
    devices = None
    if ha.discovery:  # also with no devices allowed: those of an earlier run are removed
        records = ContactDeviceRecords(store)
        devices = ContactDevices(config, topics, bus, records, protected=protected)
    bridge = Bridge(
        config, bus, lambda: transport_for(config), CommandLedger(store),
        components=AnnouncedComponents(store), observer=observer, devices=devices,
        profile=profile, manage_components=AnnouncedComponents(store, OBJECT_ID),
        telemetry_records=TelemetryRecords(store),
    )  # fmt: skip
    try:
        await bridge.run(stop)
    finally:
        if history is not None:
            history.close()
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
    mqtt = config.mqtt
    if mqtt.tls:  # the certificate and key files must load, as at start-up
        from meshcore_home_gateway.mqtt.client import tls_context  # noqa: PLC0415

        tls_context(mqtt)
    login = _login(mqtt)
    print(tr("configuration {path}: valid", path=args.config))
    for line in _summary(config, login):
        print(line)
    from meshcore_home_gateway.bridge import protected_look_alikes  # noqa: PLC0415

    for warning in protected_look_alikes(config):
        print(PREFIX + tr("warning: {detail}", detail=warning), file=sys.stderr)
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
        # Translators: why the broker did not take the login, then who logged in
        print(PREFIX + tr("{detail} ({login})", detail=error, login=login), file=sys.stderr)
        if connection.refusal is not None:
            hint = tr(
                "brokers such as Mosquitto refuse a wrong password, an unknown user and missing "
                "credentials alike; a standalone Mosquitto knows a user added with "
                "mosquitto_passwd only after '{command}' ({guide})",
                command="systemctl reload mosquitto",
                guide="docs/deployment.md, troubleshooting",
            )
            print(PREFIX + hint, file=sys.stderr)
        return EX_FAILURE
    # Translators: a line of check-config's summary, its value in the column of the others
    print(tr("  login      accepted by {host}:{port}", host=mqtt.host, port=mqtt.port))
    return EX_OK


def _login(mqtt: MqttConfig) -> str:
    """Who logs in to the broker, as check-config names it; never the password."""
    if not mqtt.username:
        return tr("no user name")
    if mqtt.password:
        return tr("user {user!r} with a password", user=mqtt.username)
    return tr("user {user!r} without a password", user=mqtt.username)


def _summary(config: Config, login: str) -> list[str]:
    """check-config's summary, a whole text per line."""
    radio, mqtt, ha = config.radio, config.mqtt, config.homeassistant
    where = radio.device if radio.transport == "serial" else f"{radio.host}:{radio.port}"
    broker = {"host": mqtt.host, "port": mqtt.port, "login": login, "root": mqtt.topic_root}
    language = i18n.LANGUAGES.get(ha.language, ha.language)  # in its own language
    words, senders = ", ".join(sorted(config.commands.words)), len(config.commands.senders)
    airtime = config.airtime
    # Translators: a line of check-config's summary. Its value starts in the column where the
    # other lines start theirs (12 in English): pad the label with spaces to keep one column.
    return [
        tr("  radio      {transport} {where}", transport=radio.transport, where=where),
        tr("  broker     {host}:{port} (TLS), {login}, topics {root}/...", **broker)
        if mqtt.tls
        else tr("  broker     {host}:{port}, {login}, topics {root}/...", **broker),
        tr("  discovery  on, prefix {prefix}, names in {language}", prefix=ha.prefix,
           language=language)
        if ha.discovery
        else tr("  discovery  off"),
        *(
            tr("  target     {alias}: channel {index}", alias=target.alias,
               index=target.channel_index)
            if target.channel_index is not None
            else tr("  target     {alias}: contact {key}", alias=target.alias,
                    key=target.public_key)
            for target in config.targets
        ),
        *(
            [trn("  commands   {words} from {count} sender",
                 "  commands   {words} from {count} senders", senders, words=words)]
            if words
            else []
        ),
        tr("  airtime    {budget:g} s per hour, burst {burst:g} s",
           budget=airtime.budget_s_per_hour, burst=airtime.burst_s),
        tr("  contacts   managed on request")
        if config.contacts.manage
        else tr("  contacts   not managed"),
        tr("  telemetry  asked of every companion not excluded")
        if config.telemetry.enabled
        else tr("  telemetry  off"),
    ]  # fmt: skip


# --- dashboard ------------------------------------------------------------------------------


def _dashboard(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.dashboard import dashboard  # noqa: PLC0415

    config = load_config(args.config)
    if not config.homeassistant.discovery:
        off = tr(
            "Home Assistant discovery is off ([homeassistant] discovery = false): the dashboard "
            "would find no entities."
        )
        print(PREFIX + off, file=sys.stderr)
        return EX_FAILURE
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    text, count = dashboard(config, state_dir)
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))  # whatever the terminal's encoding
    sys.stdout.buffer.flush()
    print(
        trn(
            "The map lists {count} contact with a position.",
            "The map lists {count} contacts with a position.",
            count,
        ),
        file=sys.stderr,
    )
    return EX_OK


# --- probe ----------------------------------------------------------------------------------


def _probe(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.bridge import protected_names  # noqa: PLC0415
    from meshcore_home_gateway.radio.transport import SerialTransport, TcpTransport  # noqa: PLC0415
    from meshcore_home_gateway.state import JsonStateFile, ProfileRecords  # noqa: PLC0415

    if args.serial:
        transport: Any = SerialTransport(args.serial)
    else:
        transport = TcpTransport(*args.tcp)
    config = load_config(args.config) if args.config else None
    profile = config.radio.profile if config else ProfileConfig()
    # the contacts the service keeps on the radio (targets, command senders), as it does
    protected = protected_names(config) if config and profile.protect_contacts else {}
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    records = ProfileRecords(JsonStateFile(state_dir / "state.json", read_only=True))
    try:
        report = asyncio.run(
            _probe_radio(transport, profile, records, protected, configured=config is not None)
        )
    except Exception as exc:  # noqa: BLE001 - report any failure to the operator
        print(PREFIX + tr("probe failed: {detail}", detail=exc), file=sys.stderr)
        return EX_FAILURE
    if args.list == "channels":
        for channel in report["channels"]:
            print(f"{channel['index']}\t{_safe_text(channel['name'])}")
    elif args.list == "contacts":
        for contact in report["contacts"]:
            print(f"{contact['public_key']}\t{_safe_text(contact['name'])}\t{contact['type']}")
    elif args.list == "profile":  # for the installer: the name, and what the service changes
        profile = report["profile"]
        print(f"name\t{_safe_text(report['name'])}")
        print(f"tested\t{'yes' if profile['firmware_tested'] else 'no'}")
        print(f"writes\t{'yes' if profile['writes'] else 'no'}")
        for name, change in profile["changes"].items():
            live, wanted = _json_text(change["live"]), _json_text(change["profile"])
            print(f"change\t{name}\t{live}\t{wanted}\t{change['action']}")
        for key, contact in profile["contacts"].items():
            # the name last: it may be empty, and a shell's read joins tabs in a row
            print(f"contact\t{key}\t{contact['action']}\t{_safe_text(contact['name'])}")
    elif args.json:
        print(_json_text(report, indent=2))
    else:
        _print_report(report)
    return EX_OK


NODE_TYPES = {1: "chat", 2: "repeater", 3: "room", 4: "sensor"}
ADVERT_LOCATION = {0: "none", 1: "share"}  # adverts without or with the radio's position
_FLAG_NAMES = {
    ContactFlag.FAVOURITE: "favourite",
    ContactFlag.TELEMETRY_BASE: "telemetry_base",
    ContactFlag.TELEMETRY_LOCATION: "telemetry_location",
    ContactFlag.TELEMETRY_ENVIRONMENT: "telemetry_environment",
    ContactFlag.REMOTE_CLI: "remote_cli",
}


def _mode_name(mode: TelemetryMode | int) -> str:
    return mode.name.lower() if isinstance(mode, TelemetryMode) else str(mode)


def _flag_names(flags: int) -> list[str]:
    """Names of a contact's flags; unknown bits as one hex value."""
    names = [name for flag, name in _FLAG_NAMES.items() if flags & flag]
    unknown = flags & ~sum(_FLAG_NAMES)
    return [*names, f"0x{unknown:02x}"] if unknown else names


DEFAULT_TCP_PORT = 5000


def tcp_address(text: str) -> tuple[str, int]:
    """HOST, HOST:PORT, [IPV6] or [IPV6]:PORT; a bare IPv6 address has no port."""
    host, port = text, ""
    if text.startswith("["):
        host, bracket, rest = text[1:].partition("]")
        if not bracket or (rest and not rest.startswith(":")):
            raise argparse.ArgumentTypeError(tr("expected [ADDRESS]:PORT, got {text!r}", text=text))
        port = rest[1:]
    elif text.count(":") == 1:
        host, _, port = text.partition(":")
    if not host:
        raise argparse.ArgumentTypeError(tr("no host in {text!r}", text=text))
    if not port:
        return host, DEFAULT_TCP_PORT
    if not port.isdecimal() or not 1 <= int(port) <= 65535:
        raise argparse.ArgumentTypeError(tr("port must be 1..65535, got {port!r}", port=port))
    return host, int(port)


MAX_WAIT_S = 86_400.0


def _wait_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        value = math.nan
    if not 0 <= value <= MAX_WAIT_S:  # also refuses nan and inf
        raise argparse.ArgumentTypeError(
            tr("expected 0 to {limit} seconds, got {text!r}", limit=int(MAX_WAIT_S), text=text)
        )
    return value


def _count(text: str) -> int:
    if not text.isdecimal():
        raise argparse.ArgumentTypeError(
            tr("expected a whole number, 0 or more, got {text!r}", text=text)
        )
    return int(text)


async def _probe_radio(
    transport: Any,
    profile: ProfileConfig,
    records: ProfileRecords,
    protected: Mapping[str, str],
    *,
    configured: bool,
) -> dict[str, Any]:
    from meshcore_home_gateway.core import profile as policy  # noqa: PLC0415
    from meshcore_home_gateway.radio.profile import live_values  # noqa: PLC0415
    from meshcore_home_gateway.radio.session import (  # noqa: PLC0415
        CompanionSession,
        RadioEvent,
        added_types,
        type_names,
    )
    from meshcore_home_gateway.views import SETTING_NAMES, setting_value  # noqa: PLC0415

    events: asyncio.Queue[RadioEvent] = asyncio.Queue()
    session = CompanionSession(transport, events, set_clock=False)
    try:
        info = await session.start(fetch_messages=False)
    finally:
        await session.close()
    me, device, autoadd, tuning = info.self_info, info.device, session.autoadd, session.tuning
    live = live_values(session)
    want = policy.desired(
        me.frequency_khz, name=profile.name, duty_cycle_percent=profile.duty_cycle_percent
    )
    mode = policy.Mode(profile.manage)
    tested = device.firmware_code in policy.TESTED_FIRMWARE
    # the service's own reconcile, with what it stored for this radio: the exact plan
    plan = policy.plan(
        want, records.load(info.public_key), live, mode=mode, firmware_code=device.firmware_code
    )
    changes = {
        SETTING_NAMES[key]: {
            "live": setting_value(key, live[key]), "profile": setting_value(key, value),
            "action": _profile_action(key, plan), "reason": plan.reasons.get(key),
        }
        for key, value in want.values.items()
        if live.get(key) is not None and not policy.same(key, live[key], value)
    }  # fmt: skip
    contacts = {}  # the protected contacts the service adds or makes favourites
    for key, name in sorted(protected.items()):
        record = session.contact_records.get(key)
        if record is not None and record.flags & ContactFlag.FAVOURITE:
            continue
        if plan.observing:
            action = "missing" if record is None else "not_favourite"
        else:
            action = "add" if record is None else "favourite"
        contacts[key] = {"name": name if record is None else record.name, "action": action}
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
        "position": None if me.position is None else dataclasses.asdict(me.position),
        "advert_location": ADVERT_LOCATION.get(me.advert_location, str(me.advert_location)),
        "telemetry": {
            "base": _mode_name(me.telemetry_base),
            "location": _mode_name(me.telemetry_location),
            "environment": _mode_name(me.telemetry_environment),
        },
        "multi_acks": me.multi_acks,
        # null: firmware without GET_TUNING_PARAMS; duty cycle 1 / (1 + factor)
        "airtime_factor": None if tuning is None else tuning.airtime_factor_milli / 1000,
        "battery_mv": session.battery.millivolts if session.battery else None,
        "contacts": [
            {
                "public_key": key,
                "name": rec.name,
                "type": NODE_TYPES.get(rec.node_type, str(rec.node_type)),
                "flags": _flag_names(rec.flags),
            }
            for key, rec in sorted(
                session.contact_records.items(), key=lambda kv: kv[1].name.lower()
            )
        ],
        "channels": [{"index": i, "name": n} for i, n in sorted(session.channels.items())],
        # what the managed profile changes at the service's first connect (ADR 0020)
        "profile": {
            "manage": mode.value,
            "configured": configured,  # false: the default observer profile
            "firmware_tested": tested,
            "writes": not plan.observing,
            "changes": changes,
            "contacts": contacts,
        },
    }


def _profile_action(key: str, plan: Plan) -> str:
    """What the service does about a setting that differs from the profile."""
    if key in plan.writes:
        return "set"
    if key in plan.drift:
        return "kept"  # changed on the radio after the service set it
    if key in plan.not_converging:
        return "given_up"  # written at the last connects without effect
    if key in plan.unsupported:
        return "unsupported"
    return "reported"  # observing


# Translators: a line of probe's report on how the radio adds the nodes it hears; its value
# starts in the column of the other lines; {types} lists node types (chat, repeater, room,
# sensor), which stay as they are
_ADDING = {  # manually; some types still automatically; a full table overwrites the oldest
    (True, False, False): N_("contacts   added manually"),
    (True, True, False): N_("contacts   added manually, but {types} automatically"),
    (True, True, True): N_(
        "contacts   added manually, but {types} automatically; a full table overwrites the oldest"
    ),
    (False, False, False): N_("contacts   all added automatically"),
    (False, False, True): N_(
        "contacts   all added automatically; a full table overwrites the oldest"
    ),
}
# Translators: a line of probe's report, its value in the column of the other lines: the radio's
# position, and whether its adverts carry it
_POSITION = {  # known; shared in adverts
    (False, False): N_("position   none, not in adverts"),
    (False, True): N_("position   none, shared in adverts"),
    (True, False): N_("position   {latitude}, {longitude}, not in adverts"),
    (True, True): N_("position   {latitude}, {longitude}, shared in adverts"),
}
# Translators: a line of probe's report, its value in the column of the other lines: whether
# the radio's settings differ from the managed profile, which the setting radio.profile
# configures or leaves to its default
_PROFILE = {  # differs; configured
    (False, False): N_("profile    the radio matches the default observer profile"),
    (False, True): N_("profile    the radio matches the configured profile"),
    (True, False): N_("profile    differs from the default observer profile:"),
    (True, True): N_("profile    differs from the configured profile:"),
}


def _print_report(r: dict[str, Any]) -> None:
    auto, spot = r["auto_add"], r["position"]
    types = ", ".join(auto["types"])
    adding = (bool(auto["manual"]), bool(auto["manual"] and types))
    overwrites = bool(types and auto["overwrite_oldest"])
    modes = ", ".join(f"{kind} {mode}" for kind, mode in r["telemetry"].items())
    shared = r["advert_location"] == "share"
    factor = r["airtime_factor"]
    # Translators: a line of probe's report. Its value starts in the column where the other
    # lines start theirs (12 in English): pad the label with spaces to keep one column.
    lines = [
        _safe_text(tr("radio      {name}  {key}", name=r["name"], key=r["public_key"])),
        _safe_text(tr("firmware   {version} (code {code}), {model}, built {date}",
                      version=r["firmware"], code=r["firmware_code"], model=r["model"],
                      date=r["build_date"])),
        tr("lora       {frequency:.3f} MHz, {bandwidth:g} kHz, SF {sf}, CR 4/{cr}, {power} dBm",
           frequency=r["frequency_mhz"], bandwidth=r["bandwidth_khz"],
           sf=r["spreading_factor"], cr=r["coding_rate"], power=r["tx_power_dbm"]),
        tr(_ADDING[(*adding, overwrites)], types=types),
        tr("telemetry  {modes}", modes=modes),
        tr(_POSITION[spot is not None, shared], **(spot or {})),
        *(
            [tr("airtime    factor {factor:g}: the radio sends at most {duty:.3g} %",
                factor=factor, duty=100 / (1 + factor))]
            if factor is not None
            else []
        ),
    ]  # fmt: skip
    for line in lines:
        print(line)
    for c in r["contacts"]:
        # Translators: after a contact's name in probe's report: it may use the radio's
        # command line over the air
        remote = "  " + tr("REMOTE CLI") if "remote_cli" in c["flags"] else ""
        print(_safe_text(f"  {c['public_key']}  {c['type']:<8} {c['name']}{remote}"))
    # Translators: a heading in probe's report, above the radio's channels
    print(tr("channels"))
    for ch in r["channels"]:
        print(_safe_text(f"  {ch['index']:>3}  {ch['name']}"))
    _print_plan(r["profile"])


def _print_plan(profile: dict[str, Any]) -> None:
    """What the managed profile changes at the service's next connect (ADR 0020)."""
    from meshcore_home_gateway.core.profile import NOT_CONVERGING_AFTER  # noqa: PLC0415

    differs = bool(profile["changes"] or profile["contacts"])
    print(tr(_PROFILE[differs, bool(profile["configured"])]))
    if not differs:
        return
    # Translators: why the gateway does not change a setting or contact; it follows a
    # setting's or contact's line, for example "not on the radio; only reported: …"
    reported = (
        tr("only reported: radio.profile.manage = observe")
        if profile["manage"] == "observe"
        else tr("only reported: the firmware is untested; radio.profile.manage = always sets it")
    )
    # Translators: what the gateway does about a setting of the radio that differs from the
    # managed profile, in brackets after the setting's line
    what = {
        "set": N_("set at the next connect"), "kept": N_("changed on the radio; kept"),
        "unsupported": N_("the firmware does not support it"),
    }  # fmt: skip
    for name, change in profile["changes"].items():
        live, wanted = _json_text(change["live"]), _json_text(change["profile"])
        action = change["action"]
        if action == "given_up":
            done = trn(
                "did not take effect at {count} connect; no longer set",
                "did not take effect at {count} connects; no longer set",
                NOT_CONVERGING_AFTER,
            )
        else:
            done = reported if action == "reported" else tr(what[action])
        print(f"  {name}: {live} -> {wanted}  ({done})")
    # Translators: what the gateway does about a contact it keeps on the radio, in brackets after
    # the contact's line; {reported} says why it only reports it ("only reported: …")
    doing = {
        "add": N_("added as a favourite at the next connect"),
        "favourite": N_("made a favourite at the next connect"),
        "missing": N_("not on the radio; {reported}"),
        "not_favourite": N_("no favourite; {reported}"),
    }
    for key, contact in profile["contacts"].items():
        action = tr(doing[contact["action"]], reported=reported)
        # Translators: a contact the managed profile adds or makes a favourite: its first 12 key
        # digits, its name, what the gateway does
        line = tr("  contact {key} {name}  ({action})", key=key[:12], name=contact["name"],
                  action=action)  # fmt: skip
        print(_safe_text(line))


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
        except (OSError, UnicodeError) as exc:  # refused, unreachable, unknown name, TLS failure
            return tr(
                "cannot connect to the broker {host}:{port}: {detail}",
                host=mqtt.host,
                port=mqtt.port,
                detail=exc,
            )
        client.loop_start()
        if not self.answered.wait(CONNECT_TIMEOUT_S):
            return tr("broker did not answer")
        if self.refusal is not None:
            # Translators: the reason is the broker's, in English (paho's text)
            return tr("broker refused the connection: {reason}", reason=self.refusal)
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
        missing = tr("no target {alias!r} in {path}", alias=args.alias, path=args.config)
        print(PREFIX + missing, file=sys.stderr)
        return EX_FAILURE
    try:  # bytes that are not UTF-8 reach argv as surrogates (PEP 383)
        payload = args.text.encode("utf-8")
    except UnicodeEncodeError:
        print(PREFIX + tr("the text is not valid UTF-8"), file=sys.stderr)
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
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        if args.wait <= 0:
            return EX_OK
        if not watch.done.wait(args.wait) or watch.result is None:
            print(PREFIX + tr("no delivery result in time"), file=sys.stderr)
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
        return tr("broker did not confirm the subscription")
    if refused:
        # Translators: the reason is the broker's, in English (paho's text)
        return tr("broker refused the subscription to {topic}: {reason}", topic=topic,
                  reason=refused[0])  # fmt: skip
    return None


def _publish(client: Any, topic: str, payload: bytes) -> str | None:
    """Publish with QoS 1 and wait for the broker's acknowledgement; returns an error text."""
    info = client.publish(topic, payload, qos=1)
    try:
        info.wait_for_publish(PUBLISH_TIMEOUT_S)
    except (RuntimeError, ValueError) as exc:
        return tr("request not sent: {detail}", detail=exc)
    return None if info.is_published() else tr("broker did not confirm the request")


def _listen(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

    config = load_config(args.config)
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    wanted = {
        "received": [topics.received], "command": [topics.command],
        "delivery": [topics.delivery], "state": [topics.state],
        "contact": [topics.contact_event], "node": [topics.node_event],
        "telemetry": [topics.telemetry_event],
        "all": [topics.received, topics.command, topics.delivery, topics.state,
                topics.contact_event, topics.node_event, topics.telemetry_event],
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
            print(PREFIX + error, file=sys.stderr)
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
            print(f"{PREFIX}{failures[0]}", file=sys.stderr)  # the system's text
        return EX_FAILURE
    return EX_OK


# --- contacts -------------------------------------------------------------------------------

RETAINED_WAIT_S = 2.0  # for the gateway's retained documents; brokers send them on SUBACK
_FULL_KEY = re.compile(r"[0-9a-fA-F]{64}")
# a heard node as Home Assistant's select lists it, the name after the digits optional
_LABEL = re.compile(r"([0-9a-fA-F]{12})(?:…|\.\.\.)([0-9a-fA-F]{8})(?: .*)?")


class _Retained:
    """The retained messages of some topics (paho thread); JSON where they are JSON."""

    def __init__(self, topics: Sequence[str]) -> None:
        self.topics = tuple(topics)
        self.messages: dict[str, Any] = {}
        self.complete = threading.Event()

    def on_message(self, message: Any) -> None:
        if message.topic not in self.topics or not message.retain or not message.payload:
            return
        try:
            self.messages[message.topic] = json.loads(message.payload)
        except (ValueError, RecursionError):
            self.messages[message.topic] = message.payload.decode("utf-8", errors="replace")
        if set(self.messages) == set(self.topics):
            self.complete.set()


class _ContactWatch:
    """The result of one contact request: the `contact` event with its id (paho thread)."""

    def __init__(self, request_id: str, final: set[str]) -> None:
        self._id, self._final = request_id, final
        self.result: dict[str, Any] | None = None
        self.done = threading.Event()

    def on_message(self, message: Any) -> None:
        try:
            event = json.loads(message.payload)
        except (ValueError, RecursionError):  # also nested too deeply; it would end paho's thread
            return
        if not isinstance(event, dict) or event.get("id") != self._id:
            return
        if event.get("event_type") in self._final:
            self.result = event
            self.done.set()


class _ContactSession:
    """A broker connection for the `contacts` commands: the gateway's availability and some of
    its retained documents (`documents`: "contacts", "nodes", "state"), then perhaps a request."""

    def __init__(self, config: Config, documents: Sequence[str] = ()) -> None:
        from meshcore_home_gateway.mqtt.topics import Topics  # noqa: PLC0415

        self.config = config
        self.topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
        names = [self.topics.availability, *(getattr(self.topics, d) for d in documents)]
        self.retained = _Retained(names)
        self.watch: _ContactWatch | None = None
        self.client = _mqtt_client(config.mqtt, "contacts")
        self.client.on_message = self._on_message

    def _on_message(self, _c: Any, _u: Any, message: Any) -> None:
        if message.topic == self.topics.contact_event and self.watch is not None:
            self.watch.on_message(message)
        else:
            self.retained.on_message(message)

    def open(self) -> str | None:
        """Connect and read the retained documents; an error text, or None."""
        error = _Connection().open(self.client, self.config.mqtt)
        for topic in () if error else self.retained.topics:
            error = error or _subscribe(self.client, topic)
        if error is None:
            self.retained.complete.wait(RETAINED_WAIT_S)
        return error

    def document(self, name: str) -> Any:
        return self.retained.messages.get(getattr(self.topics, name))

    @property
    def online(self) -> bool:
        return bool(self.document("availability") == "online")

    def request(
        self, topic: str, payload: dict[str, Any], wait_s: float, final: set[str],
        ok: frozenset[str] = frozenset({"added", "removed"}),
    ) -> int:  # fmt: skip
        """Publish the request and print its result: success is an event of `ok` without a
        reason."""
        request_id = f"cli-{os.urandom(4).hex()}"
        self.watch = _ContactWatch(request_id, final)
        error = _subscribe(self.client, self.topics.contact_event) if wait_s > 0 else None
        body = json.dumps({**payload, "id": request_id}).encode("utf-8")
        error = error or _publish(self.client, topic, body)
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        if wait_s <= 0:
            return EX_OK
        if not self.watch.done.wait(wait_s) or self.watch.result is None:
            late = tr(
                "no result in time (request {request}; it may still be handled: {command})",
                request=request_id,
                command="meshcore-home-gateway listen --topic contact",
            )
            print(PREFIX + late, file=sys.stderr)
            return EX_FAILURE
        result = self.watch.result
        print(_json_text(result))
        return EX_OK if result["event_type"] in ok and result.get("reason") is None else EX_FAILURE

    def close(self) -> None:
        self.client.disconnect()  # first: loop_stop() alone may wait 120 s for unacknowledged
        self.client.loop_stop()


def _opened(session: _ContactSession) -> int | None:
    """None when connected and the gateway is online; else the exit status, after saying why."""
    error = session.open()
    if error is None and not session.online:
        error = tr("the gateway is offline")
    if error is None:
        return None
    print(PREFIX + error, file=sys.stderr)
    return EX_FAILURE


def _entries(document: Any, where: str) -> list[dict[str, Any]]:
    items = document.get(where) if isinstance(document, dict) else None
    return [e for e in items or [] if isinstance(e, dict) and isinstance(e.get("key"), str)]


def _resolve_label(label: re.Match[str], document: Any, where: str) -> dict[str, Any] | None:
    """The one entry of a document's list with the label's first 12 and last 8 digits, or None
    after telling why."""
    start, end = label.group(1).lower(), label.group(2).lower()
    found = [
        e
        for e in _entries(document, where)
        if e["key"].startswith(start) and e["key"].endswith(end)
    ]
    if len(found) == 1:
        return found[0]
    if found:
        keys = ", ".join(sorted(e["key"] for e in found))
        matches = trn("{count} key matches: {keys}", "{count} keys match: {keys}", len(found),
                      keys=keys)  # fmt: skip
        print(PREFIX + matches, file=sys.stderr)
    else:
        cut = isinstance(document, dict) and document.get("truncated") is True
        print(PREFIX + tr(_NOT_LISTED[where, cut], label=label.group(0)), file=sys.stderr)
    return None


# Translators: a contact or heard node given as KEY12…KEY8 that the gateway's list lacks
_NOT_LISTED = {  # the list; cut at its bound
    ("nodes", False): N_("no node {label}"),
    ("nodes", True): N_("no node {label} (the published list is cut: give the full key)"),
    ("contacts", False): N_("no contact {label}"),
    ("contacts", True): N_("no contact {label} (the published list is cut: give the full key)"),
}


def _looks_alike(key: str, nodes: Any) -> bool:
    """The gateway flags the key, also beyond the bounded list, or a heard node shares its first
    12 digits (the key itself not heard yet)."""
    flagged = nodes.get("look_alike") if isinstance(nodes, dict) else None
    if isinstance(flagged, list) and key in flagged:
        return True
    return any(
        (e["key"] == key and e.get("look_alike") is True)
        or (e["key"] != key and e["key"][:12] == key[:12])
        for e in _entries(nodes, "nodes")
    )


def _print_key(key: str, name: str | None, *, claimed: bool = False) -> None:
    """The full key, to compare with the node's own screen: an attacker can copy the first
    digits of a key, not all of them."""
    groups = " ".join(key[i : i + 4] for i in range(0, len(key), 4))
    if name is None:
        line = tr("key {groups}", groups=groups)
    elif claimed:
        # Translators: the name a contact link or card gives itself, which nobody checked
        line = tr("key {groups}  {name!r} (claimed)", groups=groups, name=name)
    else:
        line = tr("key {groups}  {name!r}", groups=groups, name=name)
    print(_safe_text(line), file=sys.stderr)


def _contacts_add(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.protocol.links import LinkError, parse_contact_text  # noqa: PLC0415

    config = load_config(args.config)
    text = args.contact.strip()
    label = _LABEL.fullmatch(text)
    key, name, claimed = None, None, False
    if _FULL_KEY.fullmatch(text):
        key = text.lower()
    elif label is None:  # checked before connecting; the gateway parses it again
        try:
            claim = parse_contact_text(text)
        except LinkError as exc:
            # Translators: why the text is no contact, then the code of the reason (stays)
            refused = tr("{detail} ({reason})", detail=exc.text, reason=exc.reason)
            print(PREFIX + refused, file=sys.stderr)
            return EX_USAGE
        key, name, claimed = claim.public_key, claim.name, True
    session = _ContactSession(config, ["nodes"])
    try:
        if (status := _opened(session)) is not None:
            return status
        nodes = session.document("nodes")
        if label is not None:
            entry = _resolve_label(label, nodes, "nodes")
            if entry is None:
                return EX_USAGE
            key, name = entry["key"], entry.get("name")
        assert key is not None  # noqa: S101 - one of the branches above
        _print_key(key, name if isinstance(name, str) else None, claimed=claimed)
        if _looks_alike(key, nodes):
            print(tr("warning: another node has the same first 12 key digits: compare the full "
                     "key above with the node's own screen; the one added first keeps them"),
                  file=sys.stderr)  # fmt: skip
        contact = key if claimed is False else text  # a card's text: the gateway parses it
        return session.request(session.topics.add_contact, {"contact": contact}, args.wait,
                               {"added", "refused", "failed"})  # fmt: skip
    finally:
        session.close()


def _contact_given(text: str) -> tuple[str, re.Match[str] | None] | None:
    """A contact given as a full key or KEY12…KEY8: the text and its label; None after saying
    what is wanted."""
    text = text.strip()
    label = _LABEL.fullmatch(text)
    if label is None and not _FULL_KEY.fullmatch(text):
        print(PREFIX + tr("give the contact's full key (64 hex digits) or KEY12…KEY8"),
              file=sys.stderr)  # fmt: skip
        return None
    return text, label


def _indexed(text: str, label: re.Match[str] | None, index: Any) -> dict[str, Any] | None:
    """The index's entry of the contact (by label or key); a key it lacks as `{"key": key}`;
    None after saying why a label names none."""
    if label is not None:
        return _resolve_label(label, index, "contacts")
    key = text.lower()
    return next((e for e in _entries(index, "contacts") if e["key"] == key), {"key": key})


def _contacts_remove(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if (given := _contact_given(args.contact)) is None:
        return EX_USAGE
    session = _ContactSession(config, ["contacts"])
    try:
        if (status := _opened(session)) is not None:
            return status
        entry = _indexed(*given, session.document("contacts"))
        if entry is None:
            return EX_USAGE
        name = entry.get("name")
        _print_key(entry["key"], name if isinstance(name, str) else None)
        print(tr("the radio drops its direct messages from then on; messages queued to it fail"),
              file=sys.stderr)  # fmt: skip
        return session.request(session.topics.contact_remove(entry["key"]), {}, args.wait,
                               {"removed", "refused", "failed"})  # fmt: skip
    finally:
        session.close()


def _contacts_telemetry(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    if args.forget and args.state == "on":
        # Translators: {state} is the command's word off, which stays
        print(PREFIX + tr("--forget goes with {state} only", state="off"), file=sys.stderr)
        return EX_USAGE
    if (given := _contact_given(args.contact)) is None:
        return EX_USAGE
    session = _ContactSession(config, ["contacts"])
    try:
        if (status := _opened(session)) is not None:
            return status
        index = session.document("contacts")
        entry = _indexed(*given, index)
        if entry is None:
            return EX_USAGE
        key, name = entry["key"], entry.get("name")
        _print_key(key, name if isinstance(name, str) else None)
        if args.state == "off":
            # Translators: {readings} and {positions} are the names of two settings, which stay
            stops = (
                tr("the gateway stops asking it and forgets its battery, readings and position "
                   "and deletes them from its history")
                if args.forget
                else tr("the gateway stops asking it and forgets its battery, readings and "
                        "position; its history keeps them for {readings} and {positions}",
                        readings="readings_days", positions="positions_days")
            )  # fmt: skip
            print(stops, file=sys.stderr)
            listed = any(e["key"] == key for e in _entries(index, "contacts"))
            cut = isinstance(index, dict) and index.get("truncated") is True
            if not listed and not cut:  # also before it is added: asked within a minute then
                print(tr("not on the radio now: excluded in advance"), file=sys.stderr)
        request = {"telemetry": args.state, **({"forget": True} if args.forget else {})}
        return session.request(
            session.topics.contact_telemetry(key), request, args.wait,
            {"excluded", "included", "refused", "failed"}, frozenset({"excluded", "included"}),
        )  # fmt: skip
    finally:
        session.close()


def _contacts_list(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session = _ContactSession(config, ["contacts", "nodes"])
    try:
        error = session.open()
    finally:
        session.close()
    if error is not None:
        print(PREFIX + error, file=sys.stderr)
        return EX_FAILURE
    index, nodes = session.document("contacts"), session.document("nodes")
    if not isinstance(index, dict):
        print(PREFIX + tr("the gateway has published no contacts"), file=sys.stderr)
        return EX_FAILURE
    if not session.online:
        offline = tr("the gateway is offline: the lists are as it last saw them")
        print(PREFIX + offline, file=sys.stderr)
    if args.json:
        print(_json_text({"contacts": index, "nodes": nodes}, indent=2))
        return EX_OK
    manage = index.get("manage") is True
    for e in _entries(index, "contacts"):
        mark = "protected" if e.get("protected") else "removable" if manage else "-"
        print(_list_line("contact", e, mark))
    for e in _entries(nodes, "nodes"):
        print(_list_line("node", e, "look_alike" if e.get("look_alike") else "-"))
    for name, document in (("contacts", index), ("nodes", nodes)):
        if isinstance(document, dict) and document.get("truncated") is True:
            shown, total = len(_entries(document, name)), document.get("total")
            print(PREFIX + _bounded(name, shown, total), file=sys.stderr)
    return EX_OK


def _bounded(name: str, shown: int, total: Any) -> str:
    """A note that the gateway's document of its contacts or nodes lists only some of them."""
    n = total if isinstance(total, int) else 0  # chooses the form; {count} shows what was sent
    values = {"shown": shown, "count": total}
    if name == "contacts":
        return trn(
            "the gateway lists {shown} of {count} contact (its document is bounded); give the "
            "others by full key",
            "the gateway lists {shown} of {count} contacts (its document is bounded); give the "
            "others by full key",
            n, **values,
        )  # fmt: skip
    return trn(
        "the gateway lists {shown} of {count} node (its document is bounded); give the others "
        "by full key",
        "the gateway lists {shown} of {count} nodes (its document is bounded); give the others "
        "by full key",
        n, **values,
    )  # fmt: skip


def _list_line(kind: str, entry: dict[str, Any], mark: str) -> str:
    fields = [kind, entry["key"], str(entry.get("type")), mark, str(entry.get("heard_at") or "-")]
    return "\t".join([*fields, _safe_text(str(entry.get("name") or ""))])


def _contacts_link(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    session = _ContactSession(config, ["state"])
    try:
        error = session.open()
    finally:
        session.close()
    state = session.document("state")
    link = state.get("contact_link") if isinstance(state, dict) else None
    if error is not None or not isinstance(link, str):
        print(PREFIX + (error or tr("the gateway has published no contact link")), file=sys.stderr)
        return EX_FAILURE
    print(_safe_text(link))
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
        print(PREFIX + tr("no group {group!r}", group=args.group), file=sys.stderr)
        return EX_FAILURE
    try:
        write_private(args.output, text, gid)
    except OSError as exc:
        detail = exc.strerror or exc  # the system's text
        print(PREFIX + tr("cannot write {path}: {detail}", path=args.output, detail=detail),
              file=sys.stderr)  # fmt: skip
        return EX_FAILURE
    print(tr("wrote {path}", path=args.output))
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
