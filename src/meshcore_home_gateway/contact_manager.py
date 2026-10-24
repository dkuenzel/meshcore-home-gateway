"""Contact management (ADR 0017): requests to add a contact to the radio's table or remove one.

A request is checked at once (size, encoding, envelope, retained, target, switch, own key,
protected, queue) and refused with a reason, or queued. One worker takes the queued requests one
at a time: it needs the radio, a token of the radio-work budget and a fresh table, applies the
rules of ADR 0017 on that table (no look-alike of a contact or a protected key, no overwrite, no
full table), writes once, never again, and reads the table back.

Every request ends in one `contact` event: `refused` (nothing was written), `failed` (the radio
refused the write, the read-back disagrees, or the connection ended after the write went out),
or the table read that shows the change (`added`, `removed`), which carries the request's id.
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
_ID = re.compile(r"[A-Za-z0-9._:-]{1,64}")
_RESULT = {"add": "added", "remove": "removed"}  # a request's kind -> its event in the table


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
        """Check a request at once: refused, or queued for the worker."""
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
        if len(request.payload) > MAX_REQUEST_BYTES:
            raise _RefusedError("too_large")
        try:
            text = request.payload.decode("utf-8")
        except UnicodeDecodeError:
            raise _RefusedError("invalid_utf8") from None
        if text.lstrip().startswith("{"):
            text = self._json_envelope(text, request.kind, seen)
        try:  # a JSON \ud800 escape is valid UTF-8 on the wire, but no text
            text.encode("utf-8")
        except UnicodeEncodeError:
            raise _RefusedError("invalid_utf8") from None
        return text

    def _json_envelope(self, text: str, kind: str, seen: _Seen) -> str:
        try:
            envelope = json.loads(text)
        except (ValueError, RecursionError):
            raise _RefusedError("invalid_request") from None
        if not isinstance(envelope, dict):
            raise _RefusedError("invalid_request")
        given = envelope.get("id")
        if given is not None and not (isinstance(given, str) and _ID.fullmatch(given)):
            raise _RefusedError("invalid_request")
        seen.request_id = given or seen.request_id
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
