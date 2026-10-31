"""Architecture rules (ADR 0007): the core and the protocol codec are pure; only the command line
and the builders of the names sent to Home Assistant choose a language (ADR 0019)."""

import ast
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parent.parent / "src" / "meshcore_home_gateway"
FORBIDDEN = {
    "asyncio", "socket", "termios", "fcntl", "os", "subprocess", "threading", "time", "paho",
    "meshcore_home_gateway.radio", "meshcore_home_gateway.mqtt", "meshcore_home_gateway.bridge",
    "meshcore_home_gateway.cli", "meshcore_home_gateway.state", "meshcore_home_gateway.systemd",
    "meshcore_home_gateway.config", "meshcore_home_gateway.i18n",
}  # fmt: skip
PURE = sorted([
    *PACKAGE.glob("core/*.py"), *PACKAGE.glob("protocol/*.py"), PACKAGE / "views.py",
    PACKAGE / "texts.py", PACKAGE / "catalog.py",
])  # fmt: skip
# The modules that choose a language; every other one makes Texts (texts.py), which these
# translate where they show them.
SPEAKING = {
    "cli.py", "config.py", "mqtt/discovery.py", "mqtt/manage_entities.py",
    "mqtt/contact_devices.py",
}  # fmt: skip
I18N = "meshcore_home_gateway.i18n"


def imported_modules(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)  # `from package import module` imports a module too
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_every_import_form_is_recorded(tmp_path: Path) -> None:
    module = tmp_path / "module.py"
    module.write_text(
        "import a.b\nfrom c import d\nfrom e.f import g as h\nimport i as j\n", encoding="utf-8"
    )
    assert imported_modules(module) == {"a.b", "c", "c.d", "e.f", "e.f.g", "i"}


@pytest.mark.parametrize("path", PURE, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_pure_modules_do_no_io(path: Path) -> None:
    offending = {
        name for name in imported_modules(path)
        if any(name == f or name.startswith(f + ".") for f in FORBIDDEN)
    }  # fmt: skip
    assert not offending, f"{path.name} imports {sorted(offending)}"


def test_there_are_pure_modules_to_check() -> None:
    assert len(PURE) >= 10
    assert all(path.exists() for path in PURE)


def test_only_the_modules_that_show_texts_choose_a_language() -> None:
    importing = {
        path.relative_to(PACKAGE).as_posix()
        for path in PACKAGE.rglob("*.py")
        if any(name == I18N or name.startswith(I18N + ".") for name in imported_modules(path))
    }
    assert importing <= SPEAKING
