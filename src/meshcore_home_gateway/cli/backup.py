"""`backup FILE`: the configuration and the state in one archive (IMP-016, OPS-008), while the
service runs: the state file is replaced atomically, the history is copied with SQLite's online
backup. The configuration goes without the broker password, and the archive without the command
senders' code secrets, unless `--with-secrets`; the archive
holds the household's positions and messages either way, so it is written for its owner only."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sqlite3
import sys
import tarfile
import tempfile
import time
import tomllib
from pathlib import Path

from meshcore_home_gateway import __version__
from meshcore_home_gateway.cli.common import DEFAULT_STATE_DIR, EX_FAILURE, EX_OK, PREFIX, tr

STATE_FILES = ("state.json", "telemetry-excluded.json")
HISTORY_FILE = "history.sqlite3"
_PASSWORD = re.compile(r"^(\s*)password\s*=.*$")
LEFT_OUT = "# password left out by `meshcore-home-gateway backup`: set it again before a restore"


class _RefusedError(Exception):
    """Why the archive is not written, for the user."""


def _backup(args: argparse.Namespace) -> int:
    state_dir = args.state_dir or Path(os.environ.get("STATE_DIRECTORY", DEFAULT_STATE_DIR))
    try:
        members = contents(args.config, state_dir, with_secrets=args.with_secrets)
        _write(args.file, members)
    except _RefusedError as exc:
        print(PREFIX + str(exc), file=sys.stderr)
        return EX_FAILURE
    except FileExistsError:
        print(PREFIX + tr("{path} exists: choose another name", path=args.file), file=sys.stderr)
        return EX_FAILURE
    except OSError as exc:
        written = tr("cannot write {path}: {detail}", path=args.file, detail=exc.strerror or exc)
        print(PREFIX + written, file=sys.stderr)
        return EX_FAILURE
    print(tr("wrote {path}: {files}", path=args.file, files=", ".join(sorted(members))))
    return EX_OK


def contents(config_path: Path, state_dir: Path, *, with_secrets: bool) -> dict[str, bytes]:
    """The archive's files by name, MANIFEST.json included."""
    try:
        config_text = _read(config_path).decode("utf-8")
        parsed = tomllib.loads(config_text)
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise _RefusedError(
            tr("cannot read {path}: {detail}", path=config_path, detail=exc)
        ) from None
    mqtt, commands = parsed.get("mqtt", {}), parsed.get("commands", {})
    secret, codes = mqtt.get("password_file"), commands.get("codes_file")
    members = {"config.toml": _configuration(config_text, mqtt, with_secrets=with_secrets)}
    if with_secrets and isinstance(secret, str):
        members["password"] = _read(Path(secret))
    if with_secrets and isinstance(codes, str):
        members["command-codes"] = _read(Path(codes))
    for name in STATE_FILES:
        if (state_dir / name).exists():
            members[f"state/{name}"] = _read(state_dir / name)
    history = state_dir / HISTORY_FILE
    if history.exists():
        try:
            members[f"state/{HISTORY_FILE}"] = _history_copy(history)
        except (OSError, sqlite3.Error) as exc:
            raise _RefusedError(
                tr("cannot read {path}: {detail}", path=history, detail=_system_text(exc))
            ) from None
    has_password = "password" in mqtt or isinstance(secret, str)
    manifest = {
        "version": __version__,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config": str(config_path),
        "state_dir": str(state_dir),
        "password": ("kept" if with_secrets else "left_out") if has_password else "none",
        "password_file": secret if isinstance(secret, str) else None,
        "codes": ("kept" if with_secrets else "left_out") if isinstance(codes, str) else "none",
        "codes_file": codes if isinstance(codes, str) else None,
        "files": sorted(members),
    }
    members["MANIFEST.json"] = json.dumps(manifest, indent=2).encode("utf-8") + b"\n"
    return members


def _configuration(text: str, mqtt: dict[str, object], *, with_secrets: bool) -> bytes:
    if with_secrets or "password" not in mqtt:
        return text.encode("utf-8")
    kept = without_password(text)
    if kept is None:
        raise _RefusedError(
            # Translators: security text (docs/translations.md): the backup refuses rather than
            # keep the password unasked
            tr(
                'the broker password is not on a line of its own (password = "…" in [mqtt]): '
                "the backup cannot leave it out; add --with-secrets to keep it"
            )
        )
    return kept.encode("utf-8")


def _read(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        detail = exc.strerror or exc
        raise _RefusedError(tr("cannot read {path}: {detail}", path=path, detail=detail)) from None


def _system_text(exc: Exception) -> object:
    """The system's text of an error, without its number and the path again."""
    return getattr(exc, "strerror", None) or exc


def without_password(text: str) -> str | None:
    """The configuration with the line of [mqtt]'s `password` as a comment; None if the password
    stays in it (a dotted key, an inline table, a string over several lines)."""
    lines, table = text.splitlines(keepends=True), ""
    for i, line in enumerate(lines):
        if header := re.match(r"^\s*\[\s*([^\]]+?)\s*\]", line):
            table = header.group(1)
        elif table == "mqtt" and (found := _PASSWORD.match(line.rstrip("\r\n"))):
            lines[i] = found.group(1) + LEFT_OUT + "\n"
    kept = "".join(lines)
    try:
        still = tomllib.loads(kept).get("mqtt", {}).get("password")
    except tomllib.TOMLDecodeError:
        return None
    return None if still is not None else kept


def _history_copy(path: Path) -> bytes:
    """A consistent copy of the history while the service writes it (SQLite's online backup)."""
    with tempfile.TemporaryDirectory() as directory:
        copy = Path(directory) / HISTORY_FILE
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            target = sqlite3.connect(copy)
            try:
                source.backup(target)
            finally:
                target.close()
        finally:
            source.close()
        return copy.read_bytes()


def _write(path: Path, members: dict[str, bytes]) -> None:
    """A gzip'd tar archive, readable by its owner only; never over an existing file."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with (
            os.fdopen(descriptor, "wb") as file,
            tarfile.open(fileobj=file, mode="w:gz") as archive,
        ):
            now = time.time()
            for name, data in members.items():
                info = tarfile.TarInfo(name)
                info.size, info.mtime, info.mode = len(data), int(now), 0o600
                archive.addfile(info, io.BytesIO(data))
    except BaseException:
        path.unlink(missing_ok=True)  # no half archive that looks like a backup (a full disk)
        raise
