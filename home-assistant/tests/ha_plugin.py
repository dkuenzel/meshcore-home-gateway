"""The helpers of Home Assistant's test plugin, from whichever plugin the environment has.

Up to 2026.10 the contract tests run with pytest-homeassistant-custom-component, which pins one
Home Assistant release (home-assistant/requirements.in). Since 2026.11 Home Assistant publishes
its own plugin, pytest-homeassistant (package `pytest_homeassistant`), and no release of the
other one pins 2026.11 (home-assistant/requirements-2026.11.in). The test modules import the
helpers from here, so that one suite runs with either (`make ha-test HA_SERIES=2026.11`).
"""

from importlib.util import find_spec

if find_spec("pytest_homeassistant") is not None:
    from pytest_homeassistant.common import (
        CLIENT_ID,
        async_fire_mqtt_message,
        async_fire_time_changed,
    )
    from pytest_homeassistant.components.recorder.common import async_wait_recording_done
    from pytest_homeassistant.plugins import aiohttp_client
else:
    from pytest_homeassistant_custom_component.common import (
        CLIENT_ID,
        async_fire_mqtt_message,
        async_fire_time_changed,
    )
    from pytest_homeassistant_custom_component.components.recorder.common import (
        async_wait_recording_done,
    )
    from pytest_homeassistant_custom_component.plugins import aiohttp_client

__all__ = [
    "CLIENT_ID",
    "aiohttp_client",
    "async_fire_mqtt_message",
    "async_fire_time_changed",
    "async_wait_recording_done",
]
