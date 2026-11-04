"""`doctor`: the checks of the review's operational findings in one report (EXT-016): the
configuration and its warnings, the broker (login, TLS, anonymous clients), the running gateway
(its radio, clock, link, firmware and health), and the host (free space, automatic updates, a
restart that updates wait for). Each line says ok, warning or problem; the exit status is 1 when
any says problem."""

from __future__ import annotations

import argparse
import dataclasses
import os
import shutil
from pathlib import Path
from typing import Any

from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import DEFAULT_STATE_DIR, EX_FAILURE, EX_OK, tr, tr_text
from meshcore_home_gateway.cli.status import health_line
from meshcore_home_gateway.config import Config, load_config
from meshcore_home_gateway.health import DISK_LOW_MB

CLOCK_OFF_S = 60.0  # a radio clock this far off the host's is worth a word (SEC-002)
UPGRADER = Path("/usr/bin/unattended-upgrade")  # installed by the installer (OPS-001)
REBOOT_REQUIRED = Path("/run/reboot-required")  # Debian: an update waits for a restart

OK, WARNING, PROBLEM = "ok", "warning", "problem"


@dataclasses.dataclass
class Finding:
    level: str  # OK, WARNING, PROBLEM
    text: str


def _doctor(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    findings = [
        *configuration_findings(config, args.config),
        *broker_findings(config),
        *host_findings(state_dir),
    ]
    words = {OK: tr("ok"), WARNING: tr("warning"), PROBLEM: tr("problem")}
    width = max(len(word) for word in words.values())
    for finding in findings:
        print(f"  {words[finding.level]:<{width}}  {finding.text}")
    return EX_FAILURE if any(f.level == PROBLEM for f in findings) else EX_OK


def configuration_findings(config: Config, path: Path) -> list[Finding]:
    from meshcore_home_gateway.config_checks import start_warnings  # noqa: PLC0415

    findings = [Finding(OK, tr("configuration {path}: valid", path=path))]
    findings += [Finding(WARNING, tr_text(warning)) for warning in start_warnings(config)]
    if config.radio.transport == "tcp":
        # Translators: security text (docs/translations.md): the radio's TCP interface has no
        # authentication (SEC-003)
        tcp = tr(
            "the radio over TCP: anyone who reaches its port controls the radio and can read its "
            "private key; prefer USB (docs/deployment.md)"
        )
        findings.append(Finding(WARNING, tcp))
    return findings


def broker_findings(config: Config) -> list[Finding]:
    """The broker with the gateway's login, without one, and the gateway's own report."""
    mqtt = config.mqtt
    if mqtt.tls:
        from meshcore_home_gateway.mqtt.client import tls_context  # noqa: PLC0415

        try:
            tls_context(mqtt)
        except (OSError, ValueError) as exc:
            return [Finding(PROBLEM, tr("TLS: {detail}", detail=exc))]
    session = broker.GatewaySession(config, ("state", "contacts"), "doctor")
    try:
        error = session.open()
        if error is not None:
            return [Finding(PROBLEM, error)]
        findings = [
            Finding(OK, tr("broker {host}:{port}: logged in", host=mqtt.host, port=mqtt.port))
        ]
        findings += _anonymous(config)
        findings += gateway_findings(
            session.document("availability"),
            session.document("state"),
            session.document("contacts"),
        )
        return findings
    finally:
        session.close()


def _anonymous(config: Config) -> list[Finding]:
    mqtt = config.mqtt
    if mqtt.username is None:
        return []
    client = broker.mqtt_client(
        dataclasses.replace(mqtt, username=None, password=None), "anonymous"
    )
    connection = broker.Connection()
    try:
        error = connection.open(client, mqtt)
    finally:
        client.disconnect()
        client.loop_stop()
    if error is None:
        # Translators: security text (docs/translations.md): translate all of it, add nothing
        return [
            Finding(
                WARNING,
                tr(
                    "the broker accepts clients without a user name: unless its ACL keeps them off "
                    "the gateway's topics, any client that reaches it can read messages and "
                    "positions and publish command events (docs/mqtt-api.md, Securing the broker)"
                ),
            )
        ]
    # Translators: security text (docs/translations.md): the good answer to the check above
    return [Finding(OK, tr("the broker refuses clients without a user name"))]


def gateway_findings(availability: Any, state: Any, contacts: Any) -> list[Finding]:
    """What the running gateway reports of itself (docs/mqtt-api.md, State)."""
    if availability != "online":
        return [Finding(PROBLEM, tr("the gateway is not online (is its service running?)"))]
    if not isinstance(state, dict):
        return [Finding(PROBLEM, tr("the gateway has published no state document"))]
    findings = [Finding(OK, tr("the gateway is online"))]
    if state.get("radio") != "connected":
        findings.append(Finding(PROBLEM, tr("the radio is not connected")))
    offset = state.get("radio_clock_offset_s")
    if isinstance(offset, int | float) and abs(offset) > CLOCK_OFF_S:
        clock = tr(
            "the radio's clock is {seconds} s off the host's: messages and commands carry the "
            "wrong time ([radio] set_clock, docs/configuration.md)",
            seconds=offset,
        )
        findings.append(Finding(WARNING, clock))
    if state.get("radio_link_contested"):
        # Translators: security text (docs/translations.md): another client took the radio's
        # TCP link (SEC-003)
        findings.append(
            Finding(WARNING, tr("another client took the radio's TCP link in the last hour"))
        )
    if isinstance(contacts, dict) and contacts.get("manage_reason") == "untested_firmware":
        untested = tr(
            "firmware {firmware} is not one the gateway is tested with: it takes no contact "
            "requests (docs/companions.md)",
            firmware=state.get("firmware"),
        )
        findings.append(Finding(WARNING, untested))
    health = state.get("health")
    if isinstance(health, dict) and health.get("degraded"):
        findings.append(Finding(PROBLEM, health_line(health).strip()))
    return findings


def host_findings(state_dir: Path) -> list[Finding]:
    """The host the command runs on, which is the gateway's container when run there."""
    findings: list[Finding] = []
    try:
        free = shutil.disk_usage(state_dir).free // 1_000_000
    except OSError:
        findings.append(Finding(WARNING, tr("no state directory at {path}", path=state_dir)))
    else:
        level = PROBLEM if free < DISK_LOW_MB else OK
        findings.append(Finding(level, tr("{free} MB free for {path}", free=free, path=state_dir)))
    if UPGRADER.exists():
        findings.append(Finding(OK, tr("security updates are installed automatically")))
    else:
        updates = tr(
            "unattended-upgrades is not installed: security updates wait for someone to install "
            "them (docs/deployment.md)"
        )
        findings.append(Finding(WARNING, updates))
    if REBOOT_REQUIRED.exists():
        findings.append(Finding(WARNING, tr("an update waits for a restart of the container")))
    return findings
