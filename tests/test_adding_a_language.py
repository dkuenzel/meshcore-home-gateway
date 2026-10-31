"""Adding a language takes only the files of docs/translations.md, Adding a language: a copy of the
checkout gains a pseudo-language through exactly those steps, and the checks that read the
languages a version ships pass in it.

The suites run in the copy are those that read i18n.LANGUAGES, the installer's block of language
packs (L10N_), the snapshot of the public interface or a language a test offers (speaking); the
first test keeps that list complete.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from meshcore_home_gateway.catalog import read_po

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git") or "/usr/bin/git"
LANGUAGE, NAME = "xy", "Pseudo"  # not xx, which the tests offer themselves (tests/i18n_helpers.py)
GERMANIC = "nplurals=2; plural=(n != 1);"  # tools/i18n.py PLURAL_FORMS has none for xy
SUITES = [
    "tests/test_build_pyz.py",
    "tests/test_cli_language.py",
    "tests/test_dashboard.py",
    "tests/test_discovery.py",
    "tests/test_i18n.py",
    "tests/test_i18n_tool.py",
    "tests/test_installer.py",
    "tests/test_installer_language.py",
    "tests/test_public_interface.py",
    "tests/test_release_plan.py",
    "tests/test_translations.py",
]
READS_THE_LANGUAGES = re.compile(r"\bLANGUAGES\b|\bL10N_|public_interface|speaking\(")
LOCALES = ["src/meshcore_home_gateway/locale", "deploy/proxmox/locale"]  # each with one template
WRITTEN = {  # what the steps add or change, and nothing else
    f"src/meshcore_home_gateway/locale/{LANGUAGE}.po",
    f"deploy/proxmox/locale/{LANGUAGE}.po",
    f"deploy/proxmox/locale/{LANGUAGE}.messages",
    "src/meshcore_home_gateway/i18n.py",
    "deploy/proxmox/meshcore-home-gateway-lxc.sh",
    "tests/public_interface.json",
}


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def test_every_suite_that_reads_the_languages_runs_in_the_copy() -> None:
    readers = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").glob("test_*.py")
        if READS_THE_LANGUAGES.search(path.read_text(encoding="utf-8"))
    }
    assert readers - {Path(__file__).relative_to(ROOT).as_posix()} == set(SUITES)


def checkout(target: Path) -> dict[str, bytes]:
    """The checkout's files that git knows or would add, copied to `target` with their modes;
    their contents."""
    listed = subprocess.run(  # noqa: S603 - git with fixed arguments in the checkout
        [GIT, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, check=False,
    )  # fmt: skip
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    files = {}
    for name in sorted(set(listed.stdout.decode().split("\0")) - {""}):
        source = ROOT / name
        if not source.is_file():
            continue  # deleted in the working tree
        files[name] = source.read_bytes()
        (target / name).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target / name)
    return files


def marked(text: str) -> str:
    """‹text›, a final line break outside: fields and %-directives stay as they are."""
    body, newline = (text[:-1], "\n") if text.endswith("\n") else (text, "")
    return f"‹{body}›{newline}"


def translated(template: str) -> str:
    """Steps 1 and 2: the template as a catalog of the language, every text translated, the
    plural rule in its header."""
    header, entries = read_po(template)
    complete = []
    for entry in entries:
        english = [entry.msgid, entry.msgid_plural] if entry.msgid_plural else [entry.msgid]
        complete.append(dataclasses.replace(entry, msgstr=tuple(map(marked, english))))
    return str(tool.write_po({**header, "Plural-Forms": GERMANIC}, complete))


def run(copy: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """A command in the copy, its package first on the path, as an environment installed from it;
    not measured (coverage would look for the copy's modules among the checkout's)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("COVERAGE")}
    env["PYTHONPATH"] = str(copy / "src")
    return subprocess.run(  # noqa: S603 - this Python on the copy's tools and tests
        [sys.executable, *args], cwd=copy, env=env, capture_output=True, text=True, check=False,
        timeout=600,
    )  # fmt: skip


def test_a_language_needs_its_catalogs_its_line_and_what_the_tools_write(tmp_path: Path) -> None:
    copy = tmp_path / "checkout"
    before = checkout(copy)
    for folder in LOCALES:
        (template,) = (copy / folder).glob("*.pot")
        catalog = translated(template.read_text(encoding="utf-8"))
        (copy / folder / f"{LANGUAGE}.po").write_text(catalog, encoding="utf-8")
    languages = copy / "src" / "meshcore_home_gateway" / "i18n.py"  # step 3
    text = languages.read_text(encoding="utf-8")
    assert text.count('{"en": "English"') == 1
    text = text.replace('{"en": "English"', f'{{"en": "English", "{LANGUAGE}": "{NAME}"', 1)
    languages.write_text(text, encoding="utf-8")

    for step in (["tools/i18n.py", "update"], ["-m", "tests.public_interface", "--write"]):
        done = run(copy, *step)  # steps 4 and 5: make translations, make interface
        assert done.returncode == 0, done.stdout + done.stderr
    after = {
        path.relative_to(copy).as_posix()
        for path in copy.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    changed = {name for name in after if (copy / name).read_bytes() != before.get(name)}
    assert changed == WRITTEN
    assert set(before) <= after
    interface = (copy / "tests" / "public_interface.json").read_text(encoding="utf-8")
    assert f'"{LANGUAGE}"' in interface  # a choice of [homeassistant] language

    checked = run(copy, "tools/i18n.py", "check")  # step 6: make check, its translation part
    assert (checked.returncode, checked.stdout) == (0, ""), checked.stdout + checked.stderr
    tested = run(copy, "-m", "pytest", "-q", "-p", "no:cacheprovider", *SUITES)
    assert tested.returncode == 0, tested.stdout[-4000:] + tested.stderr[-2000:]
