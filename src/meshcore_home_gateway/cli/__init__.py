"""Command line: `meshcore-home-gateway <command>` (design §6.9, docs/configuration.md).

Exit codes: 0 success, 1 failure (radio, broker, delivery), 2 usage error (argparse), 78
configuration error (EX_CONFIG; the systemd unit does not restart on it).

Texts for people follow the language of the environment, except those of `run`, which go to the
journal (ADR 0019); every text is a whole message marked with tr(), tr_help() or trn(). Machine
output (lists, JSON, `config answers`, `--version`) is the same in every language; the dashboard
follows [homeassistant] language, as the names in Home Assistant do.

One module per group of commands (CODE-005): `run`, `config_cmd` (`check-config`, `config render`,
`config answers`), `probe`, `messages` (`send`, `listen`), `contacts` and `dashboard`; `common`
holds the exit codes and the language of the texts, `broker` the commands' MQTT client. This
module holds the parser and `main`.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.cli.backup import _backup
from meshcore_home_gateway.cli.common import (
    DEFAULT_CONFIG,
    EX_CONFIG,
    EX_FAILURE,
    EX_OK,
    EX_USAGE,
    PREFIX,
    _speaking,
    tr,
    tr_help,
)
from meshcore_home_gateway.cli.config_cmd import _answers, _check_config, _render
from meshcore_home_gateway.cli.contacts import (
    _contacts_add,
    _contacts_link,
    _contacts_list,
    _contacts_remove,
    _contacts_telemetry,
)
from meshcore_home_gateway.cli.dashboard import _dashboard
from meshcore_home_gateway.cli.doctor import _doctor
from meshcore_home_gateway.cli.history import _history_export, since_time
from meshcore_home_gateway.cli.messages import _listen, _send
from meshcore_home_gateway.cli.probe import _probe
from meshcore_home_gateway.cli.retire import _retire
from meshcore_home_gateway.cli.run import _run
from meshcore_home_gateway.cli.status import _status
from meshcore_home_gateway.config import ConfigError

__all__ = ["EX_CONFIG", "EX_FAILURE", "EX_OK", "EX_USAGE", "main"]


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

    state_dir_help = tr_help("default: $STATE_DIRECTORY or /var/lib/meshcore-home-gateway")
    run = command("run", tr_help("run the bridge (the systemd service does this)"), _run)
    run.add_argument("--state-dir", type=Path, default=None, help=state_dir_help)

    check = command(
        "check-config", tr_help("validate the configuration and print a summary"), _check_config
    )
    check.add_argument(
        "--broker",
        action="store_true",
        help=tr_help("also log in to the broker with the configured user name and password"),
    )

    probe = command(
        "probe",
        tr_help("connect to a radio directly and list what it knows"),
        _probe,
        config=False,
    )
    where = probe.add_mutually_exclusive_group(required=True)
    where.add_argument(
        "--serial", metavar="DEVICE", help=tr_help("serial device, e.g. /dev/serial/by-id/...")
    )
    where.add_argument(
        "--tcp",
        metavar="HOST[:PORT]",
        type=tcp_address,
        help=tr_help("WiFi companion, port 5000 by default; IPv6 as [ADDRESS]:PORT"),
    )
    probe.add_argument(
        "--list",
        choices=["contacts", "channels", "profile"],
        help=tr_help("tab-separated list only"),
    )
    probe.add_argument("--json", action="store_true", help=tr_help("everything as JSON"))
    probe.add_argument(
        "--config",
        type=Path,
        default=None,
        help=tr_help(
            "show what the [radio.profile] of this configuration changes (default: the "
            "observer profile without a name)"
        ),
    )
    probe.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=tr_help(
            "the service's state, read only, for its exact plan (default: $STATE_DIRECTORY or "
            "/var/lib/meshcore-home-gateway)"
        ),
    )

    send = command("send", tr_help("send a text to a configured target through the broker"), _send)
    send.add_argument("alias")
    send.add_argument("text")
    send.add_argument(
        "--wait",
        type=_wait_seconds,
        default=0.0,
        metavar="SECONDS",
        help=tr_help(
            "wait for the delivery result; exit 1 unless sent or confirmed, also when confirmed "
            "sooner than the path allows"
        ),
    )

    listen = command("listen", tr_help("print events from the broker as JSON lines"), _listen)
    listen.add_argument(
        "--topic",
        default="all",
        choices=["received", "command", "delivery", "state", "contact", "node", "telemetry", "all"],
    )
    listen.add_argument(
        "--count", type=_count, default=0, help=tr_help("stop after this many events (0: never)")
    )

    _contacts_parser(commands)

    _operations_parser(commands, command, state_dir_help)

    dashboard = command(
        "dashboard",
        tr_help("write a Home Assistant dashboard for this gateway (YAML, to standard output)"),
        _dashboard,
    )
    dashboard.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help=tr_help(
            "the service's state, read only, for the contacts on the map (default: "
            "$STATE_DIRECTORY or /var/lib/meshcore-home-gateway)"
        ),
    )

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
    render.add_argument(
        "--password-file",
        type=Path,
        default=None,
        # Translators: security text (docs/translations.md): where the broker's password goes
        help=tr_help(
            "write the broker password into this file (mode 0640) instead of the configuration"
        ),
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


def _operations_parser(
    commands: argparse._SubParsersAction[argparse.ArgumentParser],
    command: Callable[..., argparse.ArgumentParser],
    state_dir_help: str,
) -> None:
    """The commands of an installation's life: status, doctor, backup, retire, history
    (IMP-016, IMP-047, EXT-016)."""
    status = command("status", tr_help("show what the running gateway reports of itself"), _status)
    status.add_argument(
        "--json", action="store_true", help=tr_help("the gateway's documents as JSON")
    )

    doctor = command(
        "doctor",
        tr_help("check the configuration, the broker, the running gateway and this host"),
        _doctor,
    )
    doctor.add_argument("--state-dir", type=Path, default=None, help=state_dir_help)

    backup = command(
        "backup",
        tr_help("write the configuration and the state into an archive (tar.gz, mode 0600)"),
        _backup,
    )
    backup.add_argument("file", type=Path, help=tr_help("the archive; it must not exist yet"))
    backup.add_argument(
        "--with-password",
        action="store_true",
        # Translators: security text (docs/translations.md): the archive keeps the password
        help=tr_help("keep the broker password, also the file password_file names"),
    )
    backup.add_argument("--state-dir", type=Path, default=None, help=state_dir_help)

    retire = command(
        "retire",
        tr_help(
            "before the container goes: remove the gateway from Home Assistant and clear its "
            "retained documents (the service stopped)"
        ),
        _retire,
    )
    retire.add_argument(
        "--yes", action="store_true", help=tr_help("do it; without, only say what it would do")
    )
    retire.add_argument("--state-dir", type=Path, default=None, help=state_dir_help)

    history = commands.add_parser("history", help=tr_help("the local history"))
    history_commands = history.add_subparsers(required=True, metavar="action")
    export = history_commands.add_parser(
        "export", help=tr_help("print the history as JSON lines or CSV, read only")
    )
    export.set_defaults(handler=_history_export)
    export.add_argument(
        "--table",
        choices=["messages", "positions", "readings"],
        default="messages",
        help=tr_help("the table (default: messages)"),
    )
    export.add_argument(
        "--format",
        choices=["jsonl", "csv"],
        default="jsonl",
        help=tr_help("JSON lines or CSV (default: jsonl)"),
    )
    export.add_argument(
        "--since",
        type=since_time,
        default=None,
        metavar="DATE",
        help=tr_help("from this date or time on (ISO 8601; without a zone, UTC)"),
    )
    export.add_argument("--state-dir", type=Path, default=None, help=state_dir_help)


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
        "--forget",
        action="store_true",
        help=tr_help(
            "with off: also delete its readings and its positions from telemetry from the "
            "gateway's history"
        ),
    )
    telemetry.add_argument(
        "--wait",
        type=_wait_seconds,
        default=30.0,
        metavar="SECONDS",
        help=tr_help("wait this long for the result; 0: only send the request (default 30)"),
    )


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
