"""Hardware checklist steps 3-6 and 10 on the simulated air: the gateway's service on radio A sends
to a channel and to Bob's companion B, a text too long for one message and a message B cannot
receive, and publishes what B sends it. Both radios run the real firmware; B's side is read
through its phone app (app.py)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from meshcore_home_gateway.core.model import MessageKind
from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import running
from tests.firmware.household import Household, household_ready, start

CHANNEL = 0  # Public: every firmware holds its key, so A and B share it
LONG = "Long " * 40  # 200 bytes, more than the 160 of a direct message
# the final events (docs/mqtt-api.md, Delivery): `sent` ends a channel message, not a direct one
CHANNEL_FINAL = ("sent", "failed", "expired", "rejected", "duplicate")
DIRECT_FINAL = ("confirmed", "failed", "expired", "rejected", "duplicate")


def settings(household: Household) -> str:
    """B as a contact target, the Public channel as a channel target; telemetry off, so that
    only the messages of the test are on the air."""
    return (
        "[telemetry]\nenabled = false\n"
        f'[targets.bob]\ncontact = "{household.bob_key.hex()}"\n'
        f"[targets.public]\nchannel = {CHANNEL}\n"
    )


def event(text: str, *kinds: str) -> Callable[[Any], bool]:
    def check(payload: Any) -> bool:
        return payload is not None and payload["text"] == text and payload["event_type"] in kinds

    return check


@pytest.mark.broker
def test_what_the_gateway_sends_and_receives(scene: Scene) -> None:
    """Steps 3, 4, 6 and 10: B shows the channel message (`queued`, then `sent`) and the direct
    one (`confirmed` with a round trip, not `suspect`); a text of 200 bytes reaches B cut to 160
    with `...`; B's direct and channel messages become `received` events with the sender, the
    signal and the hops."""
    household = start(scene.nodes, scene.air)
    watcher = scene.watcher

    async def scenario() -> None:
        async with App(household.bob) as bob:
            async with App(household.alice) as alice_app:
                await household_ready(household, alice_app, bob)
            bob_key = household.bob_key.hex()
            async with running(
                scene.configuration(household.alice, settings(household)), scene.state_dir
            ):
                await watcher.wait(f"contacts/{bob_key}")

                watcher.publish("send/public", "Hello channel")
                sent = (
                    await watcher.wait("delivery", event("Hello channel", *CHANNEL_FINAL))
                ).json()
                assert sent["event_type"] == "sent", sent
                queued = [e.json() for e in watcher.on("delivery")]
                assert {"queued", "sent"} <= {
                    e["event_type"] for e in queued if e["id"] == sent["id"]
                }
                shown = await bob.wait_for_message(
                    lambda m: m.kind is MessageKind.CHANNEL and m.text.endswith(": Hello channel"),
                    "channel message from the gateway",
                )
                assert shown.channel_index == CHANNEL

                watcher.publish("send/bob", "Hello Bob")
                confirmed = (
                    await watcher.wait("delivery", event("Hello Bob", *DIRECT_FINAL))
                ).json()
                assert confirmed["event_type"] == "confirmed", confirmed
                assert confirmed["rtt_ms"] > 0
                assert confirmed["suspect"] is False
                await bob.wait_for_message(
                    lambda m: m.kind is MessageKind.CONTACT and m.text == "Hello Bob",
                    "direct message from the gateway",
                )

                watcher.publish("send/bob", LONG)
                long = (
                    await watcher.wait(
                        "delivery",
                        lambda e: (
                            e is not None
                            and e["text"].startswith("Long Long")
                            and e["event_type"] in DIRECT_FINAL
                        ),
                    )
                ).json()
                assert long["event_type"] == "confirmed", long
                assert long["text"].endswith("...")
                assert len(long["text"].encode()) <= 160
                assert LONG.startswith(long["text"].removesuffix("..."))
                await bob.wait_for_message(
                    lambda m: m.kind is MessageKind.CONTACT and m.text == long["text"],
                    "the long text, cut",
                )

                await bob.send_text(household.alice_key, "Direct from Bob")
                await bob.send_channel_text(CHANNEL, "Channel from Bob")
                direct = (
                    await watcher.wait("received", lambda e: e and e["text"] == "Direct from Bob")
                ).json()
                channel = (
                    await watcher.wait("received", lambda e: e and e["text"] == "Channel from Bob")
                ).json()
        assert direct["event_type"] == "contact"
        assert (direct["sender_key"], direct["target"]) == (bob_key, "bob")
        assert direct["sender_prefix"] == bob_key[:12]
        assert isinstance(direct["snr_db"], float)
        assert direct["command"] is None
        assert channel["event_type"] == "channel"
        assert (channel["channel_index"], channel["target"]) == (CHANNEL, "public")
        assert channel["sender_name"] == household.bob.name  # claimed in the text
        assert channel["hops"] == 0  # flooded, heard straight from B
        assert isinstance(channel["snr_db"], float)

    asyncio.run(scenario())


@pytest.mark.broker
def test_a_contact_out_of_reach(scene: Scene) -> None:
    """Step 5: B gone from the air. Attempts 1 to `max_attempts` (3), the first two on B's path,
    the path reset after attempt `flood_after` (2), the last one flooded; then `failed`,
    `no_acknowledgement`."""
    household = start(scene.nodes, scene.air)
    alice = household.alice.node_id
    watcher = scene.watcher

    async def scenario() -> list[str]:
        async with App(household.bob) as bob, App(household.alice) as alice_app:
            await household_ready(household, alice_app, bob)
        async with running(
            scene.configuration(household.alice, settings(household)), scene.state_dir
        ):
            await watcher.wait(f"contacts/{household.bob_key.hex()}")
            scene.air.unlink(alice, household.bob.node_id)
            before = scene.air.count()
            watcher.publish("send/bob", "Are you there?")
            final = (
                await watcher.wait(
                    "delivery",
                    event("Are you there?", "confirmed", "failed", "expired"),
                    timeout_s=120,
                )
            ).json()
            assert (final["event_type"], final["reason"], final["attempt"]) == (
                "failed",
                "no_acknowledgement",
                3,
            ), final
            sent = [
                e.json()
                for e in watcher.on("delivery")
                if e.json()["id"] == final["id"] and e.json()["event_type"] == "sent"
            ]
            assert [e["attempt"] for e in sent] == [1, 2, 3]
        return [
            t.kind
            for t in scene.air.transmissions()[before:]
            if t.sender == alice and t.kind.startswith("TXT_MSG/")
        ]

    assert asyncio.run(scenario()) == ["TXT_MSG/D", "TXT_MSG/D", "TXT_MSG/F"]
