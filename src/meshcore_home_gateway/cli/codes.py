"""`commands enrol KEY`: a new code secret for a command sender (IMP-002). It goes into the file
`[commands] codes_file` names, which the service reads at its start; the sender adds it to an
authenticator app from the otpauth:// line this prints (a QR code tool shows it as one) and ends
each command with the app's 6-digit code (docs/configuration.md, `[commands]`). `commands revoke
KEY` removes the secret of a key that is no sender any more (#80)."""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import stat
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import quote

from meshcore_home_gateway.cli.common import EX_FAILURE, EX_OK, EX_USAGE, PREFIX, tr, tr_text
from meshcore_home_gateway.config import parse_config, read_text
from meshcore_home_gateway.core.commands import CODE_DIGITS, CODE_STEP_S
from meshcore_home_gateway.core.contacts import normalise_key
from meshcore_home_gateway.texts import TextError

SECRET_BYTES = 20  # 160 bits, as RFC 4226 recommends
HEADER = (
    "# The code secrets of meshcore-home-gateway's command senders (commands enrol). Whoever\n"
    "# reads this file can make their codes: keep it root's and the service's alone.\n"
)


class _RefusedError(Exception):
    """Why the file is not written, for the user."""


def _enrol(args: argparse.Namespace) -> int:
    config = parse_config(read_text(args.config))  # not load_config: the file may not exist yet
    try:
        key = normalise_key(args.key)
    except TextError as exc:
        print(PREFIX + tr_text(exc.text), file=sys.stderr)
        return EX_USAGE
    if config.commands.codes_file is None:
        missing = tr(
            "set [commands] codes_file first, such as /etc/meshcore-home-gateway/command-codes "
            "(docs/configuration.md)"
        )
        print(PREFIX + missing, file=sys.stderr)
        return EX_FAILURE
    if key not in config.commands.senders:
        unknown = tr("{key} is not in [commands] senders: add it there first", key=key)
        print(PREFIX + unknown, file=sys.stderr)
        return EX_FAILURE
    path = Path(config.commands.codes_file)
    secret = base64.b32encode(secrets.token_bytes(SECRET_BYTES)).decode("ascii")
    try:
        entries = _entries(path)
        _write(path, {**entries, key: secret}, args.config)
    except _RefusedError as exc:
        print(PREFIX + str(exc), file=sys.stderr)
        return EX_FAILURE
    print(otpauth_uri(secret, config.mqtt.node_id, key))
    # Translators: security text (docs/translations.md): the secret makes the sender's codes
    done = tr(
        "the code secret of {key} is in {path}: restart the service to use it (systemctl restart "
        "meshcore-home-gateway). Add the line above to the sender's authenticator app (as a QR "
        "code: qrencode -t ansiutf8 'LINE'), or type in {secret}. Commands from this sender then "
        "end with the app's 6-digit code; its earlier secret no longer works.",
        key=key,
        path=path,
        secret=secret,
    )
    print(PREFIX + done, file=sys.stderr)
    return EX_OK


def _revoke(args: argparse.Namespace) -> int:
    config = parse_config(read_text(args.config))
    try:
        key = normalise_key(args.key)
    except TextError as exc:
        print(PREFIX + tr_text(exc.text), file=sys.stderr)
        return EX_USAGE
    if config.commands.codes_file is None:
        print(PREFIX + tr("[commands] codes_file is not set: no secret to revoke"), file=sys.stderr)
        return EX_FAILURE
    if key in config.commands.senders:
        # Translators: security text (docs/translations.md): a sender without its secret would
        # command without a code; command is the command line that gives it a new secret
        still = tr(
            "{key} is in [commands] senders: without its secret it would command without a code. "
            "Remove it from the senders first, or give it a new secret: {command}",
            key=key,
            command=f"meshcore-home-gateway commands enrol {key}",
        )
        print(PREFIX + still, file=sys.stderr)
        return EX_FAILURE
    path = Path(config.commands.codes_file)
    try:
        entries = _entries(path)
        if key in entries:
            _write(path, {k: v for k, v in entries.items() if k != key}, args.config)
    except _RefusedError as exc:
        print(PREFIX + str(exc), file=sys.stderr)
        return EX_FAILURE
    if key not in entries:
        print(
            PREFIX + tr("{key} has no code secret in {path}", key=key, path=path), file=sys.stderr
        )
        return EX_FAILURE
    # Translators: the service reads the codes file only when it starts; while it ignores a
    # secret, it refuses the commands of senders without one (#80)
    done = tr(
        "the code secret of {key} is removed from {path}. The service reads the file at its "
        "start: restart it to apply this (systemctl restart meshcore-home-gateway).",
        key=key,
        path=path,
    )
    print(PREFIX + done, file=sys.stderr)
    for sender in _uncoded_after_revoke(config.commands.senders, entries, key):
        # Translators: security text (docs/translations.md): sender is a full public key;
        # command is the command line that gives it a code secret
        uncoded = tr(
            "after the restart, {sender} commands without a code: it has no code secret, and "
            "no secret is ignored any more; '{command}' gives it one",
            sender=sender,
            command=f"meshcore-home-gateway commands enrol {sender}",
        )
        print(PREFIX + tr("warning: {detail}", detail=uncoded), file=sys.stderr)
    return EX_OK


def _uncoded_after_revoke(
    senders: frozenset[str], entries: dict[str, str], revoked: str
) -> list[str]:
    """The senders that command without a code once the service reads the file without the
    secret of `revoked`: while a secret is ignored they are refused (code_missing), so they
    lose that only with the last ignored one (#80). A key corrected by hand and not enrolled
    is one of them."""
    left = {key for key in entries if key != revoked}
    if any(key not in senders for key in left):
        return []
    return sorted(sender for sender in senders if sender not in left)


def otpauth_uri(secret: str, node_id: str, key: str) -> str:
    """The secret as authenticator apps take it (Google's key URI format): this gateway and the
    sender's first 12 key digits as its name."""
    issuer = quote(f"meshcore-home-gateway {node_id}", safe="")
    return (
        f"otpauth://totp/{issuer}:{key[:12]}?secret={secret}&issuer={issuer}"
        f"&algorithm=SHA1&digits={CODE_DIGITS}&period={CODE_STEP_S}"
    )


def _entries(path: Path) -> dict[str, str]:
    """The file's secrets by normalised key; none while it does not exist."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError) as exc:
        detail = getattr(exc, "strerror", None) or exc
        raise _RefusedError(tr("cannot read {path}: {detail}", path=path, detail=detail)) from None
    try:
        return {normalise_key(k): str(v) for k, v in tomllib.loads(text).items()}
    except (tomllib.TOMLDecodeError, TextError) as exc:
        detail = tr_text(exc.text) if isinstance(exc, TextError) else exc
        raise _RefusedError(tr("cannot read {path}: {detail}", path=path, detail=detail)) from None


def _write(path: Path, entries: dict[str, str], config: Path) -> None:
    """Replace the file at once, with the mode and group it had; a new one gets mode 0640 and
    the configuration's group, which the service reads."""
    try:
        old = path.stat()
        mode, group = stat.S_IMODE(old.st_mode), old.st_gid
    except FileNotFoundError:
        mode, group = 0o640, config.stat().st_gid
    lines = "".join(f"{key} = {json.dumps(value)}\n" for key, value in sorted(entries.items()))
    try:
        descriptor, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    except OSError as exc:
        raise _RefusedError(
            tr("cannot write {path}: {detail}", path=path, detail=exc.strerror or exc)
        ) from None
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            os.fchmod(file.fileno(), mode)
            if os.fstat(file.fileno()).st_gid != group:
                os.fchown(file.fileno(), -1, group)
            file.write(HEADER + lines)
        os.replace(temporary, path)
    except BaseException as exc:
        Path(temporary).unlink(missing_ok=True)
        if not isinstance(exc, OSError):
            raise
        raise _RefusedError(
            tr("cannot write {path}: {detail}", path=path, detail=exc.strerror or exc)
        ) from None
