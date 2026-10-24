"""Bridge service end to end with the emulated radio and an in-memory bus."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway.bridge import Bridge, ha_device_topics
from meshcore_home_gateway.config import Config, ProfileConfig, parse_config
from meshcore_home_gateway.core.airtime import LoRaParams, time_on_air_s
from meshcore_home_gateway.mqtt import manage_entities
from meshcore_home_gateway.mqtt.client import ContactRequest, SendRequest
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices
from meshcore_home_gateway.mqtt.payloads import received_payload
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol.codes import AutoAdd, Command, Push
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import CompanionSession, RadioEvent
from meshcore_home_gateway.radio.transport import Transport
from meshcore_home_gateway.state import (
    AnnouncedComponents,
    CommandLedger,
    ContactDeviceRecords,
    JsonStateFile,
)
from tests.radio_emulator import SELF_KEY, START_TIME, EmulatedContact, EmulatedRadio

NOW = START_TIME + 100
GATEWAY_DOCUMENT = "/meshcore_home_gateway_radio1/config"  # its main discovery document
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
        self.contact_requests: asyncio.Queue[ContactRequest] = asyncio.Queue()
        self.on_connected: list[Callable[[], None]] = []
        self.on_ha_online: list[Callable[[], None]] = []
        self.on_ha_offline: list[Callable[[], None]] = []
        self.on_ha_device: list[Callable[[str, bytes, bool], None]] = []
        self.on_show_hidden: list[Callable[[], None]] = []
        self.published: list[tuple[str, bytes, bool]] = []
        self.qos: dict[str, set[int]] = {}
        self.stopped = False
        self.connected = True

    async def start(self) -> None:
        for callback in self.on_connected:
            callback()

    async def stop(self) -> None:
        self.stopped = True

    def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
        self.published.append((topic, payload, retain))
        self.qos.setdefault(topic, set()).add(qos)

    def send(
        self, alias: str, text: str | bytes, retained: bool = False, *, contact: bool = False
    ) -> None:
        payload = text.encode() if isinstance(text, str) else text
        self.requests.put_nowait(SendRequest(alias, payload, retained, contact=contact))

    def payloads(self, suffix: str) -> list[dict[str, Any]]:
        """Payloads on the topics ending in `suffix`; those of single contacts only if the suffix
        names contacts."""
        return [
            json.loads(p) for t, p, _ in self.published
            if t.endswith(suffix) and ("contact" in suffix or "/contact" not in t)
        ]  # fmt: skip

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
    monotonic: Callable[[], float] = time.monotonic,
    observer: Callable[[FakeBus], Observer] | None = None,
    devices: bool = False,
    profile: RadioProfile | None = None,
) -> AsyncIterator[tuple[Bridge, FakeBus, list[str]]]:
    bus = bus or FakeBus()
    stop, notes = asyncio.Event(), []

    def note(message: str) -> bool:
        notes.append(message)
        return True

    store = JsonStateFile(tmp_path / "state.json")
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    contact_devices = None
    if devices:
        records = ContactDeviceRecords(store)
        contact_devices = ContactDevices(config, topics, bus, records, wall_clock=lambda: NOW)
    bridge = Bridge(
        config, bus, transport or (lambda: radio), CommandLedger(store), session_factory=sessions,
        wall_clock=lambda: NOW, monotonic=monotonic, notifier=note,
        components=AnnouncedComponents(store), observer=observer(bus) if observer else None,
        devices=contact_devices, profile=profile,
        manage_components=AnnouncedComponents(store, "manage_contacts"),
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


FINAL = {"confirmed", "failed", "expired", "rejected", "duplicate"}


class Unreachable:
    """A radio that is not plugged in."""

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
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)  # sent again
            await bus.wait_for("/command", lambda c: c["event_type"] == "arm")

    scenario(main)


def test_copy_of_a_command_refused_for_the_broker_is_no_command(tmp_path: Path) -> None:
    """The radio acknowledged the packet, so the sender does not repeat it: an identical copy is
    a replay. The sender was verified, so its timestamp is stored, also across a restart."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            bus.connected = False
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 30)
            await bus.wait_for(
                "/received", lambda r: r["command"]["reason"] == "broker_unavailable"
            )
            bus.connected = True
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 30)  # replayed copy
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 1)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert len(bus.payloads("/received")) == 2  # the copy was dropped as a duplicate
            assert not bus.payloads("/command")
        restarted = EmulatedRadio(contacts=[ALICE, BOB])
        async with running(make_config(), restarted, tmp_path) as (_, bus, _notes):
            restarted.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 30)  # again
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "replay"
            assert not bus.payloads("/command")

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
    monkeypatch.setattr(bridge_module, "REFUSED_REPORT_INTERVAL_S", 0.2)
    caplog.set_level(logging.WARNING)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            for _request in range(5000):
                bus.send("nobody", "x")
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
            assert bridge.state_payload()["airtime_1h_s"] <= 5.0

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


def test_last_attempt_waits_for_its_acknowledgement_past_the_time_to_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No attempt starts after `queue_ttl_s`, but the last one still waits for its acknowledgement:
    one that arrives after the time to live confirms the message."""
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MIN_S", 2.0)
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MAX_S", 2.0)
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
                "failed", "exceeds_airtime_burst", 1,
            )  # fmt: skip
            assert radio.path_resets == []

    scenario(main)


def test_message_waits_for_airtime_then_is_sent(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            bridge._budget._tokens, bridge._budget._rate = 0.0, 2.0  # about 0.3 s to wait
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
            bridge._budget._tokens, bridge._budget._rate = 0.0, 1e-6  # nothing for attempt 2
            event = await bus.wait_for("/delivery", lambda d: d["event_type"] in FINAL)
            assert (event["event_type"], event["reason"], event["attempt"]) == (
                "failed", "no_acknowledgement", 1,
            )  # fmt: skip
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
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MIN_S", 0.3)
    monkeypatch.setattr(bridge_module, "ACK_TIMEOUT_MAX_S", 0.3)
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
    plan = Bridge._plan
    failures = {"plan": 1, "payload": 1}

    def plan_once(self: Bridge, *args: Any) -> Any:
        if failures["plan"]:
            failures["plan"] -= 1
            raise KeyError("bug in planning")
        return plan(self, *args)

    def payload_once(*args: Any) -> Any:
        if failures["payload"]:
            failures["payload"] -= 1
            raise KeyError("bug in the payload")
        return received_payload(*args)

    monkeypatch.setattr(Bridge, "_plan", plan_once)
    monkeypatch.setattr(bridge_module, "received_payload", payload_once)

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
        def publish(self, topic: str, payload: bytes, retain: bool = False, qos: int = 1) -> None:
            if topic.endswith("/command"):
                stored = json.loads(state.read_text(encoding="utf-8"))
                stored_at_publish.append(stored["command_timestamps"])
            super().publish(topic, payload, retain, qos)

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


def test_copy_of_a_command_rejected_at_connect_is_no_command(tmp_path: Path) -> None:
    """The radio acknowledged the packet when the bridge fetched it, so the sender does not
    repeat it: an identical copy is a replay and is dropped as a duplicate (ADR 0009)."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 30, push=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            await bus.wait_for("/received", lambda r: r["command"]["reason"] == "sender_unverified")
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 30)  # replayed copy
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 1)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert len(bus.payloads("/received")) == 2
            assert not bus.payloads("/command")

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
            count = len(bus.payloads("/state"))
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected", since=count)
            count = len(bus.payloads("/state"))
            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=count)
            radio.queue_direct_message(ALICE.public_key[:6], "arm now", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["command"]["reason"] == "sender_ambiguous"

    scenario(main)


def test_overwritten_look_alike_still_makes_the_sender_ambiguous(tmp_path: Path) -> None:
    """With a full table the radio overwrites the oldest contact and says so with a push; the
    look-alike that sent the message may be gone before the bridge reads the table."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.contacts.append(TWIN)  # added without a push: no read of the table shows it
            radio.queue_direct_message(TWIN.public_key[:6], "arm now", NOW - 5, push=False)
            radio.overwrite(TWIN)  # CONTACT_DELETED push, the only trace of it, then ...
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


def test_look_alike_counts_for_a_day_after_it_was_last_seen(tmp_path: Path) -> None:
    """A node the radio did not add (NEW_ADVERT) renews a look-alike that is still remembered,
    but does not bring back one that is forgotten (ADR 0009)."""
    clock, day = [1000.0], bridge_module.LOOK_ALIKE_MEMORY_S
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    not_added = bytes([Push.NEW_ADVERT]) + TWIN.public_key + bytes(115)

    def now() -> float:
        return clock[0]

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, monotonic=now) as (_, bus, _notes):
            radio.emit(bytes([Push.ADVERT]) + TWIN.public_key)  # a contact of the radio, once
            await asyncio.sleep(0.05)
            clock[0] += day - 10
            radio.emit(not_added)  # renews it
            await asyncio.sleep(0.05)
            clock[0] += 20  # a day after the first sighting, not after the renewal
            radio.queue_direct_message(ALICE.public_key[:6], "arm one", NOW - 9)
            first = await bus.wait_for("/received", lambda r: True)
            assert first["command"]["reason"] == "sender_ambiguous"
            clock[0] += day  # forgotten
            radio.emit(not_added)  # renews nothing
            await asyncio.sleep(0.05)
            radio.queue_direct_message(ALICE.public_key[:6], "arm two", NOW - 5)
            second = await bus.wait_for("/received", lambda r: True, since=1)
            assert second["command"] == {"word": "arm", "accepted": True, "reason": None}

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
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 4)  # sent again
            await bus.wait_for("/command", lambda c: True)  # the service is still there

    scenario(main)


def test_a_command_sent_anew_after_an_unverified_one_is_judged_again(tmp_path: Path) -> None:
    """The identical copy (a retry or a replay, which look the same) stays a duplicate."""
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.errors[Command.GET_CONTACTS] = 1
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)
            await bus.wait_for("/received", lambda r: r["command"]["reason"] == "sender_unverified")
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5)  # identical
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 4)  # sent anew
            await bus.wait_for("/command", lambda c: True)
            assert len(bus.payloads("/received")) == 2  # the identical copy gave no event

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


FORGED = "\u202e\nERROR x\x1b[31m\u2028"  # direction override, line breaks, terminal escape


def test_names_from_the_mesh_cannot_forge_log_lines(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Contact names come from the mesh: quoted in the log, they cannot start a line of their
    own, turn a line around or send escapes to a terminal."""
    caplog.set_level(logging.INFO)
    eve = EmulatedContact(bytes.fromhex("e5" * 32), "Eve" + FORGED)
    radio = EmulatedRadio(contacts=[ALICE, eve])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(eve.public_key[:6], "hello", NOW - 5)
            received = await bus.wait_for("/received", lambda r: True)
            assert received["sender_name"] == "Eve" + FORGED  # the event has the name as it is

    scenario(main)
    assert "message from 'Eve\\u202e\\nERROR x\\x1b[31m\\u2028'" in caplog.text
    assert not any(ch in caplog.text for ch in "\u202e\x1b\u2028")
    assert "\nERROR x" not in caplog.text


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


def test_messages_taken_from_the_radio_are_published_before_stopping(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """On stop the radio goes first; what it has handed over is still published. A command that
    waits for its table read is rejected, as the read can no longer happen."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    bus = FakeBus()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus):
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.silent.add(Command.GET_CONTACTS)  # the command waits for its table read
            radio.queue_direct_message(ALICE.public_key[:6], "arm", NOW - 5, push=False)
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 4, push=False)
            radio.emit(bytes([Push.MSG_WAITING]))
            for _attempt in range(100):
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break  # both messages are out of the radio, and the command waits
                await asyncio.sleep(0.01)
            assert not radio.queue
        # the service stops here

    scenario(main)
    received = bus.payloads("/received")
    assert [r["text"] for r in received] == ["arm", "hello"]
    assert received[0]["command"]["reason"] == "sender_unverified"
    assert "cannot be verified: session closed" in caplog.text  # not "no answer"
    assert not bus.payloads("/command")


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
            await asyncio.sleep(0.05)  # the gateway's document and that of its contact controls
            assert sum(t.endswith("/config") for t, _, _ in bus.published) == before + 2
            assert all(retain for t, _, retain in bus.published if t.endswith("/state"))
            assert not any(retain for t, _, retain in bus.published if t.endswith("/delivery"))
        assert notes.index("READY=1") < notes.index("STOPPING=1")
        assert any(note.startswith("STATUS=radio connected") for note in notes)
        assert bus.stopped
        assert bus.payloads("/state")[-1]["radio"] == "disconnected"

    scenario(main)


def test_watchdog_is_pinged_at_half_its_interval_and_the_state_repeated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "watchdog_interval_s", lambda: 0.1)
    monkeypatch.setattr(bridge_module, "STATUS_INTERVAL_S", 0.15)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, notes):
            pings, states = notes.count("WATCHDOG=1"), len(bus.payloads("/state"))
            await asyncio.sleep(0.6)
            assert notes.count("WATCHDOG=1") - pings >= 9  # every 0.05 s
            assert len(bus.payloads("/state")) - states >= 3  # every 0.15 s

    scenario(main)


def test_radio_reconnects_after_unplugging(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            count = len(bus.payloads("/state"))  # the first document says disconnected too
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected", since=count)
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


def test_receive_log_is_not_published_by_default(tmp_path: Path) -> None:
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.emit(bytes([Push.LOG_RX_DATA, 0x1F, 0x9F]) + b"\x0d\x00\x01")
            radio.queue_channel_message(0, "Carol: hi", NOW)  # handled after the packet log
            await bus.wait_for("/received", lambda r: True)
            assert not bus.payloads("/rx")

    scenario(main)


@pytest.mark.parametrize("set_clock", [True, False])
def test_radio_clock_is_set_only_when_configured(tmp_path: Path, set_clock: bool) -> None:
    radio = EmulatedRadio()  # its clock is 100 s behind the bridge's
    section = f'transport = "tcp"\nhost = "192.0.2.10"\nset_clock = {str(set_clock).lower()}'

    async def main() -> None:
        async with running(make_config(radio=section), radio, tmp_path):
            pass

    scenario(main)
    assert (Command.SET_DEVICE_TIME in radio.commands()) is set_clock


def test_discovery_follows_the_radio_identity(tmp_path: Path) -> None:
    """The device page shows the radio's model and firmware once they are known."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            documents = bus.payloads(GATEWAY_DOCUMENT)
            assert "mdl" not in documents[0]["dev"]  # announced on connect, radio not yet known
            assert documents[-1]["dev"]["mdl"] == "Example Radio"
            assert documents[-1]["dev"]["sn"] == SELF_KEY.hex()
            count, states = len(documents), len(bus.payloads("/state"))
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected", since=states)
            states = len(bus.payloads("/state"))
            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=states)
            assert len(bus.payloads(GATEWAY_DOCUMENT)) == count  # the same radio again
            assert not any(retain for t, _, retain in bus.published if t.endswith("/config"))

    scenario(main)


def test_discovery_can_be_switched_off(tmp_path: Path) -> None:
    config = make_config(extra="[homeassistant]\ndiscovery = false")

    async def main() -> None:
        async with running(config, EmulatedRadio(), tmp_path) as (_, bus, _notes):
            for callback in bus.on_ha_online:  # Home Assistant announces itself
                callback()
            await asyncio.sleep(0.05)
            assert not bus.payloads("/config")  # neither the gateway's nor any other
            assert bus.payloads("/state")[-1]["radio"] == "connected"

    scenario(main)


def test_components_of_an_earlier_configuration_are_removed(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({
        "ha_components": {"send_old": "notify", "radio": "binary_sensor"},
        "command_timestamps": {"a1" * 32: 5},
    })  # fmt: skip

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            documents = bus.payloads(GATEWAY_DOCUMENT)
            assert len(documents) % 2 == 0  # each announcement: the placeholder, then removal
            for bind, document in zip(documents[::2], documents[1::2], strict=True):
                assert bind["cmps"]["send_old"]["name"] == "Removed"  # enabled: see placeholder()
                assert document["cmps"]["send_old"] == {"p": "notify"}
                assert document["cmps"]["radio"]["p"] == "binary_sensor"

    scenario(main)
    data = store.load()
    assert "send_old" not in data["ha_components"]
    assert data["ha_components"]["radio"] == "binary_sensor"
    assert data["ha_removed"]["send_old"]["p"] == "notify"  # pending, in case nobody got it
    assert data["command_timestamps"] == {"a1" * 32: 5}


def test_command_events_are_never_delivered_late(tmp_path: Path) -> None:
    """QoS 0: a command event that cannot be delivered at once (a broker loss that paho has not
    noticed yet) is lost, not resent after the reconnect to trigger automations at any time."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "status", NOW - 5)
            await bus.wait_for("/command", lambda c: True)
            assert bus.qos["meshcore/radio1/command"] == {0}
            assert bus.qos["meshcore/radio1/received"] == {1}

    scenario(main)


class OfflineBus(FakeBus):
    """A broker that is never reached in this run."""

    def __init__(self) -> None:
        super().__init__()
        self.connected = False

    async def start(self) -> None:
        pass


def test_a_removal_survives_a_run_that_never_reached_the_broker(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"ha_components": {"send_old": "notify", "radio": "binary_sensor"}})

    async def offline_run() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path, bus=OfflineBus()):
            pass

    async def online_run() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            documents = bus.payloads(GATEWAY_DOCUMENT)
            removing = [d for d in documents if d["cmps"].get("send_old") == {"p": "notify"}]
            assert removing  # still sent: the first run reached nobody

    scenario(offline_run)
    scenario(online_run)


def test_a_removal_binds_the_component_first(tmp_path: Path) -> None:
    """Home Assistant removes only components it has discovered since its own start, so each
    removal is preceded by a disabled placeholder under the same unique_id."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"ha_components": {"send_old": "notify", "command": "event"}})

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            documents = bus.payloads(GATEWAY_DOCUMENT)
            first, second = documents[0]["cmps"]["send_old"], documents[1]["cmps"]["send_old"]
            assert first["name"] == "Removed"
            assert "enabled_by_default" not in first  # Home Assistant would clear the device
            assert first["unique_id"] == "meshcore_home_gateway_radio1_send_old"
            assert second == {"p": "notify"}
            assert documents[0]["cmps"]["radio"] == documents[1]["cmps"]["radio"]

    scenario(main)


def test_a_look_alike_added_during_the_airtime_wait_does_not_get_the_message(
    tmp_path: Path,
) -> None:
    """The target is resolved again after waiting for airtime, which can take minutes."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _notes):
            bridge._budget._tokens, bridge._budget._rate = 0.0, 1.0  # waits about its airtime
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


class InFlightRadio(EmulatedRadio):
    """Takes a message out of its queue when SYNC_NEXT_MESSAGE arrives, as the firmware does; the
    answer reaches the host a few milliseconds later (the transfer time)."""

    armed = False

    def __post_init__(self) -> None:
        super().__post_init__()
        self.synced = asyncio.Event()

    def _handle(self, p: bytes) -> None:
        if self.armed and p[0] == Command.SYNC_NEXT_MESSAGE and self.queue:
            self.received.append(p)
            asyncio.get_running_loop().call_later(0.005, self.emit, self.queue.pop(0))
            self.synced.set()
            return
        super()._handle(p)


def test_a_message_whose_answer_is_on_its_way_at_stop_is_published(tmp_path: Path) -> None:
    """The radio has already taken it out of its queue: lost if the stop does not wait for it."""
    radio = InFlightRadio(contacts=[ALICE, BOB])
    bus = FakeBus()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus):
            radio.armed = True
            radio.queue_direct_message(BOB.public_key[:6], "hello", NOW - 4)
            await asyncio.wait_for(radio.synced.wait(), 3)
            assert not radio.queue  # the stop begins while the answer is on its way

    scenario(main)
    assert [r["text"] for r in bus.payloads("/received")] == ["hello"]


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


@pytest.mark.parametrize("at_connect", [True, False], ids=["at-connect", "table-read-failed"])
def test_a_rejected_command_cannot_be_replayed_while_its_age_window_lasts(
    tmp_path: Path, at_connect: bool
) -> None:
    """With max_age_s = 3600 and the sender's clock 3000 s ahead, a copy passes the age check
    until 6600 s after the original: the duplicate memory must last that long."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    sent = NOW + 3000
    if at_connect:
        radio.queue_direct_message(ALICE.public_key[:6], "arm", sent, push=False)
    offset = [0.0]
    config = make_config()
    config = dataclasses.replace(
        config, commands=dataclasses.replace(config.commands, max_age_s=3600.0)
    )
    bus = FakeBus()
    store = JsonStateFile(tmp_path / "state.json")

    async def main() -> None:
        stop = asyncio.Event()
        bridge = Bridge(
            config, bus, lambda: radio, CommandLedger(store), session_factory=fast_sessions,
            wall_clock=lambda: NOW + offset[0], monotonic=lambda: time.monotonic() + offset[0],
            notifier=lambda _m: True,
        )  # fmt: skip
        task = asyncio.create_task(bridge.run(stop))
        try:
            if not at_connect:
                await bus.wait_for("/state", lambda s: s["radio"] == "connected")
                radio.errors[Command.GET_CONTACTS] = 1
                radio.queue_direct_message(ALICE.public_key[:6], "arm", sent)
            first = await bus.wait_for("/received", lambda r: True)
            assert first["command"]["reason"] == "sender_unverified"
            offset[0] = 3601.0  # an hour later, no restart
            radio.queue_direct_message(ALICE.public_key[:6], "arm", sent)  # the replayed copy
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW + 3601)
            await bus.wait_for("/received", lambda r: r["text"] == "hello")
            assert not bus.payloads("/command")
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

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
                "Carol", key,
            )  # fmt: skip
            assert radio.texts[0].prefix == carol.public_key[:6]

    scenario(main)


def test_messages_of_a_contact_also_go_to_its_own_topic(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            radio.queue_direct_message(ALICE.public_key[:6], "hello", NOW - 5)
            topic = f"radio1/contacts/{ALICE.public_key.hex()}/received"
            event = await bus.wait_for(topic, lambda e: e["text"] == "hello")
            assert event == bus.payloads("radio1/received")[-1]
            radio.queue_channel_message(0, "Bob: hi all", NOW - 4)
            await bus.wait_for("radio1/received", lambda e: e["event_type"] == "channel")
            assert len(bus.payloads(topic)) == 1  # channel messages are no contact's

    scenario(main)


def test_contacts_get_their_devices_in_home_assistant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR 0017: a contact added to the radio gets its device, one removed loses it, one the user
    deleted in Home Assistant stays hidden until shown again."""
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.01)
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()

    def device_topic(key: str) -> str:
        return f"homeassistant/device/meshcore_home_gateway_radio1/contact_{key}/config"

    def sent(key: str) -> list[bytes]:
        return [p for t, p, _ in bus.published if t == device_topic(key)]

    def gateway_documents() -> list[bytes]:
        own = "homeassistant/device/meshcore_home_gateway_radio1/config"
        return [p for t, p, _ in bus.published if t == own]

    async def until(check: Callable[[], bool]) -> None:
        async def poll() -> None:
            while True:
                if check():
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 3)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus, devices=True) as (b, _, _n):
            await until(lambda: bool(sent(alice) and sent(bob)))
            assert json.loads(sent(alice)[-1])["dev"]["name"] == "Alice"
            gateway = bus.payloads(GATEWAY_DOCUMENT)[-1]["cmps"]
            assert gateway["show_hidden"]["command_topic"].endswith("/homeassistant/show_hidden")

            radio.contacts.append(carol)  # added
            radio.emit(bytes([Push.NEW_ADVERT]) + carol.public_key)
            await until(lambda: bool(sent(carol.public_key.hex())))

            radio.contacts.remove(BOB)  # removed: placeholders, then the empty document
            radio.emit(bytes([Push.NEW_ADVERT]) + ALICE.public_key)
            await until(lambda: sent(bob)[-1:] == [b""])
            assert json.loads(sent(bob)[-2])["cmps"]["send"]["name"] == "Removed"

            for deleted in bus.on_ha_device:  # the user deletes Alice's device
                deleted(device_topic(alice), b"", False)
            hidden = await bus.wait_for("radio1/contact", lambda e: e["event_type"] == "hidden")
            assert (hidden["key"], hidden["name"]) == (alice, "Alice")
            count, gateway = len(sent(alice)), len(gateway_documents())
            for birth in bus.on_ha_online:  # Home Assistant starts: Alice stays away
                birth()
            await until(lambda: len(gateway_documents()) > gateway)
            await asyncio.sleep(0.05)
            assert len(sent(alice)) == count
            assert sent(bob)[-2:] == [sent(bob)[-4], b""]  # the removal once more: it may
            # not have been running when it was sent first

            for show in bus.on_show_hidden:
                show()
            await until(lambda: len(sent(alice)) == count + 1)
            assert b.observer.mesh.contacts.keys() == {alice, carol.public_key.hex()}

    bus = FakeBus()
    scenario(main)


def test_a_device_home_assistant_clears_is_announced_again(tmp_path: Path) -> None:
    """Home Assistant clears the gateway's discovery topic when it removes a disabled entity
    (or the user deletes the device): the entities come back with an announcement."""
    own = "homeassistant/device/meshcore_home_gateway_radio1/config"

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            count = len([p for t, p, _ in bus.published if t == own])
            for message in bus.on_ha_device:
                message(own, b"", True)  # a left-over: not live
                message(own, b"{}", False)
            await asyncio.sleep(0.05)
            assert len([p for t, p, _ in bus.published if t == own]) == count
            for message in bus.on_ha_device:  # several at once, as Home Assistant sends them
                for _ in range(3):
                    message(own, b"", False)
            await bus.wait_for("/config", lambda d: True, since=count)
            await asyncio.sleep(0.05)
            assert len([p for t, p, _ in bus.published if t == own]) == count + 1

    scenario(main)


@pytest.mark.parametrize("devices", [True, False])
def test_the_controls_that_add_contacts_come_back_when_cleared(
    tmp_path: Path, devices: bool
) -> None:
    """Their own discovery topic, below the gateway's device: Home Assistant clears it with the
    gateway's when the user deletes the device. The contact devices are left alone."""
    own = "homeassistant/device/meshcore_home_gateway_radio1/config"
    controls = "homeassistant/device/meshcore_home_gateway_radio1/manage_contacts/config"
    radio = EmulatedRadio(contacts=[ALICE])

    def sent(topic: str) -> int:
        return len([p for t, p, _ in bus.published if t == topic])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus, devices=devices) as (_, _b, _n):
            count = sent(own)
            assert sent(controls) == count  # announced with the gateway's document
            document = json.loads(next(p for t, p, _ in bus.published if t == controls))
            assert set(document["cmps"]) == {"add_node", "add_contact"}
            for message in bus.on_ha_device:
                message(controls, b"", True)  # a left-over: not live
                message(controls, b"{}", False)  # the gateway's own, back
            await asyncio.sleep(0.05)
            assert (sent(own), sent(controls)) == (count, count)
            for message in bus.on_ha_device:
                message(controls, b"", False)
            await bus.wait_for("/manage_contacts/config", lambda d: True, since=count)
            await asyncio.sleep(0.05)
            assert (sent(own), sent(controls)) == (count + 1, count + 1)
            assert not bus.payloads("radio1/contact")  # no contact device was taken as deleted

    bus = FakeBus()
    scenario(main)


def test_a_control_of_an_earlier_version_is_removed_by_the_service(tmp_path: Path) -> None:
    """The controls' document keeps its own record in the state file (state.py): placeholders
    first, then the document without the component, at every announcement of the run."""
    store = JsonStateFile(tmp_path / "state.json")
    store.save({"ha_components_manage_contacts": {"add_node": "select", "add_group": "select"},
                "ha_components": {"radio": "binary_sensor"}})  # fmt: skip

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _n):
            documents = bus.payloads("/manage_contacts/config")
            assert len(documents) % 2 == 0
            for bind, document in zip(documents[::2], documents[1::2], strict=True):
                assert bind["cmps"]["add_group"]["name"] == "Removed"
                assert document["cmps"]["add_group"] == {"p": "select"}
            gateway = bus.payloads(GATEWAY_DOCUMENT)[-1]["cmps"]
            assert "add_node" not in gateway  # the main document takes nothing of the other

    scenario(main)
    data = store.load()
    assert data["ha_components_manage_contacts"] == {"add_node": "select", "add_contact": "text"}
    assert "add_group" in data["ha_removed_manage_contacts"]
    assert "add_node" not in data["ha_components"]


def test_the_select_leaves_out_a_look_alike_of_a_protected_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Alice is a command sender, but not on this radio: a node with her first 6 bytes would be
    refused, so it is not offered."""
    monkeypatch.setattr(manage_entities, "OPTIONS_INTERVAL_S", 0.05)
    radio = EmulatedRadio(contacts=[BOB])
    dave = EmulatedContact(bytes.fromhex("d4" * 32), "Dave", last_advert=int(NOW) - 5)

    def labels(document: dict[str, Any]) -> list[str]:
        options: list[str] = document["cmps"]["add_node"]["options"]
        return options

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            radio.advertise(TWIN)
            radio.advertise(dave)
            await bus.wait_for("/manage_contacts/config",
                               lambda d: any(o.endswith(" Dave") for o in labels(d)))  # fmt: skip
            nodes = bus.payloads("radio1/nodes")[-1]
            assert TWIN.public_key.hex() in nodes["look_alike"]
            assert not any(
                "Mallory" in o for o in labels(bus.payloads("/manage_contacts/config")[-1])
            )

    scenario(main)


def test_the_service_watches_the_discovery_topics_it_announces() -> None:
    own = "homeassistant/device/meshcore_home_gateway_radio1/config"
    assert ha_device_topics(make_config()) == [
        own, "homeassistant/device/meshcore_home_gateway_radio1/+/config",
    ]  # fmt: skip
    without = make_config(extra="[homeassistant]\ncontact_devices = 0")
    assert ha_device_topics(without) == [
        own, "homeassistant/device/meshcore_home_gateway_radio1/manage_contacts/config",
    ]  # fmt: skip
    assert ha_device_topics(make_config(extra="[homeassistant]\ndiscovery = false")) == []


def test_the_profile_of_a_connect_that_fails_later_is_reported_once(tmp_path: Path) -> None:
    """The radio goes away after the profile wrote (at the channel read): the next connect
    publishes that connect's report first, with its events once, then its own."""
    radio = EmulatedRadio(manual_add=False, tuning=(0, 1000), drop_on={Command.GET_CHANNEL})
    profile = RadioProfile(ProfileConfig(), protected={"e5" * 32: "operator"})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, profile=profile) as (_, bus, _n):
            await bus.wait_for("radio1/settings", lambda d: d["status"] == "ok")
            statuses = [d["status"] for d in bus.payloads("radio1/settings")]
            assert statuses == ["written", "ok"]
            first = bus.payloads("radio1/settings")[0]
            assert first["protected_contacts"]["added"] == ["e5" * 32]
            events = [(e["event_type"], e["setting"]) for e in bus.payloads("radio1/setting")]
            assert events == [("written", "manual_add"), ("written", "duty_cycle_percent")]

    scenario(main)


def test_the_radio_becomes_an_observer_and_reports_its_settings(tmp_path: Path) -> None:
    """ADR 0020: written before the first table read, reported in `settings` and as events."""
    radio = EmulatedRadio(manual_add=False, autoadd_config=0x1F, tuning=(0, 1000))
    profile = RadioProfile(ProfileConfig())

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, profile=profile) as (_, bus, _n):
            document = await bus.wait_for("radio1/settings", lambda d: True)
            assert document["status"] == "written"
            assert document["settings"]["duty_cycle_percent"] == {
                "live": 10.0, "desired": 10.0, "state": "written",
            }  # fmt: skip
            events = {e["setting"]: e for e in bus.payloads("radio1/setting")}
            assert set(events) == {"manual_add", "autoadd", "duty_cycle_percent"}
            assert events["autoadd"] == {
                "event_type": "written", "setting": "autoadd", "reason": "initial",
                "live": [], "desired": [],
            }  # fmt: skip
            assert any(r for t, _, r in bus.published if t.endswith("radio1/settings"))
            setters = [c for c in radio.commands() if c in (38, 58, 21)]
            first_read = radio.commands().index(4)  # GET_CONTACTS
            assert setters
            assert all(radio.commands().index(c) < first_read for c in setters)

    scenario(main)
