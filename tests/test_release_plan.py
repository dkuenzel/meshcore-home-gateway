"""The release plan (tools/release_plan.py): a change that writes a version must write the next one,
with its changelog section; CI on main tags what passed. Runs in temporary git repositories."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
GIT = shutil.which("git") or "/usr/bin/git"


def _load_tool() -> ModuleType:
    path = ROOT / "tools" / "release_plan.py"
    spec = importlib.util.spec_from_file_location("release_plan", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_plan"] = module
    spec.loader.exec_module(module)
    return module


plan = _load_tool()
Version = plan.Version

INTRO = "# Changelog\n\nIntro.\n\n"


def section(version: str, *entries: str, kind: str = "Added") -> str:
    body = "\n".join(f"- {e}" for e in entries)
    return f"## [{version}] - 2026-11-02\n\n### {kind}\n\n{body}\n\n"


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.env = {
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org",
            "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
            "HOME": str(path), "PATH": os.environ.get("PATH", ""),
        }  # fmt: skip
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str) -> str:
        run = subprocess.run(  # noqa: S603 - fixed arguments in a temporary repository
            [GIT, *args], cwd=self.path, env=self.env, capture_output=True, text=True, check=True
        )
        return run.stdout

    def commit(self, version: str, changelog: str, *, program: str = "pass\n") -> str:
        files = {
            plan.VERSION_FILE: f'"""Package."""\n\n__version__ = "{version}"\n',
            "src/meshcore_home_gateway/main.py": program,
            "deploy/unit.service": "[Unit]\n",
            "pyproject.toml": "[project]\n",
            "tools/build_pyz.py": "pass\n",
            "CHANGELOG.md": INTRO + changelog,
        }
        for name, text in files.items():
            (self.path / name).parent.mkdir(parents=True, exist_ok=True)
            (self.path / name).write_text(text, encoding="utf-8")
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", version)
        return self.git("rev-parse", "HEAD").strip()

    def tag(self, name: str, *, annotated: bool = True) -> None:
        if annotated:
            self.git("tag", "-a", name, "-m", name)
        else:
            self.git("tag", name)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    made = Repo(tmp_path)

    def git(*args: str) -> str:  # as the tool's own: a failure is a PlanError
        try:
            return made.git(*args)
        except subprocess.CalledProcessError as exc:
            raise plan.PlanError(exc.stderr) from None

    monkeypatch.setattr(plan, "git", git)
    return made


def released(repo: Repo, *history: tuple[str, str]) -> None:
    """Commits and tags each (version, changelog) in turn."""
    for version, changelog in history:
        repo.commit(version, changelog)
        repo.tag(f"v{version}")


FIRST = section("1.0.0rc1", "The gateway.")


# --- versions -------------------------------------------------------------------------------------


def test_versions_order_as_pep_440() -> None:
    order = ["1.0.0a1", "1.0.0a2", "1.0.0b1", "1.0.0rc1", "1.0.0rc2", "1.0.0", "1.0.1a1", "1.1.0"]
    assert sorted(order, key=Version.parse) == order
    assert [str(Version.parse(v)) for v in order] == order


@pytest.mark.parametrize(
    "text", ["1.0.0.dev1", "1.0.0.post1", "1.0", "v1.0.0", "1.0.0rc", "01.0.0"]
)
def test_other_version_forms_are_refused(text: str) -> None:
    with pytest.raises(plan.PlanError, match="is not one of"):
        Version.parse(text)


def test_the_kind_is_the_highest_since_the_last_release() -> None:
    changelog = (
        "## [Unreleased]\n\n### Fixed\n\n- A bug.\n\n"
        + section("1.1.0a1", "A feature.")
        + section("1.0.0", "Old, removed long ago.", kind="Removed")
    )
    assert plan.kind(changelog, Version.parse("1.0.0")) == "minor"
    assert plan.kind(changelog, None) == "major"
    fixes = "## [Unreleased]\n\n### Fixed\n\n- A bug.\n\n### Security\n\n- A hole.\n\n"
    assert plan.kind(fixes, None) == "patch"
    breaking = "## [Unreleased]\n\n### Changed\n\n- **Breaking:** a topic renamed.\n\n"
    assert plan.kind(breaking, None) == "major"
    assert plan.kind("## [Unreleased]\n\n### Added\n\n", None) is None  # a heading alone


# --- check on a pull request ----------------------------------------------------------------------


def test_a_change_that_keeps_the_version_releases_nothing(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    base = repo.commit("1.0.0rc1", "## [Unreleased]\n\n### Added\n\n- More.\n\n" + FIRST)
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n### Added\n\n- More and more.\n\n" + FIRST)
    assert plan.check(base) == "no release: __version__ stays 1.0.0rc1"


def test_before_1_0_0_only_its_candidates_and_itself_are_next(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("1.0.0rc2", "## [Unreleased]\n\n" + section("1.0.0rc2", "Fixes.") + FIRST)
    assert plan.check(base) == "releases v1.0.0rc2 as a pre-release"
    repo.commit("1.0.0a1", "## [Unreleased]\n\n" + section("1.0.0a1", "Late.") + FIRST)
    with pytest.raises(plan.PlanError, match=re.escape("not newer than v1.0.0rc1")):
        plan.check(base)
    repo.commit("1.1.0a1", "## [Unreleased]\n\n" + section("1.1.0a1", "Ahead.") + FIRST)
    with pytest.raises(plan.PlanError, match=re.escape("the next release is 1.0.0")):
        plan.check(base)


def test_a_release_ships_the_program_of_its_candidate(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    final = "## [Unreleased]\n\n" + section("1.0.0", "The gateway, tested.") + FIRST
    repo.commit("1.0.0", final)  # docs may change, the program not
    assert plan.check(base) == "releases v1.0.0"
    repo.commit("1.0.0", final, program="print('changed')\n")
    with pytest.raises(plan.PlanError, match=re.escape("does not ship the program of v1.0.0rc1")):
        plan.check(base)


def test_a_release_needs_a_candidate(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST), ("1.0.0", section("1.0.0", "Out.") + FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("1.0.1", "## [Unreleased]\n\n" + section("1.0.1", "A fix.", kind="Fixed"))
    with pytest.raises(plan.PlanError, match=re.escape("needs a release candidate v1.0.1rcN")):
        plan.check(base)


@pytest.mark.parametrize(
    ("kind", "entry", "written", "expected"),
    [
        ("Added", "A feature.", "1.1.0a1", None),
        ("Fixed", "A bug.", "1.0.1a1", None),
        ("Fixed", "A bug.", "1.1.0a1", "the next release is 1.0.1"),
        ("Changed", "**Breaking:** a key renamed.", "2.0.0a1", None),
        ("Removed", "An old setting.", "2.0.0a1", None),
        ("Added", "A feature.", "1.0.1a1", "the next release is 1.1.0"),
        ("Added", "A feature.", "1.1.0a2", "the next a of 1.1.0 is 1.1.0a1"),
    ],
)
def test_the_changelog_decides_the_kind(
    repo: Repo, kind: str, entry: str, written: str, expected: str | None
) -> None:
    released(repo, ("1.0.0rc1", FIRST), ("1.0.0", section("1.0.0", "Out.") + FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    history = section("1.0.0", "Out.") + FIRST
    repo.commit(written, "## [Unreleased]\n\n" + section(written, entry, kind=kind) + history)
    if expected is None:
        assert plan.check(base) == f"releases v{written} as a pre-release"
    else:
        with pytest.raises(plan.PlanError, match=expected):
            plan.check(base)


def test_pre_releases_count_on_and_their_entries_count_too(repo: Repo) -> None:
    history = section("1.0.0", "Out.") + FIRST
    released(repo, ("1.0.0rc1", FIRST), ("1.0.0", history))
    released(repo, ("1.1.0a1", section("1.1.0a1", "A feature.") + history))
    base = repo.git("rev-parse", "HEAD").strip()
    fixes = section("1.1.0a2", "A fix.", kind="Fixed") + section("1.1.0a1", "A feature.") + history
    repo.commit("1.1.0a2", "## [Unreleased]\n\n" + fixes)  # still 1.1.0: the feature counts
    assert plan.check(base) == "releases v1.1.0a2 as a pre-release"


def test_the_changelog_must_have_the_section_after_an_empty_unreleased(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("1.0.0rc2", "## [Unreleased]\n\n" + FIRST)
    with pytest.raises(plan.PlanError, match=r"no section '## \[1.0.0rc2\]"):
        plan.check(base)
    pending = "## [Unreleased]\n\n### Added\n\n- Not yet.\n\n" + section("1.0.0rc2", "Fixes.")
    repo.commit("1.0.0rc2", pending + FIRST)
    with pytest.raises(plan.PlanError, match=r"an empty \[Unreleased\]"):
        plan.check(base)
    repo.commit("1.0.0rc2", section("1.0.0rc2", "Fixes.") + FIRST)  # no [Unreleased] at all
    with pytest.raises(plan.PlanError, match=r"an empty \[Unreleased\]"):
        plan.check(base)


def test_nothing_new_since_the_last_release_releases_nothing(repo: Repo) -> None:
    history = section("1.0.0", "Out.") + FIRST
    released(repo, ("1.0.0rc1", FIRST), ("1.0.0", history))
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("1.0.1a1", "## [Unreleased]\n\n## [1.0.1a1] - 2026-11-03\n\nNothing.\n\n" + history)
    with pytest.raises(plan.PlanError, match=re.escape("nothing in CHANGELOG.md since 1.0.0")):
        plan.check(base)


def test_an_older_base_version_form_is_no_obstacle(repo: Repo) -> None:
    base = repo.commit("0.1.0.dev0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    assert plan.check(base) == "releases v1.0.0rc1 as a pre-release"


# --- tag on main ----------------------------------------------------------------------------------


def test_main_tags_the_commit_that_changed_the_version(repo: Repo) -> None:
    repo.commit("0.1.0.dev0", "## [Unreleased]\n\n")
    head = repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    assert plan.tag() == ("v1.0.0rc1", False, False)
    repo.tag("v1.0.0rc1")  # a re-run after the tag was made
    assert plan.tag() == ("v1.0.0rc1", True, False)
    assert plan.tags()[Version.parse("1.0.0rc1")] == head  # the commit, not the tag object
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n### Added\n\n- More.\n\n" + FIRST)
    assert plan.tag() == ("", False, False)


def test_main_refuses_a_version_tagged_elsewhere(repo: Repo) -> None:
    repo.commit("0.1.0.dev0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    repo.tag("v1.0.0rc1", annotated=False)
    repo.git("commit", "-q", "--allow-empty", "-m", "later")
    repo.commit("1.0.0rc0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    with pytest.raises(plan.PlanError, match=re.escape("v1.0.0rc1 already tags")):
        plan.tag()


def test_main_tags_a_release_only_when_it_is_the_next(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    repo.commit("1.0.0", "## [Unreleased]\n\n" + section("1.0.0", "Tested.") + FIRST)
    assert plan.tag() == ("v1.0.0", False, True)
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    repo.commit("2.0.0", "## [Unreleased]\n\n" + section("2.0.0", "Wrong.") + FIRST)
    with pytest.raises(plan.PlanError, match=re.escape("the next release is 1.0.0")):
        plan.tag()


def test_the_command_line(repo: Repo, capsys: pytest.CaptureFixture[str]) -> None:
    repo.commit("0.1.0.dev0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    assert plan.main(["tag"]) == 0
    assert capsys.readouterr().out == "tag=v1.0.0rc1\nexists=false\nfinal=false\n"
    assert plan.main(["check"]) == 0
    assert capsys.readouterr().out == "releases v1.0.0rc1 as a pre-release\n"
    repo.tag("v1.0.0rc1")
    repo.commit("1.0.0rc3", "## [Unreleased]\n\n" + section("1.0.0rc3", "Skipped.") + FIRST)
    assert plan.main(["check"]) == 1
    assert "the next rc of 1.0.0 is 1.0.0rc2" in capsys.readouterr().err


def test_the_tool_runs_isolated_as_ci_runs_it(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    repo.commit("0.1.0.dev0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    run = subprocess.run(  # noqa: S603 - the tool with fixed arguments
        [sys.executable, "-I", str(ROOT / "tools" / "release_plan.py"), "check"],
        cwd=tmp_path, env=repo.env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert (run.returncode, run.stdout) == (0, "releases v1.0.0rc1 as a pre-release\n"), run.stderr


def test_the_version_is_the_newest_section_of_the_changelog() -> None:
    """A release writes `__version__` and its changelog section together; sections go down."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    dated = [Version.parse(m) for m in plan._DATED.findall(changelog)]
    init = (ROOT / plan.VERSION_FILE).read_text(encoding="utf-8")
    match = plan._VERSION_LINE.search(init)
    assert match is not None
    assert dated[0] == Version.parse(match.group(1))
    assert dated == sorted(set(dated), reverse=True)


# --- review findings ----------------------------------------------------------------------------


def test_a_tagged_release_commit_checks_again_on_a_re_run(repo: Repo) -> None:
    """'Re-run all jobs' runs the check on the release commit after its tag was made."""
    released(repo, ("1.0.0rc1", FIRST))
    repo.commit("1.0.0", "## [Unreleased]\n\n" + section("1.0.0", "Tested.") + FIRST)
    repo.tag("v1.0.0")
    assert plan.check("HEAD^") == "v1.0.0 tags this commit already"
    assert plan.tag("HEAD^") == ("v1.0.0", True, True)


def test_the_whole_push_counts_not_only_its_last_commit(repo: Repo) -> None:
    """A push of several commits: the version may change in any of them; the tip is tagged."""
    before = repo.commit("1.0.0rc0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST, program="print('follow-up')\n")
    assert plan.check("HEAD^") == "no release: __version__ stays 1.0.0rc1"  # the last commit alone
    assert plan.check(before) == "releases v1.0.0rc1 as a pre-release"
    assert plan.tag(before) == ("v1.0.0rc1", False, False)


@pytest.mark.parametrize("base", ["", "0" * 40, "1" * 40])
def test_an_unknown_base_falls_back_to_the_parent(repo: Repo, base: str) -> None:
    """A new branch (all zeros), a force push (a commit no longer known), no base at all."""
    repo.commit("1.0.0rc0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    assert plan.check(base) == "releases v1.0.0rc1 as a pre-release"
    assert plan.tag(base) == ("v1.0.0rc1", False, False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("### Internal\n\n- A tool.\n\n", "unknown heading '### Internal'"),
        ("- A loose entry.\n\n", "an entry outside a subsection"),
        ("### Added\n\n- A thing.\n  - **Breaking:** its old name goes.\n\n", None),
    ],
)
def test_the_changelog_speaks_keep_a_changelog_only(text: str, expected: str | None) -> None:
    changelog = "## [Unreleased]\n\n" + text + section("1.0.0", "Out.")
    if expected is None:
        assert plan.kind(changelog, Version.parse("1.0.0")) == "major"  # nested: still breaking
    else:
        with pytest.raises(plan.PlanError, match=re.escape(expected)):
            plan.kind(changelog, Version.parse("1.0.0"))


def test_a_branch_named_like_a_tag_does_not_hide_it(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    repo.git("branch", "v1.0.0rc1")
    assert Version.parse("1.0.0rc1") in plan.tags()


def test_a_clone_without_tags_is_told_to_fetch_them(repo: Repo) -> None:
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    repo.commit("1.0.0rc2", "## [Unreleased]\n\n" + section("1.0.0rc2", "More.") + FIRST)
    with pytest.raises(plan.PlanError, match=re.escape("git fetch --tags")):
        plan.check("HEAD^")
