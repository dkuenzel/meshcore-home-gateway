"""Contact management through the bridge, with the emulated radio (ADR 0017): add and remove
requests, the checks in their order, one result per request, and what the radio gets."""

from __future__ import annotations

import asyncio
import json
import logging
import struct
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway import bridge as bridge_module
from meshcore_home_gateway import contact_manager as manager_module
from meshcore_home_gateway.contact_manager import ContactManager
from meshcore_home_gateway.core.mesh import Heard, Node
from meshcore_home_gateway.mqtt.client import ContactRequest
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.observer import Observer
from meshcore_home_gateway.protocol.codes import Command, Push
from meshcore_home_gateway.protocol.links import contact_link
from meshcore_home_gateway.radio import session as session_module
from tests.radio_emulator import SELF_KEY, EmulatedContact, EmulatedRadio
from tests.test_bridge import ALICE, BOB, NOW, FakeBus, make_config, running, scenario
from tests.test_session import SlowTable

ROOT = "meshcore/radio1"
FINAL = {"added", "removed", "refused", "failed"}
DAVE = EmulatedContact(bytes.fromhex("d4" * 32), "Dave", last_advert=int(NOW) - 30)
ROOM = EmulatedContact(bytes.fromhex("e5" * 32), "Hilltop", node_type=3, last_advert=int(NOW) - 9)
# Alice's first 6 bytes, another key: the radio could not tell them apart
ALICE_TWIN = bytes.fromhex("a1" * 6 + "ee" * 26)


@pytest.fixture(autouse=True)
def fast_timers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bridge_module, "RECONNECT_MIN_S", 0.02)
    monkeypatch.setattr(bridge_module, "DISCOVERY_DELAY_S", (0.0, 0.0))
    monkeypatch.setattr(session_module, "CONTACTS_REFRESH_DELAY_S", 0.05)


def request(
    bus: FakeBus, kind: str, body: str | bytes | dict[str, Any], *, key: str | None = None,
    retained: bool = False,
) -> None:  # fmt: skip
    if isinstance(body, dict):
        body = json.dumps(body)
    payload = body.encode() if isinstance(body, str) else body
    bus.contact_requests.put_nowait(ContactRequest(kind, payload, retained, key))


async def result(bus: FakeBus, request_id: str, within_s: float = 3) -> dict[str, Any]:
    return await bus.wait_for(
        f"{ROOT}/contact", lambda e: e.get("id") == request_id and e["event_type"] in FINAL,
        within_s,
    )  # fmt: skip


def results(bus: FakeBus) -> list[dict[str, Any]]:
    return [e for e in bus.payloads(f"{ROOT}/contact") if e["event_type"] in FINAL and e["id"]]


def contact_writes(radio: EmulatedRadio) -> list[bytes]:
    return [
        p for p in radio.received if p[0] in (Command.ADD_UPDATE_CONTACT, Command.REMOVE_CONTACT)
    ]


def index_of(bus: FakeBus, topic: str, check: Any = None) -> int:
    for i, (t, p, _) in enumerate(bus.published):
        if t == topic and (check is None or check(p)):
            return i
    raise AssertionError(f"nothing on {topic}")


async def heard(bus: FakeBus, radio: EmulatedRadio, node: EmulatedContact) -> None:
    radio.advertise(node)
    key = node.public_key.hex()
    await bus.wait_for(f"{ROOT}/nodes", lambda d: any(n["key"] == key for n in d["nodes"]))


# --- adding ------------------------------------------------------------------------------------


def test_a_heard_node_is_added_with_its_own_advert(tmp_path: Path) -> None:
    """ADR 0017: it advertises, appears as a heard node, is added; its record as the radio
    checked it, without a position; the result comes after its device and documents."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    dave = DAVE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            await heard(bus, radio, DAVE)
            request(bus, "add", {"contact": dave, "id": "t1"})
            event = await result(bus, "t1")
            assert event == {
                "event_type": "added", "key": dave, "prefix": dave[:12], "name": "Dave",
                "type": "chat", "request": "add", "id": "t1", "reason": None,
            }  # fmt: skip
            (frame,) = contact_writes(radio)
            assert len(frame) == 144
            assert (frame[33], frame[34], frame[35]) == (1, 0x00, 0xFF)  # chat, no flags, no path
            assert struct.unpack_from("<I", frame, 132)[0] == DAVE.last_advert
            assert frame[136:144] == bytes(8)  # no position
            document = bus.payloads(f"{ROOT}/contacts/{dave}")[-1]
            assert (document["verified"], document["removable"]) == (True, True)
            contact = index_of(bus, f"{ROOT}/contacts/{dave}")
            added = index_of(bus, f"{ROOT}/contact", lambda p: b'"added"' in p)
            assert contact < added  # the recipient exists when the event arrives
            nodes = bus.payloads(f"{ROOT}/nodes")[-1]
            assert all(n["key"] != dave for n in nodes["nodes"])
            bus.send(dave, "Hello Dave", contact=True)  # a recipient at once
            await bus.wait_for("/delivery", lambda d: d["event_type"] == "queued")
            assert len(results(bus)) == 1  # one result per request

    scenario(main)


def test_a_link_adds_a_node_not_heard_with_its_claims(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    room = ROOM.public_key.hex()
    link = "meshcore://contact/add?name=Hill+top+%C3%A4&public_key=" + room.upper() + "&type=3"

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": link, "id": "link"})
            event = await result(bus, "link")
            assert (event["event_type"], event["name"], event["type"]) == (
                "added", "Hill top ä", "room",
            )  # fmt: skip
            (frame,) = contact_writes(radio)
            assert frame[33] == 3
            assert struct.unpack_from("<I", frame, 132)[0] == 0  # a claim, not signed
            assert bus.payloads(f"{ROOT}/contacts/{room}")[-1]["verified"] is False

    scenario(main)


def test_a_bare_key_gets_a_name_from_its_digits(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    dave = DAVE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", f"  {dave.upper()}\n")  # plain text, no id: the gateway makes one
            event = await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "added")
            assert (event["name"], event["type"], len(event["id"])) == (dave[:12], "chat", 8)

    scenario(main)


# --- refused: nothing is written ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason", "key"),
    [
        ("", "invalid_link", None),
        ("hello", "invalid_link", None),
        ("meshcore://channel/add?name=Public&secret=" + "00" * 16, "unsupported_link", None),
        ("meshcore://contact/add?public_key=" + "d4" * 32 + "&type=0", "unsupported_type", None),
        ("meshcore://contact/add?public_key=" + "d4" * 32 + "&public_key=" + "d4" * 32,
         "invalid_link", None),
        ("{", "invalid_request", None),
        ('{"contact": "' + "d4" * 32 + '", "id": "bad id"}', "invalid_request", None),
        ('{"a":' + "[" * 1010, "invalid_request", None),  # nested too deeply for json
        ("x" * 1025, "too_large", None),
        (b"\xff", "invalid_utf8", None),
        (BOB.public_key.hex(), "already_contact", BOB.public_key.hex()),
        (ALICE_TWIN.hex(), "look_alike", ALICE_TWIN.hex()),
        (SELF_KEY.hex(), "own_key", SELF_KEY.hex()),
    ],
)  # fmt: skip
def test_an_add_is_refused_with_its_reason(
    tmp_path: Path, body: str | bytes, reason: str, key: str | None
) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", body)
            event = await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "refused")
            assert (event["reason"], event["key"], event["request"]) == (reason, key, "add")
            await asyncio.sleep(0.1)
            assert contact_writes(radio) == []

    scenario(main)


def test_a_look_alike_of_a_protected_key_not_on_the_radio_is_refused(tmp_path: Path) -> None:
    """Bob is a target, but not on this radio: a key with his prefix must not take his place."""
    radio = EmulatedRadio(contacts=[ALICE])
    twin = "b2" * 6 + "ee" * 26

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": twin, "id": "twin"})
            assert (await result(bus, "twin"))["reason"] == "look_alike"
            assert all(p[0] != Command.ADD_UPDATE_CONTACT or p[1:33] != bytes.fromhex(twin)
                       for p in radio.received)  # fmt: skip

    scenario(main)


def test_a_retained_request_is_refused_before_its_text_is_judged(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", "not a key", retained=True)
            event = await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "refused")
            assert event["reason"] == "retained"

    scenario(main)


def test_management_off_refuses_and_names_the_key(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    dave = DAVE.public_key.hex()

    async def main() -> None:
        config = make_config("[contacts]\nmanage = false")
        async with running(config, radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": dave, "id": "off"})
            event = await result(bus, "off")
            assert (event["reason"], event["key"]) == ("management_off", dave)
            index = bus.payloads(f"{ROOT}/contacts")[-1]
            assert index["manage"] is False
            bob = bus.payloads(f"{ROOT}/contacts/{BOB.public_key.hex()}")[-1]
            assert bob["removable"] is False

    scenario(main)


def test_a_full_table_is_never_made_room_in(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], max_contacts=2)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "full"})
            assert (await result(bus, "full"))["reason"] == "table_full"
            assert contact_writes(radio) == []

    scenario(main)


def test_a_full_table_is_not_written_even_when_the_radio_would_make_room(tmp_path: Path) -> None:
    """With overwrite-oldest on (a radio only observed by the profile), the radio would drop its
    oldest contact for the new one: the gateway's own count refuses first."""
    radio = EmulatedRadio(contacts=[ALICE, BOB], max_contacts=2, autoadd_config=0x01)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "evict"})
            assert (await result(bus, "evict"))["reason"] == "table_full"
            assert contact_writes(radio) == []
            assert [c.name for c in radio.contacts] == ["Alice", "Bob"]

    scenario(main)


def test_untested_firmware_is_not_written_unless_the_profile_says_always(tmp_path: Path) -> None:
    """As the managed profile (ADR 0020): unknown firmware is not written blindly."""
    dave = DAVE.public_key.hex()

    async def main() -> None:
        radio = EmulatedRadio(contacts=[ALICE, BOB], firmware_code=15)
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": dave, "id": "untested"})
            assert (await result(bus, "untested"))["reason"] == "untested_firmware"
        radio = EmulatedRadio(contacts=[ALICE, BOB], firmware_code=15)
        config = make_config('[radio.profile]\nmanage = "always"')
        async with running(config, radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": dave, "id": "always"})
            assert (await result(bus, "always"))["event_type"] == "added"

    scenario(main)


def test_the_radio_work_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every request that reaches the radio takes a token, also one the table refuses: a client
    cannot keep the radio busy with table reads."""
    monkeypatch.setattr(manager_module, "WORK_BURST", 2.0)
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            for n in range(3):
                request(bus, "add", {"contact": BOB.public_key.hex(), "id": f"r{n}"})
            reasons = [(await result(bus, f"r{n}"))["reason"] for n in range(3)]
            assert reasons == ["already_contact", "already_contact", "rate_limited"]

    scenario(main)


def test_a_queue_of_eight_then_busy(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], silent={Command.ADD_UPDATE_CONTACT})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            keys = [f"{n:02x}" * 32 for n in range(0x10, 0x1A)]
            for n, key in enumerate(keys):
                request(bus, "add", {"contact": key, "id": f"q{n}"})
            event = await result(bus, "q9")  # the first is with the worker, eight wait
            assert event["reason"] == "busy"

    scenario(main)


def test_without_the_radio_nothing_is_tried(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            radio.disconnect()
            await bus.wait_for(f"{ROOT}/state", lambda s: s["radio"] == "disconnected")
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "away"})
            assert (await result(bus, "away"))["reason"] == "no_radio"

    scenario(main)


def test_a_flood_of_bad_requests_gives_a_bounded_number_of_events(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            for _ in range(300):
                request(bus, "add", "nonsense")
            await asyncio.sleep(0.3)
            refused = [e for e in bus.payloads(f"{ROOT}/contact") if e["event_type"] == "refused"]
            assert 100 <= len(refused) <= 110

    with caplog.at_level(logging.WARNING):
        scenario(main)
    assert "contact events of refused requests not published" in caplog.text


# --- failed: the write went out ----------------------------------------------------------------


def test_the_radio_refusing_the_write_fails_the_request(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], errors={Command.ADD_UPDATE_CONTACT: 3})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "err"})
            event = await result(bus, "err")
            assert (event["event_type"], event["reason"]) == ("failed", "table_full")

    scenario(main)


def test_a_write_the_table_does_not_show_is_not_confirmed(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], ignored={Command.ADD_UPDATE_CONTACT})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "lost"})
            event = await result(bus, "lost")
            assert (event["event_type"], event["reason"]) == ("failed", "not_confirmed")
            assert len(contact_writes(radio)) == 1  # never tried again

    scenario(main)


def test_a_connection_lost_at_the_write_leaves_the_outcome_unknown(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], drop_on={Command.ADD_UPDATE_CONTACT})

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "gone"})
            event = await result(bus, "gone")
            assert (event["event_type"], event["reason"]) == ("failed", "outcome_unknown")
            assert len(results(bus)) == 1

    scenario(main)


# --- removing ----------------------------------------------------------------------------------


def test_a_contact_is_removed_and_offered_again_as_a_heard_node(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB, DAVE])
    dave = DAVE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            radio.advertise(DAVE)  # heard, so it can come back as a heard node
            await bus.wait_for(f"{ROOT}/contacts/{dave}", lambda d: d["heard_at"] is not None)
            request(bus, "remove", {"id": "rm"}, key=dave)
            event = await result(bus, "rm")
            assert (event["event_type"], event["name"], event["request"]) == (
                "removed", "Dave", "remove",
            )  # fmt: skip
            (frame,) = contact_writes(radio)
            assert frame == bytes([Command.REMOVE_CONTACT]) + DAVE.public_key
            cleared = index_of(bus, f"{ROOT}/contacts/{dave}", lambda p: p == b"")
            removed = index_of(bus, f"{ROOT}/contact", lambda p: b'"removed"' in p)
            assert cleared < removed
            nodes = bus.payloads(f"{ROOT}/nodes")[-1]
            assert [n["key"] for n in nodes["nodes"]] == [dave]

    scenario(main)


@pytest.mark.parametrize(
    ("key", "reason", "named"),
    [
        (ALICE.public_key.hex(), "protected", True),  # a command sender and a target
        (DAVE.public_key.hex(), "unknown_contact", True),  # not on the radio
        (DAVE.public_key.hex().upper(), "unknown_contact", False),  # not as the gateway writes
        ("+", "unknown_contact", False),
    ],
)
def test_a_remove_is_refused_with_its_reason(
    tmp_path: Path, key: str, reason: str, named: bool
) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        config = make_config("[radio.profile]\nprotect_contacts = false")  # still protected
        async with running(config, radio, tmp_path) as (_, bus, _n):
            request(bus, "remove", "remove", key=key)
            event = await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "refused")
            assert (event["reason"], event["key"]) == (reason, key if named else None)
            await asyncio.sleep(0.1)
            assert all(p[0] != Command.REMOVE_CONTACT for p in radio.received)

    scenario(main)


# --- the gateway's own link ---------------------------------------------------------------------


def test_the_state_carries_the_gateways_contact_link(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], name="Gate way")

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            state = await bus.wait_for(f"{ROOT}/state", lambda s: s["radio"] == "connected")
            assert state["contact_link"] == contact_link("Gate way", SELF_KEY.hex())
            assert state["contact_link"].startswith("meshcore://contact/add?name=Gate+way&")

    scenario(main)


def test_a_heard_look_alike_of_a_contact_is_flagged(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    twin = EmulatedContact(ALICE_TWIN, "Alice?", last_advert=int(NOW) - 3)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            radio.advertise(twin)
            event = await bus.wait_for(f"{ROOT}/node", lambda e: e["event_type"] == "new")
            assert event["look_alike"] is True
            nodes = bus.payloads(f"{ROOT}/nodes")[-1]
            assert (nodes["look_alike"], nodes["nodes"][0]["look_alike"]) == (
                [ALICE_TWIN.hex()],
                True,
            )

    scenario(main)


# --- what the review found --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ({"id": "t1"}, "invalid_request"),  # an add without its contact
        ({"contact": 5, "id": "t1"}, "invalid_request"),
        ({"contact": "", "id": "t1"}, "invalid_link"),  # an empty text, judged as a text
    ],
)
def test_a_bad_envelope_with_a_good_id_gets_its_id_back(
    tmp_path: Path, body: dict[str, Any], reason: str
) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", body)
            assert (await result(bus, "t1"))["reason"] == reason

    scenario(main)


@pytest.mark.parametrize("retained", [False, True])
def test_a_lone_surrogate_is_refused_and_the_gateway_goes_on(
    tmp_path: Path, retained: bool
) -> None:
    """A JSON \\ud800 escape is valid UTF-8 on the wire but no text: it must not end the service,
    also not as a retained request the broker hands over at every start."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])
    link = "meshcore://contact/add?name=\\ud800x&public_key=" + DAVE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            payload = ('{"contact": "' + link + '", "id": "s1"}').encode()
            request(bus, "add", payload, retained=retained)
            assert (await result(bus, "s1"))["reason"] == "invalid_utf8"
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "after"})
            assert (await result(bus, "after"))["event_type"] == "added"

    scenario(main)


def test_a_refusal_names_the_node_as_the_gateway_heard_it(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB], max_contacts=2)

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            await heard(bus, radio, ROOM)
            request(bus, "add", {"contact": ROOM.public_key.hex(), "id": "room"})
            event = await result(bus, "room")
            assert (event["reason"], event["name"], event["type"]) == (
                "table_full",
                "Hilltop",
                "room",
            )
            request(bus, "remove", {"id": "gone"}, key=DAVE.public_key.hex())
            event = await result(bus, "gone")
            assert (event["reason"], event["name"], event["type"]) == (
                "unknown_contact",
                None,
                None,
            )

    scenario(main)


def test_a_write_the_session_never_sent_is_refused(tmp_path: Path) -> None:
    """The session ends while the write waits for the radio (a message fetch that gets no
    answer): nothing was written, so the request is refused, not failed."""
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            radio.silent = {Command.SYNC_NEXT_MESSAGE}
            radio.queue_direct_message(ALICE.public_key[:6], "hi", int(NOW))  # a fetch that hangs
            await asyncio.sleep(0.05)
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "late"})
            event = await result(bus, "late")
            assert (event["event_type"], event["reason"]) == ("refused", "no_radio")
            assert all(p[0] != Command.ADD_UPDATE_CONTACT for p in radio.received)
            radio.silent = set()

    scenario(main)


class TornWrite(EmulatedRadio):
    """The radio takes the add, then the connection breaks before the write returns."""

    async def write(self, data: bytes) -> None:
        await super().write(data)
        if len(data) > 3 and data[3] == Command.ADD_UPDATE_CONTACT and not self.torn:
            self.torn = True
            raise ConnectionResetError("connection reset")

    torn = False


def test_a_write_that_broke_off_has_an_unknown_outcome_and_one_result(tmp_path: Path) -> None:
    radio = TornWrite(contacts=[ALICE, BOB])
    dave = DAVE.public_key.hex()

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            request(bus, "add", {"contact": dave, "id": "torn"})
            event = await result(bus, "torn")
            assert (event["event_type"], event["reason"]) in {
                ("failed", "outcome_unknown"), ("added", None),
            }  # fmt: skip
            await bus.wait_for(f"{ROOT}/contact", lambda e: e["event_type"] == "added")
            await asyncio.sleep(0.1)
            assert len([e for e in results(bus) if e["id"] == "torn"]) == 1

    scenario(main)


def test_a_change_during_the_read_a_request_waited_for_is_read_too(tmp_path: Path) -> None:
    """The radio adds a contact itself (overwrite-oldest on) while the read the request waits for
    runs: the rules apply to a table read after that change, so the full table is refused instead
    of letting the radio evict Alice, a protected contact."""
    radio = SlowTable(contacts=[ALICE], max_contacts=2, autoadd_config=0x01)
    carol = EmulatedContact(bytes.fromhex("c3" * 32), "Carol")

    async def main() -> None:
        async with running(make_config(), radio, tmp_path) as (_, bus, _n):
            reads = radio.commands().count(Command.GET_CONTACTS)
            radio.advertise(ROOM)  # the maintenance task reads the table after its delay
            for _ in range(300):  # until that read has begun
                if radio.commands().count(Command.GET_CONTACTS) > reads:
                    break
                await asyncio.sleep(0.01)
            request(bus, "add", {"contact": DAVE.public_key.hex(), "id": "during"})
            await asyncio.sleep(0.05)
            radio.contacts.append(carol)  # added by the radio during that read
            radio.emit(bytes([Push.ADVERT]) + carol.public_key)
            assert (await result(bus, "during"))["reason"] == "table_full"
            assert contact_writes(radio) == []
            assert [c.name for c in radio.contacts] == ["Alice", "Carol"]

    scenario(main)


def test_a_refusal_at_once_names_the_heard_node(tmp_path: Path) -> None:
    radio = EmulatedRadio(contacts=[ALICE, BOB])

    async def main() -> None:
        async with running(make_config("[contacts]\nmanage = false"), radio, tmp_path) as (
            _, bus, _n,
        ):  # fmt: skip
            await heard(bus, radio, ROOM)
            request(bus, "add", {"contact": ROOM.public_key.hex(), "id": "off"})
            event = await result(bus, "off")
            assert (event["name"], event["type"]) == ("Hilltop", "room")

    scenario(main)


def test_a_refusal_names_a_node_heard_after_its_request_was_taken() -> None:
    """The node advertises while its request waits: the refusal names it as heard."""
    room = ROOM.public_key.hex()

    async def main() -> None:
        bus = FakeBus()
        observer = Observer(bus, Topics("meshcore", "radio1"), wall_clock=lambda: NOW)
        events: asyncio.Queue[Any] = asyncio.Queue()
        manager = ContactManager(make_config(), observer, events, lambda: None,
                                 protected=frozenset())  # fmt: skip
        manager.accept(ContactRequest("add", json.dumps({"contact": room, "id": "q"}).encode(),
                                      retained=False))  # fmt: skip
        observer.mesh.node_heard(Node(room, "Hilltop", 3), Heard(NOW, "advert"))
        worker = asyncio.create_task(manager.run())
        try:
            event = await result(bus, "q")
        finally:
            worker.cancel()
        assert (event["reason"], event["name"], event["type"]) == ("no_radio", "Hilltop", "room")

    asyncio.run(main())
