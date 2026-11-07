"""Bridge service end to end with the emulated radio and an in-memory bus."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import outbox as outbox_module
from meshcore_home_gateway.config import ProfileConfig, parse_config
from meshcore_home_gateway.core import telemetry
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.ports import ContactRequest, RadioEvent, Transport
from meshcore_home_gateway.protocol.codec import expected_ack
from meshcore_home_gateway.protocol.codes import Command, Push
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.radio.profile import RadioProfile
from meshcore_home_gateway.radio.session import CompanionSession, default_session_factory
from meshcore_home_gateway.radio.transport import TcpTransport
from meshcore_home_gateway.state import (
    EXCLUSIONS_FILE,
    CommandLedger,
    JsonStateFile,
    TelemetryExclusions,
    TelemetryRecords,
)
from meshcore_home_gateway.telemetry_poller import TelemetryPoller
from meshcore_home_gateway.wiring import gateway
from tests.bridge_harness import (
    ALICE,
    BOB,
    NOW,
    FakeBus,
    fast_sessions,
    fast_timers,  # noqa: F401 - autouse
    make_config,
    running,
    scenario,
    until,
)
from tests.radio_emulator import SELF_KEY, EmulatedContact, EmulatedRadio, TcpRadioServer


@pytest.mark.parametrize(("transport", "warned"), [("tcp", True), ("serial", False)])
def test_commands_over_tcp_are_warned_about(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, transport: str, warned: bool
) -> None:
    """Over TCP the radio drops pushes when its send queue is full (ADR 0009)."""
    radio_section = (
        'transport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"'
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


def test_adverts_over_tcp_are_checked_as_over_usb(tmp_path: Path) -> None:
    """Over TCP the radio queues an advert's log and its push in one pass of its loop
    (SerialWifiInterface.cpp:21-40, MyMesh.cpp:2388-2421): a companion's advert, and the advert
    of a node heard and then added, are checked as over USB, and command 17 is never sent
    (ADR 0025)."""
    position = (50_123_456, 8_654_321)
    dave = EmulatedContact(bytes.fromhex("d4" * 32), "Dave", last_advert=int(NOW) - 30)
    alice, dave_key = ALICE.public_key.hex(), dave.public_key.hex()
    radio = EmulatedRadio(contacts=[ALICE])

    def shares(key: str) -> Callable[[dict[str, Any]], bool]:
        return lambda e: (e["event_type"], e["key"]) == ("position_in_advert", key)

    async def main(server: TcpRadioServer) -> None:
        def transport() -> Transport:
            return TcpTransport("127.0.0.1", server.port)

        async with running(make_config(), radio, tmp_path, transport) as (_, bus, _notes):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            server.call(lambda: radio.send_advert(ALICE, position=position))
            await bus.wait_for("/contact", shares(alice))
            server.call(lambda: radio.send_advert(dave, position=position))
            await bus.wait_for("/nodes", lambda d: [n["key"] for n in d["nodes"]] == [dave_key])
            request = json.dumps({"contact": dave_key, "id": "t1"}).encode()
            bus.contact_requests.put_nowait(ContactRequest("add", request, False))
            await bus.wait_for("/contact", shares(dave_key))
            for key in (alice, dave_key):
                document = bus.payloads(f"/contacts/{key}")[-1]
                assert document["advert_carries_position"] is True
                assert document["advert_checked_at"] is not None

    with TcpRadioServer(radio) as server:
        scenario(lambda: main(server))
    assert 17 not in radio.commands()  # EXPORT_CONTACT (ADR 0025)


def test_telemetry_is_on_by_default(tmp_path: Path) -> None:
    """The shipped default (ADR 0018): a configuration without [telemetry] asks the companions.
    The poller gives each companion's document its state and hears of every exclusion and every
    contact added."""
    plain = parse_config(
        '[radio]\ntransport = "tcp"\ntcp_unauthenticated = true\n'
        'host = "192.0.2.10"\n[mqtt]\nhost = "h"\nnode_id = "n"\n'
    )
    assert plain.telemetry.enabled is True
    store = JsonStateFile(tmp_path / "state.json")
    bridge = gateway(
        plain,
        FakeBus(),
        EmulatedRadio,
        CommandLedger(store),
        session_factory=default_session_factory,
    )
    poller = bridge._telemetry
    assert poller is not None
    assert bridge.observer.telemetry_state == poller.state_of
    assert bridge.observer.on_telemetry_exclusion == [poller.exclusion_changed]
    assert bridge.observer.on_contact_added == [poller.contact_added]
    off = gateway(
        make_config(),
        FakeBus(),
        EmulatedRadio,
        CommandLedger(store),
        session_factory=default_session_factory,
    )
    assert off._telemetry is None
    assert (off.observer.telemetry_state, off.observer.on_telemetry_exclusion) == (None, [])
    assert off.observer.on_contact_added == []


def test_with_telemetry_off_an_exclusion_or_inclusion_forgets_the_saved_answers(
    tmp_path: Path,
) -> None:
    """Also when telemetry is switched on again later: the saved schedule no longer knows
    the answers of a contact excluded, included or added again while telemetry was off."""
    store = JsonStateFile(tmp_path / "state.json")
    records = TelemetryRecords(store)
    key = "d4" * 32
    records.save(telemetry.Saved({key: telemetry.SavedContact(812.5, 0, 95.0)}), NOW)
    off = gateway(
        make_config(),
        FakeBus(),
        EmulatedRadio,
        CommandLedger(store),
        session_factory=default_session_factory,
        telemetry_records=records,
    )
    off.observer.telemetry_excluded(key, False)  # included again, as the contact manager does
    saved, _ = records.load(NOW)
    assert saved is not None
    assert saved.contacts[key].answered_age_s is None
    records.save(telemetry.Saved({key: telemetry.SavedContact(812.5, 0, 95.0)}), NOW)
    for added in off.observer.on_contact_added:  # the radio's table added it again
        added(key)
    saved, _ = records.load(NOW)
    assert saved is not None
    assert saved.contacts[key].answered_age_s is None


def test_the_saved_schedule_is_restored_before_anything_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Before the bus starts: the first documents already carry the restored states."""
    order: list[str] = []
    restore = TelemetryPoller.restore

    def restoring(poller: TelemetryPoller) -> None:
        order.append("restore")
        restore(poller)

    class Bus(FakeBus):
        async def start(self) -> None:
            order.append("bus")
            await super().start()

    monkeypatch.setattr(TelemetryPoller, "restore", restoring)

    async def main() -> None:
        async with running(make_config(telemetry=True), EmulatedRadio(), tmp_path, bus=Bus()):
            assert order[:2] == ["restore", "bus"]

    scenario(main)


def test_an_exclusion_not_stored_is_written_at_the_next_status_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge_module, "STATUS_INTERVAL_S", 0.05)
    path = tmp_path / EXCLUSIONS_FILE
    save = JsonStateFile.save
    full = [True]

    def failing(store: JsonStateFile, data: dict[str, Any]) -> None:
        if store.path == path and full[0]:
            raise OSError(28, "No space left on device")
        save(store, data)

    monkeypatch.setattr(JsonStateFile, "save", failing)

    def observer(bus: FakeBus) -> Observer:
        topics = Topics("meshcore", "radio1")
        return Observer(bus, topics, exclusions=TelemetryExclusions(path), wall_clock=lambda: NOW)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path, observer=observer) as (
            bridge,
            _bus,
            _notes,
        ):
            exclusions = bridge.observer.exclusions
            assert not exclusions.exclude(ALICE.public_key.hex())
            await asyncio.sleep(0.15)
            assert exclusions.pending
            full[0] = False
            await until(path.exists)
            assert json.loads(path.read_text())["excluded"] == [ALICE.public_key.hex()]
            assert not exclusions.pending

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
    section = (
        'transport = "tcp"\ntcp_unauthenticated = true\n'
        f'host = "192.0.2.10"\nset_clock = {str(set_clock).lower()}'
    )

    async def main() -> None:
        async with running(make_config(radio=section), radio, tmp_path) as (_, bus, _n):
            # the state shows how far the radio's clock is off (SEC-002)
            state = await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            assert state["radio_clock_offset_s"] == (0 if set_clock else -100)

    scenario(main)
    assert (Command.SET_DEVICE_TIME in radio.commands()) is set_clock


def test_the_step_limit_of_the_radio_clock_comes_from_the_configuration(tmp_path: Path) -> None:
    radio = EmulatedRadio(clock=int(NOW) - 2 * 86_400)  # two days behind: off for a while
    section = (
        'transport = "tcp"\ntcp_unauthenticated = true\nhost = "192.0.2.10"\nset_clock_max_days = 1'
    )

    async def main() -> None:
        async with running(make_config(radio=section), radio, tmp_path) as (_, bus, _n):
            state = await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            assert state["radio_clock_offset_s"] == -2 * 86_400

    scenario(main)
    assert Command.SET_DEVICE_TIME not in radio.commands()


class KeepsItsClock(EmulatedRadio):
    """A radio whose restart does not set its clock back."""

    def _handle(self, p: bytes) -> None:
        clock = self.clock
        super()._handle(p)
        if p[0] == Command.REBOOT:
            self.clock = clock


def test_the_radio_is_restarted_for_its_clock_at_most_once_a_day(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A radio clock ahead of the host's is set back by a restart (test_session.py); one that
    stays ahead after it is not restarted again within the day."""
    monkeypatch.setattr(bridge_module, "RESTART_WAIT_S", 0.02)
    radio = KeepsItsClock(clock=int(NOW) + 300, contacts=[ALICE])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            state = await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            assert state["radio_clock_offset_s"] == 300

    scenario(main)
    assert radio.reboots == 1


def test_the_radio_is_not_restarted_for_its_clock_while_a_message_is_delivered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2.0.0rc3: a restart drops what the radio holds, the acknowledgement a delivery waits for
    included. A radio clock that runs ahead while a message waits for it is set back only once
    the delivery ended."""
    monkeypatch.setattr(bridge_module, "RESTART_WAIT_S", 0.02)
    monkeypatch.setattr(session_module, "HEARTBEAT_INTERVAL_S", 0.02)  # the clock read often
    monkeypatch.setattr(outbox_module, "ACK_TIMEOUT_MIN_S", 30.0)  # waits until acknowledged
    radio = EmulatedRadio(contacts=[ALICE, BOB], auto_ack=False)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (bridge, bus, _n):
            await bus.wait_for("/state", lambda s: s["radio"] == "connected")
            bus.send("alice", "door open")
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "sent")
            radio.clock = int(NOW) + 300  # ahead now, with the acknowledgement still to come
            reads = radio.commands().count(Command.GET_DEVICE_TIME)
            await until(lambda: radio.commands().count(Command.GET_DEVICE_TIME) >= reads + 5)
            assert radio.reboots == 0
            assert bridge.radio.session is not None
            assert bridge.radio.session.clock_offset_s == 300
            radio.acknowledge(expected_ack(int(NOW), 0, "door open", SELF_KEY))
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "confirmed")
            await until(lambda: radio.reboots == 1)

    scenario(main)


def test_a_tcp_link_the_radio_closed_while_running_was_taken_over(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """SEC-003: the radio serves one TCP client and closes the gateway's link when another one
    connects, while its uptime goes on. The gateway takes the link back, warns, and the state
    says so for an hour. A radio that restarted closed the link too, but its uptime began anew;
    a link that broke otherwise (unplugged, the network) was not taken."""
    caplog.set_level(logging.WARNING)
    radio = EmulatedRadio(contacts=[ALICE])

    async def reconnect(bus: FakeBus, end: Callable[[], None]) -> dict[str, Any]:
        since = len(bus.payloads("/state"))
        end()
        await bus.wait_for("/state", lambda s: s["radio"] == "disconnected", since=since)
        return await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=since)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            assert bus.payloads("/state")[-1]["radio_link_contested"] is False
            assert (await reconnect(bus, radio.disconnect))["radio_link_contested"] is False
            assert "another client" not in caplog.text

            assert (await reconnect(bus, radio.close_link))["radio_link_contested"] is True
            assert "radio: the radio closed the TCP connection and kept running" in caplog.text
            assert "another client connected to it" in caplog.text

            caplog.clear()
            radio.uptime_s = 5  # restarted: no takeover, the earlier one counts for the hour
            assert (await reconnect(bus, radio.close_link))["radio_link_contested"] is True
            assert "another client" not in caplog.text

    scenario(main)


@pytest.mark.parametrize(
    ("radio", "stats"),
    [('transport = "serial"\ndevice = "/dev/ttyACM0"', True), (None, False)],
    ids=["usb", "tcp-without-statistics"],
)
def test_a_link_that_cannot_be_taken_or_told_is_not_contested(
    tmp_path: Path, radio: str | None, stats: bool
) -> None:
    """Over USB there is no other client; firmware without statistics reports no uptime."""
    config = make_config() if radio is None else make_config(radio=radio)
    emulated = EmulatedRadio(contacts=[ALICE], stats=stats)

    async def main() -> None:
        async with running(config, emulated, tmp_path) as (_, bus, _notes):
            assert bus.payloads("/state")[-1]["radio_link_contested"] is None

    scenario(main)


def test_the_state_file_is_written_at_the_status_tick_and_at_the_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARC-004: what the sections change (here the contact devices) waits in memory for the
    bridge's next status tick, and its stop writes what is left; no write per change."""
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.01)
    path = tmp_path / "state.json"

    def stored() -> set[str]:
        devices = JsonStateFile(path).load().get("ha_contact_devices", {})
        return set(devices.get("announced", {}))

    async def until(check: Callable[[], bool]) -> None:
        async def poll() -> None:
            while True:
                if check():
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 3)

    def announced(bus: FakeBus, contact: EmulatedContact) -> bool:
        key = contact.public_key.hex()
        topic = f"homeassistant/device/meshcore_home_gateway_radio1/contact_{key}/config"
        return any(t == topic for t, _, _ in bus.published)

    async def first() -> None:  # a status tick every minute: none in this run
        radio = EmulatedRadio(contacts=[ALICE], manual_add=False)
        async with running(make_config(), radio, tmp_path, devices=True) as (_, bus, _n):
            await until(lambda: announced(bus, ALICE))
            await asyncio.sleep(0.1)
            assert stored() == set()

    async def second() -> None:
        radio = EmulatedRadio(contacts=[ALICE], manual_add=False)  # after the restart
        async with running(make_config(), radio, tmp_path, devices=True) as (_, bus, _n):
            radio.contacts.append(BOB)
            radio.emit(bytes([Push.ADVERT]) + BOB.public_key)
            await until(lambda: announced(bus, BOB))
            await until(lambda: BOB.public_key.hex() in stored())

    scenario(first)
    assert stored() == {ALICE.public_key.hex()}
    monkeypatch.setattr(bridge_module, "STATUS_INTERVAL_S", 0.05)
    scenario(second)


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
                "live": 10.0,
                "desired": 10.0,
                "state": "written",
            }
            events = {e["setting"]: e for e in bus.payloads("radio1/setting")}
            assert set(events) == {"manual_add", "autoadd", "duty_cycle_percent"}
            assert events["autoadd"] == {
                "event_type": "written",
                "setting": "autoadd",
                "reason": "initial",
                "live": [],
                "desired": [],
            }
            assert any(r for t, _, r in bus.published if t.endswith("radio1/settings"))
            setters = [c for c in radio.commands() if c in (38, 58, 21)]
            first_read = radio.commands().index(4)  # GET_CONTACTS
            assert setters
            assert all(radio.commands().index(c) < first_read for c in setters)

    scenario(main)
