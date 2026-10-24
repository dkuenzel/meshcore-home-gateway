"""Configuration: TOML parsing with strict validation, and rendering from installer answers."""

import re
import tomllib
from dataclasses import fields
from pathlib import Path

import pytest

from meshcore_home_gateway import config as config_module
from meshcore_home_gateway.config import (
    Config,
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

[history]
enabled = false
messages_days = 7
positions_days = 90

[telemetry]
enabled = true

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
    assert (config.history.enabled, config.history.messages_days) == (False, 7.0)
    assert config.history.positions_days == 90.0


def test_the_history_is_on_by_default_and_bounded() -> None:
    history = parse_config(MINIMAL).history
    assert (history.enabled, history.messages_days, history.positions_days) == (True, 30.0, 30.0)
    with pytest.raises(ConfigError, match=r"history\.messages_days: must be between 1\.0 and"):
        parse_config(MINIMAL + "\n[history]\nmessages_days = 0\n")


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
            "contacts.manage=false",
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
    assert config.contacts.manage is False


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
    ["broker.example.org", "broker.example.org.", "192.0.2.20", "2001:db8::1", "fe80::1%eth0",
     "::ffff:192.0.2.1", "mqtt.müller.example", "xn--mller-kva.example", "my_broker"],
)  # fmt: skip
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


def test_telemetry_is_off_by_default_until_contacts_can_be_excluded() -> None:
    """ADR 0018 lets a contact be excluded; until that exists, asking every companion of the
    radio is the user's choice, not a default."""
    assert parse_config(MINIMAL).telemetry.enabled is False
    assert parse_config(FULL).telemetry.enabled is True
    with pytest.raises(ConfigError, match=r"telemetry\.enabled"):
        parse_config(MINIMAL + '\n[telemetry]\nenabled = "yes"\n')
    with pytest.raises(ConfigError, match=r"telemetry\.interval_s: unknown setting"):
        parse_config(MINIMAL + "\n[telemetry]\ninterval_s = 60\n")
    assert "telemetry.enabled=true" in config_answers(FULL).splitlines()


def test_contacts_are_managed_on_request_by_default() -> None:
    assert parse_config(MINIMAL).contacts.manage is True
    assert parse_config(MINIMAL + "\n[contacts]\nmanage = false\n").contacts.manage is False
    with pytest.raises(ConfigError, match=r"contacts\.manage"):
        parse_config(MINIMAL + '\n[contacts]\nmanage = "no"\n')
    with pytest.raises(ConfigError, match=r"contacts\.add: unknown setting"):
        parse_config(MINIMAL + "\n[contacts]\nadd = true\n")
