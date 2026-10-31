"""The fetch of the firmware sources: pinned by tag and full commit, checked, offline when done.

The firmware tests themselves run with `make firmware-test` (tests/firmware); these tests need
only git and run with `make test`.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git") or "/usr/bin/git"
TOOL = "fetch_firmware.py"
URL = "https://example.org/firmware.git"


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_firmware", ROOT / "tools" / TOOL)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["fetch_firmware"] = module
    spec.loader.exec_module(module)
    return module


fetch_firmware = _load_tool()


@dataclass
class Upstream:
    """A repository standing in for MeshCore's: `v1` tags the first commit, `v2` the second."""

    path: Path
    env: dict[str, str]

    def git(self, *args: str) -> str:
        result = subprocess.run(  # noqa: S603 - fixed arguments in a temporary repository
            [GIT, *args], cwd=self.path, env=self.env, capture_output=True, text=True, check=True
        )
        return result.stdout.strip()

    def commit(self, files: dict[str, str]) -> str:
        for name, text in files.items():
            (self.path / name).parent.mkdir(parents=True, exist_ok=True)
            (self.path / name).write_text(text, encoding="utf-8")
        self.git("add", "--all")
        self.git("commit", "--quiet", "--message", "Release")
        return self.git("rev-parse", "HEAD")

    def pin(self, commit: str, *tags: str, paths: tuple[str, ...] = ("src",)) -> object:
        return fetch_firmware.Pin("firmware", URL, tags, commit, paths)


@pytest.fixture
def upstream(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Upstream:
    # no user or system configuration (signing, hooks), as in a clean CI checkout
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.org",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.org",
    }
    path = tmp_path / "upstream"
    path.mkdir()
    repository = Upstream(path, env)
    repository.git("init", "--quiet", "--initial-branch=main")
    repository.commit({"src/Mesh.cpp": "// one\n", "examples/app.cpp": "// app\n"})
    repository.git("tag", "--annotate", "--message", "Release 1", "v1")
    repository.git("tag", "companion-v1")  # a second, lightweight tag of the same commit
    repository.commit({"src/Mesh.cpp": "// two\n"})
    repository.git("tag", "v2")
    return repository


def test_the_pinned_commit_is_checked_out_with_a_stamp(upstream: Upstream, tmp_path: Path) -> None:
    v1 = upstream.git("rev-parse", "v1^{commit}")
    pin = upstream.pin(v1, "v1", "companion-v1")
    dest = tmp_path / "src"
    assert fetch_firmware.fetch(pin, dest, str(upstream.path))
    tree = dest / "firmware"
    assert (tree / "src/Mesh.cpp").read_text(encoding="utf-8") == "// one\n"
    assert not (tree / "examples").exists()  # only the pinned paths
    assert v1 in (tree / fetch_firmware.STAMP).read_text(encoding="utf-8")
    assert sorted(p.name for p in dest.iterdir()) == ["firmware"]  # no scratch left behind


def test_a_second_fetch_needs_no_source(upstream: Upstream, tmp_path: Path) -> None:
    pin = upstream.pin(upstream.git("rev-parse", "v1^{commit}"), "v1")
    dest = tmp_path / "src"
    assert fetch_firmware.fetch(pin, dest, str(upstream.path))
    assert not fetch_firmware.fetch(pin, dest, str(tmp_path / "gone"))


def test_a_tag_on_another_commit_is_refused(upstream: Upstream, tmp_path: Path) -> None:
    """A moved tag or a wrong pin: nothing is checked out."""
    v1 = upstream.git("rev-parse", "v1^{commit}")
    v2 = upstream.git("rev-parse", "v2")
    dest = tmp_path / "src"
    with pytest.raises(fetch_firmware.FetchError, match=f"tag v2 .* is commit {v2}, pinned {v1}"):
        fetch_firmware.fetch(upstream.pin(v1, "v1", "v2"), dest, str(upstream.path))
    assert list(dest.iterdir()) == []


def test_a_new_pin_replaces_the_checkout(upstream: Upstream, tmp_path: Path) -> None:
    dest = tmp_path / "src"
    old = upstream.pin(upstream.git("rev-parse", "v1^{commit}"), "v1", paths=("src", "examples"))
    assert fetch_firmware.fetch(old, dest, str(upstream.path))
    new = upstream.pin(upstream.git("rev-parse", "v2"), "v2")
    assert fetch_firmware.fetch(new, dest, str(upstream.path))
    assert (dest / "firmware/src/Mesh.cpp").read_text(encoding="utf-8") == "// two\n"
    assert not (dest / "firmware/examples").exists()


def test_main_fetches_from_the_source_the_environment_names(
    upstream: Upstream, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    v1 = upstream.git("rev-parse", "v1^{commit}")
    pins = tmp_path / "sources.toml"
    pins.write_text(
        f'[meshcore]\nurl = "https://example.org/unreachable.git"\ntags = ["v1"]\n'
        f'commit = "{v1}"\npaths = ["src"]\n',
        encoding="utf-8",
    )
    dest = tmp_path / "src"
    environ = {"MESHCORE_SOURCE": str(upstream.path)}
    assert fetch_firmware.main([str(pins), str(dest)], environ) == 0
    assert capsys.readouterr().out == f"meshcore: v1 = {v1[:12]}, from {upstream.path}\n"
    assert fetch_firmware.main([str(pins), str(dest)], {}) == 0
    assert capsys.readouterr().out == f"meshcore: v1 = {v1[:12]}, up to date\n"


@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"commit": "81dbae5"}, "full hash"),
        ({"url": "http://example.org/a.git"}, "https"),
        ({"paths": ["../outside"]}, "inside the repository"),
        ({"paths": ["/etc"]}, "inside the repository"),
        ({"tags": []}, "lists of names"),
        ({"tags": ["--upload-pack=x"]}, "lists of names"),
    ],
)
def test_a_bad_pin_is_refused(change: dict[str, object], error: str) -> None:
    entry: dict[str, object] = {
        "url": "https://example.org/a.git",
        "tags": ["v1"],
        "commit": "0" * 40,
        "paths": ["src"],
    }
    with pytest.raises(fetch_firmware.FetchError, match=error):
        fetch_firmware._pin("meshcore", entry | change)


def test_the_repository_pins_meshcore_and_cayennelpp() -> None:
    """The firmware tests build MeshCore's companion and repeater of one release, with the
    CayenneLPP version that MeshCore's platformio.ini names."""
    pins = {p.name: p for p in fetch_firmware.load_pins(ROOT / "tests/firmware/sources.toml")}
    assert sorted(pins) == ["cayennelpp", "meshcore"]
    tags = pins["meshcore"].tags
    companion = [t for t in tags if t.startswith("companion-v")]
    assert companion == [t.replace("repeater-", "companion-") for t in tags if "repeater-" in t]
    raw = tomllib.loads((ROOT / "tests/firmware/sources.toml").read_text(encoding="utf-8"))
    assert all(len(entry["commit"]) == 40 for entry in raw.values())
