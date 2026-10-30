"""Configuration file (TOML) with strict validation, and rendering from installer answers.

Every setting is documented in docs/configuration.md. Unknown keys are errors, so a typo cannot
silently fall back to a default.
"""

from __future__ import annotations

import ipaddress
import math
import re
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from meshcore_home_gateway.core.commands import CommandRules
from meshcore_home_gateway.core.contacts import normalise_key
from meshcore_home_gateway.core.model import MessageKind, Target

ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
ID_RULE = (
    "1-32 of a-z, 0-9, '-', '_', starting with a letter or digit, not ending with '-' or '_', "
    "and no two of them in a row"
)
# Home Assistant collapses runs of '-' and '_' in entity IDs and drops a trailing one: 'radio-'
# would share the entity IDs of 'radio'
_MERGED_BY_HOME_ASSISTANT = re.compile(r"[-_]{2}|[-_]$")
TOPIC_SEGMENT = re.compile(r"[A-Za-z0-9_-]+")
# a host name in its ASCII form (also an IPv4 address); a trailing dot names the root
HOST_NAME = re.compile(r"[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+)*\.?")
LOG_LEVELS = ("debug", "info", "warning", "error")
CHANNEL_INDEX_MAX = 255  # one byte in the protocol; the radio's own limit is checked at runtime
MAX_CONTACT_DEVICES = 1000  # above the largest contact table of current firmware
MAX_RADIO_NAME_BYTES = 31  # the radio keeps 32 bytes with the terminating NUL
MIN_BURST_S = (
    5.0  # must hold the longest single packet (about 2 s at SF 8 / 62.5 kHz, configuration.md)
)


class ConfigError(ValueError):
    """Invalid configuration; the message names the key."""


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """The managed radio profile (ADR 0020); the observer's policy itself is fixed."""

    manage: Literal["auto", "observe", "always"] = "auto"
    name: str | None = None  # None: as found on the radio
    duty_cycle_percent: float | None = None  # None: 10 % in the 10 % sub-band, else as found
    protect_contacts: bool = True  # targets and command senders kept on the radio as favourites


@dataclass(frozen=True, slots=True)
class RadioConfig:
    transport: Literal["serial", "tcp"]
    device: str | None = None
    host: str | None = None
    port: int = 5000
    set_clock: bool = True
    profile: ProfileConfig = field(default_factory=ProfileConfig)


@dataclass(frozen=True, slots=True)
class MqttConfig:
    host: str
    node_id: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    ca_file: str | None = None
    cert_file: str | None = None
    key_file: str | None = None
    client_id: str | None = None
    keepalive_s: int = 60
    base_topic: str = "meshcore"

    @property
    def topic_root(self) -> str:
        return f"{self.base_topic}/{self.node_id}"


@dataclass(frozen=True, slots=True)
class HomeAssistantConfig:
    discovery: bool = True
    prefix: str = "homeassistant"
    device_name: str = "MeshCore radio"
    status_topic: str = "homeassistant/status"
    contact_devices: int = 50  # contacts with a device of their own (ADR 0017)


@dataclass(frozen=True, slots=True)
class MessagesConfig:
    ascii_only: bool = False
    truncate: bool = True
    dedup_window_s: float = 300.0
    queue_size: int = 20
    queue_ttl_s: float = 600.0
    max_attempts: int = 3
    flood_after: int = 2
    ignore_channels: frozenset[int] = frozenset()


@dataclass(frozen=True, slots=True)
class AirtimeConfig:
    budget_s_per_hour: float = 36.0
    burst_s: float = 10.0


@dataclass(frozen=True, slots=True)
class HistoryConfig:
    """The local history (ADR 0016)."""

    enabled: bool = True
    messages_days: float = 30.0
    positions_days: float = 30.0
    readings_days: float = 30.0


@dataclass(frozen=True, slots=True)
class ContactsConfig:
    """Managing the radio's contact table on request (ADR 0017)."""

    manage: bool = True


@dataclass(frozen=True, slots=True)
class TelemetryConfig:
    """Asking the radio's companions for their telemetry (ADR 0018)."""

    enabled: bool = True


@dataclass(frozen=True, slots=True)
class DiagnosticsConfig:
    publish_rx_log: bool = False
    log_level: str = "info"


@dataclass(frozen=True, slots=True)
class Config:
    radio: RadioConfig
    mqtt: MqttConfig
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    messages: MessagesConfig = field(default_factory=MessagesConfig)
    airtime: AirtimeConfig = field(default_factory=AirtimeConfig)
    commands: CommandRules = field(default_factory=lambda: CommandRules(frozenset(), frozenset()))
    targets: tuple[Target, ...] = ()
    history: HistoryConfig = field(default_factory=HistoryConfig)
    contacts: ContactsConfig = field(default_factory=ContactsConfig)
    telemetry: TelemetryConfig = field(default_factory=TelemetryConfig)
    diagnostics: DiagnosticsConfig = field(default_factory=DiagnosticsConfig)


def load_config(path: Path) -> Config:
    return parse_config(read_text(path))


def read_text(path: Path) -> str:
    """A configuration or answers file as text; unreadable files are configuration errors."""
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(f"cannot read {path}: not valid UTF-8 (byte {exc.start})") from exc


def parse_config(text: str) -> Config:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML: {exc}") from exc
    return _build(_Section(data, ""))


# --- validation helpers -----------------------------------------------------------------------


class _Section:
    """A TOML table that remembers which keys were read, to report unknown ones."""

    def __init__(self, data: Mapping[str, Any], path: str) -> None:
        self._data = data
        self._path = path
        self._read: set[str] = set()

    def key(self, name: str) -> str:
        return f"{self._path}.{name}" if self._path else name

    def has(self, name: str) -> bool:
        return name in self._data

    def raw(self, name: str) -> Any:
        self._read.add(name)
        return self._data.get(name)

    def section(self, name: str) -> _Section:
        value = self.raw(name)
        if value is None:
            value = {}
        if not isinstance(value, dict):
            raise ConfigError(f"{self.key(name)}: expected a table")
        return _Section(value, self.key(name))

    def string(
        self, name: str, default: str | None = None, *, required: bool = False
    ) -> str | None:
        value = self.raw(name)
        if value is None:
            if required:
                raise ConfigError(f"{self.key(name)}: required")
            return default
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{self.key(name)}: expected a non-empty string")
        return value

    def boolean(self, name: str, default: bool) -> bool:
        value = self.raw(name)
        if value is None:
            return default
        if not isinstance(value, bool):
            raise ConfigError(f"{self.key(name)}: expected true or false")
        return value

    def integer(self, name: str, default: int, lo: int, hi: int) -> int:
        value = self.raw(name)
        if value is None:
            return default
        if not isinstance(value, int) or isinstance(value, bool):
            raise ConfigError(f"{self.key(name)}: expected an integer")
        if not lo <= value <= hi:
            raise ConfigError(f"{self.key(name)}: must be between {lo} and {hi}, got {value}")
        return value

    def number(self, name: str, default: float, lo: float, hi: float = math.inf) -> float:
        value = self.raw(name)
        if value is None:
            return default
        if not isinstance(value, int | float) or isinstance(value, bool):
            raise ConfigError(f"{self.key(name)}: expected a number")
        if not lo <= value <= hi:
            raise ConfigError(f"{self.key(name)}: must be between {lo} and {hi}, got {value}")
        return float(value)

    def strings(self, name: str) -> list[str]:
        value = self.raw(name)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ConfigError(f"{self.key(name)}: expected a list of strings")
        return cast(list[str], value)

    def integers(self, name: str, lo: int, hi: int) -> list[int]:
        value = self.raw(name)
        if value is None:
            return []
        if not isinstance(value, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi for v in value
        ):
            raise ConfigError(f"{self.key(name)}: expected a list of integers {lo}..{hi}")
        return cast(list[int], value)

    def names(self) -> list[str]:
        return list(self._data)

    def check_unknown(self) -> None:
        for name in self._data:
            if name not in self._read:
                raise ConfigError(f"{self.key(name)}: unknown setting")


def _checked(section: _Section, builder: Callable[[_Section], Any]) -> Any:
    result = builder(section)
    section.check_unknown()
    return result


def _build(root: _Section) -> Config:
    config = Config(
        radio=_checked(root.section("radio"), _radio),
        mqtt=_checked(root.section("mqtt"), _mqtt),
        homeassistant=_checked(root.section("homeassistant"), _homeassistant),
        messages=_checked(root.section("messages"), _messages),
        airtime=_checked(root.section("airtime"), _airtime),
        commands=_checked(root.section("commands"), _commands),
        targets=_targets(root.section("targets")),
        history=_checked(root.section("history"), _history),
        contacts=_checked(root.section("contacts"), _contacts),
        telemetry=_checked(root.section("telemetry"), _telemetry),
        diagnostics=_checked(root.section("diagnostics"), _diagnostics),
    )
    root.check_unknown()
    return config


def _radio(s: _Section) -> RadioConfig:
    transport = s.string("transport", required=True)
    if transport not in ("serial", "tcp"):
        raise ConfigError(f"{s.key('transport')}: must be 'serial' or 'tcp', got {transport!r}")
    for name, used_by in (("device", "serial"), ("host", "tcp"), ("port", "tcp")):
        if s.has(name) and transport != used_by:  # it would have no effect
            raise ConfigError(f"{s.key(name)}: only used with transport = {used_by!r}")
    device = _absolute_path(s, "device")
    host = _host(s)
    if transport == "serial" and device is None:
        raise ConfigError(f"{s.key('device')}: required for the serial transport")
    if transport == "tcp" and host is None:
        raise ConfigError(f"{s.key('host')}: required for the tcp transport")
    return RadioConfig(
        transport=cast(Literal["serial", "tcp"], transport),
        device=device,
        host=host,
        port=s.integer("port", 5000, 1, 65535),
        set_clock=s.boolean("set_clock", True),
        profile=_checked(s.section("profile"), _profile),
    )


def _profile(s: _Section) -> ProfileConfig:
    manage = s.string("manage", "auto")
    if manage not in ("auto", "observe", "always"):
        raise ConfigError(f"{s.key('manage')}: one of auto, observe, always, got {manage!r}")
    name = s.string("name")
    if name is not None:
        size = len(name.encode("utf-8"))
        if not 1 <= size <= MAX_RADIO_NAME_BYTES or any(ord(c) < 0x20 or c == "\x7f" for c in name):
            raise ConfigError(
                f"{s.key('name')}: 1 to {MAX_RADIO_NAME_BYTES} bytes of UTF-8 without control "
                f"characters, got {name!r}"
            )
    duty = None
    if s.has("duty_cycle_percent"):  # the firmware cannot limit below 10 % (ADR 0020)
        duty = s.number("duty_cycle_percent", 10.0, 10.0, 100.0)
    return ProfileConfig(
        manage=cast(Literal["auto", "observe", "always"], manage),
        name=name,
        duty_cycle_percent=duty,
        protect_contacts=s.boolean("protect_contacts", True),
    )


def _host(s: _Section, *, required: bool = False) -> str | None:
    """A host name or address. A URL or `host:port` would only fail when connecting; a name that
    the resolver cannot encode (an empty or overlong part) would end paho's network thread."""
    host = s.string("host", required=required)
    hint = None if host is None else _host_problem(host)
    if hint is None:
        return host
    raise ConfigError(f"{s.key('host')}: a host name or IP address only{hint}, got {host!r}")


def _ipv6_address(host: str) -> bool:
    address, percent, zone = host.partition("%")
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return False
    return not percent or HOST_NAME.fullmatch(zone) is not None


def _host_problem(host: str) -> str | None:
    """None for a usable host, else a hint for the message (may be empty)."""
    if "://" in host:
        return ", without a scheme such as mqtt:// (TLS and port are settings of their own)"
    if "]:" in host or (host.count(":") == 1 and "[" not in host):
        return ", without the port (that is the setting port)"
    if "[" in host or "]" in host:
        return ", an IPv6 address without brackets"
    if ":" in host:  # an IPv6 address, with a zone for a link-local one
        return None if _ipv6_address(host) else ""
    try:
        name = host.encode("idna").decode("ascii")  # what the resolver gets, as Python sends it
    except UnicodeError:
        return " (a part between dots is empty or longer than 63 characters)"
    return None if HOST_NAME.fullmatch(name) else ""


def _absolute_path(s: _Section, name: str) -> str | None:
    """A file path that does not depend on the working directory: the service runs in /."""
    value = s.string(name)
    if value is not None and not Path(value).is_absolute():
        raise ConfigError(f"{s.key(name)}: must be an absolute path, got {value!r}")
    return value


def _is_id(name: str) -> bool:
    return bool(ID_PATTERN.fullmatch(name)) and not _MERGED_BY_HOME_ASSISTANT.search(name)


def _mqtt(s: _Section) -> MqttConfig:
    node_id = cast(str, s.string("node_id", required=True))
    if not _is_id(node_id):
        raise ConfigError(f"{s.key('node_id')}: use {ID_RULE}, got {node_id!r}")
    base = cast(str, s.string("base_topic", "meshcore"))
    if not all(TOPIC_SEGMENT.fullmatch(part) for part in base.split("/")):
        raise ConfigError(
            f"{s.key('base_topic')}: segments of A-Z, a-z, 0-9, '-', '_' separated by '/', "
            f"got {base!r}"
        )
    tls = s.boolean("tls", False)
    files = {name: _absolute_path(s, name) for name in ("ca_file", "cert_file", "key_file")}
    for name, value in files.items():
        if value is not None and not tls:  # never fall back to a plain connection silently
            raise ConfigError(f"{s.key(name)}: only used with tls = true")
    if files["key_file"] is not None and files["cert_file"] is None:
        raise ConfigError(f"{s.key('key_file')}: needs cert_file")
    username, password = s.string("username"), s.string("password")
    if password is not None and username is None:  # the client would not send it
        raise ConfigError(f"{s.key('password')}: needs username")
    if username is not None and username != username.strip():  # copied with a blank, mostly
        raise ConfigError(f"{s.key('username')}: no blank at the start or end, got {username!r}")
    return MqttConfig(
        host=cast(str, _host(s, required=True)),
        node_id=node_id,
        port=s.integer("port", 1883, 1, 65535),
        username=username,
        password=password,
        tls=tls,
        ca_file=files["ca_file"],
        cert_file=files["cert_file"],
        key_file=files["key_file"],
        client_id=s.string("client_id"),
        keepalive_s=s.integer("keepalive_s", 60, 10, 3600),
        base_topic=base,
    )


def _homeassistant(s: _Section) -> HomeAssistantConfig:
    prefix = cast(str, s.string("prefix", "homeassistant"))
    if not TOPIC_SEGMENT.fullmatch(prefix):
        raise ConfigError(f"{s.key('prefix')}: a single topic segment, got {prefix!r}")
    status_topic = cast(str, s.string("status_topic", "homeassistant/status"))
    if not all(TOPIC_SEGMENT.fullmatch(part) for part in status_topic.split("/")):
        raise ConfigError(
            f"{s.key('status_topic')}: segments of A-Z, a-z, 0-9, '-', '_' separated by '/', "
            f"got {status_topic!r}"
        )
    return HomeAssistantConfig(
        discovery=s.boolean("discovery", True),
        prefix=prefix,
        device_name=cast(str, s.string("device_name", "MeshCore radio")),
        status_topic=status_topic,
        contact_devices=s.integer("contact_devices", 50, 0, MAX_CONTACT_DEVICES),
    )


def _messages(s: _Section) -> MessagesConfig:
    max_attempts = s.integer("max_attempts", 3, 1, 4)  # the radio's attempt counter is 0..3
    return MessagesConfig(
        ascii_only=s.boolean("ascii_only", False),
        truncate=s.boolean("truncate", True),
        dedup_window_s=s.number("dedup_window_s", 300.0, 0.0, 86_400.0),
        queue_size=s.integer("queue_size", 20, 1, 1000),
        queue_ttl_s=s.number("queue_ttl_s", 600.0, 1.0, 86_400.0),
        max_attempts=max_attempts,
        flood_after=s.integer("flood_after", 2, 1, max(1, max_attempts)),
        ignore_channels=frozenset(s.integers("ignore_channels", 0, CHANNEL_INDEX_MAX)),
    )


def _airtime(s: _Section) -> AirtimeConfig:
    return AirtimeConfig(
        budget_s_per_hour=s.number("budget_s_per_hour", 36.0, 0.001, 3600.0),
        burst_s=s.number("burst_s", 10.0, MIN_BURST_S, 3600.0),
    )


def _commands(s: _Section) -> CommandRules:
    senders, words = s.strings("senders"), s.strings("words")
    max_age = s.number("max_age_s", 900.0, 0.0, 86_400.0)
    try:
        return CommandRules.create(senders=senders, words=words, max_age_s=max_age)
    except ValueError as exc:
        name = "senders" if "public key" in str(exc) else "words"
        raise ConfigError(f"{s.key(name)}: {exc}") from exc


def _targets(s: _Section) -> tuple[Target, ...]:
    targets = []
    for alias in s.names():
        t = s.section(alias)
        if not _is_id(alias):
            raise ConfigError(f"{t.key('')[:-1]}: alias must be {ID_RULE}")
        if t.has("channel") == t.has("contact"):
            raise ConfigError(f"{t.key('')[:-1]}: set exactly one of 'channel' or 'contact'")
        if t.has("channel"):
            index = t.integer("channel", 0, 0, CHANNEL_INDEX_MAX)
            targets.append(Target(alias, MessageKind.CHANNEL, channel_index=index))
        else:
            try:
                key = normalise_key(cast(str, t.string("contact", required=True)))
            except ValueError as exc:
                raise ConfigError(f"{t.key('contact')}: {exc}") from exc
            targets.append(Target(alias, MessageKind.CONTACT, public_key=key))
        t.check_unknown()
    return tuple(targets)


def _history(s: _Section) -> HistoryConfig:
    return HistoryConfig(
        enabled=s.boolean("enabled", True),
        messages_days=s.number("messages_days", 30.0, 1.0, 3650.0),
        positions_days=s.number("positions_days", 30.0, 1.0, 3650.0),
        readings_days=s.number("readings_days", 30.0, 1.0, 3650.0),
    )


def _contacts(s: _Section) -> ContactsConfig:
    return ContactsConfig(manage=s.boolean("manage", True))


def _telemetry(s: _Section) -> TelemetryConfig:
    return TelemetryConfig(enabled=s.boolean("enabled", True))


def _diagnostics(s: _Section) -> DiagnosticsConfig:
    level = cast(str, s.string("log_level", "info"))
    if level not in LOG_LEVELS:
        raise ConfigError(f"{s.key('log_level')}: one of {', '.join(LOG_LEVELS)}, got {level!r}")
    return DiagnosticsConfig(publish_rx_log=s.boolean("publish_rx_log", False), log_level=level)


# --- rendering from installer answers -------------------------------------------------------

_ANSWER_TYPES: dict[str, type] = {
    "radio.transport": str, "radio.device": str, "radio.host": str, "radio.port": int,
    "radio.set_clock": bool, "radio.profile.manage": str, "radio.profile.name": str,
    "radio.profile.duty_cycle_percent": float, "radio.profile.protect_contacts": bool,
    "mqtt.host": str, "mqtt.port": int, "mqtt.username": str, "mqtt.password": str,
    "mqtt.tls": bool, "mqtt.ca_file": str, "mqtt.cert_file": str, "mqtt.key_file": str,
    "mqtt.client_id": str, "mqtt.keepalive_s": int, "mqtt.base_topic": str, "mqtt.node_id": str,
    "homeassistant.discovery": bool, "homeassistant.prefix": str,
    "homeassistant.device_name": str, "homeassistant.status_topic": str,
    "homeassistant.contact_devices": int,
    "messages.ascii_only": bool, "messages.truncate": bool, "messages.dedup_window_s": float,
    "messages.queue_size": int, "messages.queue_ttl_s": float, "messages.max_attempts": int,
    "messages.flood_after": int, "messages.ignore_channels": list,
    "airtime.budget_s_per_hour": float, "airtime.burst_s": float,
    "commands.senders": list, "commands.words": list, "commands.max_age_s": float,
    "history.enabled": bool, "history.messages_days": float, "history.positions_days": float,
    "history.readings_days": float, "contacts.manage": bool, "telemetry.enabled": bool,
    "diagnostics.publish_rx_log": bool, "diagnostics.log_level": str,
}  # fmt: skip

HEADER = """\
# meshcore-home-gateway configuration, written by the installer.
# Reference: docs/configuration.md. Check after editing: meshcore-home-gateway check-config
"""


def render_config(answers: str) -> str:
    """Build a validated TOML configuration from `key=value` lines (installer answers).

    Keys are dotted settings (`mqtt.host`) or `target.<alias>` with `channel:<index>` or
    `contact:<public key>`. Lists are comma-separated. Empty values are skipped.
    """
    tables: dict[str, dict[str, Any]] = {}
    # split at "\n" only: str.splitlines() would also split at characters a password may contain
    for number, raw_line in enumerate(answers.split("\n"), start=1):
        line = raw_line.removesuffix("\r")
        if not line.strip():
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep:
            raise ConfigError(f"answers line {number}: expected key=value")
        if value == "":
            continue
        table, _, name = key.rpartition(".")
        if table == "target":
            if not _is_id(name):  # before it becomes a TOML table header
                raise ConfigError(f"{key}: alias must be {ID_RULE}")
            tables.setdefault(f"targets.{name}", {}).update(_target_answer(key, value))
            continue
        kind = _ANSWER_TYPES.get(key)
        if kind is None:
            raise ConfigError(f"{key}: unknown answer")
        tables.setdefault(table, {})[name] = _convert(key, value, kind)
    text = HEADER + "".join(_toml_table(name, values) for name, values in tables.items())
    parse_config(text)  # the same validation as at startup
    return text


def config_answers(text: str) -> str:
    """The installer answers for a configuration: the inverse of `render_config`.

    Only the settings present in the file are listed, so settings left at their default keep
    following the default. Public keys are normalised. The installer uses the answers as the
    defaults when it reconfigures an installation, so settings it does not ask for are kept.
    """
    parse_config(text)  # only valid files
    lines = []
    for table, values in tomllib.loads(text).items():
        for name, value in values.items():
            if table == "targets":
                kind = "channel" if "channel" in value else "contact"
                ref = value[kind] if kind == "channel" else normalise_key(value[kind])
                lines.append(f"target.{name}={kind}:{ref}")
            elif (table, name) == ("commands", "senders"):
                lines.append(f"commands.senders={','.join(normalise_key(k) for k in value)}")
            elif isinstance(value, dict):  # a sub-table, such as [radio.profile]
                lines += [
                    f"{table}.{name}.{key}={_answer_value(f'{table}.{name}.{key}', item)}"
                    for key, item in value.items()
                ]
            else:
                lines.append(f"{table}.{name}={_answer_value(f'{table}.{name}', value)}")
    return "".join(f"{line}\n" for line in lines)


def _answer_value(key: str, value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        if any("," in str(item) for item in value):
            raise ConfigError(f"{key}: an item contains a comma, which the answers cannot express")
        text = ",".join(str(item) for item in value)
    else:
        text = str(value)
    if "\n" in text or "\r" in text:
        raise ConfigError(f"{key}: contains a line break, which the answers cannot express")
    return text


def _target_answer(key: str, value: str) -> dict[str, Any]:
    kind, _, ref = value.partition(":")
    if kind == "channel" and ref.isascii() and ref.isdigit():  # isdigit() alone accepts "²"
        return {"channel": int(ref)}
    if kind == "contact" and ref:
        return {"contact": ref}
    raise ConfigError(f"{key}: expected channel:<index> or contact:<public key>, got {value!r}")


def _convert(key: str, value: str, kind: type) -> Any:
    if kind is bool and value in ("true", "false"):
        return value == "true"
    try:
        if kind is list:
            items = [v.strip() for v in value.split(",") if v.strip()]
            return [int(v) for v in items] if key.endswith("channels") else items
        if kind is not bool:
            return kind(value)
    except ValueError:
        pass
    raise ConfigError(f"{key}: cannot read {value!r} as {kind.__name__}")


def _toml_table(name: str, values: Mapping[str, Any]) -> str:
    lines = [f"\n[{name}]"]
    lines += [f"{key} = {_toml_value(value)}" for key, value in values.items()]
    return "\n".join(lines) + "\n"


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, Iterable):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"cannot write {type(value).__name__} to TOML")


def _toml_string(value: str) -> str:
    out = []
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'
