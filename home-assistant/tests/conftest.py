"""Settings of the Home Assistant test plugin (pytest-homeassistant-custom-component)."""

import pytest


@pytest.fixture
def expected_lingering_timers() -> bool:
    """The MQTT integration leaves its periodic housekeeping timer behind after a test."""
    return True
