"""The configuration as the `config` document shows it (docs/mqtt-api.md, design §6.5).

An allowlist: each value is named here, none is copied by its section. Every user of Home
Assistant reads the document, so it never holds a host, port, device path, user name, password
or password file, client ID or key file, a contact's or sender's key (a target's first 8 digits
aside), the codes file, a code or a command word. The settings that protect people, the radio or
the law are shown, so that the household sees why Home Assistant cannot change them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.mqtt.discovery import text_room
from meshcore_home_gateway.views import TargetSetting

if TYPE_CHECKING:
    from meshcore_home_gateway.ports import RadioInfo


def target_settings(config: Config, radio: RadioInfo | None) -> list[TargetSetting]:
    """The targets in the order of the configuration; each text's room as `radio`'s name leaves
    it (None: before the radio connected, the room of the shortest name)."""
    alert, hidden = config.messages.alert_targets, config.homeassistant.hidden_targets
    return [
        TargetSetting(
            target.alias,
            target.kind.value,
            target.channel_index,
            target.public_key,
            target.prefix,
            target.suffix,
            target.alias in alert,
            target.alias in hidden,
            text_room(config, target, radio),
        )
        for target in config.targets
    ]


def shown_settings(config: Config) -> dict[str, dict[str, Any]]:
    """The other settings that are shown, by section; lists sorted. The hidden targets are
    counted (each target says whether it is one), the command senders too, and of the codes only
    whether commands need them."""
    messages, ha, radio = config.messages, config.homeassistant, config.radio
    history, commands = config.history, config.commands
    return {
        "messages": {
            "publish_channels": messages.publish_channels,
            "ignore_channels": sorted(messages.ignore_channels),
            "confirm_nonce": messages.confirm_nonce,
            "clean_received": messages.clean_received,
            "truncate": messages.truncate,
            "ascii_only": messages.ascii_only,
            "dedup_window_s": messages.dedup_window_s,
            "queue_size": messages.queue_size,
            "queue_ttl_s": messages.queue_ttl_s,
            "max_attempts": messages.max_attempts,
        },
        "contacts": {"manage": config.contacts.manage, "heard_nodes": config.contacts.heard_nodes},
        "telemetry": {
            "enabled": config.telemetry.enabled,
            "ask": config.telemetry.ask,
            "ask_types": sorted(config.telemetry.ask_types),
        },
        "homeassistant": {
            "controls": ha.controls,
            "hidden_targets": len(ha.hidden_targets),
            "contact_entity_prefix": ha.contact_entity_prefix,
            "language": ha.language,
            "contact_devices": ha.contact_devices,
        },
        "radio": {
            "transport": radio.transport,
            "tcp_unauthenticated": radio.tcp_unauthenticated,
            "set_clock": radio.set_clock,
            "profile": {"manage": radio.profile.manage},
        },
        "airtime": {
            "budget_s_per_hour": config.airtime.budget_s_per_hour,
            "burst_s": config.airtime.burst_s,
        },
        "history": {
            "enabled": history.enabled,
            "messages_days": history.messages_days,
            "positions_days": history.positions_days,
            "readings_days": history.readings_days,
        },
        "commands": {
            "senders": len(commands.senders),
            "codes": commands.codes_file is not None,
            "reply": commands.reply,
        },
    }
