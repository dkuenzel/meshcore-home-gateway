"""The zipapp build is reproducible and contains only the package."""

import importlib.util
import sys
import zipfile
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("build_pyz", ROOT / "tools" / "build_pyz.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["build_pyz"] = module
    spec.loader.exec_module(module)
    return module


build_pyz = _load_tool()


def test_two_builds_are_byte_identical(tmp_path: Path) -> None:
    first = build_pyz.build(ROOT / "src", tmp_path / "a.pyz", "/usr/bin/env python3")
    second = build_pyz.build(ROOT / "src", tmp_path / "b.pyz", "/usr/bin/env python3")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes().startswith(b"#!/usr/bin/env python3\n")
    assert first.stat().st_mode & 0o777 == 0o755


def test_archive_holds_the_package_and_an_entry_point(tmp_path: Path) -> None:
    pyz = build_pyz.build(ROOT / "src", tmp_path / "app.pyz", "/usr/bin/python3")
    with zipfile.ZipFile(pyz) as archive:
        names = archive.namelist()
    assert names[0] == "__main__.py"
    assert "meshcore_home_gateway/__init__.py" in names
    assert "meshcore_home_gateway/py.typed" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert names[1:] == sorted(names[1:])
