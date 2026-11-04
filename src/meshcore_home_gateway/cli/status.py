"""`status`: what the gateway tells of itself on the bus (IMP-016, OPS-011): the radio, the
airtime and the queue, the telemetry, the health section and the contacts. It reads the
gateway's retained documents only; the service runs on."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from meshcore_home_gateway.cli import broker
from meshcore_home_gateway.cli.common import EX_FAILURE, EX_OK, PREFIX, _json_text, _safe_text, tr
from meshcore_home_gateway.config import load_config
from meshcore_home_gateway.health import DISK_LOW_MB, LAG_WARN_S

DOCUMENTS = ("state", "contacts")


def _status(args: argparse.Namespace) -> int:
    """Exit 0 while the gateway is online and its health section is not degraded."""
    config = load_config(args.config)
    session = broker.GatewaySession(config, DOCUMENTS, "status")
    try:
        error = session.open()
        if error is not None:
            print(PREFIX + error, file=sys.stderr)
            return EX_FAILURE
        availability = session.document("availability")
        state, contacts = session.document("state"), session.document("contacts")
        if args.json:
            report = {"availability": availability, "state": state, "contacts": contacts}
            print(_json_text(report, indent=2))
        else:
            for line in status_lines(config.mqtt.node_id, availability, state, contacts):
                print(line)
        degraded = not isinstance(state, dict) or _get(state, "health", "degraded") is not False
        return EX_FAILURE if not session.online or degraded else EX_OK
    finally:
        session.close()


def status_lines(node: str, availability: Any, state: Any, contacts: Any) -> list[str]:
    """The summary, a whole text per line, in the columns of check-config's."""
    known = {"online": tr("online"), "offline": tr("offline")}
    lines = [
        tr(
            "gateway {node}: {availability}",
            node=node,
            availability=known.get(availability, tr("never seen on this broker")),
        )
    ]
    if not isinstance(state, dict):
        return lines
    # Translators: the lines of `status`. Each value starts in the column where the other lines
    # start theirs (12 in English): pad the label with spaces to keep one column.
    if state.get("radio") == "connected":
        lines.append(
            tr(
                "  radio      connected: {name}, {model}, firmware {firmware}",
                name=_safe_text(str(state.get("radio_name"))),
                model=_safe_text(str(state.get("model"))),
                firmware=_safe_text(str(state.get("firmware"))),
            )
        )
    else:
        lines.append(tr("  radio      not connected"))
    if (battery := state.get("battery_v")) is not None:
        lines.append(tr("  battery    {volts} V", volts=battery))
    if (offset := state.get("radio_clock_offset_s")) is not None:
        lines.append(tr("  clock      the radio's is {seconds} s off the host's", seconds=offset))
    if state.get("radio_link_contested"):
        # Translators: security text (docs/translations.md): another client took the radio's
        # TCP link (SEC-003)
        lines.append(tr("  link       another client took the TCP link in the last hour"))
    lines.append(
        tr(
            "  airtime    {used} s used in the last hour, {available} s available, "
            "{budget} s per hour",
            used=state.get("airtime_1h_s"),
            available=state.get("airtime_available_s"),
            budget=state.get("airtime_budget_s_per_hour"),
        )
    )
    lines.append(tr("  queue      {count}", count=state.get("queue")))
    telemetry, reason = state.get("telemetry"), state.get("telemetry_reason")
    lines.append(
        tr("  telemetry  {status} ({reason})", status=telemetry, reason=reason)
        if reason
        else tr("  telemetry  {status}", status=telemetry)
    )
    lines.append(health_line(state.get("health")))
    if isinstance(contacts, dict):
        lines.append(
            tr(
                "  contacts   {total} of {max}, {excluded} excluded from telemetry",
                total=contacts.get("total"),
                max=contacts.get("max"),
                excluded=contacts.get("telemetry_excluded_total", 0),
            )
        )
    return lines


def health_line(health: Any) -> str:
    """The health section in a line: the values that make it degraded, by the names of the state
    document (docs/mqtt-api.md, State)."""
    if not isinstance(health, dict):
        return tr("  health     not reported")
    if not health.get("degraded"):
        return tr("  health     ok")
    problems = [
        f"{name}: {health[name]}"
        for name in ("history", "state_file", "exclusions")
        if health.get(name) not in ("ok", "off", None)
    ]
    lag, disk = health.get("loop_lag_max_s"), health.get("disk_free_mb")
    if health.get("errors_1h"):
        problems.append(f"errors_1h: {health['errors_1h']}")
    if isinstance(lag, int | float) and lag >= LAG_WARN_S:
        problems.append(f"loop_lag_max_s: {lag}")
    if isinstance(disk, int) and disk < DISK_LOW_MB:
        problems.append(f"disk_free_mb: {disk}")
    if not problems:
        return tr("  health     degraded (the journal says why)")
    # Translators: the problems are names and values of the state document, in English
    return tr("  health     degraded ({problems})", problems=", ".join(problems))


def _get(document: Any, *path: str) -> Any:
    for key in path:
        if not isinstance(document, dict):
            return None
        document = document.get(key)
    return document
