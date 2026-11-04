"""`run`: the service (wiring.py), logging in the journal's form."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

from meshcore_home_gateway import __version__
from meshcore_home_gateway.cli.common import DEFAULT_STATE_DIR, EX_OK
from meshcore_home_gateway.config import Config, load_config

log = logging.getLogger("meshcore_home_gateway")


def _run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    setup_logging(config.diagnostics.log_level)
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    log.info("meshcore-home-gateway %s starting, state in %s", __version__, state_dir)
    return asyncio.run(_serve(config, state_dir))


async def _serve(config: Config, state_dir: Path) -> int:
    from meshcore_home_gateway.wiring import serve  # noqa: PLC0415 - light start

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await serve(config, state_dir, stop)
    log.info("meshcore-home-gateway stopped")
    return EX_OK


def setup_logging(level: str) -> None:
    under_journal = "JOURNAL_STREAM" in os.environ  # the journal adds its own time stamps
    fmt = "%(levelname)s %(name)s: %(message)s"
    logging.basicConfig(
        level=getattr(logging, level.upper()),
        format=fmt if under_journal else "%(asctime)s " + fmt,
        stream=sys.stderr,
        force=True,
    )
