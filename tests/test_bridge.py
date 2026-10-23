"""Bridge service end to end with the emulated radio and an in-memory bus."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway.bridge import Bridge
from meshcore_home_gateway.config import Config, parse_config
from meshcore_home_gateway.mqtt.client import SendRequest
from meshcore_home_gateway.protocol.codes import AutoAdd, Command, Push
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.session import CompanionSession, RadioEvent
from meshcore_home_gateway.radio.transport import Transport
from meshcore_home_gateway.state import AnnouncedComponents, CommandLedger, JsonStateFile
from tests.radio_emulator import SELF_KEY, START_TIME, EmulatedContact, EmulatedRadio

NOW = START_TIME + 100
ALICE = EmulatedContact(bytes.fromhex("a1" * 32), "Alice")
BOB = EmulatedContact(bytes.fromhex("b2" * 32), "Bob", out_path_len=0x02)
TWIN = EmulatedContact(ALICE.public_key[:6] + bytes(range(26)), "Mallory")  # same prefix


def make_config(
    extra: str = "",
    messages: str = "",
    airtime: str = "",
    radio: str = 'transport = "tcp"\nhost = "192.0.2.10"',
) -> Config:
    return parse_config(
        f"""
[radio]
{radio}
[mqtt]
host = "broker.example.org"
node_id = "radio1"
[targets.alerts]
channel = 0
[targets.alice]
contact = "{ALICE.public_key.hex()}"
[targets.bob]
contact = "{BOB.public_key.hex()}"
[commands]
senders = ["{ALICE.public_key.hex()}"]
words = ["status", "arm"]
[messages]
dedup_window_s = 300
{messages}
[airtime]
{airtime}
{extra}
"""
    )


class FakeBus:
    def __init__(self) -> None:
        self.requests: asyncio.Queue[SendRequest] = asyncio.Queue()
        self.on_connected: list[Callable[[], None]] = []
        self.on_ha_online: list[Callable[[], None]] = []
        self.published: list[tuple[str, bytes, bool]] = []
        self.stopped = False
        self.connected = True

    async def start(self) -> None:
        for callback in self.on_connected:
            callback()

    async def stop(self) -> None:
        self.stopped = True

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))

    def send(self, alias: str, text: str | bytes, retained: bool = False) -> None:
        payload = text.encode() if isinstance(text, str) else text
        self.requests.put_nowait(SendRequest(alias, payload, retained))

    def payloads(self, suffix: str) -> list[dict[str, Any]]:
        return [json.loads(p) for t, p, _ in self.published if t.endswith(suffix)]

    async def wait_for(
        self, suffix: str, check: Callable[[dict[str, Any]], bool], within_s: float = 3,
        since: int = 0,
    ) -> dict[str, Any]:  # fmt: skip
        """First payload on the topic (from index `since`) that passes `check`."""

        async def poll() -> dict[str, Any]:
            while True:
                for payload in self.payloads(suffix)[since:]:
                    if check(payload):
                        return payload
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), within_s)


def fast_sessions(
    transport: Transport, events: asyncio.Queue[RadioEvent], set_clock: bool
) -> CompanionSession:
    return CompanionSession(
        transport, events, wall_clock=lambda: NOW, command_timeout_s=0.5, flush_settle_s=0.01,
        set_clock=set_clock,
    )  # fmt: skip


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MIN_S", 0.05)
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MAX_S", 0.15)
    monkeypatch.setattr(bridge_module, "RECONNECT_MIN_S", 0.02)
    monkeypatch.setattr(bridge_module, "DISCOVERY_DELAY_S", (0.0, 0.0))
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.05)


@asynccontextmanager
async def running(
    config: Config,
    radio: EmulatedRadio,
    tmp_path: Path,
    transport: Callable[[], Transport] | None = None,
    *,
    sessions: Callable[..., CompanionSession] = fast_sessions,
    bus: FakeBus | None = None,
) -> AsyncIterator[tuple[Bridge, FakeBus, list[str]]]:
    bus = bus or FakeBus()
    stop, notes = asyncio.Event(), []

    def note(message: str) -> bool:
        notes.append(message)
        return True

    store = JsonStateFile(tmp_path / "state.json")
    bridge = Bridge(
        config, bus, transport or (lambda: radio), CommandLedger(store), session_factory=sessions,
        wall_clock=lambda: NOW, notifier=note, components=AnnouncedComponents(store),
    )  # fmt: skip
    task = asyncio.create_task(bridge.run(stop))
    try:
        if transport is None:
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
        yield bridge, bus, notes
    finally:
        stop.set()
        await asyncio.wait_for(task, 5)


def scenario(coro: Callable[[], Any]) -> None:
    asyncio.run(asyncio.wait_for(coro(), 20))


def test_channel_message_is_queued_and_sent(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alerts", "Gate open")
            sent = await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            assert sent["target"] == "alerts"
            assert sent["airtime_ms"] > 300
            assert radio.channel_texts == [(0, "Gate open", NOW)]
            states = [d["event_type"] for d in bus.payloads("/delivery")]
            assert states == ["queued", "sent"]

    scenario(main)


def test_direct_message_is_confirmed(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], ack_round_trip_ms=2300)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alice", "Hello Alice")
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["attempt"], done["rtt_ms"]) == (1, 2300)
            assert [t.text for t in radio.texts] == ["Hello Alice"]

    scenario(main)


def test_unacknowledged_message_is_retried_then_flooded_then_failed(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Are you there?")
            failed = await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed")
            assert failed["reason"] == "no_acknowledgement"
            assert [t.attempt for t in radio.texts] == [0, 1, 2]
            assert len({t.timestamp for t in radio.texts}) == 1  # same timestamp on every attempt
            assert radio.path_resets == [BOB.public_key]  # before the third attempt only
            reset_at = radio.commands().index(Command.RESET_PATH)
            sends = [i for i, c in enumerate(radio.commands()) if c == Command.SEND_TXT_MSG]
            assert sends[1] < reset_at < sends[2]

    scenario(main)


def test_late_acknowledgement_of_an_earlier_attempt_counts(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alice", "late")
            await bus.wait_for(
                "/delivery", lambda d: d["event_type"] == "sent" and d["attempt"] == 2
            )
            from meshcore_home_gateway.protocol.codec import expected_ack
            from tests.radio_emulator import SELF_KEY

            radio.acknowledge(expected_ack(NOW, 0, "late", SELF_KEY), 9000)
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["attempt"], done["rtt_ms"]) == (2, 9000)

    scenario(main)


def test_acknowledgement_arriving_before_a_retry_ends_the_delivery(tmp_path: Path) -> None:
    """The acknowledgement of attempt 1 arrives while attempt 2 waits for airtime: nothing is
    sent again and the stored path is kept."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            bus.send("alice", "early")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bridge._budget._tokens, bridge._budget._rate = 0.0, 2.0  # attempt 2 waits ~0.3 s
            await asyncio.sleep(0.25)
            from meshcore_home_gateway.protocol.codec import expected_ack
            from tests.radio_emulator import SELF_KEY

            radio.acknowledge(expected_ack(NOW, 0, "early", SELF_KEY), 700)
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["attempt"], done["rtt_ms"]) == (1, 700)
            assert len(radio.texts) == 1
            assert not radio.path_resets

    scenario(main)


def test_command_is_refused_while_the_broker_is_unreachable(tmp_path: Path) -> None:
    """Queued until the reconnect, an accepted command would reach the automations at any later
    time; it is refused instead, and the sender can send it again."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.connected = False
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 10)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"] == {
                "word": "arm", "accepted": False, "reason": "broker_unavailable",
            }  # fmt: skip
            bus.connected = True
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 10)  # the retry
            await bus.wait_for("/command", lambda c: c["event_type"] == "arm")

    scenario(main)


@pytest.mark.parametrize(
    ("alias", "payload", "retained", "reason"),
    [
        ("nobody", "x", False, "unknown_target"),
        ("alerts", "x", True, "retained"),
        ("alerts", b"\xff\xfe", False, "invalid_utf8"),
        ("alerts", "  \x00 ", False, "empty"),
        ("alerts", "x" * 2000, False, "too_large"),
    ],
)
def test_bad_requests_are_rejected_with_a_reason(
    tmp_path: Path, alias: str, payload: str | bytes, retained: bool, reason: str
) -> None:
    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            bus.send(alias, payload, retained)
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] == "rejected")
            assert event["reason"] == reason

    scenario(main)


def test_too_long_text_is_truncated_or_rejected(tmp_path: Path) -> None:
    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE])
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alice", "y" * 200)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert radio.texts[0].text == "y" * 157 + "..."
        strict = make_config(messages="truncate = false")
        async with running(strict, EmulatedRadio(), tmp_path) as (_, bus, _notes):
            bus.send("alerts", "z" * 200)
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] == "rejected")
            assert event["reason"] == "too_long"

    scenario(main)


def test_channel_limit_accounts_for_the_radio_name(tmp_path: Path) -> None:
    radio = EmulatedRadio(name="Gateway")  # 7 bytes: channel texts get 160 - 9 = 151 bytes

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alerts", "c" * 300)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            assert len(radio.channel_texts[0][1]) == 151

    scenario(main)


def test_duplicates_are_suppressed(tmp_path: Path) -> None:
    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            bus.send("alerts", "Smoke")
            bus.send("alerts", "Smoke")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "duplicate")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")

    scenario(main)


def test_full_queue_rejects_and_waiting_messages_expire(tmp_path: Path) -> None:
    class Unreachable:
        description = "unreachable"
        needs_flush = False

        async def open(self) -> None:
            raise FileNotFoundError("no such device")

        async def read(self) -> bytes:
            raise ConnectionError

        async def write(self, data: bytes) -> None:
            raise ConnectionError

        async def close(self) -> None:
            return None

    config = make_config(messages="queue_size = 1\nqueue_ttl_s = 1")

    async def main() -> None:
        async with running(config, EmulatedRadio(), tmp_path, transport=Unreachable) as (_, bus, _):
            bus.send("alerts", "first")
            bus.send("alerts", "second")
            await bus.wait_for("/delivery", lambda d: d["reason"] == "queue_full")
            expired = await bus.wait_for("/delivery", lambda d: d["event_type"] == "expired")
            assert (expired["text"], expired["reason"]) == ("first", "no_radio")
            state = await bus.wait_for("/state", lambda s: True)
            assert state["radio"] == "disconnected"

    scenario(main)


def test_airtime_budget_makes_messages_expire(tmp_path: Path) -> None:
    config = make_config(
        messages="queue_ttl_s = 2", airtime="budget_s_per_hour = 0.001\nburst_s = 5"
    )

    async def main() -> None:
        async with running(config, EmulatedRadio(), tmp_path) as (bridge, bus, _notes):
            for i in range(20):  # each channel message here costs about 0.5 s of airtime
                bus.send("alerts", f"message {i}")
            expired = await bus.wait_for("/delivery", lambda d: d["event_type"] == "expired")
            assert expired["reason"] == "airtime_budget"
            assert bridge.state_payload()["airtime_1h_s"] <= 5.0

    scenario(main)


FINAL = {"confirmed", "failed", "expired", "rejected", "duplicate"}


def test_request_rejected_for_a_full_queue_is_no_duplicate_later(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)

    async def main() -> None:
        async with running(make_config(messages="queue_size = 1"), radio, tmp_path) as (_, bus, _):
            bus.send("alice", "busy")  # the outbox now waits for acknowledgements
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bus.send("alerts", "one")
            bus.send("alerts", "two")
            await bus.wait_for("/delivery", lambda d: d["reason"] == "queue_full")
            await bus.wait_for(
                "/delivery", lambda d: (d["text"], d["event_type"]) == ("one", "sent")
            )
            count = len(bus.payloads("/delivery"))
            bus.send("alerts", "two")  # the automation tries again
            again = await bus.wait_for("/delivery", lambda d: d["text"] == "two", since=count)
            assert again["event_type"] == "queued"

    scenario(main)


def test_interrupted_send_is_repeated_after_reconnecting(tmp_path: Path) -> None:
    radio = EmulatedRadio(drop_on={Command.SEND_CHANNEL_TXT_MSG})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alerts", "Smoke")
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL | {"sent"})
            assert (event["event_type"], event["attempt"]) == ("sent", 1)
            assert radio.commands().count(Command.APP_START) == 2
            assert [text for _, text, _ in radio.channel_texts] == ["Smoke"]

    scenario(main)


def test_running_out_of_time_after_sending_is_a_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`expired` means never sent; a sent but unconfirmed message `failed`. A message that waited
    too long behind another one, with the radio connected, expired because of the queue."""
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MIN_S", 0.6)
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MAX_S", 0.6)
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)

    async def main() -> None:
        async with running(make_config(messages="queue_ttl_s = 1"), radio, tmp_path) as (_, bus, _):
            bus.send("alice", "busy")
            bus.send("alerts", "late")
            busy = await bus.wait_for(
                "/delivery", lambda d: d["text"] == "busy" and d["event_type"] in FINAL, 5
            )
            assert (busy["event_type"], busy["reason"], busy["attempt"]) == (
                "failed", "no_acknowledgement", 2,
            )  # fmt: skip
            late = await bus.wait_for(
                "/delivery", lambda d: d["text"] == "late" and d["event_type"] in FINAL, 5
            )
            assert (late["event_type"], late["reason"]) == ("expired", "queue_ttl")

    scenario(main)


def test_attempts_after_the_path_reset_are_budgeted_as_floods(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
    config = make_config(messages="max_attempts = 4\nflood_after = 2")

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Are you there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed")
            sent = [d for d in bus.payloads("/delivery") if d["event_type"] == "sent"]
            airtime = [d["airtime_ms"] for d in sent]
            assert len(airtime) == 4
            assert airtime[0] == airtime[1] < airtime[2] == airtime[3]

    scenario(main)


def test_message_that_can_never_fit_the_burst_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    radio = EmulatedRadio(contacts=[ALICE], spreading_factor=12)  # about 20 s for a long message

    async def main() -> None:
        async with running(make_config(airtime="burst_s = 5"), radio, tmp_path) as (_, bus, _):
            bus.send("alice", "x" * 150)
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL)
            assert (event["event_type"], event["reason"]) == ("failed", "exceeds_airtime_burst")

    scenario(main)
    assert "burst_s" in caplog.text  # warned when the radio connected


def test_burst_is_checked_before_the_path_is_reset(tmp_path: Path) -> None:
    """At SF 11 a 20-byte text to Bob takes 3.3 s direct and 6.1 s flooded: the flood attempt can
    never fit a 5 s burst, so the stored path must stay."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], spreading_factor=11, auto_ack=False)
    config = make_config(messages="max_attempts = 2\nflood_after = 1", airtime="burst_s = 5")

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "x" * 20)
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL)
            assert (event["event_type"], event["reason"], event["attempt"]) == (
                "failed", "exceeds_airtime_burst", 1,
            )  # fmt: skip
            assert radio.path_resets == []

    scenario(main)


def test_targets_missing_on_the_radio_fail(tmp_path: Path) -> None:
    twin = EmulatedContact(ALICE.public_key[:6] + bytes(26), "Twin")
    radio = EmulatedRadio(contacts=[ALICE, twin], channels={})  # no channel configured

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alerts", "x")
            bus.send("alice", "y")
            bus.send("bob", "z")
            reasons = set()
            for _ in range(3):
                event = await bus.wait_for(
                    "/delivery",
                    lambda d: d["event_type"] == "failed" and d["reason"] not in reasons,
                )
                reasons.add(event["reason"])
            assert reasons == {"channel_not_configured", "ambiguous_target", "unknown_contact"}
            assert radio.texts == []
            assert radio.channel_texts == []

    scenario(main)


def test_received_messages_and_commands(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "ARM now", NOW - 10)
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["event_type"], command["args"], command["sender_name"]) == (
                "arm",
                "now",
                "Alice",
            )
            received = bus.payloads("/received")[0]
            assert received["target"] == "alice"
            assert received["command"] == {"word": "arm", "accepted": True, "reason": None}

            radio.queue_direct_message(ALICE.public_key[:6], "ARM now", NOW - 10)  # sender retry
            radio.queue_direct_message(ALICE.public_key[:6], "arm again", NOW - 20)  # older: replay
            radio.queue_direct_message(BOB.public_key[:6], "status", NOW - 5)  # not allowlisted
            radio.queue_channel_message(0, "Carol: arm", NOW - 1)  # channels never command
            await bus.wait_for("/received", lambda r: r["event_type"] == "channel")
            events = bus.payloads("/received")
            assert len(events) == 4  # the retry was dropped as duplicate
            assert events[1]["command"]["reason"] == "replay"
            assert events[2]["command"]["reason"] == "sender_not_allowed"
            assert events[3]["command"] is None
            assert (events[3]["sender_name"], events[3]["text"]) == ("Carol", "arm")
            assert len(bus.payloads("/command")) == 1

    scenario(main)
    ledger = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert ledger["command_timestamps"] == {ALICE.public_key.hex(): NOW - 10}


def test_command_time_is_stored_before_the_command_is_published(tmp_path: Path) -> None:
    """A crash right after publishing must not let the same packet command again (ADR 0009)."""
    state = tmp_path / "state.json"
    stored_at_publish: list[Any] = []

    class WatchingBus(FakeBus):
        def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
            if topic.endswith("/command"):
                stored = json.loads(state.read_text(encoding="utf-8"))
                stored_at_publish.append(stored["command_timestamps"])
            super().publish(topic, payload, retain)

    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=WatchingBus()) as (_, bus, _):
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 7)
            await bus.wait_for("/command", lambda c: True)

    scenario(main)
    assert stored_at_publish == [{ALICE.public_key.hex(): NOW - 7}]


def test_command_is_judged_by_the_contacts_of_this_moment(tmp_path: Path) -> None:
    """A twin with Alice's 6-byte prefix that the radio added a moment ago makes her commands
    ambiguous, even when the radio did not announce the new contact (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(TWIN)  # added automatically from its advert, without a push
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"] == {
                "word": "arm",
                "accepted": False,
                "reason": "sender_ambiguous",
            }
            assert not bus.payloads("/command")

    scenario(main)


def test_command_is_refused_when_the_contacts_cannot_be_read(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.errors[Command.GET_CONTACTS] = 1
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unverified"
            assert not bus.payloads("/command")

    scenario(main)


def test_only_possible_commands_read_the_contacts_again(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            before = radio.commands().count(Command.GET_CONTACTS)
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW - 5)
            radio.queue_channel_message(0, "Carol: arm", NOW - 1)
            await bus.wait_for("/received", lambda r: r["event_type"] == "channel")
            assert radio.commands().count(Command.GET_CONTACTS) == before
            radio.queue_direct_message(ALICE.public_key[:6], "status", NOW - 4)
            await bus.wait_for("/command", lambda c: True)
            assert radio.commands().count(Command.GET_CONTACTS) == before + 1

    scenario(main)


def test_new_contacts_are_read_before_their_messages(tmp_path: Path) -> None:
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")
    radio = EmulatedRadio(contacts=[ALICE], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(carol)
            radio.emit(bytes([Push.NEW_ADVERT]) + carol.public_key)
            radio.queue_direct_message(carol.public_key[:6], "hello", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["sender_name"] == "Carol"

    scenario(main)


def test_command_waiting_in_the_radio_at_connect_is_rejected(tmp_path: Path) -> None:
    """Sent while no bridge listened: a look-alike may have come and gone unseen meanwhile, so the
    sender has to send it again (ADR 0009). The message itself is still published."""
    radio = EmulatedRadio(contacts=[ALICE])
    radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 30, push=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unverified"
            assert (received["sender_name"], received["target"]) == ("Alice", "alice")
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)  # sent again
            command = await bus.wait_for("/command", lambda c: True)
            assert (command["event_type"], command["sender_name"]) == ("arm", "Alice")

    scenario(main)


def test_look_alikes_are_remembered_across_reconnects(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # seen by this session only
            for _attempt in range(100):
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break  # the advert was handled: the table is being read again
                await asyncio.sleep(0.01)
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected")
            count = len(bus.payloads("/state"))
            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=count)
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"

    scenario(main)


def test_overwritten_look_alike_still_makes_the_sender_ambiguous(tmp_path: Path) -> None:
    """With a full table the radio overwrites the oldest contact and says so with a push; the
    look-alike that sent the message may be gone before the bridge reads the table."""
    radio = EmulatedRadio(contacts=[ALICE, BOB, TWIN], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5, push=False)
            radio.overwrite(TWIN)  # CONTACT_DELETED push, then ...
            radio.emit(bytes([Push.MSG_WAITING]))  # ... the bridge fetches the message
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"
            assert not bus.payloads("/command")

    scenario(main)


def test_look_alike_that_left_the_table_silently_still_counts(tmp_path: Path) -> None:
    """Firmware 1.18 drops a contact without CONTACT_DELETED: after an advert that claims type 0,
    its next advert clears the slot and may not add it again (BaseChatMesh.cpp:151-182). The
    adverts' pushes carry its key, and the bridge remembers it (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], autoadd_config=AutoAdd.CHAT)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(TWIN)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # added from its advert
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5, push=False)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # claims type 0
            radio.contacts.remove(TWIN)  # the next advert clears the slot ...
            radio.emit(bytes([Push.NEW_ADVERT]) + TWIN.public_key + bytes(115))  # ... no re-add
            radio.emit(bytes([Push.MSG_WAITING]))
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"
            assert not bus.payloads("/command")

    scenario(main)


def test_a_look_alike_the_radio_did_not_add_does_not_block_the_sender(tmp_path: Path) -> None:
    """With manual adding the radio refuses the node and only reports it (NEW_ADVERT); it cannot
    decrypt that node's messages, so its key does not count (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.emit(bytes([Push.NEW_ADVERT]) + TWIN.public_key + bytes(115))
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            command = await bus.wait_for("/command", lambda c: True)
            assert command["sender_name"] == "Alice"

    scenario(main)


def test_look_alike_seen_only_in_a_table_read_still_counts(tmp_path: Path) -> None:
    """Over TCP the radio drops pushes when its send queue is full; a key from an earlier read
    of the table is remembered as well."""
    radio = EmulatedRadio(contacts=[ALICE, TWIN])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            radio.contacts.remove(TWIN)  # gone without any push
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.emit(bytes([Push.ADVERT]) + BOB.public_key)  # the bridge reads the table again
            for _attempt in range(100):
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break
                await asyncio.sleep(0.01)
            await asyncio.sleep(0.05)  # the read is complete: its table no longer holds the twin
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"

    scenario(main)


def test_sender_known_only_from_memory_is_not_accepted(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5, push=False)
            radio.overwrite(ALICE)
            radio.emit(bytes([Push.MSG_WAITING]))
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unknown"
            assert not bus.payloads("/command")

    scenario(main)


@pytest.mark.parametrize(("transport", "warned"), [("tcp", True), ("serial", False)])
def test_commands_over_tcp_are_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, transport: str, warned: bool
) -> None:
    """Over TCP the radio drops pushes when its send queue is full (ADR 0009)."""
    radio_section = (
        'transport = "tcp"\nhost = "192.0.2.10"'
        if transport == "tcp"
        else 'transport = "serial"\ndevice = "/dev/serial/by-id/usb-radio-if00"'
    )
    caplog.set_level(logging.WARNING)
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(radio=radio_section), radio, tmp_path) as (_, bus, _):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")

    scenario(main)
    assert ("commands over TCP" in caplog.text) is warned


def test_contact_reads_are_spent_only_where_they_can_change_the_verdict(tmp_path: Path) -> None:
    """Senders the cached table already rejects need no read; one read serves every message
    fetched before it."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            before = radio.commands().count(Command.GET_CONTACTS)
            for second in range(5):
                radio.queue_direct_message(BOB.public_key[:6], "arm", NOW - 20 + second, push=False)
            for second in range(3):
                radio.queue_direct_message(
                    ALICE.public_key[:6], "status", NOW - 10 + second, push=False
                )
            radio.emit(bytes([Push.MSG_WAITING]))
            await bus.wait_for("/received", lambda r: True, since=7)
            reasons = [r["command"]["reason"] for r in bus.payloads("/received")]
            assert reasons == ["sender_not_allowed"] * 5 + [None] * 3
            assert len(bus.payloads("/command")) == 3
            assert radio.commands().count(Command.GET_CONTACTS) == before + 1

    scenario(main)


def test_unwritable_state_file_rejects_the_command_and_keeps_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    def full_disk(_store: object, _data: object) -> None:
        raise OSError(28, "No space left on device")

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            monkeypatch.setattr(JsonStateFile, "save", full_disk)
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "state_unwritable"
            monkeypatch.undo()
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)  # the sender's retry
            await bus.wait_for("/command", lambda c: True)  # the service is still there

    scenario(main)


def test_retry_of_an_unverified_command_is_judged_again(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.errors[Command.GET_CONTACTS] = 1
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            await bus.wait_for("/received", lambda r: r["command"]["reason"] == "sender_unverified")
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)  # same packet again
            await bus.wait_for("/command", lambda c: True)

    scenario(main)


def test_names_come_from_the_table_just_read(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.clear()  # removed in the app a moment ago
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_unknown"
            assert received["sender_name"] is None

    scenario(main)


def test_ignored_channels_are_not_published(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(messages="ignore_channels = [0]"), radio, tmp_path) as (
            _,
            bus,
            _,
        ):
            radio.queue_channel_message(0, "Dave: chatter", NOW)
            radio.queue_direct_message(bytes.fromhex("c3" * 6), "hi", NOW)
            await bus.wait_for("/received", lambda r: r["event_type"] == "contact")
            assert all(r["event_type"] != "channel" for r in bus.payloads("/received"))

    scenario(main)


def test_state_discovery_and_systemd_notifications(tmp_path: Path) -> None:
    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, notes):
            state = await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            assert state["battery_v"] == pytest.approx(4.012)
            assert state["radio_name"] == "Bridge"
            assert state["spreading_factor"] == 8
            assert any(t.endswith("/config") for t, _, _ in bus.published)
            before = sum(t.endswith("/config") for t, _, _ in bus.published)
            for callback in bus.on_ha_online:
                callback()
            await asyncio.sleep(0.05)
            assert sum(t.endswith("/config") for t, _, _ in bus.published) == before + 1
            assert all(retain for t, _, retain in bus.published if t.endswith("/state"))
            assert not any(retain for t, _, retain in bus.published if t.endswith("/delivery"))
        assert notes.index("READY=1") < notes.index("STOPPING=1")
        assert any(note.startswith("STATUS=radio connected") for note in notes)
        assert bus.stopped
        assert bus.payloads("/state")[-1]["radio"] == "disconnected"

    scenario(main)


def test_radio_reconnects_after_unplugging(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected")
            count = len(bus.payloads("/state"))

            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=count)
            assert radio.commands().count(Command.APP_START) == 2

    scenario(main)


def test_unexpected_error_while_connecting_is_retried(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug on the connect path is logged with its traceback; the service stays up."""
    radio = EmulatedRadio()
    attempts = []

    def flaky(
        transport: Transport, events: asyncio.Queue[RadioEvent], set_clock: bool
    ) -> CompanionSession:
        session = fast_sessions(transport, events, set_clock)
        attempts.append(session)
        if len(attempts) == 1:

            async def broken(**_: Any) -> Any:
                raise KeyError("bug")

            session.start = broken  # type: ignore[method-assign]
        return session

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, sessions=flaky) as (_, bus, _notes):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
        assert len(attempts) >= 2

    scenario(main)
    assert "KeyError" in caplog.text


def test_receive_log_is_published_when_enabled(tmp_path: Path) -> None:
    radio = EmulatedRadio()
    config = make_config(extra="[diagnostics]\npublish_rx_log = true")

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            radio.emit(bytes([Push.LOG_RX_DATA, 0x1F, 0x9F]) + b"\x0d\x00\x01")
            rx = await bus.wait_for("/rx", lambda r: True)
            assert (rx["snr_db"], rx["rssi_dbm"], rx["raw"]) == (7.75, -97, "0d0001")

    scenario(main)


def test_discovery_follows_the_radio_identity(tmp_path: Path) -> None:
    """The device page shows the radio's model and firmware once they are known."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            documents = bus.payloads("/config")
            assert "mdl" not in documents[0]["dev"]  # announced on connect, radio not yet known
            assert documents[-1]["dev"]["mdl"] == "Example Radio"
            assert documents[-1]["dev"]["sn"] == SELF_KEY.hex()
            count = len(documents)
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected")
            states = len(bus.payloads("/state"))
            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=states)
            assert len(bus.payloads("/config")) == count  # the same radio again
            assert not any(retain for t, _, retain in bus.published if t.endswith("/config"))

    scenario(main)


def test_components_of_an_earlier_configuration_are_removed(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({
        "ha_components": {"send_old": "notify", "radio": "binary_sensor"},
        "command_timestamps": {"a1" * 32: 5},
    })  # fmt: skip

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            for document in bus.payloads("/config"):  # every announcement of this run
                assert document["cmps"]["send_old"] == {"p": "notify"}
                assert document["cmps"]["radio"]["p"] == "binary_sensor"

    scenario(main)
    data = store.load()
    assert "send_old" not in data["ha_components"]
    assert data["ha_components"]["radio"] == "binary_sensor"
    assert data["command_timestamps"] == {"a1" * 32: 5}
