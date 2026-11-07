"""Command policy: allowlisted contacts, command words, replay and age checks (ADR 0009)."""

import dataclasses

import pytest

from meshcore_home_gateway.core.commands import (
    CODE_STEP_S,
    CommandDecision,
    CommandRules,
    Rejection,
    code_at,
    command_word,
    evaluate_command,
)
from meshcore_home_gateway.core.contacts import Contact, ContactBook
from meshcore_home_gateway.core.model import InboundMessage, MessageKind, TextType

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


@pytest.mark.parametrize(
    ("reply", "answers"),
    [
        ("none", [None, None, None, None]),
        ("accepted", ["ok: arm", None, None, None]),
        ("all", ["ok: arm", "rejected: replay", None, None]),
    ],
)
def test_a_command_is_answered_only_as_far_as_asked(reply: str, answers: list[str | None]) -> None:
    """EXT-009: "all" also answers the rejection of a sender the checks identified, never that of
    one they did not (not allowed, ambiguous): it would tell a stranger which words exist."""
    rules = dataclasses.replace(RULES, reply=reply)
    look_alike = ContactBook([*BOOK.resolve(ALICE[:12]), Contact(MALLORY_SAME_PREFIX, "M")])
    decisions = [
        evaluate_command(dm("arm now"), rules, BOOK, {}, NOW),
        evaluate_command(dm("arm"), rules, BOOK, {ALICE: int(NOW)}, NOW),
        evaluate_command(dm("arm", key=BOB), rules, BOOK, {}, NOW),
        evaluate_command(dm("arm"), rules, look_alike, {}, NOW),
    ]
    assert [d.rejection if d else "no command" for d in decisions] == [
        None,
        Rejection.REPLAY,
        Rejection.SENDER_NOT_ALLOWED,
        Rejection.SENDER_AMBIGUOUS,
    ]
    assert [rules.reply_text(d) for d in decisions if d] == answers
    assert [d.sender_key for d in decisions if d] == [None, ALICE, None, None]


@pytest.mark.parametrize("rejection", list(Rejection))
def test_only_the_rejections_of_an_identified_sender_are_answered(rejection: Rejection) -> None:
    """The list is closed: a rejection added later is not answered until it is judged safe."""
    rules = dataclasses.replace(RULES, reply="all")
    text = rules.reply_text(CommandDecision("arm", rejection=rejection, sender_key=ALICE))
    safe = {
        "replay",
        "too_old",
        "from_the_future",
        "code_missing",
        "code_invalid",
        "code_reused",
        "state_unwritable",
        "broker_unavailable",
    }
    assert text == (f"rejected: {rejection}" if rejection in safe else None)


def test_rules_take_only_the_known_ways_to_answer() -> None:
    assert CommandRules.create([ALICE], ["arm"], 60.0).reply == "none"
    assert CommandRules.create([ALICE], ["arm"], 60.0, reply="all").reply == "all"


def test_any_whitespace_ends_the_command_word() -> None:
    """A phone may put a line break or a tab after the word; it is still the first word."""
    for text in ("arm\nnow please", "arm\tnow please", "arm\u00a0 now please  "):
        decision = evaluate_command(dm(text), RULES, BOOK, last_accepted={}, now=NOW)
        assert decision is not None
        assert decision.command is not None
        assert (decision.command.word, decision.command.args) == ("arm", "now please")
    decision = evaluate_command(dm("status"), RULES, BOOK, last_accepted={}, now=NOW)
    assert decision is not None
    assert decision.command is not None
    assert decision.command.args == ""


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
        (dm("arm", ts=NOW - 1000), BOOK, {ALICE: int(NOW)}, Rejection.TOO_OLD),  # checked first
    ],
)
def test_rejections(
    message: InboundMessage, book: ContactBook, last: dict[str, int], reason: Rejection
) -> None:
    decision = evaluate_command(message, RULES, book, last, NOW)
    assert decision is not None
    assert decision.command is None
    assert decision.rejection == reason


@pytest.mark.parametrize("ts", [NOW - 900, NOW + 900])
def test_the_age_window_includes_its_edges(ts: float) -> None:
    """max_age_s = 900: a timestamp exactly 900 s behind or ahead of the clock is accepted."""
    decision = evaluate_command(dm("arm", ts=ts), RULES, BOOK, last_accepted={}, now=NOW)
    assert decision is not None
    assert decision.command is not None


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
    assert command_word(dm("status\nplease"), RULES) == "status"
    assert command_word(dm(" \n "), RULES) is None
    assert command_word(dm("arm", text_type=TextType.SIGNED_PLAIN), RULES) is None
    channel = InboundMessage(
        kind=MessageKind.CHANNEL,
        text="arm",
        sender_timestamp=int(NOW),
        received_at=NOW,
        channel_index=1,
    )
    assert command_word(channel, RULES) is None


def test_far_future_timestamps_are_refused_even_without_the_age_check() -> None:
    """With max_age_s = 0 one command dated years ahead would be stored and turn every later
    command of that sender into a replay."""
    rules = CommandRules(senders=frozenset({ALICE}), words=frozenset({"arm"}), max_age_s=0)
    far = evaluate_command(dm("arm", ts=NOW + 2 * 86_400), rules, BOOK, {}, NOW)
    assert far is not None
    assert far.rejection is Rejection.FROM_THE_FUTURE
    near = evaluate_command(dm("arm", ts=NOW + 3_600), rules, BOOK, {}, NOW)  # a skewed clock
    assert near is not None
    assert near.command is not None
    edge = evaluate_command(dm("arm", ts=NOW + 86_400), rules, BOOK, {}, NOW)  # exactly a day
    assert edge is not None
    assert edge.command is not None


# --- codes (IMP-002) ---------------------------------------------------------------------------

SECRET = b"12345678901234567890"  # RFC 6238's test secret
CAROL = "c3" * 32
CODED = dataclasses.replace(RULES, senders=frozenset({ALICE, CAROL}), secrets=((ALICE, SECRET),))
CODED_BOOK = ContactBook([Contact(ALICE, "Alice"), Contact(BOB, "Bob"), Contact(CAROL, "Carol")])
STEP = int(NOW // CODE_STEP_S)


def code(at: float = NOW, secret: bytes = SECRET) -> str:
    return code_at(secret, int(at // CODE_STEP_S))


def coded(
    text: str, *, now: float = NOW, last: dict[str, int] | None = None, key: str = ALICE
) -> CommandDecision | None:
    return evaluate_command(dm(text, key=key), CODED, CODED_BOOK, {}, now, last_codes=last)


@pytest.mark.parametrize(
    ("unix_time", "expected"),
    [
        (59, "287082"),
        (1_111_111_109, "081804"),
        (1_111_111_111, "050471"),
        (1_234_567_890, "005924"),
        (2_000_000_000, "279037"),
        (20_000_000_000, "353130"),
    ],
)
def test_codes_are_rfc_6238s(unix_time: int, expected: str) -> None:
    """RFC 6238's test values for SHA-1, in the 6 digits authenticator apps show."""
    assert code_at(SECRET, unix_time // CODE_STEP_S) == expected


def test_a_sender_with_a_secret_ends_its_commands_with_the_code() -> None:
    decision = coded(f"ARM now please {code()}")
    assert decision is not None
    assert decision.command is not None
    assert (decision.command.args, decision.command.code_step) == ("now please", STEP)
    alone = coded(f"arm\n{code()}")
    assert alone is not None
    assert alone.command is not None
    assert alone.command.args == ""


def test_a_code_does_not_bind_the_text() -> None:
    """#105, design §7.4: a code shows that the sender's app made it in that time window, not
    which text it came with. Whoever reads `arm now <code>` before the gateway, such as a host
    posing as the radio over TCP, can send `status <code>` instead, once."""
    seen = code()
    taken = coded(f"status {seen}")
    assert taken is not None
    assert taken.command is not None
    assert (taken.command.word, taken.command.code_step) == ("status", STEP)
    again = coded(f"arm now {seen}", last={ALICE: STEP})  # the sender's own comes too late
    assert again is not None
    assert again.rejection is Rejection.CODE_REUSED


@pytest.mark.parametrize(("steps", "accepted"), [(-1, True), (1, True), (-2, False), (2, False)])
def test_a_code_may_be_a_time_step_off_and_no_more(steps: int, accepted: bool) -> None:
    """The sender's phone and the host need not agree to the second (RFC 6238, section 5.2)."""
    decision = coded(f"arm {code(NOW + steps * CODE_STEP_S)}")
    assert decision is not None
    assert decision.rejection is (None if accepted else Rejection.CODE_INVALID)


@pytest.mark.parametrize(
    "text",
    [
        "arm now",
        "arm",
        "arm now 12345",
        "arm now 1234567",
        "arm now \u0661\u0662\u0663\u0664\u0665\u0666",
    ],
)
def test_a_command_without_a_code_is_refused(text: str) -> None:
    decision = coded(text)
    assert decision is not None
    assert decision.rejection is Rejection.CODE_MISSING


def test_a_wrong_code_is_refused() -> None:
    right = code()
    wrong = f"{(int(right[0]) + 1) % 10}{right[1:]}"
    for text in (f"arm {wrong}", f"arm {code(secret=b'another secret of 20b')}"):
        decision = coded(text)
        assert decision is not None
        assert decision.rejection is Rejection.CODE_INVALID


def test_a_code_serves_once() -> None:
    """Whoever saw a code (a host that took the radio's TCP link) cannot use it again."""
    for last, rejection in ((STEP, Rejection.CODE_REUSED), (STEP + 1, Rejection.CODE_REUSED)):
        decision = coded(f"arm {code()}", last={ALICE: last})
        assert decision is not None
        assert decision.rejection is rejection
    later = coded(f"arm {code()}", last={ALICE: STEP - 1, CAROL: STEP})
    assert later is not None
    assert later.command is not None


def test_senders_without_a_secret_keep_the_rules() -> None:
    decision = coded(f"arm now {code()}", key=CAROL)
    assert decision is not None
    assert decision.command is not None
    assert (decision.command.args, decision.command.code_step) == (f"now {code()}", None)


def test_the_other_checks_come_before_the_code() -> None:
    replayed = evaluate_command(
        dm(f"arm {code()}"), CODED, CODED_BOOK, {ALICE: int(NOW)}, NOW, last_codes={}
    )
    assert replayed is not None
    assert replayed.rejection is Rejection.REPLAY
    stranger = coded(f"arm {code()}", key=BOB)
    assert stranger is not None
    assert stranger.rejection is Rejection.SENDER_NOT_ALLOWED


def test_the_rules_never_show_a_secret() -> None:
    assert "1234567890" not in repr(CODED)
    assert CODED.secret(ALICE) == SECRET
    assert CODED.secret(CAROL) is None
