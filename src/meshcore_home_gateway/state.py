"""Small persistent state in one JSON file, written atomically.

Today it holds the newest accepted command timestamp per sender (replay protection, ADR 0009).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

log = logging.getLogger(__name__)


class JsonStateFile:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
        if not isinstance(data, dict):
            aside = self.path.with_name(self.path.name + ".corrupt")
            self.path.replace(aside)
            log.warning("state file %s unreadable, moved to %s, starting empty", self.path, aside)
            return {}
        return data

    def save(self, data: dict[str, Any]) -> None:
        """Write to a temporary file in the same directory, fsync, then rename over the old one."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=self.path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        directory = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


class CommandLedger:
    """Newest accepted sender timestamp per sender key; only ever moves forward."""

    _KEY = "command_timestamps"

    def __init__(self, store: JsonStateFile) -> None:
        self._store = store
        raw = store.load().get(self._KEY, {})
        self._last: dict[str, int] = {}
        if isinstance(raw, dict):
            self._last = {k: v for k, v in raw.items() if isinstance(v, int)}

    def last(self, sender_key: str) -> int | None:
        return self._last.get(sender_key)

    @property
    def view(self) -> Mapping[str, int]:
        return MappingProxyType(self._last)

    def record(self, sender_key: str, timestamp: int) -> None:
        """Store the timestamp durably; memory follows only after a successful save."""
        if timestamp <= self._last.get(sender_key, -1):
            return
        updated = {**self._last, sender_key: timestamp}
        data = self._store.load()
        data[self._KEY] = updated
        self._store.save(data)
        self._last = updated
