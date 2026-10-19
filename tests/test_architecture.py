"""Architecture rules (ADR 0007): the core and the protocol codec are pure."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "meshcore_bus"
FORBIDDEN = {
    "asyncio", "socket", "termios", "fcntl", "os", "subprocess", "threading", "time", "paho",
    "meshcore_bus.radio", "meshcore_bus.mqtt", "meshcore_bus.bridge", "meshcore_bus.cli",
    "meshcore_bus.state", "meshcore_bus.systemd", "meshcore_bus.config",
}  # fmt: skip
PURE = sorted([*PACKAGE.glob("core/*.py"), *PACKAGE.glob("protocol/*.py")])


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


@pytest.mark.parametrize("path", PURE, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_pure_modules_do_no_io(path: Path) -> None:
    offending = {
        name for name in imported_modules(path)
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    }  # fmt: skip
    assert not offending, f"{path.name} imports {sorted(offending)}"


def test_there_are_pure_modules_to_check() -> None:
    assert len(PURE) >= 10
