"""`dashboard`: the gateway's Home Assistant dashboard as YAML (design §6.13)."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from meshcore_home_gateway.cli.common import DEFAULT_STATE_DIR, EX_FAILURE, EX_OK, PREFIX, tr, trn
from meshcore_home_gateway.config import load_config


def _dashboard(args: argparse.Namespace) -> int:
    from meshcore_home_gateway.dashboard import dashboard  # noqa: PLC0415

    config = load_config(args.config)
    if not config.homeassistant.discovery:
        off = tr(
            "Home Assistant discovery is off ([homeassistant] discovery = false): the dashboard "
            "would find no entities."
        )
        print(PREFIX + off, file=sys.stderr)
        return EX_FAILURE
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    text, count = dashboard(config, state_dir)
    sys.stdout.flush()
    sys.stdout.buffer.write(text.encode("utf-8"))  # whatever the terminal's encoding
    sys.stdout.buffer.flush()
    print(
        trn(
            "The map lists {count} contact with a position.",
            "The map lists {count} contacts with a position.",
            count,
        ),
        file=sys.stderr,
    )
    return EX_OK
