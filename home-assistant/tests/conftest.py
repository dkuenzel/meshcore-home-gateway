"""Settings of the Home Assistant test plugin (pytest-homeassistant-custom-component)."""

from collections.abc import Callable
from typing import Any

import pytest
from homeassistant.core import HomeAssistant


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
