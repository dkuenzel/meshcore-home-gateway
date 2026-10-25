"""The release plan (tools/release_plan.py): a change that writes a version must write the next one,
with its changelog section, and keeps the public interface of the last release unless it says it
breaks it; CI on main tags what passed. Runs in temporary git repositories."""

from __future__ import annotations

import importlib.util
import json
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
SNAPSHOT = '{"format": 1}\n'  # the least public interface (tests/public_interface.json)


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
NONE = "public interface: no release yet to compare with"


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

    def commit(
        self,
        version: str,
        changelog: str,
        *,
        program: str = "pass\n",
        interface: str | None = SNAPSHOT,  # None: no snapshot (any one committed before goes)
    ) -> str:
        files = {
            plan.VERSION_FILE: f'"""Package."""\n\n__version__ = "{version}"\n',
            "src/meshcore_home_gateway/main.py": program,
            "deploy/unit.service": "[Unit]\n",
            "pyproject.toml": "[project]\n",
            "tools/build_pyz.py": "pass\n",
            "CHANGELOG.md": INTRO + changelog,
        }
        if interface is not None:
            files[plan.INTERFACE] = interface
        else:
            (self.path / plan.INTERFACE).unlink(missing_ok=True)
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


def release(base: str = "") -> str:
    """The first line of `check`: what HEAD releases (what the interface was compared with
    follows)."""
    return str(plan.check(base)).partition("\n")[0]


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
    assert release(base) == "no release: __version__ stays 1.0.0rc1"


def test_before_1_0_0_only_its_candidates_and_itself_are_next(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("1.0.0rc2", "## [Unreleased]\n\n" + section("1.0.0rc2", "Fixes.") + FIRST)
    assert release(base) == "releases v1.0.0rc2 as a pre-release"
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
    assert release(base) == "releases v1.0.0"
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
        assert release(base) == f"releases v{written} as a pre-release"
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
    assert release(base) == "releases v1.1.0a2 as a pre-release"


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
    assert release(base) == "releases v1.0.0rc1 as a pre-release"


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
    assert capsys.readouterr().out == (
        "releases v1.0.0rc1 as a pre-release\npublic interface: no release yet to compare with\n"
    )
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
    assert (run.returncode, run.stdout) == (0, f"releases v1.0.0rc1 as a pre-release\n{NONE}\n"), (
        run.stderr
    )


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
    assert release("HEAD^") == "v1.0.0 tags this commit already"
    assert plan.tag("HEAD^") == ("v1.0.0", True, True)


def test_the_whole_push_counts_not_only_its_last_commit(repo: Repo) -> None:
    """A push of several commits: the version may change in any of them; the tip is tagged."""
    before = repo.commit("1.0.0rc0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST, program="print('follow-up')\n")
    assert release("HEAD^") == "no release: __version__ stays 1.0.0rc1"  # the last commit alone
    assert release(before) == "releases v1.0.0rc1 as a pre-release"
    assert plan.tag(before) == ("v1.0.0rc1", False, False)


@pytest.mark.parametrize("base", ["", "0" * 40, "1" * 40])
def test_an_unknown_base_falls_back_to_the_parent(repo: Repo, base: str) -> None:
    """A new branch (all zeros), a force push (a commit no longer known), no base at all."""
    repo.commit("1.0.0rc0", "## [Unreleased]\n\n")
    repo.commit("1.0.0rc1", "## [Unreleased]\n\n" + FIRST)
    assert release(base) == "releases v1.0.0rc1 as a pre-release"
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


# --- the public interface (docs/development.md#compatibility) ------------------------------------

STATE = {"retained": True, "qos": 1, "payload": "json"}
EVENT = {"retained": False, "qos": 1, "payload": "json"}
OLD = {"format": 1, "mqtt": {"publish": {"b/n/state": STATE, "b/n/rx": EVENT}}}
NEW = {"format": 1, "mqtt": {"publish": {"b/n/state": STATE}}}  # rx removed
ALL = {"b/n/state": STATE, "b/n/rx": EVENT, "b/n/tx": EVENT}
GROWN = {"format": 1, "mqtt": {"publish": ALL}}
OUT = section("1.0.0", "Out.") + FIRST
RX = "mqtt > publish > b/n/rx"  # the path of the finding
REMOVED = f"{RX}: removed"  # the finding, as the refusal shows it and a waiver names it
TX = "mqtt > publish > b/n/tx: new"
COMPATIBLE = "public interface: compatible with v1.0.0"


def snapshot(doc: object) -> str:
    return json.dumps(doc) + "\n"


def unreleased(kind: str, entry: str) -> str:
    return f"## [Unreleased]\n\n### {kind}\n\n- {entry}\n\n" + OUT


def after_1_0_0(repo: Repo, interface: object = OLD) -> str:
    """1.0.0rc1 and 1.0.0 released, both with `interface`; the commit of 1.0.0."""
    repo.commit("1.0.0rc1", FIRST, interface=snapshot(interface))
    repo.tag("v1.0.0rc1")
    repo.commit("1.0.0", OUT, interface=snapshot(interface))
    repo.tag("v1.0.0")
    return repo.git("rev-parse", "HEAD").strip()


def test_a_removal_needs_a_breaking_entry_also_without_a_release(repo: Repo) -> None:
    base = after_1_0_0(repo)
    repo.commit("1.0.0", unreleased("Changed", "No rx."), interface=snapshot(NEW))
    with pytest.raises(plan.PlanError, match=re.escape(REMOVED)):
        plan.check(base)
    repo.commit("1.0.0", unreleased("Changed", "**Breaking:** no rx."), interface=snapshot(NEW))
    assert plan.check(base) == (  # each pull request's log lists the breaks the release takes
        "no release: __version__ stays 1.0.0\n"
        f"public interface: breaks v1.0.0 (1 finding), for 2.0.0:\n  {REMOVED}"
    )


def test_a_removed_section_counts_as_breaking(repo: Repo) -> None:
    base = after_1_0_0(repo)
    repo.commit("1.0.0", unreleased("Removed", "rx."), interface=snapshot(NEW))
    assert release(base) == "no release: __version__ stays 1.0.0"


def test_growth_needs_an_entry_that_makes_a_minor_release(repo: Repo) -> None:
    base = after_1_0_0(repo)
    fix = "## [Unreleased]\n\n" + section("1.0.1rc1", "tx.", kind="Fixed") + OUT
    repo.commit("1.0.1rc1", fix, interface=snapshot(GROWN))
    with pytest.raises(plan.PlanError) as refused:
        plan.check(base)
    assert str(refused.value) == (
        "the public interface of v1.0.0 grows or changes, but CHANGELOG.md has only fixes since:"
        " describe it under ### Added or ### Changed (the next release is then a minor one; a"
        f' false alarm is waived in {plan.WAIVERS} under "1.0.0"):\n  {TX}'
    )
    repo.commit("1.0.0", "## [Unreleased]\n\n" + OUT, interface=snapshot(GROWN))
    with pytest.raises(plan.PlanError, match=re.escape("but CHANGELOG.md has no entry since")):
        plan.check(base)
    feature = "## [Unreleased]\n\n" + section("1.1.0rc1", "tx.") + OUT
    repo.commit("1.1.0rc1", feature, interface=snapshot(GROWN))
    assert plan.check(base) == f"releases v1.1.0rc1 as a pre-release\n{COMPATIBLE}"


def test_a_feature_grows_through_its_pre_releases(repo: Repo) -> None:
    """As telemetry does: 1.1.0a1 adds a topic, 1.1.0a2 another with only a fix of its own."""
    base = after_1_0_0(repo)
    feature = section("1.1.0a1", "Telemetry.") + OUT
    repo.commit("1.1.0a1", "## [Unreleased]\n\n" + feature, interface=snapshot(GROWN))
    assert plan.check(base) == f"releases v1.1.0a1 as a pre-release\n{COMPATIBLE}"
    repo.tag("v1.1.0a1")
    base = repo.git("rev-parse", "HEAD").strip()
    more = {"format": 1, "mqtt": {"publish": {**ALL, "b/n/telemetry": STATE}}}
    fix = section("1.1.0a2", "A fix.", kind="Fixed") + feature
    repo.commit("1.1.0a2", "## [Unreleased]\n\n" + fix, interface=snapshot(more))
    assert plan.check(base) == f"releases v1.1.0a2 as a pre-release\n{COMPATIBLE}"


def test_a_patch_release_cannot_hide_a_removal(repo: Repo) -> None:
    base = after_1_0_0(repo)
    fix = "## [Unreleased]\n\n" + section("1.0.1rc1", "rx gone.", kind="Fixed") + OUT
    repo.commit("1.0.1rc1", fix, interface=snapshot(NEW))
    with pytest.raises(plan.PlanError) as refused:
        plan.check(base)
    assert str(refused.value) == (
        "the public interface of v1.0.0 breaks, but CHANGELOG.md has no **Breaking:** entry and"
        " no ### Removed section since (docs/development.md#compatibility; a false alarm is"
        f' waived in {plan.WAIVERS} under "1.0.0"):\n  {REMOVED}'
    )


def test_a_refusal_lists_every_finding(repo: Repo) -> None:
    many = {"format": 1, "mqtt": {"publish": {f"b/n/t{i:02}": STATE for i in range(25)}}}
    base = after_1_0_0(repo, many)
    none = {"format": 1, "mqtt": {"publish": {}}}
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=snapshot(none))
    with pytest.raises(plan.PlanError) as refused:
        plan.check(base)
    assert str(refused.value).count(": removed") == 25


def test_a_breaking_entry_of_a_pre_release_covers_later_changes(repo: Repo) -> None:
    after_1_0_0(repo)
    history = section("2.0.0a1", "**Breaking:** no rx.", kind="Changed") + OUT
    repo.commit("2.0.0a1", "## [Unreleased]\n\n" + history, interface=snapshot(NEW))
    repo.tag("v2.0.0a1")
    base = repo.git("rev-parse", "HEAD").strip()
    gone = {"format": 1, "mqtt": {"publish": {}}}  # state goes too, in 2.0.0 still
    fix = "## [Unreleased]\n\n### Fixed\n\n- x.\n\n" + history
    repo.commit("2.0.0a1", fix, interface=snapshot(gone))
    assert plan.check(base) == (  # review gives the second break its own entry: the log shows it
        "no release: __version__ stays 2.0.0a1\n"
        "public interface: breaks v1.0.0 (2 findings), for 2.0.0:\n"
        f"  {REMOVED}\n  mqtt > publish > b/n/state: removed"
    )


def test_a_waiver_lets_a_finding_of_its_release_pass(repo: Repo) -> None:
    base = after_1_0_0(repo)
    waivers = repo.path / plan.WAIVERS
    for other in ({"0.9.0": {REMOVED: "another release"}}, {"1.0.0": {RX: "its path only"}}):
        waivers.write_text(snapshot(other), encoding="utf-8")
        repo.commit("1.0.0", unreleased("Fixed", "Snapshot."), interface=snapshot(NEW))
        with pytest.raises(plan.PlanError, match=re.escape(REMOVED)):
            plan.check(base)
    waivers.write_text(snapshot({"1.0.0": {REMOVED: "never published"}}), encoding="utf-8")
    repo.commit("1.0.0", unreleased("Fixed", "Snapshot."), interface=snapshot(NEW))
    assert plan.check(base) == f"no release: __version__ stays 1.0.0\n{COMPATIBLE}"


def test_a_waiver_lets_growth_pass_as_a_patch(repo: Repo) -> None:
    """A key that was wrongly missing, a bound wrongly narrow: their fix grows the interface."""
    base = after_1_0_0(repo)
    (repo.path / plan.WAIVERS).write_text(snapshot({"1.0.0": {TX: "a fix"}}), encoding="utf-8")
    fix = "## [Unreleased]\n\n" + section("1.0.1rc1", "tx.", kind="Fixed") + OUT
    repo.commit("1.0.1rc1", fix, interface=snapshot(GROWN))
    assert plan.check(base) == f"releases v1.0.1rc1 as a pre-release\n{COMPATIBLE}"


def test_a_waiver_lets_only_its_one_finding_pass(repo: Repo) -> None:
    """Another value removed from the same list needs a waiver of its own."""
    lists = {"format": 1, "mqtt": {"documents": {"state": ["battery_v", "queue", "radio"]}}}
    base = after_1_0_0(repo, lists)
    gone = 'mqtt > documents > state: no longer has "battery_v"'
    waivers = snapshot({"1.0.0": {gone: "never published"}})
    (repo.path / plan.WAIVERS).write_text(waivers, encoding="utf-8")
    state = ["queue", "radio"]
    fewer = {"format": 1, "mqtt": {"documents": {"state": state}}}
    repo.commit("1.0.0", unreleased("Fixed", "Snapshot."), interface=snapshot(fewer))
    assert release(base) == "no release: __version__ stays 1.0.0"
    state.remove("radio")
    repo.commit("1.0.0", unreleased("Fixed", "Snapshot."), interface=snapshot(fewer))
    with pytest.raises(plan.PlanError) as refused:
        plan.check(base)
    assert 'no longer has "radio"' in str(refused.value)
    assert "battery_v" not in str(refused.value)


@pytest.mark.parametrize(
    "waivers", [snapshot({"1.0.0": {REMOVED: " "}}), snapshot({"1.0.0": [REMOVED]}), "[]", "{"]
)
def test_a_waiver_needs_its_form_and_a_reason(repo: Repo, waivers: str) -> None:
    base = after_1_0_0(repo)
    (repo.path / plan.WAIVERS).write_text(waivers, encoding="utf-8")
    repo.commit("1.0.0", unreleased("Fixed", "Snapshot."), interface=snapshot(NEW))
    with pytest.raises(plan.PlanError, match=re.escape(plan.WAIVERS)):
        plan.check(base)


@pytest.mark.parametrize("key", ["v1.0.0", "1.1.0a1", "1.0"])
def test_a_waiver_under_another_key_than_a_release_is_refused(repo: Repo, key: str) -> None:
    """GitHub shows a pre-release as the newest release; the tag names it with a v."""
    base = after_1_0_0(repo)
    waivers = snapshot({"1.0.0": {}, key: {REMOVED: "never published"}})
    (repo.path / plan.WAIVERS).write_text(waivers, encoding="utf-8")
    repo.commit("1.0.0", unreleased("Fixed", "Snapshot."), interface=snapshot(NEW))
    expected = (
        f'the key "{key}" is not a final release written X.Y.Z; findings against v1.0.0 go under'
        ' "1.0.0"'
    )
    with pytest.raises(plan.PlanError, match=re.escape(expected)):
        plan.check(base)


def test_before_1_0_0_nothing_is_compared(repo: Repo) -> None:
    released(repo, ("1.0.0rc1", FIRST))
    changed = "## [Unreleased]\n\n### Changed\n\n- x.\n\n" + FIRST
    repo.commit("1.0.0rc1", changed, interface=snapshot(NEW))
    assert plan.interface("HEAD") == NONE


def test_a_release_needs_the_snapshot(repo: Repo) -> None:
    repo.commit("1.0.0rc1", FIRST, interface=None)
    repo.tag("v1.0.0rc1")
    repo.commit("1.0.0", "## [Unreleased]\n\n" + OUT, interface=None)
    with pytest.raises(plan.PlanError, match=re.escape(f"a release needs {plan.INTERFACE}")):
        plan.check("HEAD^")
    repo.tag("v1.0.0")  # released before the rule: nothing to compare with
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=snapshot(NEW))
    assert plan.interface("HEAD") == (
        f"public interface: v1.0.0 has no {plan.INTERFACE}, nothing to compare"
    )


def test_deleting_the_snapshot_is_refused(repo: Repo) -> None:
    base = after_1_0_0(repo)
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=None)
    with pytest.raises(
        plan.PlanError, match=re.escape("missing, but v1.0.0 has one: make interface")
    ):
        plan.check(base)


def test_a_branch_without_the_last_release_is_told_to_merge(repo: Repo) -> None:
    """Compared with a release it does not contain, its additions would read as removals."""
    base = after_1_0_0(repo)
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=snapshot(OLD))
    repo.git("checkout", "-q", "main")
    history = section("1.1.0", "tx.") + section("1.1.0rc1", "tx.") + OUT
    released(repo, ("1.1.0rc1", section("1.1.0rc1", "tx.") + OUT), ("1.1.0", history))
    repo.git("checkout", "-q", "feature")
    with pytest.raises(
        plan.PlanError,
        match=re.escape("HEAD does not contain v1.1.0: merge origin/main and run again"),
    ):
        plan.check(base)


def _clone(origin: Repo, path: Path, *options: str) -> None:
    """A clone of `origin` with the tags, as `git fetch --tags origin` gives them."""
    origin.git("clone", "-q", *options, f"file://{origin.path}", str(path))
    subprocess.run(  # noqa: S603 - fixed arguments in a temporary repository
        [GIT, "fetch", "-q", "--tags", "origin"], cwd=path, env=origin.env, check=True
    )


def _checked(path: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - the tool with fixed arguments
        [sys.executable, "-I", str(ROOT / "tools" / "release_plan.py"), "check", "--base", "HEAD"],
        cwd=path, env=env, capture_output=True, text=True, check=False,
    )  # fmt: skip


def test_a_shallow_clone_is_told_to_fetch_the_history(tmp_path: Path) -> None:
    """Without the history between the tag and HEAD, HEAD cannot be shown to contain the tag;
    merging changes nothing. Cloud sessions clone 50 commits deep."""
    (tmp_path / "origin").mkdir()
    origin = Repo(tmp_path / "origin")
    after_1_0_0(origin)
    origin.commit("1.0.0", unreleased("Fixed", "x."), interface=snapshot(OLD))
    _clone(origin, tmp_path / "full")
    assert _checked(tmp_path / "full", origin.env).stdout.endswith(f"{COMPATIBLE}\n")
    shallow = tmp_path / "shallow"
    _clone(origin, shallow, "--depth", "1")
    run = _checked(shallow, origin.env)
    refusal = (
        "release_plan: a shallow clone: git fetch --unshallow and run again (it does not show"
        " whether HEAD contains v1.0.0)\n"
    )
    assert (run.returncode, run.stderr) == (1, refusal)
    subprocess.run(  # noqa: S603 - fixed arguments in a temporary repository
        [GIT, "fetch", "-q", "--unshallow"], cwd=shallow, env=origin.env, check=True
    )
    assert _checked(shallow, origin.env).stdout.endswith(f"{COMPATIBLE}\n")


def test_a_clone_without_the_tag_of_the_last_release_is_told_to_fetch_it(repo: Repo) -> None:
    """A fork synced with the main repository has its commits but not its tags: compared with
    an older release, or with none, a removal would pass."""
    base = after_1_0_0(repo)
    repo.git("tag", "-d", "v1.0.0")
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=snapshot(NEW))
    expected = (
        "CHANGELOG.md has 1.0.0, but there is no tag v1.0.0 here: fetch the tags of the main"
        " repository"
    )
    with pytest.raises(plan.PlanError, match=re.escape(expected)):
        plan.check(base)
    fix = "## [Unreleased]\n\n" + section("1.0.1rc1", "x.", kind="Fixed") + OUT
    repo.commit("1.0.1rc1", fix, interface=snapshot(OLD))
    with pytest.raises(plan.PlanError, match=re.escape(expected)):
        plan.check(base)


def test_the_release_that_is_not_tagged_yet_needs_no_tag(repo: Repo) -> None:
    """The pull request of 1.0.0 and the job on main that tags it: the changelog has 1.0.0."""
    released(repo, ("1.0.0rc1", FIRST))
    base = repo.git("rev-parse", "HEAD").strip()
    repo.commit("1.0.0", "## [Unreleased]\n\n" + OUT)
    assert plan.check(base) == f"releases v1.0.0\n{NONE}"
    assert plan.tag(base) == ("v1.0.0", False, True)


def test_the_section_of_a_release_stays_as_tagged(repo: Repo) -> None:
    """Merging the release into a branch can move the branch's entries of [Unreleased] into the
    release's section without a conflict: they would count for no later release."""
    base = after_1_0_0(repo)
    moved = "## [Unreleased]\n\n" + OUT.replace("- Out.\n", "- Out.\n- A fix.\n", 1)
    repo.commit("1.0.0", moved, interface=snapshot(OLD))
    with pytest.raises(plan.PlanError) as refused:
        plan.check(base)
    assert str(refused.value) == (
        "CHANGELOG.md: the section of 1.0.0 differs from the one at v1.0.0 (a merge can move"
        " entries of [Unreleased] into it): entries since go above it"
    )
    repo.commit("1.0.0", unreleased("Fixed", "A fix."), interface=snapshot(OLD))
    assert plan.check(base) == f"no release: __version__ stays 1.0.0\n{COMPATIBLE}"


def test_a_release_the_branch_does_not_contain_yet_is_not_its_concern(repo: Repo) -> None:
    """A branch from 1.0.0, while main released 1.1.0a1: it is compared with 1.0.0."""
    base = after_1_0_0(repo)
    repo.git("checkout", "-q", "-b", "feature")
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=snapshot(OLD))
    repo.git("checkout", "-q", "main")
    released(repo, ("1.1.0a1", "## [Unreleased]\n\n" + section("1.1.0a1", "tx.") + OUT))
    repo.git("checkout", "-q", "feature")
    assert plan.check(base) == f"no release: __version__ stays 1.0.0\n{COMPATIBLE}"


def test_the_check_says_that_it_reads_the_commit(
    repo: Repo, capsys: pytest.CaptureFixture[str]
) -> None:
    base = after_1_0_0(repo)
    assert plan.main(["check", "--base", base]) == 0
    assert capsys.readouterr().err == ""
    (repo.path / plan.INTERFACE).write_text(snapshot(NEW), encoding="utf-8")
    (repo.path / plan.WAIVERS).write_text("{}\n", encoding="utf-8")
    assert plan.main(["check", "--base", base]) == 0  # a note, no refusal
    assert capsys.readouterr().err == (
        "release_plan: the check reads HEAD; commit first (tests/public_interface.json,"
        " tests/public_interface_waivers.json changed)\n"
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("{", "not JSON"),
        ("[]", "not a JSON object"),
        (snapshot({**NEW, "format": 2}), "at HEAD: snapshot format 2, this tool reads 1"),
        (snapshot({"format": 1, "config": {"x": {"colour": "red"}}}), "no rule for the attribute"),
        (
            snapshot({"format": 1, "config": {"x": {"min": "1"}}}),
            'x > min: expected a number, got "1"',
        ),
        (snapshot({"format": 1, "config": {"x": {"choices": [{}]}}}), "x > choices: expected a"),
    ],
)
def test_a_snapshot_that_cannot_be_compared_is_refused(
    repo: Repo, text: str, expected: str
) -> None:
    base = after_1_0_0(repo)
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=text)
    with pytest.raises(plan.PlanError, match=re.escape(expected)):
        plan.check(base)


def test_a_release_snapshot_the_comparator_no_longer_reads_is_named(repo: Repo) -> None:
    """An attribute that tools/public_interface.py dropped: the release's snapshot still has it."""
    base = after_1_0_0(repo, {"format": 1, "config": {"x": {"colour": "red"}}})
    repo.commit("1.0.0", unreleased("Fixed", "x."), interface=SNAPSHOT)
    with pytest.raises(plan.PlanError) as refused:
        plan.check(base)
    assert str(refused.value) == (
        f"{plan.INTERFACE} at v1.0.0: config > x: no rule for the attribute 'colour' (the"
        " snapshot of the last release: an attribute dropped or narrowed in"
        " tools/public_interface.py needs a new FORMAT, with a converter of the older ones)"
    )


def test_the_isolated_tool_loads_the_comparator(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    after_1_0_0(repo)
    repo.commit("1.0.0", unreleased("Changed", "No rx."), interface=snapshot(NEW))
    run = subprocess.run(  # noqa: S603 - the tool with fixed arguments
        [sys.executable, "-I", str(ROOT / "tools" / "release_plan.py"), "check"],
        cwd=tmp_path, env=repo.env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert run.returncode == 1
    assert REMOVED in run.stderr


def test_a_malformed_snapshot_is_refused_not_a_traceback(tmp_path: Path) -> None:
    repo = Repo(tmp_path)
    after_1_0_0(repo)
    bad = {"format": 1, "config": {"mqtt.port": {"type": "integer", "min": "1"}}}
    repo.commit("1.0.0", unreleased("Changed", "x."), interface=snapshot(bad))
    run = subprocess.run(  # noqa: S603 - the tool with fixed arguments
        [sys.executable, "-I", str(ROOT / "tools" / "release_plan.py"), "check"],
        cwd=tmp_path, env=repo.env, capture_output=True, text=True, check=False,
    )  # fmt: skip
    expected = f'{plan.INTERFACE} at HEAD: config > mqtt.port > min: expected a number, got "1"'
    assert (run.returncode, run.stderr) == (1, f"release_plan: {expected}\n")
