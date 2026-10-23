"""Configuration: TOML parsing with strict validation, and rendering from installer answers."""

import tomllib
from pathlib import Path

import pytest

from meshcore_home_gateway.config import (
    ConfigError,
    config_answers,
    load_config,
    parse_config,
    render_config,
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
host = "192.0.2.10"
port = 5000
set_clock = false

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

[airtime]
budget_s_per_hour = 72
burst_s = 12

[commands]
senders = ["{KEY.upper()}"]
words = ["Status", "arm"]
max_age_s = 300

[targets.alerts]
channel = 1

[targets.alice]
contact = "{KEY}"

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
    assert config.commands.senders == frozenset()
    assert config.targets == ()


def test_full_configuration_is_read_completely() -> None:
    config = parse_config(FULL)
    assert (config.radio.transport, config.radio.host, config.radio.port) == (
        "tcp",
        "192.0.2.10",
        5000,
    )
    assert config.radio.set_clock is False
    assert config.mqtt.password == 's3cret "quoted"'
    assert config.mqtt.tls is True
    assert config.mqtt.base_topic == "mesh/home"
    assert config.homeassistant.prefix == "ha"
    assert config.messages.ignore_channels == frozenset({0})
    assert config.commands.senders == frozenset({KEY})
    assert config.commands.words == frozenset({"status", "arm"})
    alerts, alice = sorted(config.targets, key=lambda t: t.alias)
    assert (alerts.alias, alerts.kind, alerts.channel_index) == ("alerts", MessageKind.CHANNEL, 1)
    assert (alice.kind, alice.public_key) == (MessageKind.CONTACT, KEY)
    assert config.diagnostics.log_level == "debug"


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
        ('[mqtt]\nhost = "h"\nnode_id = "Bad Name"', "mqtt.node_id"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nport = 70000', "mqtt.port"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nbase_topic = "a/#"', "mqtt.base_topic"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nbase_topic = "/a"', "mqtt.base_topic"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nport = "1883"', "mqtt.port: expected an integer"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nkeyfile = "/k"\ntls = true', "mqtt.keyfile: unknown"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nca_file = "/ca.pem"', "mqtt.ca_file: only used"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\ncert_file = "/c.pem"\ntls = false', "mqtt.cert_file"),
        ('[mqtt]\nhost = "h"\nnode_id = "n"\nkey_file = "/k.pem"\ntls = true', "needs cert_file"),
        ("[airtime]\nbudget_s_per_hour = 0", "airtime.budget_s_per_hour"),
        ("[airtime]\nburst_s = 2", "airtime.burst_s"),
        ("[messages]\nmax_attempts = 0", "messages.max_attempts"),
        ('[commands]\nsenders = ["a1b2c3d4e5f6"]\nwords = ["arm"]', "commands.senders"),
        ('[commands]\nwords = ["two words"]', "commands.words"),
        ("[targets.Alerts]\nchannel = 1", "targets.Alerts"),
        ("[targets.alerts]\nchannel = 1\ncontact = 'x'", "targets.alerts"),
        ("[targets.alerts]\nchannel = 256", "targets.alerts.channel"),
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


def test_rendering_rejects_unknown_or_invalid_answers() -> None:
    with pytest.raises(ConfigError, match=r"mqtt\.colour"):
        render_config(ANSWERS + "mqtt.colour=blue\n")
    with pytest.raises(ConfigError, match=r"target\.x"):
        render_config(ANSWERS + "target.x=planet:3\n")
    with pytest.raises(ConfigError, match="line 1"):
        render_config("no equals sign")


def test_rendering_escapes_control_characters() -> None:
    text = render_config(ANSWERS.replace("device_name=Mesh radio", "device_name=Mesh\tradio"))
    assert parse_config(text).homeassistant.device_name == "Mesh\tradio"


def test_the_shipped_example_is_valid() -> None:
    example = Path(__file__).resolve().parent.parent / "deploy" / "config.example.toml"
    config = load_config(example)
    assert {t.alias for t in config.targets} == {"alerts", "operator"}


def test_every_setting_can_be_answered() -> None:
    """The installer answers cover the whole configuration (docs/configuration.md)."""
    answers = ANSWERS + "\n".join(
        [
            "radio.set_clock=false", "mqtt.tls=true", "mqtt.cert_file=/etc/c.pem",
            "mqtt.key_file=/etc/k.pem",
            "mqtt.client_id=bridge-a", "mqtt.keepalive_s=30", "messages.truncate=false",
            "messages.dedup_window_s=60", "messages.queue_size=5", "messages.queue_ttl_s=120",
            "messages.max_attempts=4", "messages.flood_after=3", "messages.ignore_channels=0,2",
            "airtime.burst_s=6.5", "commands.max_age_s=0", "diagnostics.publish_rx_log=true",
        ]
    )  # fmt: skip
    config = parse_config(render_config(answers))
    assert config.radio.set_clock is False
    assert (config.mqtt.tls, config.mqtt.cert_file, config.mqtt.key_file) == (
        True, "/etc/c.pem", "/etc/k.pem",
    )  # fmt: skip
    assert (config.mqtt.client_id, config.mqtt.keepalive_s) == ("bridge-a", 30)
    assert (config.messages.max_attempts, config.messages.flood_after) == (4, 3)
    assert config.messages.ignore_channels == frozenset({0, 2})
    assert config.airtime.burst_s == 6.5
    assert config.commands.max_age_s == 0
    assert config.diagnostics.publish_rx_log is True


# --- answers from a configuration (the installer's reconfigure mode) ---------------------------


@pytest.mark.parametrize("text", [MINIMAL, FULL], ids=["minimal", "full"])
def test_answers_render_back_to_the_same_configuration(text: str) -> None:
    answers = config_answers(text)
    assert parse_config(render_config(answers)) == parse_config(text)


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
    text = MINIMAL + 'password = "p\\u2028w,=\\"x"\n'
    config = parse_config(render_config(config_answers(text)))
    assert config.mqtt.password == 'p\u2028w,="x'


def test_answers_refuse_values_with_line_breaks() -> None:
    with pytest.raises(ConfigError, match=r"mqtt\.password"):
        config_answers(MINIMAL + 'password = "a\\nb"\n')


def test_answers_refuse_invalid_files() -> None:
    with pytest.raises(ConfigError, match=r"radio\.device"):
        config_answers('[radio]\ntransport = "serial"\n')


def test_rendering_accepts_windows_line_ends() -> None:
    config = parse_config(render_config(ANSWERS.replace("\n", "\r\n")))
    assert config.mqtt.host == "broker.example.org"
