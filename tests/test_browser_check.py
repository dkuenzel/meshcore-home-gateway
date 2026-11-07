"""The browser check's choice of what fails a run (tools/browser_check.py); the browser part itself
needs a Home Assistant and a Chromium, so it runs by hand (docs/testing.md)."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "browser_check", ROOT / "tools" / "browser_check.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["browser_check"] = module
    spec.loader.exec_module(module)
    return module


browser_check = _load_tool()
TILE = "Failed to load resource: the server responded with a status of 502 (Bad Gateway)"


def test_an_allowed_error_is_kept_in_the_report_but_fails_no_page() -> None:
    """Offline, the map's tile proxy answers 502 on every map page (the stage-5 browser check):
    a pattern the run names lets that page pass, and only that error."""
    args = browser_check.parser().parse_args(
        ["overview", "--user", "u", "--dashboard", "d", "--allow-error", r"502 \(Bad Gateway\)"]
    )
    other = "uncaught: TypeError: x is undefined"
    assert browser_check.failing([TILE, other], args.allow_error) == [other]
    assert browser_check.failing([TILE], args.allow_error) == []
    assert browser_check.failing([TILE], []) == [TILE]


def test_an_allowed_error_must_be_a_pattern() -> None:
    with pytest.raises(SystemExit):
        browser_check.parser().parse_args(
            ["overview", "--user", "u", "--dashboard", "d", "--allow-error", "("]
        )
