"""`make firmware-play` (EXT-019): the playground brings up the household, the broker and the
service, which reports the radio connected, and its stop takes them down."""

from __future__ import annotations

from pathlib import Path

from tests.firmware import play
from tests.firmware.conftest import Firmware


def test_the_playground_starts_and_stops(firmware: Firmware, tmp_path: Path) -> None:
    arguments = ["--directory", str(tmp_path), "--port", "0", "--hops", "0", "--for", "5"]
    assert play.main([*arguments, "--no-input"]) == 0
    assert 'reply = "all"' in (tmp_path / "config.toml").read_text(encoding="utf-8")
    log = (tmp_path / "service.log").read_text(encoding="utf-8")
    assert "starting, state in" in log
    assert (tmp_path / "A" / "node.log").exists()
    assert (tmp_path / "B" / "node.log").exists()
