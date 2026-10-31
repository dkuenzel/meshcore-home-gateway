"""The command line's language (ADR 0019): argparse's own words are in the catalog's template;
the errors and warnings it shows are Texts, English in str() and translated where shown; the
language of the names in Home Assistant is a strict setting."""

from __future__ import annotations

import argparse
import ast
import functools
import inspect
import json
import tomllib
from collections.abc import Callable
from pathlib import Path

import pytest

from meshcore_home_gateway import config as configuration
from meshcore_home_gateway import i18n
from meshcore_home_gateway.bridge import protected_look_alikes
from meshcore_home_gateway.catalog import read_po
from meshcore_home_gateway.cli import EX_CONFIG, main
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
from meshcore_home_gateway.protocol.links import LinkError, parse_contact_text
from meshcore_home_gateway.texts import Text, TextError
from tests.i18n_helpers import pseudo, speaking
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
