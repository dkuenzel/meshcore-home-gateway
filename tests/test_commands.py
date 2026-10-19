"""Command policy: allowlisted contacts, command words, replay and age checks (ADR 0009)."""

import pytest

from meshcore_bus.core.commands import CommandRules, Rejection, command_word, evaluate_command
from meshcore_bus.core.contacts import Contact, ContactBook
from meshcore_bus.core.model import InboundMessage, MessageKind, TextType

ALICE = "a1" * 32
MALLORY_SAME_PREFIX = "a1" * 6 + "ff" * 26
BOB = "b2" * 32
NOW = 1_800_000_000.0

RULES = CommandRules(
    senders=frozenset({ALICE}), words=frozenset({"status", "arm"}), max_age_s=900.0
)
BOOK = ContactBook([Contact(ALICE, "Alice"), Contact(BOB, "Bob")])


def dm(
    text: str, key: str = ALICE, ts: float = NOW - 5, text_type: TextType = TextType.PLAIN
) -> InboundMessage:
    return InboundMessage(
        kind=MessageKind.CONTACT,
        text=text,
        sender_timestamp=int(ts),
        received_at=NOW,
        text_type=text_type,
        sender_prefix=key[:12],
    )


def test_allowlisted_sender_with_known_word_is_accepted() -> None:
    decision = evaluate_command(dm("ARM now please"), RULES, BOOK, last_accepted={}, now=NOW)
    assert decision is not None
    assert decision.rejection is None
    assert decision.command is not None
    assert (decision.command.word, decision.command.args) == ("arm", "now please")
    assert decision.command.sender_key == ALICE


def test_unknown_word_is_just_a_message() -> None:
    assert evaluate_command(dm("hello there"), RULES, BOOK, {}, NOW) is None


def test_channel_messages_never_command() -> None:
    msg = InboundMessage(
        kind=MessageKind.CHANNEL,
        text="arm",
        sender_timestamp=int(NOW),
        received_at=NOW,
        channel_index=1,
    )
    assert evaluate_command(msg, RULES, BOOK, {}, NOW) is None


def test_non_plain_text_types_never_command() -> None:
    assert evaluate_command(dm("arm", text_type=TextType.CLI_DATA), RULES, BOOK, {}, NOW) is None


@pytest.mark.parametrize(
    ("message", "book", "last", "reason"),
    [
        (dm("arm", key=BOB), BOOK, {}, Rejection.SENDER_NOT_ALLOWED),
        (dm("arm", key="c3" * 32), BOOK, {}, Rejection.SENDER_UNKNOWN),
        (
            dm("arm"),
            ContactBook([Contact(ALICE, "Alice"), Contact(MALLORY_SAME_PREFIX, "Alice")]),
            {},
            Rejection.SENDER_AMBIGUOUS,
        ),
        (dm("arm", ts=NOW - 901), BOOK, {}, Rejection.TOO_OLD),
        (dm("arm", ts=NOW + 901), BOOK, {}, Rejection.FROM_THE_FUTURE),
        (dm("arm", ts=NOW - 5), BOOK, {ALICE: int(NOW - 5)}, Rejection.REPLAY),
        (dm("arm", ts=NOW - 10), BOOK, {ALICE: int(NOW - 5)}, Rejection.REPLAY),
    ],
)
def test_rejections(
    message: InboundMessage, book: ContactBook, last: dict[str, int], reason: Rejection
) -> None:
    decision = evaluate_command(message, RULES, book, last, NOW)
    assert decision is not None
    assert decision.command is None
    assert decision.rejection == reason


def test_prefix_collision_with_a_different_key_is_not_the_allowlisted_sender() -> None:
    """A contact that only shares the 6-byte prefix must not inherit the permission."""
    book = ContactBook([Contact(MALLORY_SAME_PREFIX, "Alice")])
    decision = evaluate_command(dm("arm", key=MALLORY_SAME_PREFIX), RULES, book, {}, NOW)
    assert decision is not None
    assert decision.rejection == Rejection.SENDER_NOT_ALLOWED


def test_a_look_alike_seen_recently_makes_the_sender_ambiguous() -> None:
    """The radio can drop a contact without telling (ADR 0009): a key with the same prefix that
    the bridge saw on the radio recently still counts, though the table no longer holds it."""
    decision = evaluate_command(dm("arm"), RULES, BOOK, {}, NOW, seen=[ALICE, MALLORY_SAME_PREFIX])
    assert decision is not None
    assert decision.rejection == Rejection.SENDER_AMBIGUOUS


def test_a_sender_known_only_from_memory_is_not_accepted() -> None:
    """Remembered keys only add doubt; the sender itself must be in the table just read."""
    decision = evaluate_command(dm("arm"), RULES, ContactBook([]), {}, NOW, seen=[ALICE])
    assert decision is not None
    assert decision.rejection == Rejection.SENDER_UNKNOWN


def test_seen_keys_of_other_prefixes_do_not_matter() -> None:
    decision = evaluate_command(dm("arm"), RULES, BOOK, {}, NOW, seen=[ALICE, BOB, "c3" * 32])
    assert decision is not None
    assert decision.command is not None


def test_newer_timestamp_than_last_accepted_passes() -> None:
    decision = evaluate_command(dm("status", ts=NOW - 1), RULES, BOOK, {ALICE: int(NOW - 5)}, NOW)
    assert decision is not None
    assert decision.command is not None


def test_age_check_can_be_disabled() -> None:
    rules = CommandRules(senders=RULES.senders, words=RULES.words, max_age_s=0.0)
    decision = evaluate_command(dm("arm", ts=NOW - 86_400), rules, BOOK, {}, NOW)
    assert decision is not None
    assert decision.command is not None


def test_rules_normalise_and_validate_keys_and_words() -> None:
    rules = CommandRules.create(senders=[ALICE.upper()], words=["Arm", " STATUS "], max_age_s=60)
    assert rules.senders == frozenset({ALICE})
    assert rules.words == frozenset({"arm", "status"})
    with pytest.raises(ValueError, match="public key"):
        CommandRules.create(senders=["a1b2c3d4e5f6"], words=["arm"], max_age_s=60)
    with pytest.raises(ValueError, match="word"):
        CommandRules.create(senders=[ALICE], words=["two words"], max_age_s=60)


def test_command_word_marks_the_messages_that_need_sender_checks() -> None:
    """The bridge reads the radio's contact table again only for these (ADR 0009)."""
    assert command_word(dm("  Status please"), RULES) == "status"
    assert command_word(dm("statuses"), RULES) is None
    assert command_word(dm("arm", text_type=TextType.SIGNED_PLAIN), RULES) is None
    channel = InboundMessage(
        kind=MessageKind.CHANNEL, text="arm", sender_timestamp=int(NOW), received_at=NOW,
        channel_index=1,
    )  # fmt: skip
    assert command_word(channel, RULES) is None
