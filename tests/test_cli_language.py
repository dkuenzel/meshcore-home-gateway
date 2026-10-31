"""The command line's language (ADR 0019): it follows the environment, argparse's own words
included, except `run`; machine output stays the same in every language. The errors and warnings
it shows are Texts, English in str() and translated where shown; the language of the names in
Home Assistant is a strict setting."""

from __future__ import annotations

import argparse
import ast
import contextlib
import dataclasses
import functools
import inspect
import io
import json
import re
import sys
import tomllib
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from meshcore_home_gateway import cli, i18n
from meshcore_home_gateway import config as configuration
from meshcore_home_gateway.bridge import protected_look_alikes
from meshcore_home_gateway.catalog import read_po
from meshcore_home_gateway.cli import EX_CONFIG, EX_OK, EX_USAGE, main
from meshcore_home_gateway.config import (
    ConfigError,
    HomeAssistantConfig,
    MqttConfig,
    config_answers,
    load_config,
    parse_config,
    render_config,
)
from meshcore_home_gateway.core.commands import CommandRules
from meshcore_home_gateway.core.contacts import normalise_key
from meshcore_home_gateway.mqtt.client import tls_context
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.links import LinkError, contact_link, parse_contact_text
from meshcore_home_gateway.texts import Text, TextError
from tests.i18n_helpers import hostile, percents, pseudo, speaking, wide, widened
from tests.test_links import card

ROOT = Path(__file__).resolve().parent.parent
LISTED = ROOT / "tools" / "argparse_messages.json"
TEMPLATE = ROOT / "src" / "meshcore_home_gateway" / "locale" / "meshcore-home-gateway.pot"


def argparse_texts() -> set[tuple[str, str | None]]:
    """This Python's argparse texts: the literals it passes to _() and ngettext()."""
    found: set[tuple[str, str | None]] = set()
    for node in ast.walk(ast.parse(inspect.getsource(argparse))):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            count = {"_": 1, "ngettext": 2}.get(node.func.id, 0)
            texts = [
                arg.value
                for arg in node.args[:count]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            ]
            if count and len(texts) == count:
                found.add((texts[0], texts[1] if count == 2 else None))
    return found


def test_every_argparse_message_is_listed() -> None:
    """A new Python may bring new words: `make translations` under it adds them (D10)."""
    listed = {(msgid, plural) for msgid, plural in json.loads(LISTED.read_text(encoding="utf-8"))}
    assert argparse_texts() <= listed, "run make translations with this Python"
    _, template = read_po(TEMPLATE.read_text(encoding="utf-8"), TEMPLATE.name)
    assert listed <= {(entry.msgid, entry.msgid_plural) for entry in template}
    assert {"usage: ", "positional arguments", "show this help message and exit"} <= {
        msgid for msgid, _ in listed
    }


# --- errors and the language of the names -------------------------------------------------------

RADIO = '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\n'
MQTT = '[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\n'
MINIMAL = RADIO + MQTT
ID_RULE_TEXT = (
    "1-32 of a-z, 0-9, '-', '_', starting with a letter or digit, not ending with '-' or '_', "
    "and no two of them in a row"
)
SEGMENTS = "segments of A-Z, a-z, 0-9, '-', '_' separated by '/'"
HOST_ONLY = "mqtt.host: a host name or IP address only"
KEY = "ab" * 32


@functools.cache
def _pseudo() -> i18n.Translator:
    return pseudo()


def _english_everywhere(text: Text, english: str) -> None:
    """str() is today's English text; each part of it, nested texts too, is a text of the
    template, and the command line shows it translated (here: marked ‹…›)."""
    assert str(text) == english
    marked = _pseudo().render(text)
    assert marked.startswith("‹")
    assert marked.replace("‹", "").replace("›", "") == english


# every ConfigError of config.py, with the text it gave before it became a Text
CONFIGURATION = [
    ("radio = 1\n" + MQTT, "radio: expected a table"),
    ('[radio]\ndevice = "/dev/ttyACM0"\n' + MQTT, "radio.transport: required"),
    ('[radio]\ntransport = ""\n' + MQTT, "radio.transport: expected a non-empty string"),
    (RADIO + "set_clock = 1\n" + MQTT, "radio.set_clock: expected true or false"),
    (
        '[radio]\ntransport = "tcp"\nhost = "192.0.2.1"\nport = "x"\n' + MQTT,
        "radio.port: expected an integer",
    ),
    (MINIMAL + "port = 0\n", "mqtt.port: must be between 1 and 65535, got 0"),
    (MINIMAL + '[messages]\ndedup_window_s = "x"\n', "messages.dedup_window_s: expected a number"),
    (
        MINIMAL + "[airtime]\nbudget_s_per_hour = 0\n",
        "airtime.budget_s_per_hour: must be between 0.001 and 3600.0, got 0",
    ),
    (MINIMAL + "[commands]\nwords = [1]\n", "commands.words: expected a list of strings"),
    (
        MINIMAL + "[messages]\nignore_channels = [256]\n",
        "messages.ignore_channels: expected a list of integers 0..255",
    ),
    (RADIO + "colour = 1\n" + MQTT, "radio.colour: unknown setting"),
    ("colour = 1\n" + MINIMAL, "colour: unknown setting"),
    (
        '[radio]\ntransport = "usb"\n' + MQTT,
        "radio.transport: must be 'serial' or 'tcp', got 'usb'",
    ),
    (RADIO + 'host = "192.0.2.1"\n' + MQTT, "radio.host: only used with transport = 'tcp'"),
    ('[radio]\ntransport = "serial"\n' + MQTT, "radio.device: required for the serial transport"),
    ('[radio]\ntransport = "tcp"\n' + MQTT, "radio.host: required for the tcp transport"),
    (
        MINIMAL + '[radio.profile]\nmanage = "never"\n',
        "radio.profile.manage: one of auto, observe, always, got 'never'",
    ),
    (
        MINIMAL + '[radio.profile]\nname = "a\\tb"\n',
        "radio.profile.name: 1 to 31 bytes of UTF-8 without control characters, got 'a\\tb'",
    ),
    (
        RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "mqtt://broker"\n',
        (
            f"{HOST_ONLY}, without a scheme such as mqtt:// (TLS and port are settings of their"
            " own), got 'mqtt://broker'"
        ),
    ),
    (
        RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "broker:1883"\n',
        f"{HOST_ONLY}, without the port (that is the setting port), got 'broker:1883'",
    ),
    (
        RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "[::1]"\n',
        f"{HOST_ONLY}, an IPv6 address without brackets, got '[::1]'",
    ),
    (RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "::g"\n', f"{HOST_ONLY}, got '::g'"),
    (
        RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "a..b"\n',
        f"{HOST_ONLY} (a part between dots is empty or longer than 63 characters), got 'a..b'",
    ),
    (RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "a b"\n', f"{HOST_ONLY}, got 'a b'"),
    (
        '[radio]\ntransport = "serial"\ndevice = "ttyACM0"\n' + MQTT,
        "radio.device: must be an absolute path, got 'ttyACM0'",
    ),
    (
        RADIO + '[mqtt]\nhost = "broker.example.org"\nnode_id = "Radio"\n',
        f"mqtt.node_id: use {ID_RULE_TEXT}, got 'Radio'",
    ),
    (MINIMAL + 'base_topic = "a//b"\n', f"mqtt.base_topic: {SEGMENTS}, got 'a//b'"),
    (MINIMAL + 'ca_file = "/etc/ca.pem"\n', "mqtt.ca_file: only used with tls = true"),
    (MINIMAL + 'tls = true\nkey_file = "/etc/key.pem"\n', "mqtt.key_file: needs cert_file"),
    (MINIMAL + 'password = "secret"\n', "mqtt.password: needs username"),
    (
        MINIMAL + 'username = " gateway"\n',
        "mqtt.username: no blank at the start or end, got ' gateway'",
    ),
    (
        MINIMAL + '[homeassistant]\nprefix = "a/b"\n',
        "homeassistant.prefix: a single topic segment, got 'a/b'",
    ),
    (
        MINIMAL + '[homeassistant]\nstatus_topic = "a//b"\n',
        f"homeassistant.status_topic: {SEGMENTS}, got 'a//b'",
    ),
    (
        MINIMAL + '[commands]\nsenders = ["xyz"]\n',
        "commands.senders: public key must be 64 hex digits, got 'xyz'",
    ),
    (
        MINIMAL + '[commands]\nwords = ["two words"]\n',
        "commands.words: command word must be a single word, got 'two words'",
    ),
    (MINIMAL + "[targets.Gate]\nchannel = 1\n", f"targets.Gate: alias must be {ID_RULE_TEXT}"),
    (MINIMAL + "[targets.gate]\n", "targets.gate: set exactly one of 'channel' or 'contact'"),
    (
        MINIMAL + '[targets.gate]\ncontact = "xyz"\n',
        "targets.gate.contact: public key must be 64 hex digits, got 'xyz'",
    ),
    (
        MINIMAL + '[diagnostics]\nlog_level = "loud"\n',
        "diagnostics.log_level: one of debug, info, warning, error, got 'loud'",
    ),
]
ANSWERS = [
    ("nonsense\n", "answers line 1: expected key=value"),
    ("target.Gate=channel:1\n", f"target.Gate: alias must be {ID_RULE_TEXT}"),
    ("colour.shade=1\n", "colour.shade: unknown answer"),
    (
        "target.gate=other\n",
        "target.gate: expected channel:<index> or contact:<public key>, got 'other'",
    ),
    ("mqtt.port=abc\n", "mqtt.port: cannot read 'abc' as int"),
]
WRITTEN_BACK = [
    (
        MINIMAL + '[commands]\nwords = ["a,b"]\n',
        "commands.words: an item contains a comma, which the answers cannot express",
    ),
    (
        MINIMAL + '[homeassistant]\ndevice_name = "a\\nb"\n',
        "homeassistant.device_name: contains a line break, which the answers cannot express",
    ),
]


@pytest.mark.parametrize(("text", "english"), CONFIGURATION)
def test_errors_keep_english_in_str(text: str, english: str) -> None:
    """A ConfigError's str() is the English text it always was: the journal, the installer's
    host check of older versions and the tests read it; the command line translates `.text`."""
    with pytest.raises(ConfigError) as caught:
        parse_config(text)
    _english_everywhere(caught.value.text, english)
    assert str(caught.value) == english


@pytest.mark.parametrize(("answers", "english"), ANSWERS)
def test_answers_errors_keep_english_in_str(answers: str, english: str) -> None:
    with pytest.raises(ConfigError) as caught:
        render_config(answers)
    _english_everywhere(caught.value.text, english)


@pytest.mark.parametrize(("text", "english"), WRITTEN_BACK)
def test_errors_of_the_answers_written_back_keep_english_in_str(text: str, english: str) -> None:
    with pytest.raises(ConfigError) as caught:
        config_answers(text)
    _english_everywhere(caught.value.text, english)


def test_file_and_toml_errors_keep_english_in_str(tmp_path: Path) -> None:
    """The values are the system's texts (strerror, tomllib), shown as they are."""
    missing = tmp_path / "missing.toml"
    with pytest.raises(ConfigError) as caught:
        load_config(missing)
    _english_everywhere(caught.value.text, f"cannot read {missing}: No such file or directory")
    binary = tmp_path / "binary.toml"
    binary.write_bytes(b"\xff")
    with pytest.raises(ConfigError) as caught:
        load_config(binary)
    _english_everywhere(caught.value.text, f"cannot read {binary}: not valid UTF-8 (byte 0)")
    with pytest.raises(tomllib.TOMLDecodeError) as toml:
        tomllib.loads("[radio")
    with pytest.raises(ConfigError) as caught:
        parse_config("[radio")
    _english_everywhere(caught.value.text, f"invalid TOML: {toml.value}")


def test_tls_errors_keep_english_in_str(tmp_path: Path) -> None:
    """The two files of a client certificate are one text, not an English " and " join."""
    missing, key = tmp_path / "missing.pem", tmp_path / "key.pem"
    cases = [
        (str(missing), None, None, f"mqtt.ca_file: cannot load {missing}"),
        (None, str(missing), None, f"mqtt.cert_file: cannot load {missing}"),
        (None, str(missing), str(key), f"mqtt.cert_file: cannot load {missing} and {key}"),
    ]
    for ca_file, cert_file, key_file, english in cases:
        config = MqttConfig(
            "broker.example.org",
            "radio1",
            tls=True,
            ca_file=ca_file,
            cert_file=cert_file,
            key_file=key_file,
        )
        with pytest.raises(ConfigError) as caught:
            tls_context(config)
        _english_everywhere(caught.value.text, f"{english}: No such file or directory")


@pytest.mark.parametrize(
    ("text", "reason", "english"),
    [
        ("\ud800", "invalid_link", "the text is not valid Unicode"),
        ("hello", "invalid_link", "not a public key, contact link or contact card"),
        (
            "meshcore://channel/add?name=Public",
            "unsupported_link",
            "a MeshCore link, but not one that adds a contact",
        ),
        ("meshcore://zz", "invalid_link", "not a contact link or contact card"),
        (
            f"meshcore://contact/add?public_key={KEY}&",
            "invalid_link",
            "the contact link is malformed",
        ),
        (
            f"meshcore://contact/add?public_key={KEY}&name=a&name=b",
            "invalid_link",
            "the contact link repeats a parameter",
        ),
        (
            "meshcore://contact/add?name=Bob",
            "invalid_link",
            "the contact link has no valid public key",
        ),
        (
            f"meshcore://contact/add?public_key={KEY}&type=9",
            "unsupported_type",
            "the contact link has type '9', not 1 to 4",
        ),
        ("meshcore://00", "invalid_link", "the contact card holds no advert"),
        (card(flags=0x85), "unsupported_type", "the contact card has type 5"),
    ],
)
def test_link_errors_keep_english_in_str_and_their_reason(
    text: str, reason: str, english: str
) -> None:
    with pytest.raises(LinkError) as caught:
        parse_contact_text(text)
    assert caught.value.reason == reason
    assert str(caught.value) == english
    _english_everywhere(caught.value.text, english)


def test_core_errors_keep_english_in_str() -> None:
    """core/ cannot choose a language (tests/test_architecture.py): it raises Texts."""
    cases: list[tuple[Callable[[], object], str]] = [
        (
            lambda: CommandRules.create(senders=[], words=["Open Gate"], max_age_s=0.0),
            "command word must be a single word, got 'Open Gate'",  # as written
        ),
        (
            lambda: CommandRules.create(senders=[], words=[], max_age_s=-1.0),
            "max_age_s must not be negative, got -1.0",
        ),
        (lambda: normalise_key("xyz"), "public key must be 64 hex digits, got 'xyz'"),
    ]
    for call, english in cases:
        with pytest.raises(TextError) as caught:
            call()
        assert isinstance(caught.value, ValueError)  # the callers catch ValueError
        _english_everywhere(caught.value.text, english)


def _look_alikes(manage: str) -> list[Text]:
    twin = "ab" * 6 + "02" * 26  # the first 6 bytes of KEY
    text = MINIMAL + (
        f'[radio.profile]\nmanage = "{manage}"\n'
        f'[commands]\nsenders = ["{KEY}", "{twin}"]\nwords = ["open"]\n'
        f'[targets.gate]\ncontact = "{KEY}"\n'
    )
    return protected_look_alikes(parse_config(text))


def test_look_alike_warnings_keep_english_in_str() -> None:
    """Two whole sentences, whose roles are texts of their own (ADR 0009)."""
    named = (
        "protected contacts abababababab…02020202 (command sender) and abababababab…abababab "
        "(target gate, command sender) have the same first 6 bytes: the radio names a sender by "
        "them only (ADR 0009); "
    )
    (writes,) = _look_alikes("auto")
    _english_everywhere(
        writes,
        named + "the managed profile, when it writes, adds abababababab…02020202 unless the other "
        "is on the radio, never both",
    )
    (observes,) = _look_alikes("observe")
    _english_everywhere(
        observes, named + "the gateway adds neither, and refuses requests to add either"
    )
    assert _pseudo().render(observes).count("‹") == 6  # the sentence, two keys, three roles


def test_a_bad_sender_key_names_senders() -> None:
    """config._commands tells the setting by the English text of the core's error (D11)."""
    for setting, value in (("senders", "xyz"), ("words", "two words"), ("senders", "a b")):
        with pytest.raises(ConfigError) as caught:
            parse_config(MINIMAL + f'[commands]\n{setting} = ["{value}"]\n')
        assert str(caught.value).startswith(f"commands.{setting}: ")


RULE = f"‹{ID_RULE_TEXT}›"


@pytest.mark.parametrize(
    ("call", "marked"),
    [
        (
            lambda: parse_config(MINIMAL + '[commands]\nsenders = ["xyz"]\n'),
            "‹commands.senders: ‹public key must be 64 hex digits, got 'xyz'››",
        ),
        (
            lambda: parse_config(MINIMAL + '[commands]\nwords = ["a b"]\n'),
            "‹commands.words: ‹command word must be a single word, got 'a b'››",
        ),
        (
            lambda: parse_config(MINIMAL + '[targets.gate]\ncontact = "xyz"\n'),
            "‹targets.gate.contact: ‹public key must be 64 hex digits, got 'xyz'››",
        ),
        (
            lambda: parse_config(RADIO + '[mqtt]\nnode_id = "radio1"\nhost = "a b"\n'),
            "‹mqtt.host: ‹a host name or IP address only, got 'a b'››",
        ),
        (
            lambda: parse_config(RADIO + '[mqtt]\nhost = "broker.example.org"\nnode_id = "-"\n'),
            f"‹mqtt.node_id: use {RULE}, got '-'›",
        ),
        (
            lambda: parse_config(MINIMAL + "[targets.Gate]\nchannel = 1\n"),
            f"‹targets.Gate: alias must be {RULE}›",
        ),
        (lambda: render_config("target.Gate=channel:1\n"), f"‹target.Gate: alias must be {RULE}›"),
    ],
)
def test_a_text_inside_an_error_is_translated_inside_it(
    call: Callable[[], object], marked: str
) -> None:
    """The core's errors, the host's problem and the rule of IDs are texts of their own, which
    the command line translates inside the configuration error (D11)."""
    with pytest.raises(ConfigError) as caught:
        call()
    assert _pseudo().render(caught.value.text) == marked


def test_language_setting_is_strict(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One of the languages this version ships, like log_level (D9); en by default."""
    assert parse_config(MINIMAL).homeassistant.language == "en"
    english = MINIMAL + '[homeassistant]\nlanguage = "en"\n'
    assert parse_config(english).homeassistant.language == "en"
    speaking(monkeypatch)  # offers xx as a shipped language: read when the file is read
    assert parse_config(english.replace('"en"', '"xx"')).homeassistant.language == "xx"
    for value in ("xx_XX", "XX", "it", "en_GB"):
        with pytest.raises(ConfigError) as caught:
            parse_config(english.replace('"en"', f'"{value}"'))
        _english_everywhere(
            caught.value.text, f"homeassistant.language: one of en, xx, got {value!r}"
        )
    path = tmp_path / "config.toml"
    for value, detail in (
        ("it", "one of en, xx, got 'it'"),
        ("", "expected a non-empty string"),
    ):
        path.write_text(english.replace('"en"', f'"{value}"'), encoding="utf-8")
        assert main(["check-config", "--config", str(path)]) == EX_CONFIG
        assert capsys.readouterr().err == (
            f"meshcore-home-gateway: configuration error: homeassistant.language: {detail}\n"
        )
    answers = (
        "radio.transport=serial\nradio.device=/dev/ttyACM0\nmqtt.host=broker.example.org\n"
        "mqtt.node_id=radio1\nhomeassistant.language=xx\n"
    )
    rendered = render_config(answers)
    assert '\n[homeassistant]\nlanguage = "xx"\n' in rendered
    assert "homeassistant.language=xx\n" in config_answers(rendered)
    assert "homeassistant.language" not in config_answers(MINIMAL)  # the default follows en


def test_the_default_device_name_is_one_constant() -> None:
    """The names sent to Home Assistant translate the default device name (D18)."""
    assert configuration.DEFAULT_DEVICE_NAME == "MeshCore radio"
    assert HomeAssistantConfig().device_name == configuration.DEFAULT_DEVICE_NAME
    assert parse_config(MINIMAL).homeassistant.device_name == configuration.DEFAULT_DEVICE_NAME


# --- the command line speaks the environment's language -----------------------------------------

PREFIX = "meshcore-home-gateway: "  # the program's name before a message, never inside one
TWIN = "ab" * 6 + "02" * 26  # KEY's first 6 bytes: a look-alike of KEY
OTHER = "cd" * 32
NODE = "d4" * 32
NODE_LABEL = f"{NODE[:12]}…{NODE[-8:]}"
NODE_TWIN = NODE[:12] + "00" * 22 + NODE[-8:]  # the label's digits too
TOPICS = Topics("meshcore", "radio1")
SHIPPED = [code for code in i18n.LANGUAGES if code != "en"]
SUMMARIES = {  # between them, every kind of line of check-config's summary
    "tcp": (
        '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\n'
        + MQTT
        + 'username = "gateway"\npassword = "secret"\n'
        f'[targets.alerts]\nchannel = 1\n[targets.gate]\ncontact = "{KEY}"\n'
        f'[commands]\nsenders = ["{KEY}", "{TWIN}"]\nwords = ["open", "status"]\n'
    ),
    "serial": (
        MINIMAL + 'tls = true\nusername = "gateway"\n[homeassistant]\ndiscovery = false\n'
        f'[commands]\nsenders = ["{KEY}"]\nwords = ["open"]\n'
        "[contacts]\nmanage = false\n[telemetry]\nenabled = false\n"
    ),
    "anonymous": MINIMAL,
}
# what the tests put into the texts: names, addresses, settings, the system's and paho's own
# texts, and the command line's own names, which argparse's errors show
VALUES = (
    "tcp", "serial", "192.0.2.10", "broker.example.org", "/dev/ttyACM0", "gateway",
    "meshcore/radio1/delivery", "meshcore/radio1", "homeassistant", "English", "alerts", "gate",
    "nowhere", "open", "status", "Not authorized", "no route to the broker",
    "the client is not connected", "the radio went away", "No such file or directory",
    "Not a directory", "File exists", "no-such-group", "invalid_link", "Alice", "Bob", "Radio",
    "v1.18.0", "X1", "2026-01-01", "chat", "repeater", "room", "sensor", "deny", "base",
    "location", "environment", "Public", "name", "manual_add", "duty_cycle_percent", "autoadd",
    "telemetry", "Old", "New", "true", "false", "hello", "off", "readings_days",
    "positions_days", "None", "radio.device", "systemctl reload mosquitto",
    "docs/deployment.md, troubleshooting", "meshcore-home-gateway listen --topic contact",
    "meshcore-home-gateway", "command", "bogus", "maybe", "run", "check-config", "probe", "send",
    "listen", "contacts", "config", "state", "on", "--serial", "--tcp", "--wait", "--count",
    "--bogus", KEY, TWIN, OTHER, NODE, NODE_TWIN,
)  # fmt: skip
# absolute paths, and keys, parts of keys and request ids as the command line shows them
_MACHINE = re.compile(
    r"(?<![\w/])/[\w.@/-]*|cli-[0-9a-f]{8}|(?:[0-9a-f]{4} ){15}[0-9a-f]{4}"
    r"|[0-9a-f]{8,}(?:…[0-9a-f]{8})?"
)
_FIELD = re.compile(r"\{\w+(?:![rsa])?(?::[^{}]*)?\}")


@dataclasses.dataclass(frozen=True)
class Ran:
    status: int
    out: str
    err: str


def _main(argv: list[str]) -> Ran:
    """A command's exit status and output; argparse's exits included."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = main(argv)
        except SystemExit as exc:
            status = exc.code if isinstance(exc.code, int) else EX_USAGE
    return Ran(status, out.getvalue(), err.getvalue())


def _written(tmp: Path, name: str, text: str) -> str:
    path = tmp / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def _speak(monkeypatch: pytest.MonkeyPatch, translator: i18n.Translator | None = None) -> None:
    """The environment asks for the pseudo-language (or `translator`'s), which this version
    offers."""
    monkeypatch.setenv("LANGUAGE", speaking(monkeypatch, translator).language)


class _Code:
    """paho's reason code: a refusal names its reason."""

    def __init__(self, refusal: str | None = None) -> None:
        self.is_failure = refusal is not None
        self.text = refusal or "Success"

    def __str__(self) -> str:
        return self.text


Answer = Callable[[str, bytes], list[tuple[str, Any]]]


@dataclasses.dataclass
class _Client:
    """paho's client of the command line, without a broker: it answers at once, as told."""

    refusal: str | None = None  # the broker refuses the connection, for this reason
    unreachable: bool = False  # no broker at the address
    silent: bool = False  # the broker never answers
    subscription: str | None = ""  # granted (""), refused for this reason, or never answered
    documents: dict[str, Any] = dataclasses.field(default_factory=dict)  # retained, by topic
    sending: str | None = ""  # confirmed (""), failed with this error, or never confirmed
    answer: Answer | None = None  # the messages a request brings
    on_connect: Any = None
    on_subscribe: Any = None
    on_message: Any = None

    def connect(self, _host: str, _port: int, _keepalive: int) -> None:
        if self.unreachable:
            raise OSError("no route to the broker")

    def loop_start(self) -> None:
        if not self.silent:
            self.on_connect(self, None, None, _Code(self.refusal), None)

    def subscribe(self, topic: str | list[tuple[str, int]], qos: int = 0) -> None:
        names = [topic] if isinstance(topic, str) else [name for name, _ in topic]
        if self.subscription is not None and self.on_subscribe is not None:
            self.on_subscribe(self, None, 1, [_Code(self.subscription or None)], None)
        for name in names:
            if name in self.documents:
                self._deliver(name, self.documents[name], retain=True)

    def publish(self, topic: str, payload: bytes, qos: int = 0) -> Any:
        for name, body in self.answer(topic, payload) if self.answer else []:
            self._deliver(name, body)

        def wait_for_publish(_timeout: float) -> None:
            if self.sending:
                raise RuntimeError(self.sending)

        def is_published() -> bool:
            return self.sending is not None

        return SimpleNamespace(wait_for_publish=wait_for_publish, is_published=is_published)

    def _deliver(self, topic: str, body: Any, *, retain: bool = False) -> None:
        text = body if isinstance(body, str) else json.dumps(body)
        message = SimpleNamespace(topic=topic, payload=text.encode(), retain=retain)
        self.on_message(self, None, message)

    def disconnect(self) -> None:
        pass

    def loop_stop(self) -> None:
        pass


def _broker(monkeypatch: pytest.MonkeyPatch, **behaviour: Any) -> None:
    """The commands' broker connections behave as told, without waiting long for an answer."""
    client = _Client(**behaviour)
    monkeypatch.setattr(cli, "_mqtt_client", lambda _mqtt, _suffix: client)
    for name in ("CONNECT_TIMEOUT_S", "SUBSCRIBE_TIMEOUT_S", "RETAINED_WAIT_S"):
        monkeypatch.setattr(cli, name, 0.01)


def _listed(kind: str, *keys: str, truncated: bool = False, **extra: Any) -> dict[str, Any]:
    """The gateway's document of its contacts or of the nodes it heard."""
    entries = [{"key": key, "name": "Alice", "type": "chat"} for key in keys]
    total = 300 if truncated else len(keys)
    return {kind: entries, "total": total, "truncated": truncated, "manage": True, **extra}


def _online(**documents: Any) -> dict[str, Any]:
    """Retained documents by topic: the gateway online, and these (by name)."""
    return {TOPICS.availability: "online"} | {
        getattr(TOPICS, name): document for name, document in documents.items()
    }


def _contact_result(event_type: str) -> Answer:
    """The gateway's answer to a contact request: its event with the request's id."""

    def answer(_topic: str, payload: bytes) -> list[tuple[str, Any]]:
        event = {"event_type": event_type, "id": json.loads(payload)["id"], "key": NODE}
        return [(TOPICS.contact_event, event | {"reason": None})]

    return answer


def _delivered(_topic: str, payload: bytes) -> list[tuple[str, Any]]:
    """The bridge's answer to a send request: queued, then sent."""
    event = {"target": "alerts", "id": "d1", "text": payload.decode()}
    return [(TOPICS.delivery, event | {"event_type": kind}) for kind in ("queued", "sent")]


def _report(**changes: Any) -> dict[str, Any]:
    """What probe reports, with these values changed."""
    actions = {"name": "set", "manual_add": "kept", "duty_cycle_percent": "given_up",
               "autoadd": "unsupported", "telemetry": "reported"}  # fmt: skip
    report = {
        "name": "Radio", "public_key": KEY, "firmware": "v1.18.0", "firmware_code": 14,
        "model": "X1", "build_date": "2026-01-01", "frequency_mhz": 869.618,
        "bandwidth_khz": 62.5, "spreading_factor": 8, "coding_rate": 8, "tx_power_dbm": 22,
        "auto_add": {"manual": True, "types": [], "overwrite_oldest": None},
        "position": None, "advert_location": "none",
        "telemetry": {"base": "deny", "location": "deny", "environment": "deny"},
        "airtime_factor": 9.0,
        "contacts": [
            {"public_key": KEY, "name": "Alice", "type": "chat", "flags": ["favourite"]},
            {"public_key": TWIN, "name": "Bob", "type": "repeater", "flags": ["remote_cli"]},
        ],
        "channels": [{"index": 0, "name": "Public"}],
        "profile": {
            "manage": "observe", "configured": True, "firmware_tested": True, "writes": False,
            "changes": {
                name: {"live": "Old", "profile": "New", "action": action, "reason": None}
                for name, action in actions.items()
            },
            "contacts": {
                key: {"name": "Alice", "action": action}
                for key, action in zip(
                    (KEY, TWIN, OTHER, NODE),
                    ("add", "favourite", "missing", "not_favourite"),
                    strict=True,
                )
            },
        },
    }  # fmt: skip
    return report | changes


def _profile(manage: str, configured: bool, *, changes: bool) -> dict[str, Any]:
    report: dict[str, Any] = _report()["profile"]
    if not changes:
        report |= {"changes": {}, "contacts": {}}
    return report | {"manage": manage, "configured": configured}


REPORTS = [
    _report(),
    _report(
        auto_add={"manual": True, "types": ["chat", "room"], "overwrite_oldest": True},
        position={"latitude": 53.55, "longitude": 9.99}, advert_location="share",
        airtime_factor=None, profile=_profile("auto", False, changes=False),
    ),
    _report(
        auto_add={"manual": True, "types": ["sensor"], "overwrite_oldest": False},
        profile=_profile("auto", True, changes=False),
    ),
    _report(
        auto_add={"manual": False, "types": ["chat"], "overwrite_oldest": True},
        position={"latitude": 53.55, "longitude": 9.99},
        profile=_profile("always", False, changes=True),
    ),
    _report(
        auto_add={"manual": False, "types": [], "overwrite_oldest": None}, advert_location="share",
    ),
]  # fmt: skip


def _probed(monkeypatch: pytest.MonkeyPatch, report: dict[str, Any] | None) -> None:
    """probe reports `report` without a radio; None: the radio went away."""

    async def probed(*_: Any, **__: Any) -> dict[str, Any]:
        if report is None:
            raise OSError("the radio went away")
        return report

    monkeypatch.setattr(cli, "_probe_radio", probed)


# Each scenario runs commands in the language of the environment and returns what they printed
# for people: standard error, and standard output where it is not for programs.


def _check_config(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    printed = []
    for name, text in SUMMARIES.items():
        ran = _main(["check-config", "--config", _written(tmp, f"{name}.toml", text)])
        printed += [ran.out, ran.err]
    bad = _written(tmp, "bad.toml", '[radio]\ntransport = "serial"\n' + MQTT)
    printed.append(_main(["check-config", "--config", bad]).err)
    printed.append(_main(["check-config", "--config", str(tmp / "missing.toml")]).err)
    path = _written(tmp, "broker.toml", SUMMARIES["tcp"])
    for behaviour in ({}, {"refusal": "Not authorized"}, {"unreachable": True}, {"silent": True}):
        _broker(monkeypatch, **behaviour)
        ran = _main(["check-config", "--config", path, "--broker"])
        printed += [ran.out, ran.err]
    return printed


def _probe(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    printed = []
    for report in [*REPORTS, None]:
        _probed(monkeypatch, report)
        ran = _main(["probe", "--tcp", "192.0.2.10", "--state-dir", str(tmp)])
        printed += [ran.out, ran.err]
    return printed


def _send(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    send = ["send", "--config", _written(tmp, "config.toml", SUMMARIES["tcp"])]
    printed = [_main([*send, "nowhere", "hello"]).err, _main([*send, "alerts", "\udcff"]).err]
    for behaviour in (
        {"unreachable": True}, {"refusal": "Not authorized"}, {"silent": True},
        {"subscription": "Not authorized"}, {"subscription": None},
        {"sending": "the client is not connected"}, {"sending": None}, {},
    ):  # fmt: skip
        _broker(monkeypatch, **behaviour)
        printed.append(_main([*send, "alerts", "hello", "--wait", "0.01"]).err)
    _broker(monkeypatch, unreachable=True)
    printed.append(_main(["listen", "--config", send[2]]).err)
    return printed


def _contacts(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    config = _written(tmp, "config.toml", SUMMARIES["tcp"])
    nodes, contacts = _listed("nodes", NODE, look_alike=[NODE]), _listed("contacts", KEY)
    link = contact_link("Bob", OTHER)
    telemetry = ["telemetry", "--wait", "0", KEY]  # only send the request
    runs: list[tuple[dict[str, Any], list[str]]] = [  # the broker's behaviour, and the command
        ({"unreachable": True}, ["add", NODE]),
        ({"documents": {TOPICS.availability: "offline"}}, ["add", NODE]),
        ({}, ["add", "hello"]),
        ({"documents": _online(nodes=_listed("nodes"))}, ["add", NODE_LABEL]),
        ({"documents": _online(nodes=_listed("nodes", truncated=True))}, ["add", NODE_LABEL]),
        ({"documents": _online(nodes=_listed("nodes", NODE, NODE_TWIN))}, ["add", NODE_LABEL]),
        ({"documents": _online(nodes=nodes), "answer": _contact_result("added")},
         ["add", NODE_LABEL]),
        ({"documents": _online()}, ["add", link, "--wait", "0.01"]),
        ({"documents": _online(), "sending": "the client is not connected"}, ["add", NODE]),
        ({}, ["remove", "xyz"]),
        ({"documents": _online(contacts=_listed("contacts"))}, ["remove", NODE_LABEL]),
        ({"documents": _online(contacts=_listed("contacts", truncated=True))},
         ["remove", NODE_LABEL]),
        ({"documents": _online(contacts=contacts), "answer": _contact_result("removed")},
         ["remove", KEY]),
        ({}, ["telemetry", KEY, "on", "--forget"]),
        ({"documents": _online(contacts=_listed("contacts"))}, [*telemetry, "off"]),
        ({"documents": _online(contacts=contacts)}, [*telemetry, "off", "--forget"]),
        ({"documents": _online(contacts=contacts)}, [*telemetry, "on"]),
        ({"unreachable": True}, ["list"]),
        ({"documents": _online()}, ["list"]),
        ({"documents": {
            TOPICS.availability: "offline", TOPICS.contacts: _listed("contacts", truncated=True),
            TOPICS.nodes: _listed("nodes", NODE) | {"truncated": True, "total": 2},
        }}, ["list"]),
        ({"documents": _online(
            contacts=_listed("contacts", truncated=True) | {"total": 1},
            nodes=_listed("nodes", NODE, truncated=True) | {"total": None},
        )}, ["list"]),
        ({"unreachable": True}, ["link"]),
        ({"documents": _online()}, ["link"]),
    ]  # fmt: skip
    printed = []
    for behaviour, command in runs:
        _broker(monkeypatch, **behaviour)
        printed.append(_main(["contacts", command[0], "--config", config, *command[1:]]).err)
    return printed


def _render(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    answers = _written(
        tmp,
        "answers",
        "radio.transport=tcp\nradio.host=192.0.2.10\nmqtt.host=broker.example.org\n"
        "mqtt.node_id=radio1\n",
    )
    render = ["config", "render", "--answers", answers, "--output"]
    printed = []
    for more in (
        [str(tmp / "out.toml")],
        [str(tmp / "out.toml"), "--group", "no-such-group"],
        [str(tmp / "out.toml" / "below")],
    ):
        ran = _main([*render, *more])
        printed += [ran.out, ran.err]
    bad = _written(tmp, "bad", "nonsense\n")
    printed.append(_main(["config", "render", "--answers", bad, "--output", "x"]).err)
    return printed


def _usage(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """argparse's errors: the last line names the error; the usage line before it is names."""
    printed = []
    for argv in (
        [], ["bogus"], ["probe"], ["probe", "--tcp", "[::1"], ["probe", "--tcp", ":1"],
        ["probe", "--tcp", "192.0.2.10:0"], ["send", "--wait", "999999", "alerts", "hello"],
        ["listen", "--count", "-1"], ["contacts", "telemetry", KEY, "maybe"],
        ["check-config", "--bogus"],
    ):  # fmt: skip
        lines = _main(argv).err.splitlines()
        assert re.match("‹[^‹›]+›meshcore-home-gateway", lines[0])  # usage: the names
        printed.append(lines[-1])
    return printed


SCENARIOS = {
    "check-config": _check_config, "probe": _probe, "send": _send, "contacts": _contacts,
    "render": _render, "usage": _usage,
}  # fmt: skip


def _unmarked(line: str) -> str:
    """The parts of a line outside every ‹…› (they nest), the program's name before it left
    out."""
    depth, kept = 0, []
    for char in line.removeprefix(PREFIX):
        if char in "‹›":
            depth += 1 if char == "‹" else -1
        elif depth == 0:
            kept.append(char)
    return "".join(kept)


def _plain_words(line: str, values: Sequence[str]) -> list[str]:
    """The words in plain letters that are none of the values: in the pseudo-language with wide
    letters, they did not pass through the catalog."""
    line = _MACHINE.sub(" ", line)
    for value in sorted(values, key=len, reverse=True):
        line = line.replace(value, " ")
    return re.findall(r"[A-Za-z]+", line)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_every_text_for_people_goes_through_the_catalog(
    scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the pseudo-language with wide letters every text the command line prints for people,
    also one shown inside another, has them: what is left in plain letters is the values the test
    put in; and no text is shown with a field it did not fill (D13, D14)."""
    _speak(monkeypatch, wide())
    lines = [
        line for text in SCENARIOS[scenario](tmp_path, monkeypatch) for line in text.splitlines()
    ]
    assert lines
    values = (*VALUES, str(tmp_path))
    found = {line: _plain_words(line, values) for line in lines}
    assert {line: words for line, words in found.items() if words} == {}
    assert [line for line in lines if _FIELD.search(line)] == []


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_the_prefix_stays_outside_every_text(
    scenario: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The installer strips the program's name from the messages it shows (lxc.sh): it is
    printed before a text, never translated with it."""
    _speak(monkeypatch)
    lines = [
        line for text in SCENARIOS[scenario](tmp_path, monkeypatch) for line in text.splitlines()
    ]
    named = [line for line in lines if line.startswith(PREFIX)]
    assert [line for line in named if not line.startswith(PREFIX + "‹")] == []
    assert not any(PREFIX in _unmarked(line) for line in lines)


def test_a_text_shown_without_its_values_is_found(tmp_path: Path) -> None:
    """The checks of the two tests above: a word in plain letters, a field left unfilled, the
    program's name inside a text."""
    values = (*VALUES, str(tmp_path))
    marked = wide().text("no target {alias!r} in {path}", alias="alerts", path=tmp_path / "c.toml")
    assert marked == f"‹{widened('no target ')}'alerts'{widened(' in ')}{tmp_path}/c.toml›"
    assert _plain_words(PREFIX + marked, values) == []
    assert _plain_words(f'  name: "Old" -> "New"  (‹{widened("set")}›)', values) == []
    assert _plain_words(f"key {' '.join([KEY[:4]] * 16)}, {KEY[:12]}…{KEY[-8:]}", values) == ["key"]
    assert _plain_words(f"‹{widened('wrote')} ‹no host in ':1'››", values) == ["no", "host", "in"]
    assert _unmarked(PREFIX + "‹a ‹b› c› d") == " d"
    assert _FIELD.search("‹not on the radio; {reported}›")
    assert _FIELD.search("‹{count:d} keys›")
    assert not _FIELD.search("‹{'base': 'deny'}›")


def test_argparse_words_are_translated_and_restored(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """argparse's own words follow the language while main() runs, and are its own again after
    it, also when argparse ends it with SystemExit (D10)."""
    words = vars(argparse)["_"], vars(argparse)["ngettext"]
    _speak(monkeypatch)
    helped = _main(["--help"])
    assert helped.status == EX_OK
    assert helped.out.startswith("‹usage: ›meshcore-home-gateway [-h] [--version] command ...")
    assert "‹options›" in helped.out
    assert "‹positional arguments›" in helped.out
    assert "‹show this help message and exit›" in helped.out
    assert "‹Gateway between home automation (MQTT) and a MeshCore companion radio.›" in helped.out
    assert (vars(argparse)["_"], vars(argparse)["ngettext"]) == words
    refused = _main(["send"])
    assert refused.status == EX_USAGE
    assert refused.err.endswith(
        "‹meshcore-home-gateway send: error: ‹the following arguments are required: alias, text››\n"
    )
    assert (vars(argparse)["_"], vars(argparse)["ngettext"]) == words
    assert "‹" not in cli._parser().format_help()  # a parser built afterwards: English again
    config = _written(tmp_path, "config.toml", MINIMAL)
    checked = _main(["check-config", "--config", config])
    assert checked.status == EX_OK
    assert checked.out.startswith("‹configuration ")
    assert (vars(argparse)["_"], vars(argparse)["ngettext"]) == words
    assert cli._parser().format_help().startswith("usage: meshcore-home-gateway")


def test_argparse_stays_english_without_its_module_attributes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Python whose argparse lacks `_` or `ngettext`: its words stay English, ours not (D10)."""
    monkeypatch.delattr(argparse, "ngettext")
    with cli._speaking(pseudo()):
        text = cli._parser().format_help()
    assert text.startswith("usage: meshcore-home-gateway")
    assert "‹Gateway between home automation (MQTT) and a MeshCore companion radio.›" in text
    assert "ngettext" not in vars(argparse)


def _parsers(parser: argparse.ArgumentParser) -> Iterator[argparse.ArgumentParser]:
    yield parser
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub in action.choices.values():
                yield from _parsers(sub)


TEST_CATALOGS = {"xx": pseudo, "hostile": hostile, "percents": percents}


@pytest.mark.parametrize("language", [*SHIPPED, *TEST_CATALOGS])
def test_every_command_builds_and_helps_in_every_language(language: str) -> None:
    """argparse %-formats help texts, on 3.14 already when an argument is added: a translation
    with a % its English text lacks must not break a command (D7, P6)."""
    catalog = TEST_CATALOGS.get(language)
    translator = i18n.translator(language) if catalog is None else catalog()
    with cli._speaking(translator):
        parser = cli._parser()
        helps = [command.format_help() for command in _parsers(parser)]
    assert len(helps) == 15  # the program, its commands and theirs
    if language == "xx":
        assert all(text.startswith("‹usage: ›") for text in helps)
    if language in {"hostile", "percents"}:  # argparse formats them: English
        english = [command.format_help() for command in _parsers(cli._parser())]
        assert helps == english


def test_help_texts_are_marked_for_argparse() -> None:
    """Help and description texts reach argparse, which %-formats them: tr_help marks them so,
    and its translations are held to their English %-directives (D7)."""
    _, entries = read_po(TEMPLATE.read_text(encoding="utf-8"), TEMPLATE.name)
    flags = {entry.msgid: entry.flags for entry in entries if entry.msgctxt is None}
    texts = {
        text
        for command in _parsers(cli._parser())
        for text in (command.description, *(action.help for action in command._actions))
        if text is not None and text != argparse.SUPPRESS
    }
    assert len(texts) > 30
    assert {text for text in texts if "python-format" not in flags.get(text, ())} == set()


def _machine_outputs(tmp: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[list[Ran], str]:
    """What every output for programs prints, with its exit status; and the texts for people
    the same commands print on standard error."""
    config = _written(tmp, "config.toml", SUMMARIES["tcp"])
    contacts = _listed("contacts", KEY, NODE)
    documents = _online(contacts=contacts, nodes=_listed("nodes", NODE, OTHER, look_alike=[NODE]))
    documents[TOPICS.state] = {"contact_link": contact_link("Gateway", KEY)}
    probe = ["probe", "--tcp", "192.0.2.10", "--state-dir", str(tmp)]
    _probed(monkeypatch, REPORTS[0])
    ran = [_main([*probe, "--list", listing]) for listing in ("channels", "contacts", "profile")]
    ran.append(_main([*probe, "--json"]))
    requests = [
        ("added", ["contacts", "add", NODE]), ("removed", ["contacts", "remove", KEY]),
        ("excluded", ["contacts", "telemetry", KEY, "off"]), ("refused", ["contacts", "add", NODE]),
    ]  # fmt: skip
    for event_type, command in requests:
        _broker(monkeypatch, documents=documents, answer=_contact_result(event_type))
        ran.append(_main([*command, "--config", config, "--wait", "1"]))
    _broker(monkeypatch, documents=documents, answer=_delivered)
    ran.append(_main(["send", "--config", config, "alerts", "hello", "--wait", "1"]))
    ran.append(_main(["listen", "--config", config, "--count", "1", "--topic", "state"]))
    ran += [
        _main(["contacts", *more, "--config", config])
        for more in (["list"], ["list", "--json"], ["link"])
    ]
    ran.append(_main(["config", "answers", "--config", config]))
    ran.append(_main(["--version"]))
    by_id = re.compile(r"cli-[0-9a-f]{8}")  # a new request id each time
    machine = [Ran(r.status, by_id.sub("cli-<id>", r.out), "") for r in ran]
    return machine, "".join(r.err for r in ran)


@pytest.mark.parametrize("language", [*SHIPPED, "xx"])
def test_machine_outputs_ignore_the_language(
    language: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Programs read them: the installer, scripts, Home Assistant (ADR 0019, D12)."""
    english, notes = _machine_outputs(tmp_path, monkeypatch)
    assert all(ran.out for ran in english)
    assert "‹" not in notes
    if language == "xx":
        speaking(monkeypatch)
    monkeypatch.setenv("LANGUAGE", language)
    translated, notes = _machine_outputs(tmp_path, monkeypatch)
    assert translated == english
    assert notes.count("‹") > 5 if language == "xx" else notes  # the notes follow the language


def test_run_speaks_english_in_any_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The service's output goes to the journal: English, whatever the environment asks for, also
    argparse's errors, decided before parsing (D8)."""
    bad = _written(tmp_path, "bad.toml", RADIO)
    commands = [["run", "--config", bad], ["run", "--bogus"]]
    english = [_main(argv) for argv in commands]
    assert [ran.status for ran in english] == [EX_CONFIG, EX_USAGE]
    _speak(monkeypatch)
    assert [_main(argv) for argv in commands] == english
    assert _main(["check-config", "--config", bad]).err.startswith(PREFIX + "‹")  # not `run`
    for argv in commands:  # as the service starts it: the arguments in sys.argv
        monkeypatch.setattr(sys, "argv", ["meshcore-home-gateway", *argv])
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            contextlib.suppress(SystemExit),
        ):
            main()
        assert err.getvalue() == english[commands.index(argv)].err


def test_check_config_names_the_language_of_the_names(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The discovery line says in which language Home Assistant gets the names (D14)."""
    english = _main(["check-config", "--config", _written(tmp_path, "en.toml", MINIMAL)])
    assert "\n  discovery  on, prefix homeassistant, names in English\n" in english.out
    speaking(monkeypatch)  # offered; the command line stays English
    chosen = MINIMAL + '[homeassistant]\nlanguage = "xx"\n'
    pseudo_names = _main(["check-config", "--config", _written(tmp_path, "xx.toml", chosen)])
    assert "\n  discovery  on, prefix homeassistant, names in Pseudo\n" in pseudo_names.out
    off = MINIMAL + "[homeassistant]\ndiscovery = false\n"
    assert (
        "\n  discovery  off\n"
        in _main(["check-config", "--config", _written(tmp_path, "off.toml", off)]).out
    )


def test_check_config_counts_the_senders(tmp_path: Path) -> None:
    """A real plural instead of "sender(s)" (D14)."""
    for senders, line in (
        ([KEY], "  commands   open from 1 sender\n"),
        ([KEY, OTHER], "  commands   open, status from 2 senders\n"),
    ):
        words = '["status", "open"]' if len(senders) == 2 else '["open"]'
        text = MINIMAL + f"[commands]\nsenders = {json.dumps(senders)}\nwords = {words}\n"
        ran = _main(["check-config", "--config", _written(tmp_path, "config.toml", text)])
        assert line in ran.out


REPORT = """\
radio      Radio  abababababababababababababababababababababababababababababababab
firmware   v1.18.0 (code 14), X1, built 2026-01-01
lora       869.618 MHz, 62.5 kHz, SF 8, CR 4/8, 22 dBm
contacts   added manually
telemetry  base deny, location deny, environment deny
position   none, not in adverts
airtime    factor 9: the radio sends at most 10 %
  abababababababababababababababababababababababababababababababab  chat     Alice
  abababababab0202020202020202020202020202020202020202020202020202  repeater Bob  REMOTE CLI
channels
    0  Public
profile    differs from the configured profile:
  name: "Old" -> "New"  (set at the next connect)
  manual_add: "Old" -> "New"  (changed on the radio; kept)
  duty_cycle_percent: "Old" -> "New"  (did not take effect at 3 connects; no longer set)
  autoadd: "Old" -> "New"  (the firmware does not support it)
  telemetry: "Old" -> "New"  (only reported: radio.profile.manage = observe)
  contact abababababab Alice  (added as a favourite at the next connect)
  contact abababababab Alice  (made a favourite at the next connect)
  contact cdcdcdcdcdcd Alice  (not on the radio; only reported: radio.profile.manage = observe)
  contact d4d4d4d4d4d4 Alice  (no favourite; only reported: radio.profile.manage = observe)
"""
UNTESTED = "only reported: the firmware is untested; radio.profile.manage = always sets it"


def test_probe_keeps_its_english_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Each case of the report in the English it had before translations (D14)."""
    printed = []
    for report in REPORTS:
        _probed(monkeypatch, report)
        printed.append(_main(["probe", "--tcp", "192.0.2.10", "--state-dir", str(tmp_path)]).out)
    assert printed[0] == REPORT
    for text, lines in zip(
        printed[1:],
        [
            [
                (
                    "contacts   added manually, but chat, room automatically; a full table "
                    "overwrites the oldest"
                ),
                "position   53.55, 9.99, shared in adverts",
                "profile    the radio matches the default observer profile",
            ],
            [
                "contacts   added manually, but sensor automatically",
                "profile    the radio matches the configured profile",
            ],
            [
                "contacts   all added automatically; a full table overwrites the oldest",
                "position   53.55, 9.99, not in adverts",
                "profile    differs from the default observer profile:",
                f'  telemetry: "Old" -> "New"  ({UNTESTED})',
                f"  contact cdcdcdcdcdcd Alice  (not on the radio; {UNTESTED})",
            ],
            ["contacts   all added automatically", "position   none, shared in adverts"],
        ],
        strict=True,
    ):
        assert set(lines) <= set(text.splitlines())
    assert "airtime" not in printed[1]  # a firmware without the setting


CONTACTS_NOTES = [
    "cannot connect to the broker broker.example.org:1883: no route to the broker",
    "the gateway is offline",
    "not a public key, contact link or contact card (invalid_link)",
    f"no node {NODE_LABEL}",
    f"no node {NODE_LABEL} (the published list is cut: give the full key)",
    f"2 keys match: {NODE_TWIN}, {NODE}",
    f"key {' '.join([NODE[:4]] * 16)}  'Alice'",
    (
        "warning: another node has the same first 12 key digits: compare the full key above with "
        "the node's own screen; the one added first keeps them"
    ),
    f"key {' '.join([OTHER[:4]] * 16)}  'Bob' (claimed)",
    (
        "no result in time (request cli-<id>; it may still be handled: meshcore-home-gateway "
        "listen --topic contact)"
    ),
    f"key {' '.join([NODE[:4]] * 16)}",
    "request not sent: the client is not connected",
    "give the contact's full key (64 hex digits) or KEY12…KEY8",
    f"no contact {NODE_LABEL}",
    f"no contact {NODE_LABEL} (the published list is cut: give the full key)",
    f"key {' '.join([KEY[:4]] * 16)}  'Alice'",
    "the radio drops its direct messages from then on; messages queued to it fail",
    "--forget goes with off only",
    f"key {' '.join([KEY[:4]] * 16)}",
    (
        "the gateway stops asking it and forgets its battery, readings and position; its history "
        "keeps them for readings_days and positions_days"
    ),
    "not on the radio now: excluded in advance",
    f"key {' '.join([KEY[:4]] * 16)}  'Alice'",
    (
        "the gateway stops asking it and forgets its battery, readings and position and deletes "
        "them from its history"
    ),
    f"key {' '.join([KEY[:4]] * 16)}  'Alice'",
    "cannot connect to the broker broker.example.org:1883: no route to the broker",
    "the gateway has published no contacts",
    "the gateway is offline: the lists are as it last saw them",
    "the gateway lists 0 of 300 contacts (its document is bounded); give the others by full key",
    "the gateway lists 1 of 2 nodes (its document is bounded); give the others by full key",
    "the gateway lists 0 of 1 contact (its document is bounded); give the others by full key",
    "the gateway lists 1 of None nodes (its document is bounded); give the others by full key",
    "cannot connect to the broker broker.example.org:1883: no route to the broker",
    "the gateway has published no contact link",
]


def test_contacts_keep_their_english_notes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every note and error of the contacts commands in the English it had before (D14); the
    program's name before the errors, not before the notes on the contact."""
    by_id = re.compile(r"cli-[0-9a-f]{8}")
    lines = [by_id.sub("cli-<id>", line) for text in _contacts(tmp_path, monkeypatch)
             for line in text.splitlines()]  # fmt: skip
    assert [line.removeprefix(PREFIX) for line in lines] == CONTACTS_NOTES
    unnamed = ("key ", "warning: ", "the radio drops", "the gateway stops", "not on the radio")
    assert [line.startswith(PREFIX) for line in lines] == [
        not note.startswith(unnamed) for note in CONTACTS_NOTES
    ]


def test_check_config_keeps_its_english_summary(tmp_path: Path) -> None:
    """The summary's English is the one before translations, but for the two lines of D14: the
    values in one column."""
    ran = _main(["check-config", "--config", _written(tmp_path, "c.toml", SUMMARIES["tcp"])])
    path = tmp_path / "c.toml"
    assert ran.out == (
        f"configuration {path}: valid\n"
        "  radio      tcp 192.0.2.10:5000\n"
        "  broker     broker.example.org:1883, user 'gateway' with a password, topics "
        "meshcore/radio1/...\n"
        "  discovery  on, prefix homeassistant, names in English\n"
        "  target     alerts: channel 1\n"
        f"  target     gate: contact {KEY}\n"
        "  commands   open, status from 2 senders\n"
        "  airtime    36 s per hour, burst 10 s\n"
        "  contacts   managed on request\n"
        "  telemetry  asked of every companion not excluded\n"
    )
    serial = _main(["check-config", "--config", _written(tmp_path, "s.toml", SUMMARIES["serial"])])
    assert serial.out.splitlines()[1:] == [
        "  radio      serial /dev/ttyACM0",
        (
            "  broker     broker.example.org:1883 (TLS), user 'gateway' without a password, "
            "topics meshcore/radio1/..."
        ),
        "  discovery  off",
        "  commands   open from 1 sender",
        "  airtime    36 s per hour, burst 10 s",
        "  contacts   not managed",
        "  telemetry  off",
    ]
    anonymous = _main(["check-config", "--config", _written(tmp_path, "a.toml", MINIMAL)])
    assert ", no user name, " in anonymous.out


TRANSLATING = {"tr", "tr_help", "trn", "render", "ha_name", "N_", "N_ha"}
TRANSLATOR_METHODS = {"text", "plural", "pgettext", "argparse_text", "argparse_words"}
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}


def _translated_in_log_calls(tree: ast.AST) -> list[int]:
    """The lines of a translation inside a call of the log (log.info, logging.warning, …)."""
    found = []
    for node in ast.walk(tree):
        logged = (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in LOG_METHODS
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {"log", "logging", "logger"}
        )
        if not logged:
            continue
        assert isinstance(node, ast.Call)
        for argument in [*node.args, *(keyword.value for keyword in node.keywords)]:
            for inner in ast.walk(argument):
                if not isinstance(inner, ast.Call):
                    continue
                func = inner.func
                name = func.id if isinstance(func, ast.Name) else ""
                method = func.attr if isinstance(func, ast.Attribute) else ""
                if name in TRANSLATING or method in TRANSLATING | TRANSLATOR_METHODS:
                    found.append(inner.lineno)
    return found


def test_no_translation_in_log_calls() -> None:
    """Logs and the journal stay English (ADR 0019): a Text goes to the log as itself, whose
    str() is English."""
    package = ROOT / "src" / "meshcore_home_gateway"
    found = {
        path.relative_to(ROOT).as_posix(): lines
        for path in sorted(package.rglob("*.py"))
        if (lines := _translated_in_log_calls(ast.parse(path.read_text(encoding="utf-8"))))
    }
    assert found == {}
    for logged in (
        'log.warning(tr("x"))', 'logging.info("%s", cli.trn("a", "b", 2))',
        'log.error("%s", i18n.translator(x).text("y"))', 'log.debug(m, extra=render(t))',
    ):  # fmt: skip
        assert _translated_in_log_calls(ast.parse(logged)) == [1]
    assert _translated_in_log_calls(ast.parse('log.warning("%s", warning)')) == []
