"""The outgoing queue: alerts first, each kind in order, bounded (IMP-032, ADR 0028)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import inbox as inbox_module
from meshcore_home_gateway import outbox as outbox_module
from meshcore_home_gateway.core.airtime import LoRaParams, time_on_air_s
from meshcore_home_gateway.core.model import MessageKind, Target
from meshcore_home_gateway.mqtt.payloads import received_payload
from meshcore_home_gateway.outbox import Delivery, Outbox, Outgoing
from meshcore_home_gateway.ports import SendRequest, Transport
from meshcore_home_gateway.protocol.codes import Command, Push
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.state import JsonStateFile
from tests.bridge_harness import (
    ALICE,
    BOB,
    FINAL,
    FORGED,
    NEIGHBOUR,
    NOW,
    TWIN,
    FakeBus,
    Unreachable,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
)
from tests.radio_emulator import SELF_KEY, EmulatedContact, EmulatedRadio

CHANNEL = Target("alerts", MessageKind.CHANNEL, channel_index=0)


def message(name: str, *, alert: bool = False) -> Outgoing:
    return Outgoing(name, CHANNEL, name, 0.0, alert=alert)


def drained(outbox: Outbox) -> list[str]:
    async def take() -> list[str]:
        return [(await outbox.get()).id for _ in range(outbox.qsize())]

    return asyncio.run(take())


def test_alerts_leave_first_and_each_kind_in_order() -> None:
    outbox = Outbox(10)
    for item in (message("a"), message("A", alert=True), message("b"), message("B", alert=True)):
        outbox.put(item)
    assert outbox.alert_waiting
    assert drained(outbox) == ["A", "B", "a", "b"]
    assert not outbox.alert_waiting


def test_a_message_put_back_goes_first_among_its_kind_also_beyond_the_size() -> None:
    """Paused for an alert, or kept at a stop: it had its place."""
    outbox = Outbox(2)
    outbox.put(message("b"))
    outbox.put(message("c"))
    with pytest.raises(asyncio.QueueFull):
        outbox.put(message("d"))
    outbox.put_back(message("a"))
    outbox.restore(message("e"))  # kept by the last run: in its order
    assert outbox.qsize() == 4
    assert drained(outbox) == ["a", "b", "c", "e"]


def test_take_all_empties_the_queue_in_the_order_it_would_leave() -> None:
    outbox = Outbox(5)
    outbox.put(message("a"))
    outbox.put(message("A", alert=True))
    outbox.put_back(message("first"))
    assert [item.id for item in outbox.take_all()] == ["A", "first", "a"]
    assert outbox.qsize() == 0


def test_get_waits_for_a_message() -> None:
    async def main() -> None:
        outbox = Outbox(1)
        waiting = asyncio.create_task(outbox.get())
        await asyncio.sleep(0)
        assert not waiting.done()
        outbox.put(message("a"))
        assert (await asyncio.wait_for(waiting, 1)).id == "a"

    asyncio.run(main())


# --- the delivery, through the bridge


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


def test_a_request_s_correlation_data_comes_back_with_each_of_its_events(tmp_path: Path) -> None:
    """ARC-014: the MQTT 5 correlation data the adapter took from a request, until its final
    event, also for one refused at once; a request without it gets none."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], ack_round_trip_ms=2300)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            for alias, text, correlation in (
                ("alice", b"Hello Alice", b"direct"),
                ("alerts", b"Gate open", b"channel"),
                ("nowhere", b"Lost", b"refused"),
            ):
                bus.requests.put_nowait(SendRequest(alias, text, False, False, correlation))
            bus.send("alerts", "uncorrelated")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            await bus.wait_for(
                "/delivery", lambda d: (d["text"], d["event_type"]) == ("uncorrelated", "sent")
            )
            echoed: dict[bytes, list[str]] = {}
            for topic, event, correlation in bus.correlations:
                assert topic.endswith("/delivery")
                echoed.setdefault(correlation, []).append(event["event_type"])
            assert echoed == {
                b"direct": ["queued", "sent", "confirmed"],
                b"channel": ["queued", "sent"],
                b"refused": ["rejected"],
            }
            assert bridge.outbox._correlations == {}  # forgotten with the final events

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


def test_flood_after_equal_to_max_attempts_never_floods(tmp_path: Path) -> None:
    """`flood_after` counts unanswered attempts before the reset: none is left to flood."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
    config = make_config(messages="max_attempts = 3\nflood_after = 3")

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Are you there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed")
            assert [t.attempt for t in radio.texts] == [0, 1, 2]
            assert radio.path_resets == []

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
    sent again and the stored path is kept (Bob has one, and attempt 2 would flood)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        config = make_config(messages="flood_after = 1")
        async with running(config, radio, tmp_path) as (bridge, bus, _notes):
            bus.send("bob", "early")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            # attempt 2 waits ~1.2 s for airtime: the acknowledgement comes long before it, also on
            # a loaded CI runner (a margin of 50 ms once was not enough on Python 3.11)
            bridge.outbox.budget._tokens, bridge.outbox.budget._rate = 0.0, 0.5
            await asyncio.sleep(0.25)
            from meshcore_home_gateway.protocol.codec import expected_ack
            from tests.radio_emulator import SELF_KEY

            radio.acknowledge(expected_ack(NOW, 0, "early", SELF_KEY), 700)
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["attempt"], done["rtt_ms"]) == (1, 700)
            assert len(radio.texts) == 1
            assert not radio.path_resets

    scenario(main)


def test_a_stop_keeps_the_queue_and_the_next_start_sends_it(tmp_path: Path) -> None:
    """What waits for the radio at a stop is in the state file; the next start queues it again
    with its id (`queued` again) and sends it, and keeps none any more."""
    path = tmp_path / "state.json"
    ids: dict[str, str] = {}

    async def stopped() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path, Unreachable) as (_, bus, _n):
            bus.send("alerts", "Gate open")
            bus.send("bob", "Pump stopped")
            await bus.wait_for("/delivery", lambda d: d["target"] == "bob")
            ids.update({d["target"]: d["id"] for d in bus.payloads("/delivery")})

    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def started() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            events = [(d["id"], d["event_type"]) for d in bus.payloads("/delivery")]
            assert events[:2] == [(ids["alerts"], "queued"), (ids["bob"], "queued")]
            assert events[2:] == [
                (ids["alerts"], "sent"),
                (ids["bob"], "sent"),
                (ids["bob"], "confirmed"),
            ]
            bus.send("alerts", "Gate open")  # its client sends it again: it was sent
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "duplicate")

    scenario(stopped)
    kept = JsonStateFile(path).load()["outbox"]["messages"]
    assert [(m["id"], m["target"], m["text"], m["sent"]) for m in kept] == [
        (ids["alerts"], "alerts", "Gate open", 0),
        (ids["bob"], "bob", "Pump stopped", 0),
    ]
    scenario(started)
    assert radio.channel_texts == [(0, "Gate open", NOW)]
    assert "outbox" not in JsonStateFile(path).load()


def test_kept_messages_whose_time_ran_out_or_whose_target_left_end_at_the_start(
    tmp_path: Path,
) -> None:
    """With the same final event as had the gateway run on; nothing is sent."""

    def kept(ident: str, target: str, age_s: float, sent: int = 0) -> dict[str, Any]:
        timestamp = NOW - 600 if sent else None
        return {
            "id": ident,
            "target": target,
            "text": "x",
            "age_s": age_s,
            "sent": sent,
            "timestamp": timestamp,
        }

    JsonStateFile(tmp_path / "state.json").save(
        {
            "outbox": {
                "saved_at": NOW - 30,
                "messages": [
                    kept("old", "alerts", 590.0),
                    kept("tried", "bob", 590.0, sent=1),
                    kept("gone", "porch", 1.0),
                    kept("fresh", "alerts", 1.0),
                ],
            }
        }
    )
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            await bus.wait_for(
                "/delivery", lambda d: d["id"] == "fresh" and d["event_type"] == "sent"
            )
            events = [
                (d["id"], d["event_type"], d["reason"], d["attempt"])
                for d in bus.payloads("/delivery")
            ]
            assert events == [
                ("old", "expired", "queue_ttl", 0),
                ("tried", "failed", "no_acknowledgement", 1),
                ("gone", "failed", "unknown_target", 0),
                ("fresh", "queued", None, 0),
                ("fresh", "sent", None, 1),
            ]
            assert radio.texts == []

    scenario(main)


def test_a_stop_waits_for_the_acknowledgement_of_the_message_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The radio stays open until it comes (for at most STOP_OUTBOX_S): a message that left is
    not repeated after the restart for want of a minute."""
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 5.0)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 5.0)
    monkeypatch.setattr(outbox_module, "STOP_OUTBOX_S", 3.0)
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
    buses: list[FakeBus] = []

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            buses.append(bus)
            bus.send("bob", "Still there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bus.send("alice", "Next")  # waits behind it, and is kept
            await bus.wait_for("/delivery", lambda d: d["target"] == "alice")
            from meshcore_home_gateway.protocol.codec import expected_ack

            ack = expected_ack(NOW, 0, "Still there?", SELF_KEY)
            asyncio.get_running_loop().call_later(0.3, radio.acknowledge, ack, 800)

    scenario(main)
    events = [(d["target"], d["event_type"], d["attempt"]) for d in buses[0].payloads("/delivery")]
    assert events == [
        ("bob", "queued", 0),
        ("bob", "sent", 1),
        ("alice", "queued", 0),
        ("bob", "confirmed", 1),
    ]
    assert [t.text for t in radio.texts] == ["Still there?"]  # no attempt starts in the stop
    (kept,) = JsonStateFile(tmp_path / "state.json").load()["outbox"]["messages"]
    assert (kept["target"], kept["sent"]) == ("alice", 0)


def test_a_message_unacknowledged_in_the_stop_is_kept_for_its_next_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its wait ends within the stop without an acknowledgement: the next attempt is the next
    start's."""
    monkeypatch.setattr(outbox_module, "STOP_OUTBOX_S", 3.0)
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            bus.send("bob", "Still there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")

    scenario(main)
    assert [t.attempt for t in radio.texts] == [0]
    (kept,) = JsonStateFile(tmp_path / "state.json").load()["outbox"]["messages"]
    assert (kept["target"], kept["sent"]) == ("bob", 1)


def test_a_message_whose_acknowledgement_misses_the_stop_goes_on_after_the_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Kept with its attempts and its timestamp: the next start sends the next attempt, which
    the recipient takes for a retry."""
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 5.0)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 5.0)
    monkeypatch.setattr(outbox_module, "STOP_OUTBOX_S", 0.2)
    path = tmp_path / "state.json"

    async def stopped() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            bus.send("bob", "Still there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")

    second = EmulatedRadio(contacts=[ALICE, BOB])

    async def started() -> None:
        async with running(make_config(), second, tmp_path) as (_, bus, _n):
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert done["attempt"] == 2
            assert bus.payloads("/delivery")[0]["event_type"] == "queued"

    scenario(stopped)
    state = JsonStateFile(path).load()
    (kept,) = state["outbox"]["messages"]
    assert (kept["sent"], kept["timestamp"]) == (1, NOW)
    kept["timestamp"] = NOW - 100  # sent by an earlier clock: the next attempt keeps it
    JsonStateFile(path).save(state)
    scenario(started)
    assert [(t.attempt, t.timestamp, t.text) for t in second.texts] == [
        (1, NOW - 100, "Still there?")
    ]


def test_an_alert_goes_before_the_queue_and_between_the_attempts_of_a_message(
    tmp_path: Path,
) -> None:
    """IMP-032: Bob's message is in flight and Alice's waits when the alert comes: it goes
    after Bob's first attempt, before his second and before Alice's."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
    config = make_config(
        messages='alert_targets = ["alerts"]', airtime="budget_s_per_hour = 3600\nburst_s = 60"
    )

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _n):
            bus.send("bob", "first")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bus.send("alice", "second")
            bus.send("alerts", "Smoke")
            await bus.wait_for(
                "/delivery", lambda d: d["target"] == "alice" and d["event_type"] == "failed"
            )

    scenario(main)
    sends = [
        c for c in radio.commands() if c in (Command.SEND_TXT_MSG, Command.SEND_CHANNEL_TXT_MSG)
    ]
    assert sends[:3] == [Command.SEND_TXT_MSG, Command.SEND_CHANNEL_TXT_MSG, Command.SEND_TXT_MSG]
    assert [(t.text, t.attempt) for t in radio.texts] == [
        ("first", 0),
        ("first", 1),
        ("first", 2),
        ("second", 0),
        ("second", 1),
        ("second", 2),
    ]


def test_the_budget_level_survives_a_restart_and_a_long_message_still_goes_out(
    tmp_path: Path,
) -> None:
    """ARC-005: a restart adds no burst; it gives what the longest direct message needs at the
    radio's settings, so that the first alert after it is not held up."""
    path = tmp_path / "state.json"
    levels: list[float] = []

    async def drained() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (bridge, bus, _n):
            levels.append(bridge.outbox.budget.available_s(time.monotonic()))
            bus.send("alerts", "Gate open")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            on_disk = JsonStateFile(path).load()["airtime"]["level_s"]  # at once, not at the stop
            assert on_disk == pytest.approx(
                bridge.outbox.budget.available_s(time.monotonic()), abs=0.01
            )
            assert on_disk < 10.0
            bridge.outbox.budget._tokens = 0.0

    async def restarted() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (bridge, _bus, _n):
            levels.append(bridge.outbox.budget.available_s(time.monotonic()))

    scenario(drained)
    saved = JsonStateFile(path).load()["airtime"]
    assert saved["level_s"] == pytest.approx(0.0, abs=0.01)
    assert 0.0 < saved["longest_s"] < 10.0
    scenario(restarted)
    assert levels == [10.0, pytest.approx(saved["longest_s"], abs=0.01)]


def test_confirm_nonce_makes_a_direct_text_unguessable(tmp_path: Path) -> None:
    """SEC-001: with `confirm_nonce` a direct text leaves with 8 random hex digits, the same on
    every attempt; every event carries the text as sent, duplicates are judged without them, and
    a channel text gets none (it is not acknowledged)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        config = make_config(messages="confirm_nonce = true")
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Smoke in the kitchen")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed")
            (sent,) = {t.text for t in radio.texts}
            assert re.fullmatch(r"Smoke in the kitchen #[0-9a-f]{8}", sent)
            assert [t.attempt for t in radio.texts] == [0, 1, 2]
            assert {e["text"] for e in bus.payloads("/delivery")} == {sent}
            bus.send("bob", "Smoke in the kitchen")
            duplicate = await bus.wait_for("/delivery", lambda d: d["event_type"] == "duplicate")
            assert duplicate["text"] == "Smoke in the kitchen"
            bus.send("alerts", "Smoke in the kitchen")
            await bus.wait_for(
                "/delivery", lambda d: d["target"] == "alerts" and d["event_type"] == "sent"
            )
            assert radio.channel_texts == [(0, "Smoke in the kitchen", NOW)]

    scenario(main)


def test_confirm_nonce_leaves_room_for_its_digits(tmp_path: Path) -> None:
    """The 160 bytes of a direct text hold the suffix: a text is limited to 150 bytes first."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        config = make_config(messages="confirm_nonce = true")
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            bus.send("alice", "y" * 150)
            bus.send("alice", "x" * 200)  # one at a time: the second is confirmed last
            await bus.wait_for(
                "/delivery", lambda d: d["event_type"] == "confirmed" and d["text"][0] == "x"
            )
            whole, cut = (t.text for t in radio.texts)
            assert re.fullmatch(r"y{150} #[0-9a-f]{8}", whole)
            assert re.fullmatch(r"x{147}\.\.\. #[0-9a-f]{8}", cut)
            assert len(whole.encode()) == len(cut.encode()) == 160

    scenario(main)


@pytest.mark.parametrize(
    ("contact", "round_trip_ms", "suspect"),
    [(BOB, 400, True), (BOB, 1500, False), (ALICE, 1, False), (NEIGHBOUR, 1, False)],
)
def test_an_acknowledgement_sooner_than_the_path_allows_is_suspect(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    contact: EmulatedContact,
    round_trip_ms: int,
    suspect: bool,
) -> None:
    """SEC-001: Bob is two repeaters away, so a genuine acknowledgement of a 9-byte text takes
    922 ms at least at the emulated radio's settings; one in 400 ms is `suspect` and logged. To
    Alice, without a path, the radio floods, and Carol is a neighbour: a forger in range answers
    them as soon as they can, so nothing is told."""
    radio = EmulatedRadio(contacts=[ALICE, BOB, NEIGHBOUR], ack_round_trip_ms=round_trip_ms)
    caplog.set_level(logging.WARNING)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("radio1/contacts", lambda d: d["total"] == 3)
            bus.send(contact.public_key.hex(), "Hello you", contact=True)
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["rtt_ms"], done["suspect"]) == (round_trip_ms, suspect)
            queued = bus.payloads("/delivery")[0]
            assert (queued["event_type"], queued["suspect"]) == ("queued", None)

    scenario(main)
    warnings = [r.getMessage() for r in caplog.records if "may be forged" in r.getMessage()]
    assert len(warnings) == int(suspect)
    if suspect:
        assert re.fullmatch(
            r"delivery [0-9a-f]{8}: acknowledged after 400 ms, sooner than the 922 ms a genuine "
            r"acknowledgement takes over the path at least: it may be forged \(SEC-001\)",
            warnings[0],
        )


def test_a_table_changed_while_sending_is_never_suspect(tmp_path: Path) -> None:
    """The radio reports a contact change while it takes the message: the path it used may not
    be the one the gateway read, so the round trip says nothing (SEC-001)."""

    class PathChanges(EmulatedRadio):
        def _send_text(self, p: bytes) -> None:
            self.emit(bytes([Push.PATH_UPDATED]) + BOB.public_key)
            super()._send_text(p)

    radio = PathChanges(contacts=[ALICE, BOB], ack_round_trip_ms=400)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Hello you")
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["rtt_ms"], done["suspect"]) == (400, False)

    scenario(main)


def test_a_repeated_attempt_is_never_suspect(tmp_path: Path) -> None:
    """An attempt interrupted by the radio is repeated with the same code: an acknowledgement may
    answer either copy, so its round trip says nothing (SEC-001)."""
    radio = EmulatedRadio(
        contacts=[ALICE, BOB], ack_round_trip_ms=400, drop_on={Command.SEND_TXT_MSG}
    )

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Hello you")
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["attempt"], done["suspect"]) == (1, False)
            assert radio.commands().count(Command.APP_START) == 2

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


@pytest.mark.parametrize(
    ("truncate", "text", "sent"),
    [
        (True, "a" * 150, "a" * 150),  # not cut to the 127 bytes of the longest name
        (True, "b" * 200, "b" * 149 + "..."),
        (False, "c" * 155, None),  # fails as too long
    ],
    ids=["fits", "truncated", "too_long"],
)
def test_channel_text_accepted_before_the_radio_is_known_is_limited_when_sent(
    tmp_path: Path, truncate: bool, text: str, sent: str | None
) -> None:
    """The firmware puts the radio's name in front of a channel text, and before the radio has
    connected once that name is unknown: the text is limited when it is sent, for this radio
    ("Bridge") to 160 - 8 = 152 bytes."""
    radio = EmulatedRadio()
    plugged: list[bool] = []
    config = make_config(messages=f"truncate = {str(truncate).lower()}")

    def transport() -> Transport:
        return radio if plugged else Unreachable()

    async def main() -> None:
        async with running(config, radio, tmp_path, transport=transport) as (_, bus, _notes):
            bus.send("alerts", text)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "queued")
            plugged.append(True)  # the radio connects
            final = await bus.wait_for(
                "/delivery", lambda d: d["event_type"] in FINAL | {"sent"}, 5
            )
            if sent is None:
                assert (final["event_type"], final["reason"]) == ("failed", "too_long")
            else:
                assert (final["event_type"], final["text"]) == ("sent", sent)
            assert [t for _, t, _ in radio.channel_texts] == ([] if sent is None else [sent])

    scenario(main)


def test_duplicates_are_suppressed(tmp_path: Path) -> None:
    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            bus.send("alerts", "Smoke")
            bus.send("alerts", "Smoke")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "duplicate")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")

    scenario(main)


def test_events_of_a_flood_of_refused_requests_are_limited(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flood of refused requests must not become a flood of events, which would push other
    events out of a subscriber's queue on the broker; the log counts what was left out. Accepted
    requests keep their events."""
    monkeypatch.setattr(bridge_module, "STATUS_INTERVAL_S", 0.05)  # reports the count
    monkeypatch.setattr(outbox_module, "REFUSED_REPORT_INTERVAL_S", 0.2)
    caplog.set_level(logging.WARNING)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (bridge, bus, _notes):
            for number in range(5000):  # with MQTT 5 correlation data, which must not pile up
                bus.requests.put_nowait(SendRequest("nobody", b"x", False, False, b"%d" % number))
            bus.send("alerts", "valid")
            await bus.wait_for(
                "/delivery", lambda d: (d["text"], d["event_type"]) == ("valid", "sent")
            )
            refused = [d for d in bus.payloads("/delivery") if d["reason"] == "unknown_target"]
            assert 100 <= len(refused) < 200  # the burst, and the rate while the flood lasted
            await asyncio.sleep(0.4)
            reports = [r.getMessage() for r in caplog.records if "not published" in r.getMessage()]
            assert len(reports) == 2  # at once, then the rest of the flood
            counted = sum(int(report.rsplit(" ", 1)[1]) for report in reports)
            assert counted + len(refused) == 5000
            assert len(bus.correlations) == len(refused)
            assert bridge.outbox._correlations == {}

    scenario(main)


def test_full_queue_rejects_and_waiting_messages_expire(tmp_path: Path) -> None:
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
            assert bridge.status.payload()["airtime_1h_s"] <= 5.0

    scenario(main)


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
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 0.6)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 0.6)
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)

    async def main() -> None:
        async with running(make_config(messages="queue_ttl_s = 1"), radio, tmp_path) as (_, bus, _):
            bus.send("alice", "busy")
            bus.send("alerts", "late")
            busy = await bus.wait_for(
                "/delivery", lambda d: d["text"] == "busy" and d["event_type"] in FINAL, 5
            )
            assert (busy["event_type"], busy["reason"], busy["attempt"]) == (
                "failed",
                "no_acknowledgement",
                2,
            )
            late = await bus.wait_for(
                "/delivery", lambda d: d["text"] == "late" and d["event_type"] in FINAL, 5
            )
            assert (late["event_type"], late["reason"]) == ("expired", "queue_ttl")

    scenario(main)


def test_last_attempt_waits_for_its_acknowledgement_past_the_time_to_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No attempt starts after `queue_ttl_s`, but the last one still waits for its acknowledgement:
    one that arrives after the time to live confirms the message."""
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 2.0)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 2.0)
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)

    async def main() -> None:
        async with running(make_config(messages="queue_ttl_s = 1"), radio, tmp_path) as (_, bus, _):
            bus.send("alice", "slow")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            await asyncio.sleep(1.2)  # past the time to live, within the acknowledgement wait
            from meshcore_home_gateway.protocol.codec import expected_ack

            radio.acknowledge(expected_ack(NOW, 0, "slow", SELF_KEY), 3000)
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL)
            assert (done["event_type"], done["attempt"], done["rtt_ms"]) == ("confirmed", 1, 3000)
            assert len(radio.texts) == 1

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


def test_after_the_path_reset_the_next_message_is_budgeted_as_a_flood(tmp_path: Path) -> None:
    """The radio pushes nothing on RESET_PATH: the session's table and the contact's document
    show the reset before the next read, so the next message plans the flood it will be."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("bob", "Are you there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed")
            bob = await bus.wait_for(
                f"/contacts/{BOB.public_key.hex()}", lambda d: d["path_known"] is False
            )
            assert bob["path_hops"] is None
            radio.auto_ack = True
            bus.send("bob", "Still there?")
            confirmed = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            sent = [d for d in bus.payloads("/delivery") if d["event_type"] == "sent"]
            assert sent[-1]["id"] == confirmed["id"]
            assert sent[-1]["airtime_ms"] == sent[2]["airtime_ms"] > sent[0]["airtime_ms"]

    scenario(main)


def test_stored_path_counts_hops_times_hash_size(tmp_path: Path) -> None:
    """out_path_len 0x83 is 3 hops of 3-byte hashes: the message carries 9 bytes of path."""
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol", out_path_len=0x83)
    radio = EmulatedRadio(contacts=[ALICE, BOB, carol])
    config = make_config(extra=f'[targets.carol]\ncontact = "{carol.public_key.hex()}"')

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _notes):
            bus.send("carol", "Hello Carol")
            sent = await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            # header 1, path length 1, path 9, payload 4 + 16 for 11 text bytes
            expected = time_on_air_s(31, LoRaParams(62_500.0, 8, 8, 32))
            assert sent["airtime_ms"] == round(expected * 1000)

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
                "failed",
                "exceeds_airtime_burst",
                1,
            )
            assert radio.path_resets == []

    scenario(main)


def test_message_waits_for_airtime_then_is_sent(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            bridge.outbox.budget._tokens, bridge.outbox.budget._rate = (
                0.0,
                2.0,
            )  # about 0.3 s to wait
            bus.send("alerts", "Smoke")
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL | {"sent"})
            assert event["event_type"] == "sent"
            assert event["airtime_ms"] > 300
            assert [text for _, text, _ in radio.channel_texts] == ["Smoke"]

    scenario(main)


def test_airtime_running_out_after_sending_is_a_failure(tmp_path: Path) -> None:
    """The message left once, so it was not acknowledged: `failed`, not `expired`."""
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            bus.send("alice", "x")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bridge.outbox.budget._tokens, bridge.outbox.budget._rate = (
                0.0,
                1e-6,
            )  # nothing for attempt 2
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL)
            assert (event["event_type"], event["reason"], event["attempt"]) == (
                "failed",
                "no_acknowledgement",
                1,
            )
            assert len(radio.texts) == 1

    scenario(main)


@pytest.mark.parametrize(
    ("alias", "refusal", "reason"),
    [
        ("alice", {"errors": {Command.SEND_TXT_MSG: 3}}, "table_full"),
        ("alerts", {"disabled": {Command.SEND_CHANNEL_TXT_MSG}}, "disabled"),
    ],
    ids=["error", "disabled"],
)
def test_a_refusal_of_the_radio_ends_the_delivery(
    tmp_path: Path, alias: str, refusal: dict[str, Any], reason: str
) -> None:
    """ERR or DISABLED is the radio's answer, not an interruption: nothing is sent again."""
    radio = EmulatedRadio(contacts=[ALICE], **refusal)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send(alias, "x")
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL | {"sent"})
            assert (event["event_type"], event["reason"], event["attempt"]) == ("failed", reason, 0)
            sends = {Command.SEND_TXT_MSG, Command.SEND_CHANNEL_TXT_MSG}
            assert len([c for c in radio.commands() if c in sends]) == 1

    scenario(main)


def test_channel_text_too_long_for_a_renamed_radio_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A text accepted for the radio's old name no longer fits after a rename."""
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 0.3)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 0.3)
    radio = EmulatedRadio(contacts=[ALICE], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alice", "busy")  # the outbox waits for acknowledgements meanwhile
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bus.send("alerts", "c" * 152)  # just fits "Bridge"
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "queued")
            radio.name = "Bridge 2"  # the bridge learns the new name when it connects again
            radio.disconnect()
            event = await bus.wait_for(
                "/delivery", lambda d: d["target"] == "alerts" and d["event_type"] in FINAL, 5
            )
            assert (event["event_type"], event["reason"]) == ("failed", "too_long")
            assert radio.channel_texts == []

    scenario(main)


def test_unexpected_errors_lose_one_message_and_the_service_goes_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A bug while sending fails that delivery (`internal_error`), a bug while handling a received
    message loses that one; both are logged with their traceback."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    plan = Delivery._plan
    failures = {"plan": 1, "payload": 1}

    def plan_once(self: Delivery, *args: Any) -> Any:
        if failures["plan"]:
            failures["plan"] -= 1
            raise KeyError("bug in planning")
        return plan(self, *args)

    def payload_once(*args: Any) -> Any:
        if failures["payload"]:
            failures["payload"] -= 1
            raise KeyError("bug in the payload")
        return received_payload(*args)

    monkeypatch.setattr(Delivery, "_plan", plan_once)
    monkeypatch.setattr(inbox_module, "received_payload", payload_once)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("alerts", "first")
            bus.send("alerts", "second")
            await bus.wait_for(
                "/delivery", lambda d: (d["text"], d["event_type"]) == ("second", "sent")
            )
            first = [d for d in bus.payloads("/delivery") if d["text"] == "first"]
            assert (first[-1]["event_type"], first[-1]["reason"]) == ("failed", "internal_error")
            radio.queue_direct_message(BOB.public_key[:6], "lost", NOW - 5)
            radio.queue_direct_message(BOB.public_key[:6], "published", NOW - 4)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["text"] == "published"

    scenario(main)
    assert "bug in planning" in caplog.text
    assert "bug in the payload" in caplog.text


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


def test_contact_change_the_radio_reported_is_read_before_sending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The radio encrypts for the first contact with the target's 6-byte prefix. A look-alike it
    has just added in an earlier slot, and reported, must make the delivery fail instead of
    receiving the message, although the regular read of the table follows only later."""
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 2.0)  # the production delay
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.insert(0, TWIN)
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)
            await asyncio.sleep(0.05)  # the push has been handled
            bus.send("alice", "Alarm")
            final = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL)
            assert (final["event_type"], final["reason"]) == ("failed", "ambiguous_target")
            assert radio.texts == []

    scenario(main)


def test_alias_of_an_unknown_send_topic_cannot_forge_log_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The alias comes from the topic of a request; brokers pass direction and line separator
    characters in topic names."""
    caplog.set_level(logging.INFO)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            bus.send("x" + FORGED, "hi")
            await bus.wait_for("/delivery", lambda d: d["reason"] == "unknown_target")

    scenario(main)
    assert "to 'x\\u202e\\nERROR x\\x1b[31m\\u2028': rejected" in caplog.text
    assert not any(ch in caplog.text for ch in "\u202e\x1b\u2028")
    assert "\nERROR x" not in caplog.text


def test_a_look_alike_added_during_the_airtime_wait_does_not_get_the_message(
    tmp_path: Path,
) -> None:
    """The target is resolved again after waiting for airtime, which can take minutes."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            bridge.outbox.budget._tokens, bridge.outbox.budget._rate = (
                0.0,
                1.0,
            )  # waits about its airtime
            bus.send("alice", "Alarm")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "queued")
            await asyncio.sleep(0.1)  # the outbox waits for airtime now
            assert radio.texts == []
            radio.contacts.insert(0, TWIN)  # added in an earlier slot than Alice
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)
            final = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL, 5)
            assert (final["event_type"], final["reason"]) == ("failed", "ambiguous_target")
            assert radio.texts == []

    scenario(main)


def test_a_long_channel_text_accepted_before_the_first_connect_is_deduplicated(
    tmp_path: Path,
) -> None:
    """Shortened for the radio's name only when it is sent; the same request again is still a
    duplicate of the text that went out."""
    radio = EmulatedRadio()
    plugged: list[bool] = []

    def transport() -> Transport:
        return radio if plugged else Unreachable()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, transport=transport) as (_, bus, _n):
            bus.send("alerts", "b" * 200)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "queued")
            plugged.append(True)
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent", 5)
            since = len(bus.payloads("/delivery"))
            bus.send("alerts", "b" * 200)  # well within dedup_window_s
            second = await bus.wait_for(
                "/delivery", lambda d: d["event_type"] in FINAL | {"queued"}, 3, since=since
            )
            assert second["event_type"] == "duplicate"
            assert len(radio.channel_texts) == 1

    scenario(main)


def test_any_contact_is_a_recipient(tmp_path: Path) -> None:
    """ADR 0017: contacts/<key>/send reaches a contact that is no configured target, through the
    same queue, deduplication and acknowledgement; the delivery names the key."""
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")
    repeater = EmulatedContact(bytes.fromhex("e5" * 32), "Hilltop", node_type=2)
    radio = EmulatedRadio(contacts=[ALICE, BOB, carol, repeater])
    key = carol.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("radio1/contacts", lambda d: d["total"] == 4)
            bus.send(key, "Dinner is ready", contact=True)
            done = await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            assert (done["target"], done["text"]) == (key, "Dinner is ready")
            bus.send(key, "Dinner is ready", contact=True)
            dup = await bus.wait_for("/delivery", lambda d: d["event_type"] == "duplicate")
            assert dup["target"] == key
            bus.send("dd" * 32, "nobody", contact=True)
            refused = await bus.wait_for("/delivery", lambda d: d["event_type"] == "rejected")
            assert (refused["target"], refused["reason"]) == ("dd" * 32, "unknown_contact")
            bus.send(key.upper(), "upper case", contact=True)  # keys as the gateway writes them
            refused = await bus.wait_for("/delivery", lambda d: d["target"] == key.upper())
            assert (refused["event_type"], refused["reason"]) == ("rejected", "unknown_contact")
            bus.send(repeater.public_key.hex(), "hello", contact=True)
            refused = await bus.wait_for(
                "/delivery", lambda d: d.get("reason") == "not_a_companion"
            )
            assert refused["event_type"] == "rejected"
            history = await bus.wait_for("/history", lambda d: d["total"] == 1)
            assert (history["messages"][0]["name"], history["messages"][0]["contact_key"]) == (
                "Carol",
                key,
            )
            assert radio.texts[0].prefix == carol.public_key[:6]

    scenario(main)


def test_the_alias_of_a_refused_request_is_echoed_up_to_64_characters(tmp_path: Path) -> None:
    """SEC-017: the topic level can be any length; the sender controls it."""
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.send("x" * 300, "hello")
            event = await bus.wait_for("/delivery", lambda d: d["reason"] == "unknown_target")
            assert event["target"] == "x" * 64

    scenario(main)
