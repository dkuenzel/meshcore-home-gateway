"""The zipapp build is reproducible and contains only the package."""

import importlib.util
import os
import shutil
import stat
import subprocess
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


def test_builds_do_not_depend_on_file_times_or_modes(tmp_path: Path) -> None:
    """A checkout, an unpacked sdist and the installer's container give the same archive."""
    source = tmp_path / "src"
    shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__"))
    for path in source.rglob("*"):
        if path.is_file():
            os.utime(path, (1_700_000_000, 1_700_000_000))
            path.chmod(0o600)
    reference = build_pyz.build(ROOT / "src", tmp_path / "a.pyz", "/usr/bin/python3")
    copy = build_pyz.build(source, tmp_path / "b.pyz", "/usr/bin/python3")
    assert copy.read_bytes() == reference.read_bytes()
    with zipfile.ZipFile(copy) as archive:
        entries = archive.infolist()
    assert {entry.date_time for entry in entries} == {build_pyz.FIXED_DATE}
    assert {entry.external_attr for entry in entries} == {(stat.S_IFREG | 0o644) << 16}


def test_archive_holds_the_package_and_an_entry_point(tmp_path: Path) -> None:
    pyz = build_pyz.build(ROOT / "src", tmp_path / "app.pyz", "/usr/bin/python3")
    with zipfile.ZipFile(pyz) as archive:
        names = archive.namelist()
    assert names[0] == "__main__.py"
    assert "meshcore_home_gateway/__init__.py" in names
    assert "meshcore_home_gateway/py.typed" in names
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc")]
    assert names[1:] == sorted(names[1:])


def test_archive_holds_the_catalogs_but_no_template_or_compiled_catalog(tmp_path: Path) -> None:
    """ADR 0027: the PO files go in as they are, and a language is compiled when it is used."""
    source = tmp_path / "src"
    shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__"))
    locale = source / "meshcore_home_gateway" / "locale"
    (locale / "xx.po").write_text(XX_PO, encoding="utf-8")
    (locale / "xx.mo").write_bytes(b"\xde\x12\x04\x95")  # as Poedit leaves one
    assert (locale / "meshcore-home-gateway.pot").exists()
    pyz = build_pyz.build(source, tmp_path / "app.pyz", "/usr/bin/python3")
    with zipfile.ZipFile(pyz) as archive:
        names = archive.namelist()
    assert "meshcore_home_gateway/locale/xx.po" in names
    assert [n for n in names if n.endswith((".pot", ".mo"))] == []
    assert names[1:] == sorted(names[1:])
    # read from inside the archive, as the installed zipapp reads it
    program = (
        "import sys; from types import MappingProxyType; sys.path.insert(0, sys.argv[1]);"
        "from meshcore_home_gateway import i18n;"
        "i18n.LANGUAGES = MappingProxyType({'en': 'English', 'xx': 'Pseudo'});"
        "print(i18n.translator('xx').text('Removed'), i18n.CATALOGS)"
    )
    # not measured: coverage would look for the archive's modules among the checkout's sources
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE")}
    shown = subprocess.run(  # noqa: S603 - this Python on the archive just built
        [sys.executable, "-I", "-c", program, str(pyz)],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout.startswith("Entfernt ")
    assert "app.pyz" in shown.stdout


XX_PO = """msgid ""
msgstr ""
"Language: xx\\n"
"Content-Type: text/plain; charset=UTF-8\\n"

msgid "Removed"
msgstr "Entfernt"
"""


CONFIG = (
    '[radio]\ntransport = "tcp"\nhost = "192.0.2.10"\n'
    '[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\n'
)


def test_the_zipapp_reads_the_dashboards_template_from_inside(tmp_path: Path) -> None:
    """The template is package data, like the catalogs: `dashboard` reads it from the archive."""
    source = tmp_path / "src"
    shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__"))
    template = source / "meshcore_home_gateway" / "dashboard.yaml"
    template.write_text(template.read_text(encoding="utf-8") + "# from the archive\n", "utf-8")
    pyz = build_pyz.build(source, tmp_path / "app.pyz", "/usr/bin/python3")
    with zipfile.ZipFile(pyz) as archive:
        assert "meshcore_home_gateway/dashboard.yaml" in archive.namelist()
    config = tmp_path / "config.toml"
    config.write_text(CONFIG, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE")}
    shown = subprocess.run(  # noqa: S603 - this Python on the archive just built
        [sys.executable, "-I", str(pyz), "dashboard", "--config", str(config), "--state-dir",
         str(tmp_path / "state")],
        env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    assert shown.stdout.startswith("# meshcore-home-gateway dashboard for node radio1, ")
    assert shown.stdout.endswith("\n# from the archive\n")
    assert shown.stderr == "The map lists 0 contacts with a position.\n"


BUILD_WHEEL = "import sys, flit_core.buildapi as b; print(b.build_wheel(sys.argv[1]))"


def test_the_wheel_holds_the_dashboards_template(tmp_path: Path) -> None:
    """flit packs every file of the package (Makefile, dist)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE")}
    built = subprocess.run(  # noqa: S603 - flit's own build of this checkout
        [sys.executable, "-c", BUILD_WHEEL, str(tmp_path)],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True,
    )  # fmt: skip
    with zipfile.ZipFile(tmp_path / built.stdout.strip().splitlines()[-1]) as wheel:
        assert "meshcore_home_gateway/dashboard.yaml" in wheel.namelist()
