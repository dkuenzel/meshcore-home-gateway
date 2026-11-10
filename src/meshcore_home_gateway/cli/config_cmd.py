"""`check-config`, `config render` and `config answers`: the configuration checked (also against
the broker), written from the installer's answers, and read back for the installer."""

from __future__ import annotations

import argparse
import contextlib
import grp
import os
import sys
import tempfile
from pathlib import Path

from meshcore_home_gateway import i18n
from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import EX_FAILURE, EX_OK, PREFIX, tr, trn
from meshcore_home_gateway.config import (
    Config,
    MqttConfig,
    config_answers,
    load_config,
    parse_config,
    read_text,
    render_config,
    with_password_file,
)


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
    from meshcore_home_gateway.config_checks import start_warnings  # noqa: PLC0415

    for warning in start_warnings(config):
        print(PREFIX + tr("warning: {detail}", detail=warning), file=sys.stderr)
    if not args.broker:
        return EX_OK
    client = broker.mqtt_client(mqtt, "check")  # another client id: the service may be connected
    connection = broker.Connection()
    try:
        error = connection.open(client, mqtt)
    finally:
        client.disconnect()
        client.loop_stop()
    if error is not None:
        # Translators: why the broker did not take the login, then who logged in
        print(PREFIX + tr("{detail} ({login})", detail=error, login=login), file=sys.stderr)
        from meshcore_home_gateway.mqtt.client import UNSUPPORTED_PROTOCOL_VERSION  # noqa: PLC0415

        if connection.refusal_value == UNSUPPORTED_PROTOCOL_VERSION:
            # whatever the login: a broker of MQTT 3.1.1 only, asked with MQTT 5
            # Translators: "protocol" and "[mqtt]" are the setting and its section, not translated
            hint = tr(
                "the broker does not speak MQTT {protocol}; set protocol in [mqtt] to the version "
                "it speaks ({guide})",
                protocol=mqtt.protocol,
                guide="docs/deployment.md, troubleshooting",
            )
            print(PREFIX + hint, file=sys.stderr)
        elif connection.refusal is not None:
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
    if mqtt.username:  # without one, the login above was the anonymous one
        _probe_anonymous(mqtt)
    return EX_OK


def _probe_anonymous(mqtt: MqttConfig) -> None:
    """Whether the broker takes a client without credentials (SEC-004): then whoever reaches it
    can use the gateway's topics unless an ACL stops them, which no client can see."""
    import dataclasses  # noqa: PLC0415

    client = broker.mqtt_client(
        dataclasses.replace(mqtt, username=None, password=None), "anonymous"
    )
    connection = broker.Connection()
    try:
        error = connection.open(client, mqtt)
    finally:
        client.disconnect()
        client.loop_stop()
    if error is None:
        # Translators: security text (docs/translations.md): translate all of it, add nothing
        warning = tr(
            "the broker accepts clients without a user name: unless its ACL keeps them off the "
            "gateway's topics, any client that reaches it can read messages and positions and "
            "publish command events (docs/mqtt-api.md, Securing the broker)"
        )
        print(PREFIX + tr("warning: {detail}", detail=warning), file=sys.stderr)
    elif connection.refusal is not None:
        # Translators: a line of check-config's summary, its value in the column of the others
        print(tr("  anonymous  refused by {host}:{port}", host=mqtt.host, port=mqtt.port))


def _login(mqtt: MqttConfig) -> str:
    """Who logs in to the broker, as check-config names it; never the password."""
    if not mqtt.username:
        # Translators: security text (docs/translations.md)
        return tr("no user name")
    if mqtt.password:
        # Translators: security text (docs/translations.md)
        return tr("user {user!r} with a password", user=mqtt.username)
    # Translators: security text (docs/translations.md)
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
        tr("  radio      {transport} {where}", transport=radio.transport, where=where)
        if radio.transport == "serial"
        # Translators: security text (docs/translations.md): the radio's TCP interface has no
        # authentication (SEC-003)
        else tr(
            "  radio      tcp {where}, unauthenticated: anyone who reaches it controls the radio",
            where=where,
        ),
        # Translators: security text (docs/translations.md): the broker connection uses TLS
        tr("  broker     {host}:{port} (TLS), {login}, topics {root}/...", **broker)
        if mqtt.tls
        # Translators: security text (docs/translations.md): the broker connection has no TLS
        else tr("  broker     {host}:{port}, {login}, topics {root}/...", **broker),
        tr(
            "  discovery  on, prefix {prefix}, names in {language}",
            prefix=ha.prefix,
            language=language,
        )
        if ha.discovery
        else tr("  discovery  off"),
        *(
            tr(
                "  target     {alias}: channel {index}",
                alias=target.alias,
                index=target.channel_index,
            )
            if target.channel_index is not None
            else tr(
                "  target     {alias}: contact {key}", alias=target.alias, key=target.public_key
            )
            for target in config.targets
        ),
        *(
            [
                trn(
                    "  commands   {words} from {count} sender",
                    "  commands   {words} from {count} senders",
                    senders,
                    words=words,
                )
            ]
            if words
            else []
        ),
        *(
            [
                # Translators: security text (docs/translations.md): their commands need a code
                trn(
                    "  codes      {count} sender ends commands with a code",
                    "  codes      {count} senders end commands with a code",
                    len(config.commands.secrets),
                )
            ]
            if config.commands.secrets
            else []
        ),
        tr(
            "  airtime    {budget:g} s per hour, burst {burst:g} s",
            budget=airtime.budget_s_per_hour,
            burst=airtime.burst_s,
        ),
        tr("  contacts   managed on request")
        if config.contacts.manage
        else tr("  contacts   not managed"),
        tr("  telemetry  off")
        if not config.telemetry.enabled
        else tr("  telemetry  asked of every companion not excluded")
        if config.telemetry.ask == "all"
        else tr("  telemetry  asked of the companions the household included"),
    ]


def _render(args: argparse.Namespace) -> int:
    answers, password = read_text(args.answers), None
    if args.password_file is not None:  # the password in a file of its own (SEC-009)
        answers, password = with_password_file(answers, args.password_file)
    text = render_config(answers)
    mqtt = parse_config(text).mqtt
    if mqtt.tls:  # the certificate and key files must load, as in check-config and at start-up
        from meshcore_home_gateway.mqtt.client import tls_context  # noqa: PLC0415

        tls_context(mqtt)
    try:
        gid = None if args.group is None else grp.getgrnam(args.group).gr_gid
    except KeyError:
        print(PREFIX + tr("no group {group!r}", group=args.group), file=sys.stderr)
        return EX_FAILURE
    path = args.password_file
    try:
        if password is not None:
            write_private(path, password + "\n", gid)
        elif path is not None and mqtt.password_file is None:
            path.unlink(missing_ok=True)  # a password no longer used stays nowhere
        path = args.output
        write_private(path, text, gid)
    except OSError as exc:
        detail = exc.strerror or exc  # the system's text
        print(
            PREFIX + tr("cannot write {path}: {detail}", path=path, detail=detail), file=sys.stderr
        )
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
