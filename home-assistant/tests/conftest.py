"""Settings of the Home Assistant test plugin (pytest-homeassistant-custom-component)."""

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant

# pytest-aiohttp has a fixture of the same name. Which plugin's wins depends on the order the
# plugins load in, which differs between machines; only this one keeps the router open, so a
# component that discovery sets up after a test's websocket client started can still register
# its views (notify needs repairs, which registers one).
from pytest_homeassistant_custom_component.plugins import aiohttp_client  # noqa: F401


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The MQTT integration leaves its periodic housekeeping timer behind after a test."""
    return True


@pytest.fixture
async def mqtt(
    hass: HomeAssistant, mock_hass_config: None, mqtt_mock_entry: Callable[..., Any]
) -> Any:
    """The MQTT integration, set up; what Home Assistant publishes is recorded."""
    return await mqtt_mock_entry()
