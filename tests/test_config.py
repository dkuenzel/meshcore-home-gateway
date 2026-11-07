"""Configuration: TOML parsing with strict validation, and rendering from installer answers."""

import base64
import re
import tomllib
from dataclasses import fields
from pathlib import Path

import pytest

from meshcore_home_gateway import config as config_module
from meshcore_home_gateway.config import (
    Config,
    ConfigError,
    HomieConfig,
    config_answers,
    load_config,
    parse_config,
    render_config,
    with_password_file,
)
from meshcore_home_gateway.core.model import MessageKind

KEY = "a1" * 32

MINIMAL = """
[radio]
transport = "serial"
device = "/dev/serial/by-id/usb-Example_Radio-if00"

[mqtt]
host = "broker.example.org"
node_id = "radio1"
"""

FULL = f"""
[radio]
transport = "tcp"
tcp_unauthenticated = true
host = "192.0.2.10"
port = 5000
set_clock = false
set_clock_max_days = 7

[radio.profile]
manage = "observe"
name = "Gateway Süd"
duty_cycle_percent = 25.0
protect_contacts = false

[mqtt]
host = "192.0.2.20"
port = 8883
username = "bridge"
password = "s3cret \\"quoted\\""
tls = true
ca_file = "/etc/ssl/certs/example-ca.pem"
base_topic = "mesh/home"
node_id = "radio-2"
keepalive_s = 30

[homeassistant]
discovery = true
prefix = "ha"
device_name = "Mesh radio"

[messages]
ascii_only = true
truncate = false
dedup_window_s = 120
queue_size = 5
queue_ttl_s = 60
max_attempts = 4
flood_after = 3
ignore_channels = [0]
publish_channels = "targets"
clean_received = true
confirm_nonce = true
alert_targets = ["alerts"]

[airtime]
budget_s_per_hour = 72
burst_s = 12

[commands]
senders = ["{KEY.upper()}"]
words = ["Status", "arm"]
max_age_s = 300
reply = "all"

[targets.alerts]
channel = 1
prefix = "Haus: "

[targets.alice]
contact = "{KEY}"
suffix = " (gw)"

[history]
enabled = false
messages_days = 7
positions_days = 90
readings_days = 14.5

[telemetry]
enabled = true
ask_types = ["chat", "sensor"]

[diagnostics]
publish_rx_log = true
log_level = "debug"
"""


def test_minimal_configuration_gets_safe_defaults() -> None:
    config = parse_config(MINIMAL)
    assert config.radio.transport == "serial"
    assert config.radio.device == "/dev/serial/by-id/usb-Example_Radio-if00"
    assert config.mqtt.port == 1883
    assert config.mqtt.base_topic == "meshcore"
    assert config.homeassistant.discovery is True
    assert config.airtime.budget_s_per_hour == 36.0
    assert config.messages.max_attempts == 3
    assert config.messages.confirm_nonce is False  # opt-in: the recipient sees the suffix
    assert config.commands.senders == frozenset()
    assert config.targets == ()


def test_full_configuration_is_read_completely() -> None:
    config = parse_config(FULL)
    assert (config.radio.transport, config.radio.host, config.radio.port) == (
        "tcp",
        "192.0.2.10",
        5000,
    )
    assert config.radio.tcp_unauthenticated is True
    assert config.radio.set_clock is False
    assert config.radio.set_clock_max_days == 7
    assert config.mqtt.password == 's3cret "quoted"'
    assert config.mqtt.tls is True
    assert config.mqtt.base_topic == "mesh/home"
    assert config.homeassistant.prefix == "ha"
    assert config.messages.ignore_channels == frozenset({0})
    assert config.messages.publish_channels == "targets"
    assert config.messages.clean_received is True
    assert config.messages.confirm_nonce is True
    assert config.messages.alert_targets == frozenset({"alerts"})
    assert config.commands.senders == frozenset({KEY})
    assert config.commands.words == frozenset({"status", "arm"})
    assert config.commands.reply == "all"
    alerts, alice = sorted(config.targets, key=lambda t: t.alias)
    assert (alerts.alias, alerts.kind, alerts.channel_index) == ("alerts", MessageKind.CHANNEL, 1)
    assert (alice.kind, alice.public_key) == (MessageKind.CONTACT, KEY)
    assert (alerts.prefix, alerts.suffix, alice.prefix, alice.suffix) == ("Haus: ", "", "", " (gw)")
    assert config.diagnostics.log_level == "debug"
    assert (config.history.enabled, config.history.messages_days) == (False, 7.0)
    assert (config.history.positions_days, config.history.readings_days) == (90.0, 14.5)


def test_the_radio_clock_steps_at_most_30_days_by_default() -> None:
    """SEC-002: a larger step from a set radio clock is not written; 0 lifts the limit."""
    radio = parse_config(MINIMAL).radio
    assert (radio.set_clock, radio.set_clock_max_days) == (True, 30)
    assert MINIMAL.count("[radio]\n") == 1
    for days in (0, 3650):
        text = MINIMAL.replace("[radio]\n", f"[radio]\nset_clock_max_days = {days}\n")
        assert parse_config(text).radio.set_clock_max_days == days
    for invalid in ("-1", "3651", "1.5", '"30"'):
        text = MINIMAL.replace("[radio]\n", f"[radio]\nset_clock_max_days = {invalid}\n")
        with pytest.raises(ConfigError, match=r"radio\.set_clock_max_days"):
            parse_config(text)


COMMANDS = f'[commands]\nsenders = ["{KEY}"]\nwords = ["status"]\n'


def test_telemetry_asks_companions_and_on_request_sensors() -> None:
    """EXT-005: companions by default, as before; sensors only when named; no other type, and
    an empty list is refused (`enabled = false` says that)."""
    assert parse_config(MINIMAL).telemetry.ask_types == frozenset({"chat"})
    assert config_module.TelemetryConfig().node_types == frozenset({1})  # Config() as well
    assert parse_config(MINIMAL).telemetry.node_types == frozenset({1})
    assert parse_config(FULL).telemetry.node_types == frozenset({1, 4})
    for invalid, error in (
        ('["repeater"]', "one of chat, sensor, got 'repeater'"),
        ('["room"]', "one of chat, sensor"),
        ("[]", "name at least one type"),
        ('"sensor"', ""),
    ):
        with pytest.raises(ConfigError, match=rf"telemetry\.ask_types: .*{error}"):
            parse_config(MINIMAL + f"[telemetry]\nask_types = {invalid}\n")


def test_commands_are_answered_only_when_asked() -> None:
    """EXT-009: no answer by default, as before; only the three known ways."""
    assert parse_config(MINIMAL).commands.reply == "none"
    text = _broker("broker.example.org", USER, tls=True) + COMMANDS
    assert parse_config(text + 'reply = "accepted"\n').commands.reply == "accepted"
    for invalid in ('"yes"', "true", '"All"'):
        with pytest.raises(ConfigError, match=r"commands\.reply"):
            parse_config(text + f"reply = {invalid}\n")


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ('""', "expected printable text"),
        ("1", "expected printable text"),
        ('"a\\tb"', "expected printable text"),
        ('"a\\nb"', "expected printable text"),
        ('"\\u202e"', "expected printable text"),
        (f'"{"ü" * 17}"', "at most 32 bytes, got 34"),
    ],
)
def test_a_target_s_prefix_and_suffix_are_short_printable_text(value: str, error: str) -> None:
    """EXT-017: they go into every message to the target; no control or direction characters,
    and room for the text stays."""
    for affix in ("prefix", "suffix"):
        with pytest.raises(ConfigError, match=rf"targets\.alerts\.{affix}: {error}"):
            parse_config(MINIMAL + f"[targets.alerts]\nchannel = 1\n{affix} = {value}\n")
    text = (
        MINIMAL
        + '[targets.alerts]\nchannel = 1\nprefix = "\u00fc  "\nsuffix = "'
        + "x" * 32
        + '"\n'
    )
    (alerts,) = parse_config(text).targets
    assert (alerts.prefix, alerts.suffix) == ("\u00fc  ", "x" * 32)  # spaces as written
    decomposed = MINIMAL + '[targets.alerts]\nchannel = 1\nprefix = "u\u0308"\n'
    assert parse_config(decomposed).targets[0].prefix == "\u00fc"  # NFC, as the text


def test_a_target_s_prefix_and_suffix_survive_the_answers_with_their_spaces() -> None:
    """The installer's reconfigure writes them back as they were; one of a target that is gone
    is refused (the installer drops them with their target)."""
    answers = config_answers(FULL)
    assert "target.alerts.prefix=Haus: \n" in answers
    assert "target.alice.suffix= (gw)\n" in answers
    assert parse_config(render_config(answers)) == parse_config(FULL)
    with pytest.raises(ConfigError, match=r"targets\.gone"):
        render_config(config_answers(MINIMAL) + "target.gone.prefix=x\n")
    with pytest.raises(ConfigError, match=r"target\.Bad\.prefix: alias must be"):
        render_config(config_answers(MINIMAL) + "target.Bad.prefix=x\n")


def _broker(host: str, login: str = "", tls: bool = False) -> str:
    return (
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"\n'
        f'[mqtt]\nhost = "{host}"\nnode_id = "radio1"\n{login}' + ("tls = true\n" if tls else "")
    )


USER = 'username = "gateway"\npassword = "pw"\n'


@pytest.mark.parametrize(
    ("text", "missing"),
    [
        (_broker("broker.example.org"), "credentials"),
        (_broker("127.0.0.1"), "credentials"),  # the host's other users, or the network
        (_broker("broker.example.org", tls=True), "credentials"),
        (_broker("broker.example.org", USER), "TLS"),
        (_broker("192.168.1.2", USER), "TLS"),
    ],
)
def test_commands_need_a_broker_connection_with_credentials_and_tls(
    text: str, missing: str
) -> None:
    """Command events reach the automations through the broker: anyone who can log in, or read
    a password on the network, could publish them (SEC-004). `insecure = true` accepts it."""
    with pytest.raises(ConfigError, match=rf"mqtt\.insecure: .* has no {missing}: any client"):
        parse_config(text + COMMANDS)
    parse_config(text)  # without commands: warnings only
    assert parse_config(text + "insecure = true\n" + COMMANDS).mqtt.insecure is True


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "::1", "169.254.10.20", "fe80::1"])
def test_tls_is_not_needed_on_the_host_or_the_link(host: str) -> None:
    config = parse_config(_broker(host, USER) + COMMANDS)
    assert config.mqtt.local
    assert not config.mqtt.insecure
    assert parse_config(_broker("broker.example.org", USER, tls=True) + COMMANDS).mqtt.tls
    assert not parse_config(_broker("mosquitto.local", USER)).mqtt.local


def test_the_history_is_on_by_default_and_bounded() -> None:
    history = parse_config(MINIMAL).history
    assert (history.enabled, history.messages_days, history.positions_days) == (True, 30.0, 30.0)
    assert history.readings_days == 30.0
    with pytest.raises(ConfigError, match=r"history\.messages_days: must be between 1\.0 and"):
        parse_config(MINIMAL + "\n[history]\nmessages_days = 0\n")
    for days in (1, 3650):
        text = MINIMAL + f"\n[history]\nreadings_days = {days}\n"
        assert parse_config(text).history.readings_days == days
    for invalid in ("0", "3651", '"30"'):
        with pytest.raises(ConfigError, match=r"history\.readings_days"):
            parse_config(MINIMAL + f"\n[history]\nreadings_days = {invalid}\n")


def test_the_homie_device_is_off_unless_enabled_and_its_nodes_bounded() -> None:
    """EXT-011: opt-in; at most as many nodes as Home Assistant could have contact devices."""
    homie = parse_config(MINIMAL).homie
    assert (homie.enabled, homie.contacts) == (False, 50)
    homie = parse_config(MINIMAL + "\n[homie]\nenabled = true\ncontacts = 0\n").homie
    assert (homie.enabled, homie.contacts) == (True, 0)
    assert parse_config(MINIMAL + "\n[homie]\ncontacts = 1000\n").homie.contacts == 1000
    for invalid in ("-1", "1001", "true", '"10"'):
        with pytest.raises(ConfigError, match=r"homie\.contacts"):
            parse_config(MINIMAL + f"\n[homie]\ncontacts = {invalid}\n")
    with pytest.raises(ConfigError, match=r"homie\.root: unknown setting"):
        parse_config(MINIMAL + '\n[homie]\nroot = "devices"\n')
    rendered = render_config(ANSWERS + "homie.enabled=true\nhomie.contacts=8\n")
    assert parse_config(rendered).homie == HomieConfig(enabled=True, contacts=8)


@pytest.mark.parametrize(
    ("snippet", "message"),
    [
        ('[radio]\ntransport = "carrier-pigeon"', "radio.transport"),
        ('[radio]\ntransport = "serial"', "radio.device"),
        ('[radio]\ntransport = "tcp"', "radio.host"),
        (
            '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\nbaud = 1200',
            "radio.baud: unknown",
        ),
        ('[radio]\ntransport = "serial"\ndevice = "ttyACM0"', "radio.device: must be an absolute"),
        (
            '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\nhost = "192.0.2.1"',
            "radio.host: only used with transport = 'tcp'",
        ),
        ('[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\nport = 5000', "radio.port: only"),
        (
            '[radio]\ntransport = "tcp"\nhost = "192.0.2.1"\ndevice = "/dev/ttyACM0"',
            "radio.device: only used with transport = 'serial'",
        ),
        (  # SEC-003: anyone who reaches the interface owns the radio and its key
            '[radio]\ntransport = "tcp"\nhost = "192.0.2.1"',
            "radio.tcp_unauthenticated: the radio's TCP interface has no authentication: anyone",
        ),
        (
            '[radio]\ntransport = "tcp"\nhost = "192.0.2.1"\ntcp_unauthenticated = false',
            "radio.tcp_unauthenticated: the radio's TCP interface has no authentication",
        ),
        (
            '[radio]\ntransport = "tcp"\nhost = "192.0.2.1"\ntcp_unauthenticated = "yes"',
            "radio.tcp_unauthenticated: expected true or false",
        ),
        (
            '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\ntcp_unauthenticated = true',
            "radio.tcp_unauthenticated: only used with transport = 'tcp'",
        ),
        ('[mqtt]\nnode_id = "n"', "mqtt.host: required"),
        ('[mqtt]\nhost = "h"\nnode_id = "Bad Name"', "mqtt.node_id"),
        ('[mqtt]\nhost = "h"\nnode_id = "_lab"', "mqtt.node_id: .*starting with a letter or digit"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\npassword = "x"', "mqtt.password: needs username"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\ntls = "false"', "mqtt.tls: expected true or false"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nport = 70000', "mqtt.port"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nbase_topic = "a/#"', "mqtt.base_topic"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nbase_topic = "/a"', "mqtt.base_topic"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nport = "1883"', "mqtt.port: expected an integer"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nkeyfile = "/k"\ntls = true', "mqtt.keyfile: unknown"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nca_file = "/ca.pem"', "mqtt.ca_file: only used"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\ncert_file = "/c.pem"\ntls = false', "mqtt.cert_file"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nkey_file = "/k.pem"\ntls = true', "needs cert_file"),
        (
            '[mqtt]\nhost = "h"\nnode_id = "n"\ntls = true\nca_file = "ca.pem"',
            "mqtt.ca_file: must be an absolute path",
        ),  # the service runs in /, check-config wherever it is started
        (
            '[mqtt]\nhost = "h"\nnode_id = "n"\ntls = true\ncert_file = "./c.pem"',
            "mqtt.cert_file: must be an absolute path",
        ),
        ('[homeassistant]\nprefix = "a/b"', "homeassistant.prefix: a single topic segment"),
        ("[airtime]\nbudget_s_per_hour = 0", "airtime.budget_s_per_hour"),
        ("[airtime]\nburst_s = 2", "airtime.burst_s"),
        ("[messages]\nmax_attempts = 0", "messages.max_attempts"),
        ('[messages]\nqueue_ttl_s = "60"', "messages.queue_ttl_s: expected a number"),
        ("[messages]\nignore_channels = [300]", "messages.ignore_channels: expected a list"),
        ('[messages]\npublish_channels = "some"', "messages.publish_channels: one of"),
        ('[commands]\nsenders = ["a1b2c3d4e5f6"]\nwords = ["arm"]', "commands.senders"),
        ('[commands]\nwords = ["two words"]', "commands.words"),
        ('[commands]\nwords = "arm"', "commands.words: expected a list of strings"),
        ("[commands]\nwords = [1]", "commands.words: expected a list of strings"),
        ("[targets]\nalerts = 1", "targets.alerts: expected a table"),
        ("[targets.Alerts]\nchannel = 1", "targets.Alerts"),
        ("[targets._alerts]\nchannel = 1", "targets._alerts: .*starting with a letter or digit"),
        ("[targets.alerts]\nchannel = 1\ncontact = 'x'", "targets.alerts"),
        ("[targets.alerts]\nchannel = 256", "targets.alerts.channel"),
        ("[targets.x]\ncontact = 'zz'", "targets.x.contact"),
        ('[diagnostics]\nlog_level = "loud"', "diagnostics.log_level"),
        ("[unknown]\nx = 1", "unknown: unknown"),
    ],
)
def test_invalid_settings_name_the_offending_key(snippet: str, message: str) -> None:
    radio_part, _, mqtt_part = MINIMAL.partition("[mqtt]")
    base = {"radio": radio_part, "mqtt": "[mqtt]" + mqtt_part}
    section = snippet.partition("]")[0].lstrip("[").partition(".")[0]
    text = "\n".join(v for k, v in base.items() if k != section) + "\n" + snippet
    with pytest.raises(ConfigError, match=message):
        parse_config(text)


def test_files_that_are_not_utf8_are_config_errors(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes(b'[mqtt]\nhost = "\xff"\n')
    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(path)


def test_blank_strings_are_refused() -> None:
    with pytest.raises(ConfigError, match=r"mqtt\.host: expected a non-empty string"):
        parse_config(MINIMAL.replace('host = "broker.example.org"', 'host = "  "'))


def test_syntax_errors_are_config_errors() -> None:
    with pytest.raises(ConfigError, match="TOML"):
        parse_config("[radio\n")


def test_load_config_reads_a_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL, encoding="utf-8")
    assert load_config(path).mqtt.node_id == "radio1"
    with pytest.raises(ConfigError, match="cannot read"):
        load_config(tmp_path / "missing.toml")


ANSWERS = f"""
radio.transport=serial
radio.device=/dev/serial/by-id/usb-Example_Radio-if00
mqtt.host=broker.example.org
mqtt.port=1883
mqtt.username=bridge
mqtt.password=p"a\\ss=word
mqtt.tls=false
mqtt.node_id=radio1
mqtt.insecure=true
homeassistant.discovery=true
homeassistant.device_name=Mesh radio
messages.ascii_only=true
target.alerts=channel:1
target.alice=contact:{KEY}
commands.senders={KEY}
commands.words=status,arm
"""


def test_rendered_configuration_is_valid_and_complete() -> None:
    text = render_config(ANSWERS)
    config = parse_config(text)
    assert config.mqtt.password == 'p"a\\ss=word'
    assert config.messages.ascii_only is True
    assert {t.alias for t in config.targets} == {"alerts", "alice"}
    assert config.commands.words == frozenset({"status", "arm"})
    assert tomllib.loads(text)["mqtt"]["username"] == "bridge"
    assert text.startswith("# meshcore-home-gateway configuration")


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        ("mqtt.colour=blue", r"mqtt\.colour: unknown answer"),
        ("mqtt.port=abc", r"mqtt\.port: cannot read 'abc' as int"),
        ("mqtt.tls=maybe", r"mqtt\.tls: cannot read 'maybe' as bool"),
        ("target.x=planet:3", r"target\.x: expected channel"),
        ("target.x=channel:\u00b2", r"target\.x: expected channel"),  # a digit, but not 0-9
        ("target.news]#x=channel:2", r"target\.news\]#x: alias must be"),  # would end the header
        ("target.my news=channel:2", r"target\.my news: alias must be"),
        ("target._news=channel:2", r"target\._news: .*starting with a letter or digit"),
    ],
)
def test_rendering_names_the_offending_answer(answer: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        render_config(ANSWERS + answer + "\n")


def test_rendering_rejects_lines_without_an_equals_sign() -> None:
    with pytest.raises(ConfigError, match="line 1"):
        render_config("no equals sign")


@pytest.mark.parametrize(
    ("value", "shown"),
    [("nan", "nan"), ("NaN", "nan"), ("inf", "inf"), ("Infinity", "inf"), ("-inf", "-inf")],
)
def test_a_number_that_is_no_number_is_refused_with_its_range(value: str, shown: str) -> None:
    """CODE-014: `float()` reads "nan" and "inf", and so does TOML; the range check refuses them
    (a comparison with NaN is false, every range has an upper end) and says what it takes."""
    message = rf"airtime\.burst_s: must be between \S+ and 3600\.0, got {re.escape(shown)}$"
    with pytest.raises(ConfigError, match=message):
        render_config(ANSWERS + f"airtime.burst_s={value}\n")
    rendered = render_config(ANSWERS)
    assert "[airtime]" not in rendered
    with pytest.raises(ConfigError, match=message):
        parse_config(rendered + f"\n[airtime]\nburst_s = {shown}\n")


@pytest.mark.parametrize(
    "invisible",
    ["\u2028", "\u2029", "\u200b", "\u200e", "\u202e", "\u2066", "\ufeff", "\u0085", "\U000e0001"],
)
def test_written_strings_show_invisible_characters_escaped(invisible: str) -> None:
    """CODE-015: a line or paragraph separator, a bidirectional control, a zero-width or other
    format character, which TOML would take as they are, is written as an escape that an editor
    shows; the value reads back the same."""
    answers = ANSWERS.replace("mqtt.username=bridge", f"mqtt.username=bri{invisible}dge")
    text = render_config(answers)
    line = next(line for line in text.split("\n") if line.startswith("username = "))
    assert invisible not in line
    assert line.isascii()
    assert tomllib.loads(text)["mqtt"]["username"] == f"bri{invisible}dge"


def test_a_tcp_radio_is_answered_with_its_acknowledgement() -> None:
    """The installer writes `radio.tcp_unauthenticated` once the user accepted the risk."""
    serial = "radio.transport=serial\nradio.device=/dev/serial/by-id/usb-Example_Radio-if00\n"
    tcp = ANSWERS.replace(serial, "radio.transport=tcp\nradio.host=192.0.2.10\n")
    accepted = parse_config(render_config(tcp + "radio.tcp_unauthenticated=true\n"))
    assert accepted.radio.tcp_unauthenticated is True
    with pytest.raises(ConfigError, match=r"radio\.tcp_unauthenticated: the radio's TCP"):
        parse_config(render_config(tcp))


def test_rendering_escapes_control_characters() -> None:
    text = render_config(ANSWERS.replace("device_name=Mesh radio", "device_name=Mesh\tradio"))
    assert parse_config(text).homeassistant.device_name == "Mesh\tradio"


def test_the_shipped_example_is_valid() -> None:
    """It follows the defaults of telemetry and the history of readings: a copy of it changes
    with them."""
    example = Path(__file__).resolve().parent.parent / "deploy" / "config.example.toml"
    config = load_config(example)
    assert {t.alias for t in config.targets} == {"alerts", "operator"}
    defaults = parse_config(MINIMAL)
    assert config.telemetry == defaults.telemetry
    assert config.history.readings_days == defaults.history.readings_days
    text = example.read_text(encoding="utf-8")
    assert "\n# enabled = true " in text.split("[telemetry]")[1]
    assert "\n# readings_days = 30 " in text.split("[history]")[1]
    assert "contacts telemetry KEY on" in text


def test_every_setting_can_be_answered() -> None:
    """The installer answers cover the whole configuration (docs/configuration.md)."""
    answers = ANSWERS + "\n".join(
        [
            "radio.set_clock=false",
            "mqtt.tls=true",
            "mqtt.cert_file=/etc/c.pem",
            "mqtt.key_file=/etc/k.pem",
            "mqtt.client_id=bridge-a",
            "mqtt.keepalive_s=30",
            "messages.truncate=false",
            "messages.dedup_window_s=60",
            "messages.queue_size=5",
            "messages.queue_ttl_s=120",
            "messages.max_attempts=4",
            "messages.flood_after=3",
            "messages.ignore_channels=0,2",
            "airtime.burst_s=6.5",
            "commands.max_age_s=0",
            "diagnostics.publish_rx_log=true",
            "contacts.manage=false",
            "history.readings_days=7.5",
            "messages.clean_received=true",
            "messages.publish_channels=targets",
            "contacts.heard_nodes=off",
            "telemetry.ask=household",
        ]
    )
    config = parse_config(render_config(answers))
    assert config.radio.set_clock is False
    assert config.contacts.heard_nodes == "off"
    assert config.telemetry.ask == "household"
    assert config.messages.clean_received is True
    assert config.messages.publish_channels == "targets"
    assert (config.mqtt.tls, config.mqtt.cert_file, config.mqtt.key_file) == (
        True,
        "/etc/c.pem",
        "/etc/k.pem",
    )
    assert (config.mqtt.client_id, config.mqtt.keepalive_s) == ("bridge-a", 30)
    assert (config.messages.max_attempts, config.messages.flood_after) == (4, 3)
    assert config.messages.ignore_channels == frozenset({0, 2})
    assert config.airtime.burst_s == 6.5
    assert config.commands.max_age_s == 0
    assert config.diagnostics.publish_rx_log is True
    assert config.contacts.manage is False
    assert config.history.readings_days == 7.5


# --- answers from a configuration (the installer's reconfigure mode) ---------------------------


@pytest.mark.parametrize("text", [MINIMAL, FULL], ids=["minimal", "full"])
def test_answers_render_back_to_the_same_configuration(text: str) -> None:
    answers = config_answers(text)
    assert parse_config(render_config(answers)) == parse_config(text)


def settings_read(monkeypatch: pytest.MonkeyPatch, *texts: str) -> set[str]:
    """The dotted names of every table and setting that parsing these files looks up."""
    read: set[str] = set()
    original = config_module._Section.raw

    def raw(self: config_module._Section, name: str) -> object:
        read.add(self.key(name))
        return original(self, name)

    monkeypatch.setattr(config_module._Section, "raw", raw)
    for text in texts:
        parse_config(text)
    return read


def test_every_setting_has_an_answer_type(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without one, `--reconfigure` could not write the setting back (answers, then render)."""
    read = settings_read(monkeypatch, MINIMAL, FULL)
    tables = {key.rpartition(".")[0] for key in read}
    settings = {
        key for key in read if "." in key and not key.startswith("targets.") and key not in tables
    }
    assert len(settings) > 30
    assert settings - set(config_module._ANSWER_TYPES) == set()


def test_every_part_of_the_configuration_comes_from_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field that no table of the file fills would look like a setting that cannot be set."""
    tables = {key for key in settings_read(monkeypatch, FULL) if "." not in key}
    assert tables == {f.name for f in fields(Config)}


def test_answers_contain_only_the_settings_of_the_file() -> None:
    """Settings left at their default stay unset, so a later default change still applies."""
    lines = config_answers(MINIMAL).splitlines()
    assert lines == [
        "radio.transport=serial",
        "radio.device=/dev/serial/by-id/usb-Example_Radio-if00",
        "mqtt.host=broker.example.org",
        "mqtt.node_id=radio1",
    ]


def test_answers_use_normalised_public_keys() -> None:
    """The installer compares them with the keys the radio reports (lower case)."""
    answers = config_answers(FULL)
    assert f"commands.senders={KEY}\n" in answers
    assert f"target.alice=contact:{KEY}\n" in answers
    assert "target.alerts=channel:1\n" in answers


def test_answers_keep_unusual_characters() -> None:
    """Commas, '=', quotes and Unicode line separators survive (answers are split at \\n only)."""
    text = MINIMAL + 'username = "u"\npassword = "p\\u2028w,=\\"x"\n'
    config = parse_config(render_config(config_answers(text)))
    assert config.mqtt.password == 'p\u2028w,="x'


def test_answers_refuse_values_with_line_breaks() -> None:
    with pytest.raises(ConfigError, match=r"mqtt\.password: contains a line break"):
        config_answers(MINIMAL + 'username = "u"\npassword = "a\\nb"\n')


def test_answers_refuse_list_items_with_commas() -> None:
    """The answers separate list items by commas; a command word may contain one."""
    with pytest.raises(ConfigError, match=r"commands\.words: an item contains a comma"):
        config_answers(MINIMAL + '\n[commands]\nwords = ["a,b"]\n')


def test_the_password_can_be_in_a_file_of_its_own(tmp_path: Path) -> None:
    """SEC-009: `password_file` instead of `password`, an absolute path that load_config reads
    (one line break at the end dropped); parse_config does not, so `config answers` prints the
    path and never the password."""
    secret = tmp_path / "mqtt-password"
    secret.write_text("s3cret \n", encoding="utf-8")
    login = f'username = "bridge"\npassword_file = "{secret}"\n'
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL + login, encoding="utf-8")
    assert parse_config(path.read_text(encoding="utf-8")).mqtt.password is None
    mqtt = load_config(path).mqtt
    assert (mqtt.password, mqtt.password_file) == ("s3cret ", str(secret))
    assert f"mqtt.password_file={secret}" in config_answers(path.read_text(encoding="utf-8"))
    for content, message in ((b"", "holds no password"), (b"\xff", "not valid UTF-8")):
        secret.write_bytes(content)
        with pytest.raises(ConfigError, match=message):
            load_config(path)
    secret.unlink()
    with pytest.raises(ConfigError, match=r"mqtt\.password_file: cannot read"):
        load_config(path)


def test_a_systemd_credential_of_the_password_file_s_name_is_read_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LoadCredential= hands the service a copy; the file itself may be root's alone."""
    secret = tmp_path / "etc" / "mqtt-password"
    secret.parent.mkdir()
    secret.write_text("from the file\n", encoding="utf-8")
    path = tmp_path / "config.toml"
    path.write_text(MINIMAL + f'username = "u"\npassword_file = "{secret}"\n', encoding="utf-8")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    assert load_config(path).mqtt.password == "from the file"  # no credential of that name
    (credentials / "mqtt-password").write_text("from systemd", encoding="utf-8")
    assert load_config(path).mqtt.password == "from systemd"


@pytest.mark.parametrize(
    ("login", "message"),
    [
        ('username = "u"\npassword = "p"\npassword_file = "/etc/p"', "not both"),
        ('password_file = "/etc/p"', r"mqtt\.password_file: needs username"),
        ('username = "u"\npassword_file = "p"', r"mqtt\.password_file"),
    ],
    ids=["both", "no-user", "relative"],
)
def test_a_password_file_needs_a_user_an_absolute_path_and_no_password(
    login: str, message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        parse_config(MINIMAL + login + "\n")


def test_the_mqtt_protocol_is_3_1_1_unless_5_is_set() -> None:
    """ARC-014: MQTT 5 is an option; a broker that does not speak it refuses the connection."""
    assert parse_config(MINIMAL).mqtt.protocol == "3.1.1"
    assert parse_config(MINIMAL + 'protocol = "5"\n').mqtt.protocol == "5"
    for value in ('"4"', "5"):
        with pytest.raises(ConfigError, match=r"mqtt\.protocol"):
            parse_config(MINIMAL + f"protocol = {value}\n")


CODE_SECRET = "JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP"  # 160 bits


def _coded_config(tmp_path: Path, codes: str) -> Path:
    codes_file = tmp_path / "command-codes"
    codes_file.write_text(codes, encoding="utf-8")
    path = tmp_path / "config.toml"
    commands = f'[commands]\nsenders = ["{KEY}"]\nwords = ["arm"]\ncodes_file = "{codes_file}"\n'
    path.write_text(MINIMAL + "insecure = true\n" + commands, encoding="utf-8")
    return path


def test_the_senders_code_secrets_are_in_a_file_of_their_own(tmp_path: Path) -> None:
    """IMP-002: load_config reads them (any case, spaces allowed, as apps show a secret);
    parse_config does not, so `config answers` prints the path and never a secret."""
    spaced = f"{CODE_SECRET[:16].lower()} {CODE_SECRET[16:]}"
    path = _coded_config(tmp_path, f'# the codes\n{KEY.upper()} = "{spaced}"\n')
    text = path.read_text(encoding="utf-8")
    assert parse_config(text).commands.secrets == ()
    assert load_config(path).commands.secrets == ((KEY, base64.b32decode(CODE_SECRET)),)
    assert f"commands.codes_file={tmp_path / 'command-codes'}" in config_answers(text)


@pytest.mark.parametrize(
    ("codes", "message"),
    [
        (f'{KEY} = "not base32!"\n', "must be base32 of at least 128 bits"),
        (f'{KEY} = "{CODE_SECRET[:16]}"\n', "must be base32 of at least 128 bits"),
        (f"{KEY} = 12\n", "must be base32"),
        (f'nokey = "{CODE_SECRET}"\n', r"^commands\.codes_file: "),
        ("not toml", r"^commands\.codes_file: .*command-codes"),
        ("\udcff", "not valid UTF-8"),
    ],
    ids=["not-base32", "80-bits", "number", "no-key", "not-toml", "not-utf8"],
)
def test_a_code_file_that_holds_anything_else_is_refused(
    tmp_path: Path, codes: str, message: str
) -> None:
    path = _coded_config(tmp_path, "")
    (tmp_path / "command-codes").write_bytes(codes.encode("utf-8", "surrogateescape"))
    with pytest.raises(ConfigError, match=message):
        load_config(path)


def test_the_secret_of_a_key_that_is_no_sender_is_ignored(tmp_path: Path) -> None:
    """#80: removing a sender ends its commands and nothing else. Its secret stays in the file
    until `commands revoke` removes it; the gateway ignores it and says so at the start. A key
    that is no sender cannot command, so its secret changes no verdict. A malformed secret is
    refused all the same: the file is the one `enrol` writes."""
    other = "b2" * 32
    path = _coded_config(tmp_path, f'{KEY} = "{CODE_SECRET}"\n{other} = "{CODE_SECRET}"\n')
    commands = load_config(path).commands
    assert commands.secrets == ((KEY, base64.b32decode(CODE_SECRET)),)
    assert commands.ignored_secrets == (other,)
    (tmp_path / "command-codes").write_text(f'{other} = "not base32!"\n', encoding="utf-8")
    with pytest.raises(ConfigError, match="must be base32"):
        load_config(path)


def test_a_missing_code_file_is_refused_rather_than_taken_as_none(tmp_path: Path) -> None:
    """Commands would no longer need their codes without anyone noticing."""
    path = _coded_config(tmp_path, "")
    (tmp_path / "command-codes").unlink()
    with pytest.raises(ConfigError, match=r"commands\.codes_file: cannot read"):
        load_config(path)


def test_a_systemd_credential_of_the_code_file_s_name_is_read_instead(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _coded_config(tmp_path, "")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "command-codes").write_text(f'{KEY} = "{CODE_SECRET}"\n', encoding="utf-8")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(credentials))
    assert load_config(path).commands.secrets == ((KEY, base64.b32decode(CODE_SECRET)),)


def test_the_code_file_has_an_absolute_path() -> None:
    with pytest.raises(ConfigError, match=r"commands\.codes_file: must be an absolute path"):
        parse_config(MINIMAL + 'insecure = true\n[commands]\ncodes_file = "codes"\n')


def test_answers_put_the_password_into_a_file() -> None:
    """What `config render --password-file` renders (SEC-009)."""
    answers = "mqtt.host=b\nmqtt.password=a=b\nmqtt.password_file=/old\nmqtt.port=1\n"
    moved, password = with_password_file(answers, Path("/etc/pw"))
    assert password == "a=b"
    assert moved == "mqtt.host=b\nmqtt.port=1\n\nmqtt.password_file=/etc/pw\n"
    assert with_password_file("mqtt.host=b\n", Path("/etc/pw")) == ("mqtt.host=b\n", None)
    assert with_password_file("mqtt.password=\n", Path("/etc/pw"))[1] is None  # none


def test_answers_refuse_invalid_files() -> None:
    with pytest.raises(ConfigError, match=r"radio\.device"):
        config_answers('[radio]\ntransport = "serial"\n')


def test_rendering_accepts_windows_line_ends() -> None:
    config = parse_config(render_config(ANSWERS.replace("\n", "\r\n")))
    assert config.mqtt.host == "broker.example.org"


def test_home_assistant_status_topic_is_independent_of_the_prefix() -> None:
    """Home Assistant sends its birth message to its own topic, not below the discovery prefix."""
    assert parse_config(FULL).homeassistant.status_topic == "homeassistant/status"
    custom = parse_config(
        FULL.replace('prefix = "ha"', 'prefix = "ha"\nstatus_topic = "ha/status"')
    )
    assert custom.homeassistant.status_topic == "ha/status"
    for invalid in ("#", "ha/+", "ha//status", "/ha"):
        with pytest.raises(ConfigError, match="status_topic"):
            parse_config(
                FULL.replace('prefix = "ha"', f'prefix = "ha"\nstatus_topic = "{invalid}"')
            )


def test_contact_devices_are_limited() -> None:
    assert parse_config(FULL).homeassistant.contact_devices == 50
    none = parse_config(FULL.replace('prefix = "ha"', 'prefix = "ha"\ncontact_devices = 0'))
    assert none.homeassistant.contact_devices == 0
    for invalid in ("-1", "1001", "true", '"10"'):
        with pytest.raises(ConfigError, match="contact_devices"):
            parse_config(
                FULL.replace('prefix = "ha"', f'prefix = "ha"\ncontact_devices = {invalid}')
            )
    rendered = render_config(ANSWERS + "homeassistant.contact_devices=8\n")
    assert parse_config(rendered).homeassistant.contact_devices == 8


@pytest.mark.parametrize("name", ["radio-", "radio_", "a--b", "a-_b"])
def test_identifiers_that_home_assistant_would_merge_are_refused(name: str) -> None:
    """Home Assistant collapses runs of '-' and '_' in entity IDs and drops a trailing one, so
    'radio-' would collide with 'radio'."""
    with pytest.raises(ConfigError, match="node_id"):
        parse_config(MINIMAL.replace('node_id = "radio1"', f'node_id = "{name}"'))
    with pytest.raises(ConfigError, match="alias"):
        parse_config(MINIMAL + f"[targets.{name}]\nchannel = 1\n")


@pytest.mark.parametrize(
    ("host", "hint"),
    [
        ("mqtt://broker.example.org", "without a scheme such as mqtt://"),
        ("mqtts://broker.example.org:8883", "without a scheme"),
        ("broker.example.org:1883", "without the port"),
        ("[2001:db8::1]:1883", "without the port"),
        ("[2001:db8::1]", "without brackets"),
        ("broker.example.org/mqtt", "a host name or IP address only, got"),
        ("mqtt..example.org", "a part between dots is empty"),
        (".example.org", "a part between dots is empty"),
        ("a" * 64 + ".example.org", "longer than 63 characters"),
        ("2001:db8::zz", "a host name or IP address only, got"),
    ],
)
def test_a_url_or_a_port_in_a_host_is_refused(host: str, hint: str) -> None:
    """Accepted, it would only fail when connecting, with an error that does not name the
    setting."""
    with pytest.raises(ConfigError, match=f"mqtt.host: .*{re.escape(hint)}"):
        parse_config(MINIMAL.replace('host = "broker.example.org"', f'host = "{host}"'))
    radio = FULL.replace('host = "192.0.2.10"', f'host = "{host}"')
    with pytest.raises(ConfigError, match=f"radio.host: .*{re.escape(hint)}"):
        parse_config(radio)


@pytest.mark.parametrize(
    "host",
    [
        "broker.example.org",
        "broker.example.org.",
        "192.0.2.20",
        "2001:db8::1",
        "fe80::1%eth0",
        "::ffff:192.0.2.1",
        "mqtt.müller.example",
        "xn--mller-kva.example",
        "my_broker",
    ],
)
def test_host_names_and_addresses_are_accepted(host: str) -> None:
    """Python passes a name to the resolver in its IDNA form, so a non-ASCII name works too."""
    config = parse_config(MINIMAL.replace('host = "broker.example.org"', f'host = "{host}"'))
    assert config.mqtt.host == host


@pytest.mark.parametrize("name", [" gw", "gw ", "gw\t"])
def test_a_user_name_with_surrounding_blanks_is_refused(name: str) -> None:
    """Mosquitto would refuse it as an unknown user, with the same answer as a wrong password."""
    text = MINIMAL.replace('node_id = "radio1"', f'node_id = "radio1"\nusername = "{name}"')
    with pytest.raises(ConfigError, match=r"mqtt\.username: no blank"):
        parse_config(text.replace("\t", "\\t"))


def test_telemetry_is_on_by_default() -> None:
    """ADR 0018: every companion of the radio that is not excluded is asked; a companion's
    firmware answers only a requester it allows, and the first start holds the requests for an
    hour, so the household can exclude strangers first."""
    assert parse_config(MINIMAL).telemetry.enabled is True
    assert parse_config(MINIMAL + "\n[telemetry]\nenabled = false\n").telemetry.enabled is False
    assert parse_config(FULL).telemetry.enabled is True
    with pytest.raises(ConfigError, match=r"telemetry\.enabled"):
        parse_config(MINIMAL + '\n[telemetry]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match=r"telemetry\.interval_s: unknown setting"):
        parse_config(MINIMAL + "\n[telemetry]\ninterval_s = 60\n")
    assert "telemetry.enabled=true" in config_answers(FULL).splitlines()


def test_telemetry_asks_only_the_households_companions_unless_all() -> None:
    """SEC-016: `ask = "household"`, the default since 2.0.0, asks the contacts the household
    included and the protected keys only; `all` every companion not excluded."""
    assert parse_config(MINIMAL).telemetry.ask == "household"
    every = parse_config(MINIMAL + '\n[telemetry]\nask = "all"\n')
    assert every.telemetry.ask == "all"
    with pytest.raises(ConfigError, match=r"telemetry\.ask: one of all, household, got 'some'"):
        parse_config(MINIMAL + '\n[telemetry]\nask = "some"\n')


def test_alert_targets_must_be_targets() -> None:
    """IMP-032: the targets whose messages go first."""
    alerting = MINIMAL + '[targets.alarm]\nchannel = 2\n[messages]\nalert_targets = ["alarm"]\n'
    assert parse_config(alerting).messages.alert_targets == frozenset({"alarm"})
    assert parse_config(MINIMAL).messages.alert_targets == frozenset()
    with pytest.raises(ConfigError, match=r"messages\.alert_targets: 'siren' is not a target"):
        parse_config(alerting.replace('["alarm"]', '["alarm", "siren"]'))


def test_home_assistant_controls_hidden_targets_and_entity_prefix() -> None:
    """SEC-008, ARC-015."""
    ha = parse_config(MINIMAL).homeassistant
    assert (ha.controls, ha.hidden_targets, ha.contact_entity_prefix) == (True, frozenset(), "mesh")
    text = MINIMAL + "[targets.alarm]\nchannel = 2\n[homeassistant]\ncontrols = false\n"
    config = parse_config(text + 'hidden_targets = ["alarm"]\ncontact_entity_prefix = "mesh_2"\n')
    ha = config.homeassistant
    assert (ha.controls, ha.hidden_targets, ha.contact_entity_prefix) == (
        False,
        frozenset({"alarm"}),
        "mesh_2",
    )
    with pytest.raises(
        ConfigError, match=r"homeassistant\.hidden_targets: 'siren' is not a target"
    ):
        parse_config(text + 'hidden_targets = ["siren"]\n')
    for bad in ("Mesh", "mesh_", "_mesh", "mesh__2", "m" * 21, "mesh-2"):
        with pytest.raises(ConfigError, match=r"homeassistant\.contact_entity_prefix: lower"):
            parse_config(text + f'contact_entity_prefix = "{bad}"\n')
    with pytest.raises(ConfigError, match=r"homeassistant\.contact_entity_prefix"):
        parse_config(text + 'contact_entity_prefix = ""\n')
    assert parse_config(text + f'contact_entity_prefix = "{"m" * 20}"\n')


def test_contacts_are_managed_on_request_by_default() -> None:
    assert parse_config(MINIMAL).contacts.manage is True
    assert parse_config(MINIMAL).contacts.heard_nodes == "keys"  # SEC-015, since 2.0.0
    full = parse_config(MINIMAL + '\n[contacts]\nheard_nodes = "full"\n')
    assert full.contacts.heard_nodes == "full"
    with pytest.raises(ConfigError, match=r"contacts\.heard_nodes: one of full, keys, off"):
        parse_config(MINIMAL + '\n[contacts]\nheard_nodes = "names"\n')
    assert parse_config(MINIMAL + "\n[contacts]\nmanage = false\n").contacts.manage is False
    with pytest.raises(ConfigError, match=r"contacts\.manage"):
        parse_config(MINIMAL + '\n[contacts]\nmanage = "no"\n')
    with pytest.raises(ConfigError, match=r"contacts\.add: unknown setting"):
        parse_config(MINIMAL + "\n[contacts]\nadd = true\n")
