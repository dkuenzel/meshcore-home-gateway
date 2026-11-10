"""The announcements to Home Assistant (announcer.py, design §6.7)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import announcer as announcer_module
from meshcore_home_gateway.announcer import ha_device_topics
from meshcore_home_gateway.mqtt import manage_entities
from meshcore_home_gateway.protocol.codes import Push
from meshcore_home_gateway.radio import session as session_module
from meshcore_home_gateway.state import JsonStateFile
from tests.bridge_harness import (
    ALICE,
    BOB,
    GATEWAY_DOCUMENT,
    NOW,
    TWIN,
    FakeBus,
    OfflineBus,
    births,
    fast_timers,  # noqa: F401 - autouse
    gateway_documents,
    make_config,
    running,
    scenario,
)
from tests.radio_emulator import SELF_KEY, EmulatedContact, EmulatedRadio


def test_a_storm_of_birth_messages_does_not_postpone_the_announcement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each birth used to restart the delay: births 0.4 s apart kept the entities away for as
    long as they came (SEC-006). Now the first one's delay holds."""
    monkeypatch.setattr(announcer_module, "DISCOVERY_DELAY_S", (0.2, 0.2))

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _n):
            before = gateway_documents(bus)
            for _ in range(8):  # a birth every 0.05 s, for 0.4 s
                births(bus, 1)
                await asyncio.sleep(0.05)
            assert gateway_documents(bus) > before  # 0.2 s after the first, during the storm

    scenario(main)


def test_announcements_after_births_come_at_most_every_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """At most one announcement per ANNOUNCE_MIN_INTERVAL_S, also counting the one at start;
    many births in a minute are logged once, naming the topic."""
    monkeypatch.setattr(announcer_module, "ANNOUNCE_MIN_INTERVAL_S", 0.3)
    caplog.set_level(logging.WARNING)

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _n):
            before = gateway_documents(bus)  # the one at start
            for _ in range(20):  # 1 s of births
                births(bus, 1)
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.35)  # the last one waits for the rest of its interval
            announced = gateway_documents(bus) - before
            assert 2 <= announced <= 4  # 1.35 s at most every 0.3 s, the first after 0.3 s

    scenario(main)
    warnings = [r.getMessage() for r in caplog.records if "birth messages" in r.getMessage()]
    assert len(warnings) == 1  # once a minute
    assert "on homeassistant/status in a minute: another client writes there" in warnings[0]


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
            discovery = [r for t, _, r in bus.published if t.startswith("homeassistant/")]
            assert discovery
            assert not any(discovery)  # never retained

    scenario(main)


def test_a_renamed_radio_is_announced_again(tmp_path: Path) -> None:
    """The radio's name is part of what the discovery depends on (a channel text's room): a
    rename, seen at the next connect, announces again."""
    radio = EmulatedRadio()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _notes):
            count, states = len(bus.payloads(GATEWAY_DOCUMENT)), len(bus.payloads("/state"))
            radio.name = "Shed radio"
            radio.disconnect()
            await bus.wait_for("/state", lambda s: s["radio"] == "disconnected", since=states)
            states = len(bus.payloads("/state"))
            await bus.wait_for("/state", lambda s: s["radio"] == "connected", since=states)
            assert len(bus.payloads(GATEWAY_DOCUMENT)) == count + 1

    scenario(main)


def test_discovery_can_be_switched_off(tmp_path: Path) -> None:
    config = make_config(extra="[homeassistant]\ndiscovery = false")

    async def main() -> None:
        async with running(config, EmulatedRadio(), tmp_path) as (_, bus, _notes):
            for callback in bus.on_ha_online:  # Home Assistant announces itself
                callback()
            await asyncio.sleep(0.05)
            # neither the gateway's discovery document nor any other; its own `config` goes out
            assert not [t for t, _, _ in bus.published if t.startswith("homeassistant/")]
            assert bus.payloads("/state")[-1]["radio"] == "connected"

    scenario(main)


def test_components_of_an_earlier_configuration_are_removed(tmp_path: Path) -> None:
    store = JsonStateFile(tmp_path / "state.json")
    store.save(
        {
            "ha_components": {"send_old": "notify", "radio": "binary_sensor"},
            "command_timestamps": {"a1" * 32: 5},
        }
    )

    async def main() -> None:
        async with running(make_config(), EmulatedRadio(), tmp_path) as (_, bus, _notes):
            documents = bus.payloads(GATEWAY_DOCUMENT)
            assert len(documents) % 2 == 0  # each announcement: the placeholder, then removal
            for bind, document in zip(documents[::2], documents[1::2], strict=True):
                assert bind["cmps"]["send_old"]["name"] == "Removed"  # enabled: see placeholder()
                assert document["cmps"]["send_old"] == {"p": "notify"}
                assert document["cmps"]["radio"]["p"] == "binary_sensor"

    scenario(main)
    data = JsonStateFile(tmp_path / "state.json").load()  # as the run left it
    assert "send_old" not in data["ha_components"]
    assert data["ha_components"]["radio"] == "binary_sensor"
    assert data["ha_removed"]["send_old"]["p"] == "notify"  # pending, in case nobody got it
    assert data["command_timestamps"] == {"a1" * 32: 5}


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
            radio.emit(bytes([Push.ADVERT]) + carol.public_key)
            await until(lambda: bool(sent(carol.public_key.hex())))

            radio.contacts.remove(BOB)  # removed: placeholders, then the empty document
            radio.emit(bytes([Push.ADVERT]) + ALICE.public_key)
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


def test_a_removal_while_home_assistant_is_offline_is_sent_again_at_its_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Home Assistant's `offline` on its status topic reaches the contacts' devices (wiring.py):
    a device removed meanwhile is removed once more when Home Assistant starts again, as it may
    not have taken the removal; one removed while it runs is removed once."""
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.01)
    radio = EmulatedRadio(contacts=[ALICE, BOB], manual_add=False)
    alice, bob = ALICE.public_key.hex(), BOB.public_key.hex()

    def removals(key: str) -> int:
        topic = f"homeassistant/device/meshcore_home_gateway_radio1/contact_{key}/config"
        return sum(1 for t, p, _ in bus.published if t == topic and p == b"")

    async def until(check: Callable[[], bool]) -> None:
        async def poll() -> None:
            while True:
                if check():
                    return
                await asyncio.sleep(0.01)

        await asyncio.wait_for(poll(), 3)

    async def birth() -> None:
        count = len(bus.payloads(GATEWAY_DOCUMENT))
        for online in bus.on_ha_online:
            online()
        await until(lambda: len(bus.payloads(GATEWAY_DOCUMENT)) > count)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path, bus=bus, devices=True):
            await birth()  # Home Assistant runs
            radio.contacts.remove(ALICE)  # removed while it runs: once
            radio.emit(bytes([Push.ADVERT]) + BOB.public_key)
            await until(lambda: removals(alice) == 1)
            for offline in bus.on_ha_offline:
                offline()
            radio.contacts.remove(BOB)  # removed while it is away: again at its start
            radio.emit(bytes([Push.ADVERT]) + BOB.public_key)
            await until(lambda: removals(bob) == 1)
            await birth()
            await until(lambda: removals(bob) == 2)
            assert removals(alice) == 1

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
            await bus.wait_for(own, lambda d: True, since=count)
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
    store.save(
        {
            "ha_components_manage_contacts": {"add_node": "select", "add_group": "select"},
            "ha_components": {"radio": "binary_sensor"},
        }
    )

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
    data = JsonStateFile(tmp_path / "state.json").load()  # as the run left it
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
        async with running(make_config('[contacts]\nheard_nodes = "full"'), radio, tmp_path) as (
            _,
            bus,
            _n,
        ):
            radio.advertise(TWIN)
            radio.advertise(dave)
            await bus.wait_for(
                "/manage_contacts/config", lambda d: any(o.endswith(" Dave") for o in labels(d))
            )
            nodes = bus.payloads("radio1/nodes")[-1]
            assert TWIN.public_key.hex() in nodes["look_alike"]
            assert not any(
                "Mallory" in o for o in labels(bus.payloads("/manage_contacts/config")[-1])
            )

    scenario(main)


def test_the_service_watches_the_discovery_topics_it_announces() -> None:
    own = "homeassistant/device/meshcore_home_gateway_radio1/config"
    assert ha_device_topics(make_config()) == [
        own,
        "homeassistant/device/meshcore_home_gateway_radio1/+/config",
    ]
    without = make_config(extra="[homeassistant]\ncontact_devices = 0")
    assert ha_device_topics(without) == [
        own,
        "homeassistant/device/meshcore_home_gateway_radio1/manage_contacts/config",
    ]
    assert ha_device_topics(make_config(extra="[homeassistant]\ndiscovery = false")) == []
