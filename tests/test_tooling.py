"""Development tooling: the Makefile targets, the pre-push hook and the Dependabot configuration.

The Makefile targets run in dry runs or against temporary directories, never on the checkout's
environments or build output.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MAKE = shutil.which("make") or "/usr/bin/make"
GIT = shutil.which("git") or "/usr/bin/git"
# Set by a make that runs these tests (`make test`); they would leak its options into ours.
MAKE_ENVIRONMENT = {"MAKEFLAGS", "MFLAGS", "GNUMAKEFLAGS", "MAKELEVEL", "MAKEOVERRIDES"}


def make(*args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in MAKE_ENVIRONMENT}
    return subprocess.run(  # noqa: S603 - our Makefile with fixed arguments
        [MAKE, "--no-print-directory", "-C", str(ROOT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def git(cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed arguments in a temporary repository
        [GIT, *args], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def executable(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)
    return path


def test_check_builds_the_zipapp_once() -> None:
    """`unit-check` takes the zipapp of `dist`; a second build could race `rm -rf dist` (-j)."""
    dry_run = make("--dry-run", "check")
    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_run.stdout.count("tools/build_pyz.py") == 1, dry_run.stdout


def test_matrix_fails_when_a_python_is_missing() -> None:
    """A Python that is not installed is not a pass: CI tests every one of them."""
    # MAKE=true stands in for the `make test` of each installed Python.
    partial = make("matrix", f"PYTHONS={sys.executable} python3.98", "MAKE=true")
    assert partial.returncode != 0, partial.stdout
    assert "not installed: python3.98" in partial.stdout
    assert "all passed" not in partial.stdout
    complete = make("matrix", f"PYTHONS={sys.executable}", "MAKE=true")
    assert complete.returncode == 0, complete.stdout + complete.stderr
    assert "matrix: all passed" in complete.stdout


def test_a_changed_lock_gives_a_fresh_environment(tmp_path: Path) -> None:
    """A package dropped from the lock must not stay importable, as it would not in CI."""
    environment = tmp_path / "venv"
    # The real venv module, without pip (fast); the stand-in python skips the installs.
    wrapper = f'#!/bin/sh\nexec {sys.executable} "$@" --without-pip\n'
    python = executable(tmp_path / "python", wrapper)
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "python", "#!/bin/sh\n")
    options = [f"PYTHON={python}", f"VENV={environment}", f"BIN={tools}"]
    assert make("venv", *options).returncode == 0
    stale = next(environment.glob("lib/python*/site-packages")) / "dropped_from_the_lock.py"
    stale.write_text("", encoding="utf-8")
    os.utime(environment / ".installed", (0, 0))  # older than requirements/dev.txt
    refresh = make("venv", *options)
    assert refresh.returncode == 0, refresh.stderr
    assert not stale.exists()


@pytest.mark.parametrize("variable", ["VENV", "HA_VENV"])
def test_only_an_existing_environment_is_cleared(tmp_path: Path, variable: str) -> None:
    """`venv --clear` empties the directory: never one that VENV names by mistake."""
    environment = tmp_path / "venv"
    environment.mkdir()
    (environment / "notes.txt").write_text("keep\n", encoding="utf-8")
    target = f"{environment}/.installed"
    other = make("--dry-run", f"{variable}={environment}", target)
    assert other.returncode == 0, other.stderr
    assert "-m venv" in other.stdout
    assert "--clear" not in other.stdout
    (environment / "pyvenv.cfg").write_text("", encoding="utf-8")
    existing = make("--dry-run", f"{variable}={environment}", target)
    assert f"-m venv --clear {environment}" in existing.stdout


@dataclass
class Clone:
    """A clone with the repository's hooks; `make` is a stand-in that logs its arguments."""

    path: Path
    env: dict[str, str]
    make_log: Path

    def git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return git(self.path, self.env, *args)

    def commit(self, name: str, text: str) -> None:
        (self.path / name).write_text(text, encoding="utf-8")
        assert self.git("add", name).returncode == 0
        assert self.git("commit", "--quiet", "--message", f"Change {name}").returncode == 0

    def pushed(self) -> str:
        return self.git("ls-remote", "origin").stdout

    def checks(self) -> list[str]:
        if not self.make_log.exists():
            return []
        return self.make_log.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def clone(tmp_path: Path) -> Clone:
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "make", '#!/bin/sh\necho "$*" >>"$MAKE_LOG"\nexit "${MAKE_STATUS:-0}"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env |= {
        "PATH": f"{tools}{os.pathsep}{env['PATH']}",
        "MAKE_LOG": str(tmp_path / "make.log"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.org",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.org",
    }
    for repository in (["--bare", "origin.git"], ["--initial-branch=main", "work"]):
        assert git(tmp_path, env, "init", "--quiet", *repository).returncode == 0
    work = Clone(tmp_path / "work", env, tmp_path / "make.log")
    assert work.git("config", "core.hooksPath", str(ROOT / ".githooks")).returncode == 0
    assert work.git("remote", "add", "origin", str(tmp_path / "origin.git")).returncode == 0
    work.commit("app.py", "VALUE = 1\n")
    return work


def test_pre_push_checks_the_checked_out_commit(clone: Clone) -> None:
    result = clone.git("push", "origin", "main")
    assert result.returncode == 0, result.stderr
    assert clone.checks() == ["check"]
    assert "refs/heads/main" in clone.pushed()


@pytest.mark.parametrize("change", ["untracked", "uncommitted"])
def test_pre_push_refuses_a_working_tree_that_differs_from_the_commit(
    clone: Clone, change: str
) -> None:
    """`make check` sees the working tree, CI the commit: a forgotten `git add` fails only in CI."""
    if change == "untracked":
        (clone.path / "module.py").write_text("VALUE = 2\n", encoding="utf-8")
    else:
        (clone.path / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    result = clone.git("push", "origin", "main")
    assert result.returncode != 0
    assert "uncommitted changes or untracked files" in result.stderr
    assert clone.checks() == []
    assert clone.pushed() == ""


def test_pre_push_refuses_a_commit_that_is_not_checked_out(clone: Clone) -> None:
    assert clone.git("branch", "older").returncode == 0
    clone.commit("app.py", "VALUE = 2\n")
    result = clone.git("push", "origin", "main", "older")
    assert result.returncode != 0
    assert "refs/heads/older is not the checked-out commit" in result.stderr
    assert clone.checks() == []
    assert clone.pushed() == ""


def test_pre_push_accepts_a_tag_of_the_commit_and_deletions(clone: Clone) -> None:
    assert clone.git("tag", "--annotate", "--message", "Release", "v1.0.0").returncode == 0
    tagged = clone.git("push", "origin", "main", "v1.0.0")
    assert tagged.returncode == 0, tagged.stderr
    (clone.path / "notes.txt").write_text("not for the repository\n", encoding="utf-8")
    deleted = clone.git("push", "origin", ":v1.0.0")  # nothing to check
    assert deleted.returncode == 0, deleted.stderr
    assert clone.checks() == ["check"]
    assert "refs/tags/v1.0.0" not in clone.pushed()


def test_pre_push_stops_the_push_when_the_check_fails(clone: Clone) -> None:
    clone.env["MAKE_STATUS"] = "2"
    result = clone.git("push", "origin", "main")
    assert result.returncode != 0
    assert "checks failed, push aborted" in result.stderr
    assert clone.pushed() == ""


def test_dependabot_leaves_paho_mqtt_at_the_debian_version() -> None:
    """The development lock tests the paho-mqtt of the deployment target (ADR 0005)."""
    dev_in = (ROOT / "requirements/dev.in").read_text(encoding="utf-8")
    pin = re.search(r"^paho-mqtt==(\S+)", dev_in, re.MULTILINE)
    assert pin is not None
    assert f"python3-paho-mqtt {pin[1]}" in (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    text = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    entries = re.split(r"^  - package-ecosystem: ", text, flags=re.MULTILINE)[1:]
    (requirements,) = [e for e in entries if "\n    directory: /requirements\n" in e]
    ignore = re.search(r"^    ignore:\n((?:      .*\n)+)", requirements, re.MULTILINE)
    assert ignore is not None
    assert re.search(r'^      - dependency-name: "?paho-mqtt"?\s', ignore[1], re.MULTILINE)
