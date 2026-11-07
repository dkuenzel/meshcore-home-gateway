"""The `pending` document: the messages the gateway accepted that are not final yet, the one in
flight included (#112), from their acceptance until the history has them."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import outbox as outbox_module
from meshcore_home_gateway.views import iso
from tests.bridge_harness import (
    ALICE,
    BOB,
    NOW,
    FakeBus,
    Unreachable,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
)
from tests.radio_emulator import EmulatedRadio

PENDING, HISTORY = "meshcore/radio1/pending", "meshcore/radio1/history"


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def pending(bus: FakeBus) -> list[dict[str, Any]]:
    return [json.loads(p) for t, p, _ in bus.published if t == PENDING]


def steps(document: dict[str, Any]) -> list[tuple[str, str, int]]:
    return [(m["text"], m["step"], m["attempt"]) for m in document["messages"]]


def waiting_over_a_minute(document: dict[str, Any], now: float) -> int:
    """The rule of the dashboard's status line: messages not sent yet that were accepted more
    than a minute ago. One waiting for its acknowledgement was sent and does not count."""
    return sum(
        1
        for m in document["messages"]
        if m["step"] == "queued"
        and m["queued_at"] is not None
        and now - datetime.fromisoformat(m["queued_at"]).timestamp() > 60
    )


def test_a_message_is_pending_from_its_acceptance_to_its_final_step(tmp_path: Path) -> None:
    """Listed when it is queued, with each attempt as it goes out, and gone when it failed; a
    request refused at once is never listed."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            bus.send("bob", "Are you there?")
            bus.send("nowhere", "Lost")
            failed = await bus.wait_for("/delivery", lambda d: d["event_type"] == "failed")
            await bus.wait_for(PENDING, lambda d: d["total"] == 0, since=2)
            documents = pending(bus)
            assert [steps(d) for d in documents] == [
                [],  # at the broker connect
                [("Are you there?", "queued", 0)],
                [("Are you there?", "sent", 1)],
                [("Are you there?", "sent", 2)],
                [("Are you there?", "sent", 3)],
                [],
            ]
            entry = documents[1]["messages"][0]
            assert entry == {
                "id": failed["id"],
                "queued_at": iso(NOW),
                "target": "bob",
                "kind": "contact",
                "contact_key": BOB.public_key.hex(),
                "channel_index": None,
                "name": "Bob",
                "text": "Are you there?",
                "step": "queued",
                "attempt": 0,
                "max_attempts": 3,
            }
            assert [(d["total"], d["truncated"]) for d in documents] == [
                (len(d["messages"]), False) for d in documents
            ]

    scenario(main)


def test_a_message_is_in_the_history_before_it_leaves_pending(tmp_path: Path) -> None:
    """After each `pending`, every accepted message is in exactly one of the two documents; the
    `history` that holds a final message goes out before the `pending` without it, so a reader
    of both never misses it (MQTT keeps the order only within a topic)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            bus.send("alerts", "Gate open")
            bus.send("bob", "Pump stopped")
            bus.send(ALICE.public_key.hex(), "Back soon", contact=True)
            await bus.wait_for(HISTORY, lambda d: d["total"] == 3)
            await bus.wait_for(PENDING, lambda d: d["total"] == 0, since=2)
            accepted: set[str] = set()
            history: set[str] = set()
            listed: set[str] = set()
            checked = 0
            for topic, payload, _ in bus.published:
                document = json.loads(payload) if payload.startswith(b"{") else {}
                if topic.endswith("/delivery") and document["event_type"] == "queued":
                    accepted.add(document["id"])
                elif topic == HISTORY:
                    history = {m["id"] for m in document["messages"]}
                elif topic == PENDING:
                    listed = {m["id"] for m in document["messages"]}
                    assert not listed & history, "in both"
                    assert accepted <= listed | history, "in neither"
                    checked += 1
            assert len(accepted) == 3
            assert (listed, history) == (set(), accepted)
            assert checked >= 7

    scenario(main)


def test_the_message_in_flight_is_pending_beside_a_full_queue(tmp_path: Path) -> None:
    """With `queue_size = 2` and the radio away, the message taken for delivery waits outside
    the queue: three are pending while the queue holds two, and the rest is refused."""
    config = make_config(messages="queue_size = 2")

    async def main() -> None:
        async with running(config, EmulatedRadio(), tmp_path, Unreachable) as (bridge, bus, _n):
            bus.send("alerts", "Gate 0")
            await bus.wait_for(PENDING, lambda d: d["total"] == 1)
            await asyncio.sleep(0.05)  # the delivery takes it and waits for the radio
            assert bridge.outbox.queued() == 0
            for i in range(1, 5):
                bus.send("alerts", f"Gate {i}")
            await bus.wait_for("/delivery", lambda d: d["reason"] == "queue_full")
            await asyncio.sleep(0.05)
            assert bridge.outbox.queued() == 2
            document = pending(bus)[-1]
            assert (document["total"], document["truncated"]) == (3, False)
            assert steps(document) == [(f"Gate {i}", "queued", 0) for i in range(3)]
            assert {m["max_attempts"] for m in document["messages"]} == {1}  # channel messages

    scenario(main)


def test_a_kept_queue_is_pending_again_after_the_start_and_at_each_connect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bob's message went out once and waits for its acknowledgement when the gateway stops;
    the channel message waits behind it. The next start lists both again, oldest first, with
    their ids and when they were accepted: Bob's as `queued` again with its attempt, as its
    delivery event says. Every broker connect publishes the list again."""
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 5.0)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 5.0)
    monkeypatch.setattr(outbox_module, "STOP_OUTBOX_S", 0.2)
    first: dict[str, Any] = {}

    async def stopped() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
        async with running(make_config(), radio, tmp_path, wall_clock=Clock(NOW - 30)) as (
            _,
            bus,
            _n,
        ):
            bus.send("bob", "Still there?")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bus.send("alerts", "Gate open")
            first.update(await bus.wait_for(PENDING, lambda d: d["total"] == 2))

    async def started() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path, Unreachable) as (_, bus, _n):
            again = await bus.wait_for(PENDING, lambda d: d["total"] == 2)
            assert steps(first) == [("Still there?", "sent", 1), ("Gate open", "queued", 0)]
            assert steps(again) == [("Still there?", "queued", 1), ("Gate open", "queued", 0)]
            for before, after in zip(first["messages"], again["messages"], strict=True):
                assert before | {"step": None, "name": None} == after | {"step": None, "name": None}
                assert after["queued_at"] == iso(NOW - 30)
            # no table read yet: the recipient is the alias, as the history would name it
            assert [m["name"] for m in again["messages"]] == ["bob", "alerts"]
            published = len(pending(bus))
            for connected in bus.on_connected:
                connected()
            assert len(pending(bus)) > published
            assert pending(bus)[-1] == again

    scenario(stopped)
    scenario(started)


def test_a_burst_of_requests_is_one_document(tmp_path: Path) -> None:
    """Requests that arrive together are accepted in one moment: `pending` goes out once, with
    all of them, not once for each."""

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path, Unreachable) as (_, bus, _n):
            await bus.wait_for(PENDING, lambda d: True)
            for i in range(5):
                bus.send("alerts", f"Gate {i}")
            await bus.wait_for(PENDING, lambda d: d["total"] == 5)
            await asyncio.sleep(0.05)
            assert [d["total"] for d in pending(bus)] == [0, 5]

    scenario(main)


def test_kept_messages_are_listed_in_the_order_they_were_accepted(
    tmp_path: Path,
) -> None:
    """The queue keeps an alert in front of an older message; `pending` lists the oldest first
    also after a restart, by when each was accepted."""
    config = make_config(messages='alert_targets = ["alerts"]')
    wall = Clock(NOW - 30)

    async def stopped() -> None:
        async with running(config, EmulatedRadio(), tmp_path, Unreachable, wall_clock=wall) as (
            _,
            bus,
            _n,
        ):
            bus.send("bob", "Pump stopped")
            await bus.wait_for(PENDING, lambda d: d["total"] == 1)
            wall.now += 10
            bus.send("alerts", "Smoke")
            await bus.wait_for(PENDING, lambda d: d["total"] == 2)

    async def started() -> None:
        async with running(config, EmulatedRadio(), tmp_path, Unreachable) as (_, bus, _n):
            await bus.wait_for(PENDING, lambda d: d["total"] == 2)
            sent = [d["text"] for d in bus.payloads("/delivery")]
            assert sent == ["Smoke", "Pump stopped"]  # queued again in the order they would leave
            (document,) = [d for d in pending(bus) if d["total"]]
            assert [(m["text"], m["queued_at"]) for m in document["messages"]] == [
                ("Pump stopped", iso(NOW - 30)),
                ("Smoke", iso(NOW - 20)),
            ]

    scenario(stopped)
    scenario(started)


def test_a_message_paused_for_an_alert_stays_sent(tmp_path: Path) -> None:
    """Between two attempts Bob's message goes back to the queue for the alert, without an
    event: its entry still says `sent`, attempt 1, until its next attempt."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)
    config = make_config(
        messages='alert_targets = ["alerts"]', airtime="budget_s_per_hour = 3600\nburst_s = 60"
    )

    async def main() -> None:
        async with running(config, radio, tmp_path) as (_, bus, _n):
            bus.send("bob", "first")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            bus.send("alerts", "Smoke")
            await bus.wait_for("/delivery", lambda d: d["text"] == "Smoke" and d["attempt"] == 1)
            await bus.wait_for(PENDING, lambda d: d["total"] == 0, since=2)
            documents = [steps(d) for d in pending(bus)]
            with_alert = documents.index([("first", "sent", 1), ("Smoke", "queued", 0)])
            assert documents[with_alert + 1 : with_alert + 3] == [
                [("first", "sent", 1)],  # the alert went out while it waited in the queue
                [("first", "sent", 2)],
            ]

    scenario(main)


@pytest.mark.parametrize("holding", ["radio away", "airtime used up"])
def test_a_stuck_message_waits_over_a_minute_while_the_queue_reads_0(
    tmp_path: Path, holding: str
) -> None:
    """#112: the one message there is waits in flight, for the radio or for airtime. The queue
    in `state` never counts it; `pending` lists it as `queued`, accepted more than a minute ago."""
    wall = Clock(NOW)

    async def main() -> None:
        transport = Unreachable if holding == "radio away" else None
        async with running(
            make_config(),
            EmulatedRadio(contacts=[ALICE, BOB]),
            tmp_path,
            transport,
            wall_clock=wall,
        ) as (bridge, bus, _n):
            if holding == "airtime used up":  # about 5 minutes until it may go
                budget = bridge.outbox.budget
                budget._tokens, budget._rate = 0.0, 0.5 / 300
            bus.send("alerts", "Gate open")
            document = await bus.wait_for(PENDING, lambda d: d["total"] == 1)
            await asyncio.sleep(0.1)  # taken for delivery: out of the queue
            assert waiting_over_a_minute(document, wall.now) == 0
            wall.now += 61
            bridge.status.publish()
            state = await bus.wait_for("/state", lambda s: True, since=-1)
            assert state["queue"] == 0
            assert pending(bus)[-1] == document
            assert steps(document) == [("Gate open", "queued", 0)]
            assert waiting_over_a_minute(document, wall.now) == 1

    scenario(main)


def test_a_message_waiting_for_its_acknowledgement_is_no_stuck_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sent and waiting for its acknowledgement for over a minute, as a direct message may for
    each attempt: listed as `sent`, so the waiting count stays 0 while the queue reads 0 too."""
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 30.0)
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MAX_S", 30.0)
    monkeypatch.setattr(outbox_module, "STOP_OUTBOX_S", 0.1)
    wall = Clock(NOW)
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, wall_clock=wall) as (bridge, bus, _n):
            bus.send("bob", "Still there?")
            document = await bus.wait_for(
                PENDING, lambda d: steps(d)[:1] == [("Still there?", "sent", 1)]
            )
            wall.now += 61
            bridge.status.publish()
            state = await bus.wait_for("/state", lambda s: True, since=-1)
            assert state["queue"] == 0
            assert document["messages"][0]["queued_at"] == iso(NOW)
            assert waiting_over_a_minute(document, wall.now) == 0

    scenario(main)
