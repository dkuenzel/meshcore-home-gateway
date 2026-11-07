"""The warnings of a configuration at the start (config_checks.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.config_checks import alias_pairs, broker_warnings, start_warnings
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
