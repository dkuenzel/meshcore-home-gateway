"""Development tooling: the Makefile targets, the pre-push hook and the Dependabot configuration.

The Makefile targets run in dry runs or against temporary directories, never on the checkout's
environments or build output.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

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


def test_the_formatter_has_the_last_word() -> None:
    """CODE-007: the output of `ruff format` is the style; no line of the code opts out of it."""
    marker = "# fmt" + ": "  # not in this file's own text
    opted_out = [
        f"{path.relative_to(ROOT)}:{number}"
        for folder in ("src", "tests", "tools")
        for path in sorted((ROOT / folder).rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if marker in line
    ]
    assert opted_out == []


def test_check_builds_the_zipapp_once() -> None:
    """`unit-check` takes the zipapp of `dist`; a second build could race `rm -rf dist` (-j)."""
    dry_run = make("--dry-run", "check")
    assert dry_run.returncode == 0, dry_run.stderr
    assert dry_run.stdout.count("tools/build_pyz.py") == 1, dry_run.stdout


def test_check_leaves_the_firmware_tests_out() -> None:
    """`make check` needs no compiler and no network: the firmware tests run with
    `make firmware-test` (docs/testing.md), and `pytest` alone does not collect them."""
    dry_run = make("--dry-run", "check")
    assert dry_run.returncode == 0, dry_run.stderr
    assert "firmware" not in dry_run.stdout
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "--quiet", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout
    assert "tests/test_tooling.py::" in collected.stdout
    # no test of tests/firmware/ (a test of another module may name a file there)
    lines = collected.stdout.splitlines()
    assert not [line for line in lines if line.startswith("tests/firmware/")]


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
    os.utime(environment / ".locked", (0, 0))  # older than requirements/dev.txt
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


def test_a_changed_pyproject_keeps_the_environment(tmp_path: Path) -> None:
    """Only the lock decides what is installed; clearing for any other change would make
    `make check` fail without network access."""
    environment = tmp_path / "venv"
    wrapper = f'#!/bin/sh\nexec {sys.executable} "$@" --without-pip\n'
    python = executable(tmp_path / "python", wrapper)
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "python", "#!/bin/sh\n")
    options = [f"PYTHON={python}", f"VENV={environment}", f"BIN={tools}"]
    assert make("venv", *options).returncode == 0
    kept = next(environment.glob("lib/python*/site-packages")) / "installed_from_the_lock.py"
    kept.write_text("", encoding="utf-8")
    os.utime(environment / ".installed", (0, 0))  # older than pyproject.toml, lock unchanged
    refresh = make("venv", *options)
    assert refresh.returncode == 0, refresh.stderr
    assert "--clear" not in refresh.stdout
    assert kept.exists()


def job_commands(job: str) -> list[str]:
    """The shell commands of a workflow job: each `run:` line and the lines of a `run: |` block."""
    commands: list[str] = []
    block: int | None = None
    for line in job.splitlines():
        indent = len(line) - len(line.lstrip())
        if block is not None and line.strip() and indent > block:
            commands.append(line.strip())
            continue
        block = None
        run = re.match(r"\s*(?:- )?run: (.*)$", line)
        if run and run[1].strip() in {"|", ">"}:
            block = indent
        elif run:
            commands.append(run[1])
    return commands


def make_targets(command: str) -> set[str]:
    """The targets a command passes to make (options and variables left out)."""
    targets: set[str] = set()
    for arguments in re.findall(r"(?:^|[;&|(]\s*)make((?:\s+[^\s;&|]+)*)", command):
        targets.update(a for a in arguments.split() if not a.startswith("-") and "=" not in a)
    return targets


def test_jobs_that_run_the_tests_install_their_tools() -> None:
    """Without mosquitto or GNU gettext the tests fail (CONTRIBUTING.md): in the release
    workflow that would lose the version of the tag (docs/development.md)."""
    testing = []
    for workflow in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        jobs = workflow.read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
        for job in re.split(r"^  (?=[\w-]+:$)", jobs, flags=re.MULTILINE)[1:]:
            commands = job_commands(job)
            if not {"check", "test", "matrix"} & set().union(*map(make_targets, commands)):
                continue
            testing.append(f"{workflow.name}: {job.split(':', 1)[0]}")
            installs = [c for c in commands if "apt-get install" in c]
            assert any(
                re.search(r"\bmosquitto\b", c) and re.search(r"\bgettext\b", c) for c in installs
            ), f"{testing[-1]} installs {installs}"
    assert testing == ["ci.yml: test", "release.yml: check"]


def workflow_jobs(name: str) -> dict[str, str]:
    """The jobs of a workflow by their id, each with its text."""
    workflow = (ROOT / ".github" / "workflows" / name).read_text(encoding="utf-8")
    jobs = re.split(r"^  (?=[\w-]+:$)", workflow.split("\njobs:\n", 1)[1], flags=re.MULTILINE)
    return {job.split(":", 1)[0]: job for job in jobs[1:]}


def test_the_release_is_built_where_no_test_or_lint_tool_runs() -> None:
    """SUP-002: a test or lint tool (or one of their dependencies) cannot touch the release files.
    They are made in an environment of the build tools alone, made again on another runner and
    compared byte for byte, and published only after the checks, the build and the rebuild."""
    jobs = workflow_jobs("release.yml")
    release = "make release VENV=.venv-release REQUIREMENTS=requirements/release.txt"
    assert [job for job, text in jobs.items() if release in job_commands(text)] == [
        "build",
        "rebuild",
    ]
    assert job_commands(jobs["check"])[-1] == "make check"
    releasing = [
        job
        for job, text in jobs.items()
        if "release" in set().union(*map(make_targets, job_commands(text)))
    ]
    assert releasing == ["build", "rebuild"]
    assert "diff built/release/SHA256SUMS dist/release/SHA256SUMS" in job_commands(jobs["rebuild"])
    assert "    needs: [check, build, rebuild]\n" in jobs["publish"]
    lock = (ROOT / "requirements" / "release.txt").read_text(encoding="utf-8")
    locked = set(re.findall(r"^([\w.-]+)==", lock, flags=re.MULTILINE))
    assert {"build", "flit-core"} <= locked
    assert not locked & {"pytest", "coverage", "ruff", "mypy", "zizmor", "shellcheck-py"}
    environment = make(
        "--dry-run", "VENV=.venv-x", "REQUIREMENTS=requirements/release.txt", ".venv-x/.locked"
    )
    assert "--require-hashes --no-deps -r requirements/release.txt" in environment.stdout


# the locks of the Home Assistant series: requirements.txt, and one per HA_SERIES once it exists
HOME_ASSISTANT_LOCKS = sorted(ROOT.glob("home-assistant/requirements*.txt"))


@pytest.mark.parametrize(
    "lock",
    [
        "requirements/dev.txt",
        "requirements/release.txt",
        "requirements/audit.txt",
        *(lock.relative_to(ROOT).as_posix() for lock in HOME_ASSISTANT_LOCKS),
    ],
)
def test_a_lock_file_has_the_form_dependabot_writes(lock: str) -> None:
    """SUP-004: resolved for Linux alone and from inside its directory, as `make lock` and
    `make ha-lock` write it; a pull request of Dependabot then changes versions and hashes only."""
    text = (ROOT / lock).read_text(encoding="utf-8")
    command = text.splitlines()[1]
    assert command.startswith(f"#    uv pip compile {Path(lock).with_suffix('.in').name} ")
    assert "--python-platform linux --no-strip-extras --generate-hashes" in command
    assert "--universal" not in command
    assert not re.findall(r"^\S+==\S+ ;", text, flags=re.MULTILINE)  # no environment markers
    assert not re.findall(r"# via -r \S+/", text)


def test_every_lock_file_is_audited_and_the_tool_chains_fail_the_audit() -> None:
    """SUP-005: CI looks up each lock file in the advisory database. A vulnerability in a tool
    chain fails it; in the Home Assistant test environment, whose versions the pinned Home
    Assistant decides, it warns (home-assistant/overrides.txt raises a version past one)."""
    dry_run = make("--dry-run", "audit")
    assert dry_run.returncode == 0, dry_run.stderr
    lines = dry_run.stdout.replace("\\\n", " ").splitlines()  # a command with its continuations
    audits = [line for line in lines if "/pip-audit " in line]
    locks = [*sorted(ROOT.glob("requirements/*.txt")), *HOME_ASSISTANT_LOCKS]
    assert ROOT / "home-assistant/requirements.txt" in locks
    for lock in locks:
        name = lock.relative_to(ROOT).as_posix()
        [audit] = [line for line in audits if f"-r {name}" in line]
        assert ("||" in audit) is name.startswith("home-assistant/"), audit
    assert all("--require-hashes --strict" in audit for audit in audits)
    ci = job_commands(workflow_jobs("ci.yml")["check"])
    assert "audit" in set().union(*map(make_targets, ci))


def test_the_lock_of_each_home_assistant_series_is_audited_on_its_own(tmp_path: Path) -> None:
    """A series' lock, once `make ha-lock HA_SERIES=…` wrote it, is audited as the first one is,
    in a run of its own: the two pin different releases of the same packages."""
    locks = ["home-assistant/requirements.txt", "home-assistant/requirements-2099.1.txt"]
    for name in (
        "requirements/dev.txt",
        "requirements/release.txt",
        "requirements/audit.txt",
        *locks,
    ):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text("", encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in MAKE_ENVIRONMENT}
    dry_run = subprocess.run(  # noqa: S603 - our Makefile with fixed arguments
        [
            MAKE,
            "--no-print-directory",
            "--dry-run",
            "-f",
            str(ROOT / "Makefile"),
            "-C",
            str(tmp_path),
            "audit",
            "MAKE=true",  # the environment of pip-audit: not made here
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert dry_run.returncode == 0, dry_run.stderr
    runs = dry_run.stdout.replace("\\\n", " ").replace(";", "\n").splitlines()
    for lock in locks:
        [run] = [line for line in runs if f"-r {lock} " in line]
        assert "/pip-audit " in run
        assert run.count(" -r ") == 1
        assert "|| echo" in run


def ruleset(name: str) -> dict[str, Any]:
    """A ruleset as exported from the repository's settings to .github/rulesets (SECURITY.md)."""
    path = ROOT / ".github" / "rulesets" / f"{name}.json"
    exported: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return exported


def test_release_tags_cannot_be_moved_or_deleted() -> None:
    """ADR 0022 relies on it and SUP-007 asks to show it: the tag ruleset keeps every release tag
    on the commit CI tagged."""
    tags = ruleset("v-star")
    assert (tags["target"], tags["enforcement"]) == ("tag", "active")
    assert tags["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
    assert {"deletion", "non_fast_forward", "update"} <= {rule["type"] for rule in tags["rules"]}


GITHUB_ACTIONS = 15368  # the app id of GitHub Actions: no other app can report a required check


def test_main_takes_pull_requests_that_passed_every_job_of_ci() -> None:
    """SUP-007: no push, force push or deletion on main, and a squash merge only after the jobs
    CI runs on a pull request passed. A job renamed in ci.yml without the ruleset would block
    every merge, so the required checks are the names of those jobs, the matrix spelt out."""
    main = ruleset("main")
    assert (main["target"], main["enforcement"]) == ("branch", "active")
    assert main["conditions"]["ref_name"]["include"] == ["~DEFAULT_BRANCH"]
    rules = {rule["type"]: rule.get("parameters", {}) for rule in main["rules"]}
    assert {"deletion", "non_fast_forward", "pull_request", "required_status_checks"} <= set(rules)
    assert rules["pull_request"]["allowed_merge_methods"] == ["squash"]
    checks = rules["required_status_checks"]["required_status_checks"]
    assert {check["integration_id"] for check in checks} == {GITHUB_ACTIONS}
    names = set()
    for text in workflow_jobs("ci.yml").values():
        if "github.event_name == 'push'" in text:  # the release: on main, never on a pull request
            continue
        [name] = re.findall(r"^    name: (.+)$", text, flags=re.MULTILINE)
        versions = re.findall(r"^\s+python: \[(.+)\]$", text, flags=re.MULTILINE)
        values = [v.strip(' "') for v in versions[0].split(",")] if versions else [""]
        names |= {name.replace("${{ matrix.python }}", value) for value in values}
    assert {check["context"] for check in checks} == names


def test_a_release_is_tagged_a_day_after_its_version_commit() -> None:
    """SUP-001: CI's job tags a pre-release, a release candidate too, at once and leaves a release
    to release-due.yml, which every hour tags the version commit of main once it is due and CI
    passed on it, and starts its release once: it had tagged 2.0.0a1 before CI on main had
    finished."""
    release = workflow_jobs("ci.yml")["release"]
    assert "python3 -I tools/release_plan.py tag --base" in release
    assert release.count("steps.plan.outputs.waits != 'true'") == 2
    due = (ROOT / ".github" / "workflows" / "release-due.yml").read_text(encoding="utf-8")
    assert re.search(r'^    - cron: "\d+ \* \* \* \*"$', due, flags=re.MULTILINE)
    assert "python3 -I tools/release_plan.py due" in due
    assert "SHA: ${{ steps.plan.outputs.sha }}" in due
    assert 'gh run list --workflow ci.yml --commit "$SHA" --event push --status success' in due
    assert "        if: steps.tested.outputs.passed == 'true'\n" in due
    assert 'gh run list --workflow release.yml --branch "$TAG" --json status --jq length' in due


def _numbers(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def test_the_home_assistant_lock_keeps_its_overrides() -> None:
    """SUP-005: home-assistant/overrides.txt raises versions past known vulnerabilities, and only
    `make ha-lock` applies it: Dependabot's lock in #56 dropped them. Every package it names is
    locked at a version it allows."""
    home_assistant = ROOT / "home-assistant"
    overrides = (home_assistant / "overrides.txt").read_text(encoding="utf-8")
    raised = re.findall(r"^([\w.-]+)(>=|==)([\d.]+)$", overrides, flags=re.MULTILINE)
    assert raised
    for path in HOME_ASSISTANT_LOCKS:  # every series' lock: make ha-lock applies the same file
        lock = path.read_text(encoding="utf-8")
        locked = {
            name.lower().replace("_", "-"): version
            for name, version in re.findall(r"^([\w.-]+)==([\d.]+) ", lock, flags=re.MULTILINE)
        }
        for name, operator, version in raised:
            found = locked.get(name.lower().replace("_", "-"))
            assert found is not None, f"{name} is not in {path.name}: make ha-lock"
            allowed = _numbers(found) >= _numbers(version) if operator == ">=" else found == version
            assert allowed, f"{name}=={found} in {path.name}, {operator}{version} in overrides.txt"


def test_each_home_assistant_series_has_its_own_lock_and_environment(tmp_path: Path) -> None:
    """Since 2026.11 Home Assistant brings its own test plugin, and no lock holds both series
    (#115): HA_SERIES picks the series' lock and environment. Without its lock the series
    is not tested; only `make ha-lock HA_SERIES=…` writes it, under the cooldown."""
    default = make("--dry-run", "ha-lock")
    assert "uv pip compile requirements.in " in default.stdout
    assert "--output-file requirements.txt" in default.stdout
    series = make("--dry-run", "ha-lock", "HA_SERIES=2026.11")
    assert series.returncode == 0, series.stderr
    assert "uv pip compile requirements-2026.11.in " in series.stdout
    assert "--output-file requirements-2026.11.txt" in series.stdout
    assert "--exclude-newer \"$(date -u -d '14 days ago' +%Y-%m-%d)\"" in series.stdout
    assert (ROOT / "home-assistant" / "requirements-2026.11.in").is_file()
    for name, lock, venv in (
        ("", "requirements.txt", ".venv-homeassistant"),
        ("2026.11", "requirements-2026.11.txt", ".venv-homeassistant-2026.11"),
    ):
        environment = tmp_path / f"venv{name}"
        installed = make(
            "--dry-run",
            f"HA_SERIES={name}",
            f"HA_VENV={environment}",
            f"{environment}/.installed",
        )
        assert f"-r home-assistant/{lock}" in installed.stdout
        tested = make("--dry-run", "ha-test", f"HA_SERIES={name}")
        assert f"{venv}/bin/python -m pytest -c home-assistant/pytest.ini" in tested.stdout
    environment = tmp_path / "venv"
    missing = make("ha-test", "HA_SERIES=2000.1", f"HA_VENV={environment}")
    assert missing.returncode != 0
    assert (
        "no lock home-assistant/requirements-2000.1.txt yet; make ha-lock HA_SERIES=2000.1"
        in missing.stderr
    )
    assert not environment.exists()


def test_ci_tests_the_second_home_assistant_series_once_its_lock_exists() -> None:
    """The contract suite runs on both series in CI (#115); the job of 2026.11
    runs it from the commit that adds the series' lock and says why it does not before."""
    job = workflow_jobs("ci.yml")["homeassistant-2026-11"]
    lock = "hashFiles('home-assistant/requirements-2026.11.txt')"
    commands = job_commands(job)
    [run] = [c for c in commands if "ha-test" in make_targets(c)]
    assert "HA_SERIES=2026.11" in run
    steps = job.split("\n      - ")[1:]
    [test] = [s for s in steps if run in s]
    assert f"if: {lock} != ''" in test
    assert any(f"if: {lock} == ''" in s and "::notice::" in s for s in steps)
    release = workflow_jobs("ci.yml")["release"]
    assert re.search(r"needs: \[[^]]*\bhomeassistant-2026-11\b", release)


def test_make_targets_are_read_from_commands() -> None:
    assert make_targets("make check") == {"check"}
    assert make_targets('make --jobs="$(nproc)" firmware-test') == {"firmware-test"}
    assert make_targets("make ha-test HA_PYTHON=python") == {"ha-test"}
    assert make_targets("sudo apt-get update && make lint test") == {"lint", "test"}
    assert make_targets("python3 -I tools/release_plan.py tag") == set()


def test_dist_refuses_a_compiled_catalog(tmp_path: Path) -> None:
    """flit packs every file of the package, untracked ones too: a catalog Poedit compiled next
    to its PO file would reach the wheel and the sdist."""
    locale = tmp_path / "src" / "meshcore_home_gateway" / "locale"
    locale.mkdir(parents=True)
    (locale / "xx.po").write_text("", encoding="utf-8")
    (tmp_path / "requirements").mkdir()
    for name in ("requirements/dev.txt", "pyproject.toml"):
        (tmp_path / name).write_text("", encoding="utf-8")
    environment = tmp_path / "venv"
    environment.mkdir()
    for stamp in (".locked", ".installed"):  # newer than the lock and pyproject.toml
        (environment / stamp).write_text("", encoding="utf-8")
    tools = tmp_path / "tools"
    tools.mkdir()
    executable(tools / "python", '#!/bin/sh\necho "python $*" >>"$0.log"\n')
    options = [f"VENV={environment}", f"BIN={tools}", "MAKE=true"]
    env = {k: v for k, v in os.environ.items() if k not in MAKE_ENVIRONMENT}

    def dist() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - our Makefile with fixed arguments
            [
                MAKE,
                "--no-print-directory",
                "-f",
                str(ROOT / "Makefile"),
                "-C",
                str(tmp_path),
                "dist",
                *options,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    clean = dist()
    assert clean.returncode == 0, clean.stderr
    assert "-m build" in (tools / "python.log").read_text(encoding="utf-8")
    (tools / "python.log").unlink()
    (locale / "xx.mo").write_bytes(b"\xde\x12\x04\x95")
    refused = dist()
    assert refused.returncode != 0
    assert "src/meshcore_home_gateway/locale/xx.mo" in refused.stderr
    assert "flit would pack it" in refused.stderr
    assert not (tools / "python.log").exists()  # refused before the build


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = module
    spec.loader.exec_module(module)
    return module


# `make release` with stubs for the build: a zipapp that names its version, release notes
STUB_PYTHON = """#!/bin/sh
case "$1" in
  -c) echo 9.9.9 ;;
  tools/build_pyz.py) printf '#!/bin/sh\\necho 9.9.9\\n' >"$5" && chmod +x "$5" ;;
  tools/release_notes.py) echo notes ;;
  *) exec {python} "$@" ;;
esac
"""


def test_a_release_publishes_the_pack_of_each_language_its_installer_offers(
    tmp_path: Path,
) -> None:
    """Beside the installer, which fetches it from its release (lxc.sh pack_text), listed in
    SHA256SUMS; refused when it is not the pack the installer pins."""
    tool = _load_tool()
    (tmp_path / "requirements").mkdir()
    for name in ("requirements/dev.txt", "pyproject.toml"):
        (tmp_path / name).write_text("", encoding="utf-8")
    environment = tmp_path / "venv"
    environment.mkdir()
    for stamp in (".locked", ".installed"):  # newer than the lock and pyproject.toml
        (environment / stamp).write_text("", encoding="utf-8")
    stubs = tmp_path / "stubs"
    stubs.mkdir()
    executable(stubs / "python", STUB_PYTHON.format(python=sys.executable))
    (tmp_path / "tools").mkdir()
    shutil.copy(ROOT / "tools" / "i18n.py", tmp_path / "tools" / "i18n.py")
    # EXT-004: the AsyncAPI document, from the snapshot, for the version the release builds
    asyncapi = ROOT / "tools" / "asyncapi.py"
    shutil.copy(asyncapi, tmp_path / "tools" / "asyncapi.py")
    spec = importlib.util.spec_from_file_location("asyncapi_tool", asyncapi)
    assert spec is not None
    assert spec.loader is not None
    snapshot = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(snapshot)
    (tmp_path / "tests").mkdir()
    shutil.copy(snapshot.SNAPSHOT, tmp_path / snapshot.SNAPSHOT.relative_to(ROOT))
    (tmp_path / "docs").mkdir()

    def interface(version: str) -> None:
        subprocess.run(  # noqa: S603 - our tool with fixed arguments
            [
                sys.executable,
                str(tmp_path / "tools" / "asyncapi.py"),
                "--write",
                "--release",
                version,
            ],
            capture_output=True,
            check=True,
        )

    interface("9.9.9")
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in (
        "meshcore_home_gateway-9.9.9-py3-none-any.whl",
        "meshcore_home_gateway-9.9.9.tar.gz",
    ):
        (dist / name).write_text("", encoding="utf-8")
    (tmp_path / "deploy" / "systemd").mkdir(parents=True)
    unit = "deploy/systemd/meshcore-home-gateway.service"
    shutil.copy(ROOT / unit, tmp_path / unit)
    locale = tmp_path / "deploy" / "proxmox" / "locale"
    locale.mkdir(parents=True)
    pack = "# meshcore-home-gateway installer messages, xx\nmsg\tYes\t‹Yes›\n"
    (locale / "xx.messages").write_text(pack, encoding="utf-8")
    block = tool.pin_block({"en": "English", "xx": "Pseudo"}, {"xx": pack})
    pins = 'RELEASE_PYZ_SHA256=""\nRELEASE_UNIT_SHA256=""\n'
    script = f'#!/usr/bin/env bash\nRELEASE=""     # make release\n{pins}{block}'
    (locale.parent / "meshcore-home-gateway-lxc.sh").write_text(script, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in MAKE_ENVIRONMENT}

    def release() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - our Makefile with fixed arguments
            [
                MAKE,
                "--no-print-directory",
                "-f",
                str(ROOT / "Makefile"),
                "-C",
                str(tmp_path),
                "release",
                f"VENV={environment}",
                f"BIN={stubs}",
                "MAKE=true",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    made = release()
    assert made.returncode == 0, made.stderr
    published = dist / "release" / "meshcore-home-gateway-lxc.xx.messages"
    assert published.read_text(encoding="utf-8") == pack
    digest = hashlib.sha256(pack.encode("utf-8")).hexdigest()
    sums = (dist / "release" / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"{digest}  meshcore-home-gateway-lxc.xx.messages\n" in sums
    assert f"[xx]={digest}" in (dist / "release" / "meshcore-home-gateway-lxc.sh").read_text(
        encoding="utf-8"
    )
    # OPS-016: the unit names the release it came with, also once a rollback keeps it
    released = (dist / "release" / "meshcore-home-gateway.service").read_text(encoding="utf-8")
    source = (ROOT / unit).read_text(encoding="utf-8").split("\n")
    assert released.split("\n") == [source[0], "# Version: 9.9.9", *source[2:]]
    assert "  meshcore-home-gateway.service\n" in sums
    # OPS-002: the installer pins what it installs, so its attestation covers the program
    pinning = (dist / "release" / "meshcore-home-gateway-lxc.sh").read_text(encoding="utf-8")
    for name, file in (
        ("PYZ", "meshcore-home-gateway-9.9.9.pyz"),
        ("UNIT", "meshcore-home-gateway.service"),
    ):
        digest = hashlib.sha256((dist / "release" / file).read_bytes()).hexdigest()
        assert f'\nRELEASE_{name}_SHA256="{digest}"\n' in pinning
        assert f"{digest}  {file}\n" in sums
    # EXT-004: the MQTT API for machines, as of this release
    api = (dist / "release" / "meshcore-home-gateway-9.9.9.asyncapi.yaml").read_bytes()
    assert api == (tmp_path / "docs" / "mqtt-api.asyncapi.yaml").read_bytes()
    assert b'\n    "version": "9.9.9",\n' in api
    assert f"{hashlib.sha256(api).hexdigest()}  meshcore-home-gateway-9.9.9.asyncapi.yaml\n" in sums

    (locale / "xx.messages").write_text(pack + "msg\tNo\t‹No›\n", encoding="utf-8")
    refused = release()
    assert refused.returncode != 0
    assert "deploy/proxmox/locale/xx.messages: not the pack the installer pins" in refused.stderr
    assert not (dist / "release" / "SHA256SUMS").exists()

    (locale / "xx.messages").write_text(pack, encoding="utf-8")
    text = (tmp_path / unit).read_text(encoding="utf-8")
    (tmp_path / unit).write_text(text.replace("# Version:", "# Release:"), encoding="utf-8")
    unnamed = release()
    assert unnamed.returncode != 0
    assert "release: the unit names no version" in unnamed.stderr
    assert not (dist / "release" / "SHA256SUMS").exists()

    (tmp_path / unit).write_text(text, encoding="utf-8")
    installer = locale.parent / "meshcore-home-gateway-lxc.sh"
    installer.write_text(script.replace(pins, 'RELEASE_PYZ_SHA256=""\n'), encoding="utf-8")
    unpinned = release()
    assert unpinned.returncode != 0
    assert "release: the installer pins no digests" in unpinned.stderr
    assert not (dist / "release" / "SHA256SUMS").exists()

    installer.write_text(script, encoding="utf-8")
    interface("9.9.8")
    stale = release()  # a version written without `make interface`
    assert stale.returncode != 0
    assert "docs/mqtt-api.asyncapi.yaml is not the snapshot's: make interface" in stale.stderr
    assert not (dist / "release" / "SHA256SUMS").exists()
