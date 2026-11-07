"""`dashboard`: the gateway's Home Assistant dashboard as YAML (design §6.13), and on standard
error what it holds and what it needs."""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from meshcore_home_gateway.cli.common import DEFAULT_STATE_DIR, EX_FAILURE, EX_OK, PREFIX, tr, trn
from meshcore_home_gateway.config import load_config


def _dashboard(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.dashboard import HOME_ASSISTANT, build  # noqa: PLC0415

    config = load_config(args.config)
    if not config.homeassistant.discovery:
        off = tr(
            "Home Assistant discovery is off ([homeassistant] discovery = false): the dashboard "
            "would find no entities."
        )
        print(PREFIX + off, file=sys.stderr)
        return EX_FAILURE
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    built = build(config, state_dir)
    sys.stdout.flush()
    sys.stdout.buffer.write(built.yaml.encode("utf-8"))  # whatever the terminal's encoding
    sys.stdout.buffer.flush()
    found = built.chats
    lines = [
        trn(
            "The map lists {count} contact with a position.",
            "The map lists {count} contacts with a position.",
            built.trackers,
        ),
        # Translators: after the dashboard was written: how many chat pages it has, for the
        # channels of the configuration and for people (contacts with a device)
        tr(
            "Chat pages: {channels} for channels, {people} for people.",
            channels=found.channels,
            people=found.people,
        ),
    ]
    if found.without_page:
        lines.append(
            trn(
                "{count} contact has no chat page; it shows in Chats without one.",
                "{count} contacts have no chat page; they show in Chats without one.",
                found.without_page,
            )
        )
    for name, value in found.skipped:  # repr: a slug of the state file may hold anything
        # Translators: field is the name of a value of the page (path, title, key), value the
        # value as written in the service's state, quoted
        lines.append(
            tr(
                "Skipped a chat page: its {field} {value} is not allowed.",
                field=name,
                value=repr(value),
            )
        )
    for first, second in found.pairs:
        # Translators: a and b are aliases of targets of the configuration; Home Assistant
        # turns '-' into '_' in entity IDs, so both get the same
        lines.append(
            tr(
                "The targets {a} and {b} get the same entity IDs in Home Assistant; their chat "
                "pages have no message field. Rename one of them.",
                a=first,
                b=second,
            )
        )
    if built.written_at is not None:
        written = time.strftime("%Y-%m-%d %H:%M", time.localtime(built.written_at))
        # Translators: time is the date and time of the service's state file, such as
        # 2026-11-07 14:10
        lines.append(
            tr(
                "Contacts as of the gateway's state written at {time}; a contact added since "
                "then is missing until the dashboard is made again.",
                time=written,
            )
        )
    # Translators: version is a version of Home Assistant, such as 2026.10
    lines.append(
        tr("This dashboard needs Home Assistant {version} or later.", version=HOME_ASSISTANT)
    )
    for line in lines:
        print(line, file=sys.stderr)
    return EX_OK
