"""Command policy for messages that should trigger automations (ADR 0009).

A received message is a command only if all of these hold:

* it is a plain-text direct message (channel messages never command: anyone with the channel
  secret can write there and the sender name is just text);
* its first word is a configured command word;
* the 6-byte sender prefix resolves to exactly one contact and that contact's full public key is
  allowlisted; the contact table must be read from the radio for each command, because a contact
  with the same prefix may have been added a moment ago;
* the sender timestamp is newer than the last accepted command of that sender (replay) and,
  unless disabled, within `max_age_s` of the bridge clock.

The bridge publishes accepted commands as events; it never executes anything itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from meshcore_bus.core.contacts import ContactBook, normalise_key
from meshcore_bus.core.model import InboundMessage, MessageKind, TextType


class Rejection(StrEnum):
    SENDER_UNKNOWN = "sender_unknown"  # prefix matches no contact
    SENDER_AMBIGUOUS = "sender_ambiguous"  # prefix matches several contacts
    SENDER_NOT_ALLOWED = "sender_not_allowed"
    REPLAY = "replay"  # not newer than the last accepted command of this sender
    TOO_OLD = "too_old"
    FROM_THE_FUTURE = "from_the_future"
    SENDER_UNVERIFIED = "sender_unverified"  # the radio's contact table could not be read


@dataclass(frozen=True, slots=True)
class CommandRules:
    senders: frozenset[str]  # full public keys, lower-case hex
    words: frozenset[str]  # lower-case, single words
    max_age_s: float = 900.0  # 0 disables the age check

    @classmethod
    def create(cls, senders: Iterable[str], words: Iterable[str], max_age_s: float) -> CommandRules:
        normalised_words = set()
        for word in words:
            value = word.strip().casefold()
            if not value or any(ch.isspace() for ch in value):
                raise ValueError(f"command word must be a single word, got {word!r}")
            normalised_words.add(value)
        if max_age_s < 0:
            raise ValueError(f"max_age_s must not be negative, got {max_age_s}")
        return cls(
            frozenset(normalise_key(k) for k in senders), frozenset(normalised_words), max_age_s
        )


@dataclass(frozen=True, slots=True)
class Command:
    word: str
    args: str
    sender_key: str
    sender_timestamp: int


@dataclass(frozen=True, slots=True)
class CommandDecision:
    word: str
    command: Command | None = None
    rejection: Rejection | None = None


def command_word(message: InboundMessage, rules: CommandRules) -> str | None:
    """The command word if the message may be a command: a plain direct message with a sender
    prefix whose first word is configured. Only such messages need the sender checks."""
    if message.kind is not MessageKind.CONTACT or message.text_type is not TextType.PLAIN:
        return None
    if message.sender_prefix is None:
        return None
    word = message.text.strip().partition(" ")[0].casefold()
    return word if word in rules.words else None


def evaluate_command(
    message: InboundMessage,
    rules: CommandRules,
    contacts: ContactBook,
    last_accepted: Mapping[str, int],
    now: float,
) -> CommandDecision | None:
    """None if the message is not a command at all, otherwise acceptance or the reason.

    `contacts` must be the radio's contact table read after the message arrived.
    """
    word = command_word(message, rules)
    if word is None or message.sender_prefix is None:
        return None
    args = message.text.strip().partition(" ")[2]
    sender, rejection = _authorise(message.sender_prefix, rules, contacts)
    if rejection is None and sender is not None:
        rejection = _check_time(message.sender_timestamp, rules, last_accepted.get(sender), now)
    if sender is None or rejection is not None:
        return CommandDecision(word, rejection=rejection)
    return CommandDecision(
        word, command=Command(word, args.strip(), sender, message.sender_timestamp)
    )


def _authorise(
    prefix: str, rules: CommandRules, contacts: ContactBook
) -> tuple[str | None, Rejection | None]:
    candidates = contacts.resolve(prefix)
    if not candidates:
        return None, Rejection.SENDER_UNKNOWN
    if len(candidates) > 1:
        return None, Rejection.SENDER_AMBIGUOUS
    sender = candidates[0].public_key
    if sender not in rules.senders:
        return None, Rejection.SENDER_NOT_ALLOWED
    return sender, None


def _check_time(
    sent: int, rules: CommandRules, last_accepted: int | None, now: float
) -> Rejection | None:
    if rules.max_age_s > 0 and now - sent > rules.max_age_s:
        return Rejection.TOO_OLD
    if rules.max_age_s > 0 and sent - now > rules.max_age_s:
        return Rejection.FROM_THE_FUTURE
    if last_accepted is not None and sent <= last_accepted:
        return Rejection.REPLAY
    return None
