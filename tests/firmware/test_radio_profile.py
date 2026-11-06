"""Hardware checklist step 14 on the simulated air: the managed radio profile (ADR 0020) sets back
what the app changed on the gateway's radio A, and a restart writes nothing."""

from __future__ import annotations

import asyncio

import pytest

from tests.firmware.app import App
from tests.firmware.conftest import Scene
from tests.firmware.gateway import running

ALLOW_ALL = 0b10_10_10  # the app's telemetry "Allow All" for base, location and environment
SET_BACK = ("manual_add", "telemetry", "advert_location")


@pytest.mark.broker
def test_the_radio_profile_is_set_back_and_kept(scene: Scene) -> None:
    """Automatic adding, telemetry for all and the position in adverts, switched on in the app:
    at the start each is written back (`settings` `written`, a `setting` event each), the radio
    then adds by hand, denies telemetry and sends no position; the next start finds it as it
    should be (`ok`) and writes nothing."""
    alice = scene.nodes.companion("A")
    watcher = scene.watcher
    config = scene.configuration(alice, "[telemetry]\nenabled = false\n")

    async def scenario() -> None:
        async with App(alice) as app:
            await app.start()
            await app.settings(ALLOW_ALL, manual_add=False, share_position=True)
        since = watcher.mark()
        async with running(config, scene.state_dir):
            first = (await watcher.wait("settings", lambda d: d is not None, since=since)).json()
        written = {
            e.json()["setting"]
            for e in watcher.on("setting", since)
            if e.json()["event_type"] == "written"
        }
        assert first["status"] == "written", first
        for name in SET_BACK:
            assert first["settings"][name]["state"] == "written", (name, first)
            assert name in written
        async with App(alice) as app:
            info = await app.start()
        assert info.manual_add_contacts is True
        assert (info.telemetry_modes, info.advert_location) == (0, 0)  # deny, no position

        since = watcher.mark()
        async with running(config, scene.state_dir):
            again = (await watcher.wait("settings", lambda d: d is not None, since=since)).json()
        assert again["status"] == "ok", again
        assert not [e for e in watcher.on("setting", since) if e.json()["event_type"] == "written"]

    asyncio.run(scenario())
