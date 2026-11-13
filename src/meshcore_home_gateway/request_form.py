"""The form of the gateway's requests on the bus: at most MAX_REQUEST_BYTES of UTF-8, and, when the
text is a JSON object, an optional `id` that comes back in the request's events (docs/mqtt-api.md).
The contact requests (ADR 0017) and the advert requests (ADR 0033) share it.

Pure: no I/O.
"""

from __future__ import annotations

import json
import re
from typing import Any

MAX_REQUEST_BYTES = 1024
REQUEST_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")


class RefusedError(Exception):
    """A request is refused; `reason` goes into its event."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def request_text(payload: bytes) -> str:
    """A request's payload as text: at most MAX_REQUEST_BYTES of UTF-8."""
    if len(payload) > MAX_REQUEST_BYTES:
        raise RefusedError("too_large")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise RefusedError("invalid_utf8") from None


def json_object(text: str) -> dict[str, Any]:
    """The JSON object a request's text holds."""
    try:
        envelope = json.loads(text)
    except (ValueError, RecursionError):
        raise RefusedError("invalid_request") from None
    if not isinstance(envelope, dict):
        raise RefusedError("invalid_request")
    return envelope


def request_id(envelope: dict[str, Any]) -> str | None:
    """The envelope's id, if it has one; a bad one refuses the request."""
    given = envelope.get("id")
    if given is not None and not (isinstance(given, str) and REQUEST_ID.fullmatch(given)):
        raise RefusedError("invalid_request")
    return given
