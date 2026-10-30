"""Contact management (ADR 0017): requests to add a contact to the radio's table or remove one,
and to exclude a companion from telemetry or include it again (ADR 0018).

A request is checked at once (size, encoding, envelope, retained, target, switch, own key,
protected, queue) and refused with a reason, or queued. One worker takes the queued requests one
at a time: it needs the radio, a token of the radio-work budget and a fresh table, applies the
rules of ADR 0017 on that table (no look-alike of a contact or a protected key, no overwrite, no
full table), writes once, never again, and reads the table back.

A telemetry request needs no radio: it is checked (size, encoding, envelope, retained, rate,
key, type, the limit of stored keys) and taken at once, without the worker, the radio-work budget
or `[contacts] manage`. Exclusion only narrows what the gateway does, so `off` is taken for any
key, also one that is not on the radio yet.

Every request ends in one `contact` event: `refused` (nothing was written), `failed` (the radio
refused the write, the read-back disagrees, or the connection ended after the write went out),
or the table read that shows the change (`added`, `removed`), which carries the request's id; a
telemetry request in `excluded`, `included`, `refused` or `failed`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core import profile as policy
from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.core.contacts import (
    add_refusal,
    is_key,
    key_from_label,
    remove_refusal,
)
from meshcore_home_gateway.core.mesh import Node
from meshcore_home_gateway.core.text import contact_name
from meshcore_home_gateway.mqtt.client import ContactRequest
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol import codec
from meshcore_home_gateway.protocol.codes import NodeType
from meshcore_home_gateway.protocol.links import LinkContact, LinkError, parse_contact_text
from meshcore_home_gateway.radio.session import (
    Barrier,
    CommandFailedError,
    CompanionSession,
    NotSentError,
    RadioError,
    RadioEvent,
    RadioInfo,
)
from meshcore_home_gateway.views import contact_event

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 1024
MAX_WAITING = 8  # queued requests; then `busy`
# radio work: a request that reaches the radio reads the table and perhaps writes once; the
# radio rewrites its contacts file for each change
WORK_PER_S, WORK_BURST = 1 / 60, 10.0
REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST = 20.0, 100.0
REFUSED_REPORT_INTERVAL_S = 60.0
# telemetry requests: each may write the exclusions file, on the event loop
TELEMETRY_PER_S, TELEMETRY_BURST = 1.0, 30.0
_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_RESULT = {"add": "added", "remove": "removed"}  # a request's kind -> its event in the table
_EXCLUDED = {"off": True, "on": False}  # a telemetry request's text -> whether it excludes


class _RefusedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class _Seen:
    """What the checks of a request have found so far, for its refusal."""

    request_id: str
    key: str | None = None
    claim: LinkContact | None = None


@dataclass(frozen=True, slots=True)
class Job:
    kind: str  # "add" or "remove"
    key: str
    request_id: str
    source: str  # add: "key", "link", "card", "heard" (a label); remove: "topic"
    name: str | None = None  # add: claimed (link, card)
    node_type: int | None = 1  # add: claimed; remove: None until the table names it
    heard: Node | None = None  # add: the record of the node's signed advert, if it was heard


class ContactManager:
    def __init__(
        self,
        config: Config,
        observer: Observer,
        events: asyncio.Queue[RadioEvent],
        session: Callable[[], CompanionSession | None],
        *,
        protected: frozenset[str],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """`events`: the bridge's queue, whose order the results keep; `session`: the live one."""
        self._config = config
        self._observer = observer
        self._events = events
        self._session = session
        self._protected = protected
        self._mono = monotonic
        self._jobs: asyncio.Queue[Job] = asyncio.Queue(maxsize=MAX_WAITING)
        self._work = RateLimit(WORK_PER_S, WORK_BURST, now=monotonic())
        self._refusals = RateLimit(REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, now=monotonic())
        self._telemetry_rate = RateLimit(TELEMETRY_PER_S, TELEMETRY_BURST, now=monotonic())
        self._unpublished = 0
        self._next_report = monotonic()
        self._writing = False  # the worker's request has reached its write

    @property
    def manage(self) -> bool:
        return self._config.contacts.manage

    def takes_requests(self, info: RadioInfo | None) -> bool:
        """Whether requests can be written on this radio: the switch, and firmware the gateway
        is tested with (as the managed profile: ADR 0020's firmware gate) unless the profile
        says `always`. Without a radio, the switch alone."""
        return self.manage and (info is None or self._firmware_allowed(info))

    def _firmware_allowed(self, info: RadioInfo) -> bool:
        tested = info.device.firmware_code in policy.TESTED_FIRMWARE
        return tested or self._config.radio.profile.manage == "always"

    # --- accept ---------------------------------------------------------------------------------

    def accept(self, request: ContactRequest) -> None:
        """Check a request at once: refused, or queued for the worker; a telemetry request is
        taken at once."""
        if request.kind == "telemetry":
            self._telemetry(request)
            return
        seen = _Seen(secrets.token_hex(4))
        try:
            job = self._checked(request, seen)
            self._jobs.put_nowait(job)
        except _RefusedError as exc:
            level = request.key if request.kind == "remove" and seen.key is None else None
            self._refuse(request.kind, seen.key, seen.request_id, exc.reason, seen.claim,
                         level=level)  # fmt: skip
        except asyncio.QueueFull:
            self._refuse(request.kind, seen.key, seen.request_id, "busy", seen.claim)

    def _checked(self, request: ContactRequest, seen: _Seen) -> Job:
        """The checks in their documented order; the first that fails names the reason."""
        text = self._envelope(request, seen)
        target = None  # why the target is refused, once retained requests are
        if request.kind == "remove":  # the level is any text: logged with %r, never echoed
            seen.key = request.key if request.key is not None and is_key(request.key) else None
            target = None if seen.key else "unknown_contact"
        elif request.node_label:  # Home Assistant's select: a heard node (or a contact)
            seen.claim = self._labelled(text)
            seen.key = seen.claim.public_key if seen.claim else None
            target = None if seen.claim else "unknown_node"
        else:
            try:
                seen.claim = parse_contact_text(text)
                seen.key = seen.claim.public_key
            except LinkError as exc:
                target = exc.reason
        if request.retained:
            raise _RefusedError("retained")
        if target is not None or seen.key is None:
            raise _RefusedError(target or "unknown_contact")
        return self._job(request.kind, seen.key, seen.request_id, seen.claim)

    def _envelope(self, request: ContactRequest, seen: _Seen) -> str:
        """The request's text (add); its id goes to `seen` as soon as it is valid."""
        text = _text(request.payload)
        if text.lstrip().startswith("{"):
            text = self._json_envelope(text, request.kind, seen)
        try:  # a JSON \ud800 escape is valid UTF-8 on the wire, but no text
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise _RefusedError("invalid_utf8") from None
        return text

    def _json_envelope(self, text: str, kind: str, seen: _Seen) -> str:
        envelope = _json_object(text)
        seen.request_id = _request_id(envelope) or seen.request_id
        contact = envelope.get("contact")
        if kind == "add" and not isinstance(contact, str):
            raise _RefusedError("invalid_request")  # an empty text is judged as a text
        return contact if isinstance(contact, str) else ""

    def _labelled(self, label: str) -> LinkContact | None:
        """The node a label names, among the heard nodes and the contacts, as known now."""
        mesh = self._observer.mesh
        key = key_from_label(label, {*mesh.nodes, *mesh.contacts})
        tracked = (mesh.nodes.get(key) or mesh.contacts.get(key)) if key else None
        if key is None or tracked is None:
            return None
        return LinkContact(key, tracked.node.name, tracked.node.node_type, "heard")

    def _job(self, kind: str, key: str, request_id: str, claim: LinkContact | None) -> Job:
        if not self.manage:
            raise _RefusedError("management_off")
        session = self._session()
        info = session.info if session is not None else None
        if kind == "add" and info is not None and key == info.public_key:
            raise _RefusedError("own_key")
        if kind == "remove":
            if key in self._protected:
                raise _RefusedError("protected")
            return Job("remove", key, request_id, "topic", node_type=None)
        assert claim is not None  # noqa: S101 - parsed above
        heard = self._observer.mesh.nodes.get(key)
        return Job(
            "add", key, request_id, claim.source, claim.name, claim.node_type,
            heard.node if heard is not None else None,
        )  # fmt: skip

    # --- telemetry (ADR 0018) --------------------------------------------------------------------

    def _telemetry(self, request: ContactRequest) -> None:
        """Exclude a companion from telemetry or include it again. Nothing here awaits, so the
        exclusion, its documents and its event cannot interleave with the poller."""
        seen = _Seen(secrets.token_hex(4))
        try:
            excluding, forget = self._telemetry_checked(request, seen)
        except _RefusedError as exc:
            level = request.key if seen.key is None else None
            self._refuse("telemetry", seen.key, seen.request_id, exc.reason, None, level=level)
            return
        key = seen.key
        assert key is not None  # noqa: S101 - checked above
        kind, reason = self._exclude(key, forget) if excluding else self._include(key)
        log.info("contact request %s telemetry %s %s: %s%s", seen.request_id,
                 "off" if excluding else "on", key[:12], kind,
                 f" ({reason})" if reason else "")  # fmt: skip
        tracked = self._observer.mesh.contacts.get(key)
        self._observer.contact_result(contact_event(
            kind, key, tracked.node if tracked is not None else None, request="telemetry",
            request_id=seen.request_id, reason=reason,
        ))  # fmt: skip

    def _telemetry_checked(self, request: ContactRequest, seen: _Seen) -> tuple[bool, bool]:
        """The checks in their documented order: whether the request excludes, and whether it
        forgets the contact's history too."""
        excluding, forget = _telemetry_envelope(request.payload, seen)
        seen.key = request.key if request.key is not None and is_key(request.key) else None
        if request.retained:
            raise _RefusedError("retained")
        if not self._telemetry_rate.allow(self._mono()):  # also one refused below counts
            raise _RefusedError("rate_limited")
        exclusions, key = self._observer.exclusions, seen.key
        tracked = self._observer.mesh.contacts.get(key) if key is not None else None
        if key is None or (tracked is None and not excluding and key not in exclusions):
            raise _RefusedError("unknown_contact")
        if tracked is not None and tracked.node.node_type != NodeType.CHAT:
            raise _RefusedError("not_a_companion")  # only companions are asked
        if excluding and key not in exclusions and len(exclusions) >= exclusions.LIMIT:
            raise _RefusedError("too_many")
        return excluding, forget

    def _exclude(self, key: str, forget: bool) -> tuple[str, str | None]:
        """In effect also when it cannot be stored: written again at every status tick."""
        stored = self._observer.exclusions.exclude(key)
        forgotten = self._excluded(key, True, forget)
        if not stored:
            return "excluded", "not_saved"
        return "excluded", "not_forgotten" if forgotten is False else None

    def _include(self, key: str) -> tuple[str, str | None]:
        """Only once stored; the poller is told only of a change (it asks an included contact
        at once)."""
        exclusions = self._observer.exclusions
        was = key in exclusions
        if not exclusions.include(key):
            return "failed", "state_unwritable"
        if was:
            self._excluded(key, False, False)
        return "included", None

    def _excluded(self, key: str, excluded: bool, forget: bool) -> bool | None:
        """The observer forgets the values, tells the poller and publishes: a bug there must not
        lose the result."""
        try:
            return self._observer.telemetry_excluded(key, excluded, forget=forget)
        except Exception:
            log.exception("contact request telemetry %s: observer failed", key[:12])
            return False if forget else None

    # --- the worker -----------------------------------------------------------------------------

    async def run(self) -> None:
        while True:
            job = await self._jobs.get()
            self._writing = False
            try:
                await self._handle(job)
            except _RefusedError as exc:
                self._refuse(job.kind, job.key, job.request_id, exc.reason, job)
            except asyncio.CancelledError:
                raise
            except Exception:  # a bug: the request still ends, the worker goes on
                log.exception("contact request %s failed", job.request_id)
                try:
                    if self._writing:
                        await self._done(job, "failed", "internal_error")
                    else:
                        self._observer.contact_result(self._event("failed", job, "internal_error"))
                except Exception:  # not even the result: the service must stay up
                    log.exception("contact request %s: no result published", job.request_id)

    async def _handle(self, job: Job) -> None:
        session = self._session()
        if session is None or session.closed or session.info is None:
            raise _RefusedError("no_radio")
        info = session.info
        if not self._firmware_allowed(info):
            raise _RefusedError("untested_firmware")
        if not self._work.allow(self._mono()):
            raise _RefusedError("rate_limited")
        await self._fresh_table(session)
        if job.kind == "add":
            await self._add(session, info.public_key, info.device.max_contacts, job)
        else:
            await self._remove(session, job)

    async def _fresh_table(self, session: CompanionSession) -> None:
        """The rules apply to the table as the radio has it now: read again if it changed since
        the last read began (a read in progress that began later is waited for, not repeated)."""
        try:
            await session.refresh_contacts(newer_than=session.contacts_changed_at)
        except CommandFailedError as exc:
            raise _RefusedError(exc.reason) from None
        except RadioError:
            raise _RefusedError("no_radio") from None

    async def _add(self, session: CompanionSession, own_key: str, limit: int, job: Job) -> None:
        heard = job.heard or _node(self._observer.mesh.nodes.get(job.key))
        if heard is not None:  # its own signed advert: name, type and time are the node's
            name, node_type, advert = heard.name, heard.node_type, heard.last_advert
        else:  # a claim: the node's first advert replaces name and type (ADR 0017)
            name, node_type, advert = job.name or "", job.node_type or 1, 0
        name = contact_name(name) or job.key[:12]
        reason = add_refusal(
            job.key, node_type, table=session.contact_records, protected=self._protected,
            own_key=own_key, max_contacts=limit,
        )  # fmt: skip
        if reason is not None:
            raise _RefusedError(reason)
        request = codec.new_contact(
            bytes.fromhex(job.key), name, 0, node_type=node_type, last_advert=advert
        )
        claimed = Job(job.kind, job.key, job.request_id, job.source, name, node_type, job.heard)
        await self._write(session, claimed, session.write_contact(request))

    async def _remove(self, session: CompanionSession, job: Job) -> None:
        record = session.contact_records.get(job.key)
        reason = remove_refusal(job.key, table=session.contact_records)
        if reason is not None or record is None:
            raise _RefusedError(reason or "unknown_contact")
        named = Job(job.kind, job.key, job.request_id, job.source, record.name, record.node_type)
        await self._write(session, named, session.remove_contact(record.public_key))

    async def _write(self, session: CompanionSession, job: Job, write: Awaitable[None]) -> None:
        """Write once, read the table back; the result after the events before it."""
        expected = _RESULT[job.kind]
        self._writing = True  # from here on, the result follows the events before it
        self._observer.expect(expected, job.key, job.kind, job.request_id)
        try:
            await write
        except NotSentError:
            await self._done(job, "refused", "no_radio")
            return
        except CommandFailedError as exc:
            await self._done(job, "failed", exc.reason)
            return
        except RadioError:
            await self._done(job, "failed", "outcome_unknown")
            return
        try:
            await session.refresh_contacts(newer_than=session.commands_sent)
        except RadioError:  # also the radio refusing the read: the write's outcome is unknown
            await self._done(job, "failed", "outcome_unknown")
            return
        present = job.key in session.contact_records
        if present == (job.kind == "add"):
            await self._done(job, expected, None)  # reported by the read, normally
            return
        log.warning("contact request %s %s %s: the radio answered OK, but its table does not "
                    "show the change", job.request_id, job.kind, job.key[:12])  # fmt: skip
        await self._done(job, "failed", "not_confirmed")

    async def _done(self, job: Job, kind: str, reason: str | None) -> None:
        """The result of a request whose write was tried: after the table read it caused."""
        barrier = Barrier()
        await self._events.put(barrier)
        await barrier.done.wait()
        if reason is not None:
            log.info("contact request %s %s %s: %s (%s)", job.request_id, job.kind,
                     job.key[:12], kind, reason)  # fmt: skip
        self._observer.request_done(_RESULT[job.kind], job.key, self._event(kind, job, reason))

    # --- results --------------------------------------------------------------------------------

    def _refuse(
        self, kind: str, key: str | None, request_id: str, reason: str,
        claim: LinkContact | Job | None, *, level: str | None = None,
    ) -> None:  # fmt: skip
        """A refusal (nothing written), at a bounded rate: a flood of requests must not flood
        the bus. `level`: the topic level of a remove request that names no key."""
        named = key[:12] if key else repr(level[:80]) if level is not None else None
        log.info("contact request %s %s %s refused: %s", request_id, kind, named, reason)
        if not self._refusals.allow(self._mono()):
            self._unpublished += 1
            self.report_unpublished()
            return
        name = claim.name if claim is not None else None
        node_type = claim.node_type if claim is not None else None
        mesh = self._observer.mesh
        tracked = (
            mesh.contacts.get(key) or (mesh.nodes.get(key) if kind == "add" else None)
            if key
            else None
        )  # as known now: a node's own record over a claim
        node = tracked.node if tracked is not None else None
        self._observer.contact_result(contact_event(
            "refused", key, node, name=name, node_type=node_type, request=kind,
            request_id=request_id, reason=reason,
        ))  # fmt: skip

    def _event(self, kind: str, job: Job, reason: str | None) -> dict[str, Any]:
        return contact_event(
            kind, job.key, name=job.name, node_type=job.node_type, request=job.kind,
            request_id=job.request_id, reason=reason,
        )  # fmt: skip

    def report_unpublished(self) -> None:
        now = self._mono()
        if self._unpublished and now >= self._next_report:
            log.warning(
                "contact events of refused requests not published (limit %g per second, bursts "
                "of %g): %d", REFUSED_EVENTS_PER_S, REFUSED_EVENTS_BURST, self._unpublished,
            )  # fmt: skip
            self._unpublished = 0
            self._next_report = now + REFUSED_REPORT_INTERVAL_S


def _node(tracked: Any) -> Node | None:
    return tracked.node if tracked is not None else None


def _text(payload: bytes) -> str:
    """A request's payload as text: at most MAX_REQUEST_BYTES of UTF-8."""
    if len(payload) > MAX_REQUEST_BYTES:
        raise _RefusedError("too_large")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError:
        raise _RefusedError("invalid_utf8") from None


def _json_object(text: str) -> dict[str, Any]:
    try:
        envelope = json.loads(text)
    except (ValueError, RecursionError):
        raise _RefusedError("invalid_request") from None
    if not isinstance(envelope, dict):
        raise _RefusedError("invalid_request")
    return envelope


def _request_id(envelope: dict[str, Any]) -> str | None:
    """The envelope's id, if it has one; a bad one refuses the request."""
    given = envelope.get("id")
    if given is not None and not (isinstance(given, str) and _ID.fullmatch(given)):
        raise _RefusedError("invalid_request")
    return given


def _telemetry_envelope(payload: bytes, seen: _Seen) -> tuple[bool, bool]:
    """`on` or `off` (spaces around stripped), or JSON with `telemetry`, `id` and `forget`:
    whether it excludes, and whether it forgets the history too (only with `off`). Its id goes to
    `seen` as soon as it is valid."""
    value: object = _text(payload)
    forget: object = False
    if isinstance(value, str) and value.lstrip().startswith("{"):
        envelope = _json_object(value)
        seen.request_id = _request_id(envelope) or seen.request_id
        value, forget = envelope.get("telemetry"), envelope.get("forget", False)
    if not isinstance(value, str) or not isinstance(forget, bool):
        raise _RefusedError("invalid_request")
    state = value.strip()
    if state not in _EXCLUDED or (forget and state == "on"):
        raise _RefusedError("invalid_request")
    return _EXCLUDED[state], forget
