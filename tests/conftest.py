"""Shared fixtures. The broker fixture starts a private mosquitto on a free local port; telemetry's
first-start hold is skipped unless a test is marked `first_start_hold`. Every test runs in an
English environment, and a text looked up in a catalog must be in the template unless the test is
marked `real_catalogs` (tools/i18n.py writes the template)."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

from meshcore_home_gateway import i18n, telemetry_poller
from meshcore_home_gateway.catalog import read_po

SKIP_BROKER = os.environ.get("MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS") == "1"
SKIP_GETTEXT = os.environ.get("MESHCORE_HOME_GATEWAY_SKIP_GETTEXT_TESTS") == "1"
TEMPLATE = (
    Path(__file__).resolve().parent.parent
    / "src"
    / "meshcore_home_gateway"
    / "locale"
    / "meshcore-home-gateway.pot"
)
# The firmware tests need the firmware built with a C++ compiler: `make firmware-test` runs them by
# naming the directory, which pytest then collects; `make test` leaves them out.
collect_ignore = ["firmware"]


@dataclass(frozen=True)
class Broker:
    host: str
    port: int


@pytest.fixture(autouse=True)
def no_first_start_hold(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Telemetry asks nobody for an hour the first time it runs on a state directory: only the
    tests marked `first_start_hold` wait for it."""
    if request.node.get_closest_marker("first_start_hold") is None:
        monkeypatch.setattr(telemetry_poller, "FIRST_START_S", 0.0)


@pytest.fixture(autouse=True)
def english_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """The command line speaks the environment's language: a developer's German shell must not
    change what the tests read. Subprocesses inherit it."""
    for name in ("LANGUAGE", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("LC_ALL", "C.UTF-8")


@cache
def _template_keys() -> frozenset[str]:
    _, entries = read_po(TEMPLATE.read_text(encoding="utf-8"), TEMPLATE.name)
    return frozenset(entry.key for entry in entries)


@pytest.fixture(autouse=True)
def strict_catalogs(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """A text looked up in a catalog that the committed template lacks fails the test, in every
    language, English included: a text shown later must be marked (N_, N_ha) where it is written,
    and `make translations` must have seen it (D13). Tests marked `real_catalogs` look up texts
    of their own."""
    if request.node.get_closest_marker("real_catalogs") is not None:
        return
    lookup = i18n.Translator._lookup

    def strict(
        translator: i18n.Translator, context: str | None, msgid: str, plural: str | None, n: int
    ) -> str:
        key = msgid if context is None else f"{context}\x04{msgid}"
        if key not in _template_keys():
            pytest.fail(
                f"{key!r} is not in the template: mark it (N_, N_ha) and run make translations",
                pytrace=False,
            )
        return lookup(translator, context, msgid, plural, n)

    monkeypatch.setattr(i18n.Translator, "_lookup", strict)


@pytest.fixture
def msgfmt() -> str:
    """GNU msgfmt, for the tests that compare our catalogs with GNU gettext's (D22)."""
    if SKIP_GETTEXT:
        pytest.skip("MESHCORE_HOME_GATEWAY_SKIP_GETTEXT_TESTS=1")
    found = shutil.which("msgfmt")
    if found is None:
        pytest.fail(
            "msgfmt not found: install GNU gettext (apt install gettext) or set "
            "MESHCORE_HOME_GATEWAY_SKIP_GETTEXT_TESTS=1 to skip the gettext tests deliberately"
        )
    return found


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _mosquitto() -> str:
    found = shutil.which("mosquitto") or shutil.which("mosquitto", path="/usr/sbin:/usr/local/sbin")
    if found is None:
        pytest.fail(
            "mosquitto not found: install it (apt install mosquitto) or set "
            "MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1 to skip the broker tests deliberately"
        )
    return found


@pytest.fixture
def broker(tmp_path: Path) -> Iterator[Broker]:
    yield from _run_broker(tmp_path, anonymous=True)


@pytest.fixture
def refusing_broker(tmp_path: Path) -> Iterator[Broker]:
    """A broker that refuses every client (no anonymous access, no users)."""
    yield from _run_broker(tmp_path, anonymous=False)


def _run_broker(tmp_path: Path, *, anonymous: bool) -> Iterator[Broker]:
    if SKIP_BROKER:
        pytest.skip("MESHCORE_HOME_GATEWAY_SKIP_BROKER_TESTS=1")
    port = _free_port()
    conf = tmp_path / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous {str(anonymous).lower()}\npersistence false\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(  # noqa: S603 - fixed arguments, test only
        [_mosquitto(), "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and process.poll() is None:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.05)
    else:
        process.kill()
        _, errors = process.communicate(timeout=5)  # also closes the pipe
        pytest.fail(f"mosquitto did not start: {errors.decode(errors='replace').strip()}")
    try:
        yield Broker("127.0.0.1", port)
    finally:
        process.terminate()
        process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()
