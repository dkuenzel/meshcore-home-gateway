"""The warnings of a configuration at the start (config_checks.py)."""

from __future__ import annotations

import dataclasses
import logging
from pathlib import Path

import pytest

from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.config_checks import (
    alias_pairs,
    broker_warnings,
    ignored_secrets,
    start_warnings,
)
from tests.bridge_harness import (
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
)
from tests.radio_emulator import EmulatedRadio


def test_the_broker_connection_is_warned_about_until_accepted() -> None:
    """SEC-004: without credentials or TLS the gateway's topics are open to whoever reaches the
    broker or reads the network; `insecure = true` accepts it."""
    base = (
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\n'
        'host = "192.0.2.10"\n[mqtt]\nnode_id = "radio1"\n'
    )
    anonymous = parse_config(base + 'host = "broker.example.org"\n')
    assert [str(w).split(":")[0] for w in broker_warnings(anonymous)] == [
        "MQTT without a user name",
        "MQTT without TLS",
    ]
    assert broker_warnings(parse_config(base + 'host = "127.0.0.1"\nusername = "gw"\n')) == []
    accepted = parse_config(base + 'host = "broker.example.org"\ninsecure = true\n')
    assert broker_warnings(accepted) == []


def test_the_log_level_debug_is_warned_about_at_the_start(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """OPS-017: texts, positions and keys would go to the journal."""
    config = make_config(extra='[diagnostics]\nlog_level = "debug"')
    assert [str(w).split(":")[0] for w in start_warnings(config)] == ["log level debug"]
    assert start_warnings(make_config()) == []
    caplog.set_level(logging.WARNING)

    async def main() -> None:
        async with running(config, EmulatedRadio(), tmp_path):
            pass

    scenario(main)
    assert "log level debug: the journal gets the texts of the messages" in caplog.text


def test_target_aliases_that_differ_only_in_dash_and_underscore_are_warned_about() -> None:
    """Both are valid aliases, but Home Assistant makes the same entity IDs of them: the notify
    entity of the target announced later gets `_2`, and a dashboard cannot tell the two apart."""
    pair = make_config(extra="[targets.a-b]\nchannel = 1\n[targets.a_b]\nchannel = 2")
    (warning,) = alias_pairs(pair)
    assert str(warning).startswith("targets a-b, a_b differ only in '-' and '_':")
    assert warning in start_warnings(pair)
    three = make_config(
        extra="[targets.x-y-z]\nchannel = 1\n[targets.x_y-z]\nchannel = 2\n"
        "[targets.x-y_z]\nchannel = 3"
    )
    assert [str(w).split(" differ")[0] for w in alias_pairs(three)] == [
        "targets x-y-z, x-y_z, x_y-z"
    ]
    apart = make_config(extra="[targets.a-b]\nchannel = 1\n[targets.a-c]\nchannel = 2")
    assert alias_pairs(apart) == []
    assert alias_pairs(make_config()) == []


def test_the_secrets_of_keys_that_are_no_senders_are_warned_about() -> None:
    """#80: each secret the gateway ignores is named once, with the command that removes it."""
    config = make_config()
    assert ignored_secrets(config) == []
    keys = ("b2" * 32, "c3" * 32)
    secrets = tuple((sender, b"12345678901234567890") for sender in config.commands.senders)
    config = dataclasses.replace(
        config,
        commands=dataclasses.replace(config.commands, secrets=secrets, ignored_secrets=keys),
    )
    warnings = [str(w) for w in ignored_secrets(config)]
    assert warnings == [
        f"commands.codes_file: {key} has a code secret but is not in commands.senders: the "
        f"secret is ignored; 'meshcore-home-gateway commands revoke {key}' removes it"
        for key in keys
    ]
    assert [str(w) for w in start_warnings(config) if "codes_file" in str(w)] == warnings


def test_a_sender_without_a_secret_beside_an_ignored_secret_is_warned_about() -> None:
    """#80: a sender's key corrected by hand leaves the old key's secret ignored and the new
    key without one; before, that made the configuration an error. While a secret is ignored,
    the commands of each sender without one are refused, so each is named, with the command
    that gives it one, and what `commands revoke` does to it instead: once no secret is ignored,
    it commands without a code. A sender with a secret is not named, nor is any without an
    ignored one."""
    base = make_config()
    sender = next(iter(base.commands.senders))
    coded = "d4" * 32
    commands = dataclasses.replace(
        base.commands,
        senders=frozenset({sender, coded}),
        secrets=((coded, b"12345678901234567890"),),
    )
    assert ignored_secrets(dataclasses.replace(base, commands=commands)) == []
    old = "b2" * 32
    config = dataclasses.replace(
        base, commands=dataclasses.replace(commands, ignored_secrets=(old,))
    )
    warnings = [str(w) for w in ignored_secrets(config)]
    named = (
        f"commands.senders: {sender} has no code secret while one is ignored: its commands "
        f"are refused (code_missing) until 'meshcore-home-gateway commands enrol {sender}' "
        f"gives it one. Once no secret is ignored (commands revoke), it commands without a "
        f"code: enrol a key corrected by hand first. Restart the service after enrol or revoke"
    )
    assert warnings[1:] == [named]
    assert len(warnings) == 2
    assert old in warnings[0]
