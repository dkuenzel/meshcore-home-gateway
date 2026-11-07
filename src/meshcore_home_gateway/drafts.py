"""The drafts of Home Assistant's chats: write, then send (docs/mqtt-api.md, Drafts).

A chat is a target not in `hidden_targets` (kind `target`, named by its alias) or a companion
whose device has the draft and its Send button (kind `contact`, named by its key; never a contact
target's key, whose chat is the target's). The gateway holds one draft per chat, in memory only,
and publishes it retained on `homeassistant/draft/<kind>/<id>` as `{"text": …}`. Home Assistant's
text entity writes `{"text": …}` to the chat's request topic below it, its Send button
`{"send": true}`, both at QoS 0 on that one topic, so the broker keeps their order. The MQTT
adapter hands both to `request` on the event loop as they come, with no wait: a press sends the
text stored just before it.

* A press hands the draft to the outbox as a request on `send/<alias>` or `contacts/<key>/send`
  would be: the same checks, queue, history and delivery events. A blank draft too, which ends
  `rejected` with reason `empty`; only a press within REPEAT_S after a queued press of the chat,
  the second tap of a double tap, ends silently: the first one's events are its outcome. Queued,
  the draft is cleared at once, inside the press's handler; refused or a duplicate, it stays, to
  be changed.
* A request that is not exactly `{"text": <string>}` or `{"send": true}`, not UTF-8 (a lone
  surrogate, which JSON writes as an escape, is none either), or with a text longer than the
  chat's `max` is ignored, and the draft published again. A retained request is a left-over and
  ignored.
* A stored text is echoed at once, then at most once per ECHO_WINDOW_S and chat: of a burst only
  the last follows when the window ends. The clear after a press, the republish at a connect to the
  broker and at Home Assistant's birth, a cut and an expiry go out at once, and drop an echo the
  window still holds: a held echo of the text before a press would refill the field after the
  clear.
* A draft unchanged for EXPIRY_S is cleared: a forgotten half sentence would otherwise be sent by
  the next press, maybe days later and by someone else.
* A lower `max` (a renamed radio, or its first connect, takes room from channel texts) cuts a
  longer draft, which goes out before the gateway's discovery: Home Assistant cannot take a state
  longer than `max`, and writes the state again as soon as it takes the discovery.
* A clean stop clears every draft that is not empty or whose echo is held, so no unsent text
  stays retained on the broker while the gateway is away. A chat that is gone has its topic
  deleted once its device's discovery is gone; the broker's copies of topics no chat owns are
  deleted at most CLEARS_PER_S a second. The draft topics are not documents of `views`: they
  exist for Home Assistant only.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, TypeGuard

from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.budget import RateLimit
from meshcore_home_gateway.core.model import DeliveryState
from meshcore_home_gateway.core.text import cut_chars
from meshcore_home_gateway.mqtt.discovery import text_room
from meshcore_home_gateway.mqtt.payloads import encode
from meshcore_home_gateway.mqtt.retained import CLEARS_BURST, CLEARS_PER_S, Cancellable
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.ports import RadioInfo, SendRequest
from meshcore_home_gateway.protocol.codec import MAX_TEXT_BYTES

if TYPE_CHECKING:
    from meshcore_home_gateway.observer import Publisher

log = logging.getLogger(__name__)

TARGET, CONTACT = "target", "contact"  # the kinds of chats, as the topics name them
ECHO_WINDOW_S = 0.2  # after an echo, the next of the chat waits this long
EXPIRY_S = 15 * 60.0  # a draft unchanged this long is cleared (the owner's choice, 2026-11-07)
REPEAT_S = 2.0  # a blank press this soon after a queued one of the chat is a double tap's second
# a request as Home Assistant's `tojson` writes it: every character outside ASCII as an escape,
# an emoji as two, "\ud83d\ude00", so 12 bytes for each of at most MAX_TEXT_BYTES characters
MAX_REQUEST_BYTES = 12 * MAX_TEXT_BYTES + 64

Chat = tuple[str, str]  # (kind, id)


@dataclass(eq=False)
class _Draft:
    max: int  # characters, as Home Assistant's text entity was announced with
    text: str = ""
    changed_at: float = 0.0  # monotonic
    queued_at: float | None = None  # when its last press was queued (monotonic)
    held: bool = False  # an echo waits for the window's end
    window: Cancellable | None = None  # the echo limiter's window
    expiry: Cancellable | None = None

    def cancel(self) -> None:
        for timer in (self.window, self.expiry):
            if timer is not None:
                timer.cancel()
        self.window = self.expiry = None
        self.held = False


class Drafts:
    """One draft per chat; `request` takes Home Assistant's texts and presses (above)."""

    def __init__(
        self,
        config: Config,
        topics: Topics,
        bus: Publisher,
        accept: Callable[[SendRequest], DeliveryState],
        *,
        monotonic: Callable[[], float],
        call_later: Callable[[float, Callable[[], None]], Cancellable],
        contacts: Iterable[str] = (),
    ) -> None:
        """`accept`: the outbox's, which queues or refuses a request at once and says which;
        `call_later`: the event loop's timers; `contacts`: the companions with a draft, as their
        devices were announced last (ContactDevices.writable)."""
        self._config, self._topics, self._bus, self._accept = config, topics, bus, accept
        self._now, self._call_later = monotonic, call_later
        self._root = topics.draft_subscription.removesuffix("+/+")
        hidden = config.homeassistant.hidden_targets
        self._targets = {t.alias: t for t in config.targets if t.alias not in hidden}
        # a contact target's key, hidden or not, has no chat of its own: a press on a companion
        # pair kept from before it became one would send without the target's prefix, suffix
        # and priority
        self._target_keys = frozenset(t.public_key for t in config.targets if t.public_key)
        self._drafts: dict[Chat, _Draft] = {
            (TARGET, alias): _Draft(text_room(config, target, None))
            for alias, target in self._targets.items()
        }
        for key in sorted(frozenset(contacts) - self._target_keys):
            self._drafts[(CONTACT, key)] = _Draft(self._contact_room())
        self._clears = RateLimit(CLEARS_PER_S, CLEARS_BURST, now=monotonic())
        self._stopped = False

    def chats(self) -> dict[Chat, str]:
        """Every chat with its draft."""
        return {chat: draft.text for chat, draft in self._drafts.items()}

    # --- Home Assistant's requests --------------------------------------------------------------

    def request(self, kind: str, ident: str, payload: bytes, retained: bool) -> None:
        """A request on a chat's request topic: a text to store, or the press of Send."""
        chat = (kind, ident)
        draft = self._drafts.get(chat)
        # a left-over; no chat of this gateway (hidden, gone); after the clean stop's clear
        if retained or draft is None or self._stopped:
            return
        wanted = _parsed(payload)
        if wanted is None or (isinstance(wanted, str) and len(wanted) > draft.max):
            log.debug("draft of %s: request ignored", _named(chat))
            self._echo(chat, draft)  # the field shows the draft again
        elif isinstance(wanted, str):
            if wanted != draft.text:
                draft.text, draft.changed_at = wanted, self._now()
                self._expire_later(chat, draft)
            self._echo(chat, draft)
        else:
            self._press(chat, draft)

    def _press(self, chat: Chat, draft: _Draft) -> None:
        now = self._now()
        repeat = draft.queued_at is not None and now - draft.queued_at < REPEAT_S
        if repeat and not draft.text.strip():  # a double tap's second: nothing to say
            return
        kind, ident = chat
        request = SendRequest(ident, draft.text.encode("utf-8"), False, contact=kind == CONTACT)
        if self._accept(request) is DeliveryState.QUEUED:
            draft.queued_at = now
            self._set(chat, draft, "")  # at once: typing that follows is the next message

    # --- the gateway's side ---------------------------------------------------------------------

    def republish(self) -> None:
        """At a connect to the broker and at Home Assistant's birth: every draft as it is held, at
        once. Never an empty text in place of a held one, which the next press would still
        send."""
        for chat, draft in self._drafts.items():
            self._publish_now(chat, draft)

    def contacts(self, keys: frozenset[str]) -> None:
        """The companions whose device has the draft, after their devices' documents went out: a
        new one's draft is empty, a gone one's topic is deleted (its device is gone already)."""
        current = {ident for kind, ident in self._drafts if kind == CONTACT}
        keys -= self._target_keys
        for key in sorted(current - keys):
            self._drafts.pop((CONTACT, key)).cancel()
            self._bus.publish(self._topic((CONTACT, key)), b"", retain=True)
        for key in sorted(keys - current):
            chat = (CONTACT, key)
            self._drafts[chat] = draft = _Draft(self._contact_room())
            self._publish_now(chat, draft)

    def retained_copy(self, kind: str, ident: str, payload: bytes) -> None:
        """The broker's copy of a draft topic, for the subscription at a connect: one of a chat of
        this run is left to the republish; one of no chat (of an earlier run, or another client's)
        is deleted, at most CLEARS_PER_S a second."""
        chat = (kind, ident)
        if chat in self._drafts or not payload or not self._clears.allow(self._now()):
            return
        log.info("a draft of no chat of this gateway was retained: deleting %s", self._topic(chat))
        self._bus.publish(self._topic(chat), b"", retain=True)

    def announcing(self, radio: RadioInfo | None) -> None:
        """Before the gateway's discovery with `radio`: the targets' `max` as it announces them. A
        draft longer than its new `max` is cut to it and published at once, before the
        discovery: Home Assistant writes the state again as it takes the discovery, and raises
        for one longer than the new `max`; the old one, larger, takes the cut draft."""
        for alias, target in self._targets.items():
            chat = (TARGET, alias)
            draft = self._drafts[chat]
            draft.max = text_room(self._config, target, radio)
            if len(draft.text) > draft.max:
                draft.text = cut_chars(draft.text, draft.max)
                self._publish_now(chat, draft)

    def stop(self) -> None:
        """A clean stop: every draft that is not empty is cleared, so that no unsent text stays
        retained on the broker while the gateway is away; so is one whose echo is held, which
        the broker has with the text before. Requests that come later are ignored: their drafts
        would stay."""
        self._stopped = True
        for chat, draft in self._drafts.items():
            held = draft.held
            draft.cancel()
            if draft.text or held:
                draft.text = ""
                self._publish(chat, "")

    # --- internals ------------------------------------------------------------------------------

    def _contact_room(self) -> int:
        return text_room(self._config, None, None)

    def _topic(self, chat: Chat) -> str:
        kind, ident = chat
        return f"{self._root}{kind}/{ident}"

    def _publish(self, chat: Chat, text: str) -> None:
        self._bus.publish(self._topic(chat), encode({"text": text}), retain=True)

    def _set(self, chat: Chat, draft: _Draft, text: str) -> None:
        """A text the gateway sets, published at once."""
        draft.text, draft.changed_at = text, self._now()
        self._expire_later(chat, draft)
        self._publish_now(chat, draft)

    def _publish_now(self, chat: Chat, draft: _Draft) -> None:
        """At once, without the limiter; an echo it holds is dropped and its window closed."""
        if draft.window is not None:
            draft.window.cancel()
            draft.window = None
        draft.held = False
        self._publish(chat, draft.text)

    def _echo(self, chat: Chat, draft: _Draft) -> None:
        """The leading edge: at once when no window is open, else held for its end."""
        if draft.window is not None:
            draft.held = True
            return
        self._publish(chat, draft.text)
        draft.window = self._call_later(ECHO_WINDOW_S, lambda: self._window_ends(chat, draft))

    def _window_ends(self, chat: Chat, draft: _Draft) -> None:
        draft.window = None
        if draft.held and self._drafts.get(chat) is draft:
            draft.held = False
            self._echo(chat, draft)  # the last text of the burst, and the next window

    def _expire_later(self, chat: Chat, draft: _Draft) -> None:
        if draft.expiry is not None:
            draft.expiry.cancel()
            draft.expiry = None
        if draft.text:
            draft.expiry = self._call_later(EXPIRY_S, lambda: self._expire(chat, draft))

    def _expire(self, chat: Chat, draft: _Draft) -> None:
        draft.expiry = None
        if self._drafts.get(chat) is not draft or not draft.text:
            return
        left = draft.changed_at + EXPIRY_S - self._now()
        if left > 0:  # the loop's clock ran ahead of ours
            draft.expiry = self._call_later(left, lambda: self._expire(chat, draft))
            return
        log.info("draft of %s unchanged for %d minutes: cleared", _named(chat), EXPIRY_S // 60)
        self._set(chat, draft, "")


def _named(chat: Chat) -> str:
    """A chat for the log: a target's alias as written (%r), a contact's key shortened."""
    kind, ident = chat
    return f"{kind} {ident!r}" if kind == TARGET else f"{kind} {ident[:12]}"


def _parsed(payload: bytes) -> str | bool | None:
    """The text of `{"text": …}`, True for `{"send": true}`; None for anything else."""
    if len(payload) > MAX_REQUEST_BYTES:
        return None
    try:
        request = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(request, dict) or len(request) != 1:
        return None
    ((name, value),) = request.items()
    if name == "text" and _unicode(value):
        return value
    if name == "send" and value is True:
        return True
    return None


def _unicode(value: object) -> TypeGuard[str]:
    """A string that UTF-8 can carry: JSON writes a lone surrogate as an escape, in ASCII, and
    no UTF-8 text holds it; held, it would raise at every echo, press, republish and cut."""
    if not isinstance(value, str):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True
