"""Command policy for messages that should trigger automations (ADR 0009).

A received message is a command only if all of these hold:

* it is a plain-text direct message (channel messages never command: anyone with the channel
  secret can write there and the sender name is just text);
* its first word is a configured command word;
* the 6-byte sender prefix resolves to exactly one contact and that contact's full public key is
  allowlisted. The contact must be in the radio's contact table as read after the message
  arrived, and no other key with that prefix may have been a contact of the radio recently: a
  look-alike can enter the table and leave it again, sometimes without telling;
* the sender timestamp is newer than the last accepted command of that sender (replay) and,
  unless disabled, within `max_age_s` of the bridge clock;
* if the sender has a code secret (IMP-002), its last word is the sender's 6-digit code (RFC 6238)
  for the bridge clock, within a time step either way, of a step newer than its last accepted
  code's: the code authenticates the text whatever reached the bridge, and serves once. A sender
  with CODE_FAILURE_LIMIT wrong codes within CODE_FAILURE_WINDOW_S has its codes refused
  unchecked (RFC 4226, section 7.3): each guess wins with 3 in 10^6, so guesses must be few.

The bridge publishes accepted commands as events; it never executes anything itself.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from meshcore_home_gateway.core.contacts import ContactBook, normalise_key
from meshcore_home_gateway.core.model import InboundMessage, MessageKind, TextType
from meshcore_home_gateway.texts import TextError


class Rejection(StrEnum):
    SENDER_UNKNOWN = "sender_unknown"  # prefix matches no contact
    SENDER_AMBIGUOUS = "sender_ambiguous"  # prefix matches several contacts or recent keys
    SENDER_NOT_ALLOWED = "sender_not_allowed"
    REPLAY = "replay"  # not newer than the last accepted command of this sender
    TOO_OLD = "too_old"
    FROM_THE_FUTURE = "from_the_future"
    SENDER_UNVERIFIED = "sender_unverified"  # the bridge cannot rule out a look-alike sender
    STATE_UNWRITABLE = "state_unwritable"  # the replay ledger could not be saved
    BROKER_UNAVAILABLE = "broker_unavailable"  # the event could only reach automations late
    CODE_MISSING = "code_missing"  # the sender has a code secret; the last word is no code
    CODE_INVALID = "code_invalid"  # not the sender's code within a time step of the bridge clock
    CODE_REUSED = "code_reused"  # the code of a time step this sender already used
    CODE_LOCKED = "code_locked"  # too many wrong codes of this sender of late: none is checked


CODE_DIGITS = 6
CODE_STEP_S = 30  # RFC 6238's time step, as authenticator apps use it
CODE_SKEW_STEPS = 1  # the sender's clock may be a step off the bridge's, either way
MIN_SECRET_BYTES = 16  # RFC 4226: a secret has at least 128 bits
# RFC 4226 section 7.3: a sender's wrong codes within the window; at the limit its codes are
# refused unchecked until the oldest of them leaves the window (#79)
CODE_FAILURE_LIMIT = 5
CODE_FAILURE_WINDOW_S = 3600.0
REPLIES = ("none", "accepted", "all")  # `[commands] reply` (EXT-009)
# rejections of a sender the checks identified: telling it the reason reveals nothing new
REPLIED_REJECTIONS = frozenset(
    {
        Rejection.REPLAY,
        Rejection.TOO_OLD,
        Rejection.FROM_THE_FUTURE,
        Rejection.CODE_MISSING,
        Rejection.CODE_INVALID,
        Rejection.CODE_REUSED,
        Rejection.CODE_LOCKED,
        Rejection.STATE_UNWRITABLE,
        Rejection.BROKER_UNAVAILABLE,
    }
)
# what counts as a wrong code: a guess, or a command of a sender with a secret that lacks one
WRONG_CODES = frozenset({Rejection.CODE_MISSING, Rejection.CODE_INVALID})


@dataclass(frozen=True, slots=True)
class CommandRules:
    senders: frozenset[str]  # full public keys, lower-case hex
    words: frozenset[str]  # lower-case, single words
    max_age_s: float = 900.0  # 0 disables the age check
    codes_file: str | None = None  # the senders' code secrets, which load_config reads
    secrets: tuple[tuple[str, bytes], ...] = field(default=(), repr=False)  # key, secret
    reply: str = "none"  # REPLIES: answer accepted commands, and also safe rejections
    # keys with a secret in codes_file that are not senders: ignored, and the start says so (#80)
    ignored_secrets: tuple[str, ...] = ()

    @classmethod
    def create(
        cls,
        senders: Iterable[str],
        words: Iterable[str],
        max_age_s: float,
        codes_file: str | None = None,
        reply: str = "none",
    ) -> CommandRules:
        normalised_words = set()
        for word in words:
            value = word.strip().casefold()
            if not value or any(ch.isspace() for ch in value):
                raise TextError("command word must be a single word, got {word!r}", word=word)
            normalised_words.add(value)
        if max_age_s < 0:
            raise TextError("max_age_s must not be negative, got {value}", value=max_age_s)
        return cls(
            frozenset(normalise_key(k) for k in senders),
            frozenset(normalised_words),
            max_age_s,
            codes_file,
            reply=reply,
        )

    def reply_text(self, decision: CommandDecision) -> str | None:
        """The answer to a sender the checks identified: `ok: <word>`, `rejected: <reason>`
        (machine words, in English as the events), or None when none is due."""
        if decision.command is not None and self.reply != "none":
            return f"ok: {decision.word}"
        if self.reply == "all" and decision.rejection in REPLIED_REJECTIONS:
            return f"rejected: {decision.rejection}"
        return None

    def secret(self, sender: str) -> bytes | None:
        """The sender's code secret; None for a sender without one."""
        return next((secret for key, secret in self.secrets if key == sender), None)


@dataclass(frozen=True, slots=True)
class Command:
    word: str
    args: str  # without the code
    sender_key: str
    sender_timestamp: int
    code_step: int | None = None  # the time step of its code, for a sender with a secret


@dataclass(frozen=True, slots=True)
class CommandDecision:
    word: str
    command: Command | None = None
    rejection: Rejection | None = None
    sender_key: str | None = None  # of a rejection after the sender checks passed


def command_word(message: InboundMessage, rules: CommandRules) -> str | None:
    """The command word if the message may be a command: a plain direct message with a sender
    prefix whose first word is configured. Only such messages need the sender checks."""
    if message.kind is not MessageKind.CONTACT or message.text_type is not TextType.PLAIN:
        return None
    if message.sender_prefix is None:
        return None
    word = _split(message.text)[0].casefold()
    return word if word in rules.words else None


def evaluate_command(
    message: InboundMessage,
    rules: CommandRules,
    contacts: ContactBook,
    last_accepted: Mapping[str, int],
    now: float,
    *,
    seen: Iterable[str] = (),
    last_codes: Mapping[str, int] | None = None,
    code_failures: Mapping[str, Iterable[float]] | None = None,
) -> CommandDecision | None:
    """None if the message is not a command at all, otherwise acceptance or the reason.

    For an acceptance, `contacts` must be the radio's contact table as read after the message
    arrived; an older table can only confirm a rejection. `seen` are full keys the radio
    reported recently (table reads, pushes); they only add doubt, never a sender (ADR 0009).
    `last_codes` are the time steps of the senders' last accepted codes, `code_failures` the
    times of their recent wrong codes (WRONG_CODES), on the clock of `now`.
    """
    word = command_word(message, rules)
    if word is None or message.sender_prefix is None:
        return None
    args = _split(message.text)[1]
    sender, rejection = _authorise(message.sender_prefix, rules, contacts, seen)
    if rejection is None and sender is not None:
        rejection = _check_time(message.sender_timestamp, rules, last_accepted.get(sender), now)
    step = None
    if rejection is None and sender is not None and (secret := rules.secret(sender)) is not None:
        if codes_locked((code_failures or {}).get(sender, ()), now):
            rejection = Rejection.CODE_LOCKED
        else:
            last_step = (last_codes or {}).get(sender)
            args, step, rejection = _check_code(args, secret, last_step, now)
    if sender is None or rejection is not None:
        return CommandDecision(word, rejection=rejection, sender_key=sender)
    command = Command(word, args, sender, message.sender_timestamp, step)
    return CommandDecision(word, command=command)


def _split(text: str) -> tuple[str, str]:
    """The first word and the rest; words are separated by any whitespace (space, tab, line)."""
    parts = text.split(maxsplit=1)
    return (parts[0] if parts else ""), (parts[1].strip() if len(parts) > 1 else "")


def _authorise(
    prefix: str, rules: CommandRules, contacts: ContactBook, seen: Iterable[str]
) -> tuple[str | None, Rejection | None]:
    candidates = contacts.resolve(prefix)
    prefix = prefix.lower()
    keys = {c.public_key for c in candidates} | {k for k in seen if k.startswith(prefix)}
    if len(keys) > 1:
        return None, Rejection.SENDER_AMBIGUOUS
    if not candidates:
        return None, Rejection.SENDER_UNKNOWN
    sender = candidates[0].public_key
    if sender not in rules.senders:
        return None, Rejection.SENDER_NOT_ALLOWED
    return sender, None


# Even without the age check: one accepted command dated far ahead would make every later command
# of that sender a replay, until its clock caught up.
MAX_AHEAD_S = 86_400.0


def _check_time(
    sent: int, rules: CommandRules, last_accepted: int | None, now: float
) -> Rejection | None:
    if rules.max_age_s > 0 and now - sent > rules.max_age_s:
        return Rejection.TOO_OLD
    if sent - now > (rules.max_age_s if rules.max_age_s > 0 else MAX_AHEAD_S):
        return Rejection.FROM_THE_FUTURE
    if last_accepted is not None and sent <= last_accepted:
        return Rejection.REPLAY
    return None


def recent_code_failures(times: Iterable[float], now: float) -> list[float]:
    """The times of wrong codes within CODE_FAILURE_WINDOW_S of `now`, oldest first. A time
    ahead of `now` counts as well (the clock went back), as far as the window reaches."""
    return sorted(t for t in times if abs(now - t) < CODE_FAILURE_WINDOW_S)


def codes_locked(times: Iterable[float], now: float) -> bool:
    """Whether a sender's codes are refused unchecked: CODE_FAILURE_LIMIT wrong codes within
    the window. It ends when the oldest of them leaves it."""
    return len(recent_code_failures(times, now)) >= CODE_FAILURE_LIMIT


def code_at(secret: bytes, step: int) -> str:
    """The code of a time step: RFC 6238's TOTP with SHA-1 and 6 digits, the default of
    authenticator apps (RFC 4226's HOTP of the step)."""
    digest = hmac.new(secret, step.to_bytes(8, "big"), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = int.from_bytes(digest[offset : offset + 4], "big") & 0x7FFF_FFFF
    return f"{number % 10**CODE_DIGITS:0{CODE_DIGITS}d}"


def _check_code(
    args: str, secret: bytes, last_step: int | None, now: float
) -> tuple[str, int | None, Rejection | None]:
    """The arguments without the code, the code's time step, or why the code is refused."""
    parts = args.rsplit(maxsplit=1)
    code = parts[-1] if parts else ""
    if len(code) != CODE_DIGITS or not (code.isascii() and code.isdigit()):
        return args, None, Rejection.CODE_MISSING
    rest = parts[0] if len(parts) == 2 else ""
    current = int(now // CODE_STEP_S)
    steps = [
        step
        for step in range(current - CODE_SKEW_STEPS, current + CODE_SKEW_STEPS + 1)
        if hmac.compare_digest(code_at(secret, step), code)
    ]
    fresh = [step for step in steps if last_step is None or step > last_step]
    if not steps:
        return rest, None, Rejection.CODE_INVALID
    if not fresh:
        return rest, None, Rejection.CODE_REUSED
    return rest, fresh[0], None
