"""The warnings of a configuration at the start (config_checks.py)."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from meshcore_home_gateway.config import parse_config
from meshcore_home_gateway.config_checks import broker_warnings, start_warnings
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
