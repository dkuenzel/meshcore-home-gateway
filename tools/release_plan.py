"""The release plan of a change: does it release, and is the version it writes the next one?

    python3 -I tools/release_plan.py check [--base REV]   # CI: a pull request, or a push
    python3 -I tools/release_plan.py tag [--base REV]     # CI on main: prints tag=, exists=, final=
    python3 -I tools/release_plan.py due                  # hourly: tag=, sha=, exists=, final=, …
    python3 -I tools/release_plan.py admit vX.Y.Z         # release.yml on a tag: sha=, final=

A change releases when it changes `__version__` (docs/development.md, ADR 0022). It must then
write exactly the next version: the kind of change comes from the changelog entries since the
last release (a `**Breaking:**` entry or a `### Removed` section: major; Added, Changed or
Deprecated: minor; Fixed or Security only: patch), the stage is the author's (aN, bN or rcN for a
pre-release, none for a release). Until 1.0.0 is out the next release is 1.0.0. A release needs a
release candidate of the same version that ships the same program: only `__version__` may
differ. Every check also compares the public interface (tests/public_interface.json, by
tools/public_interface.py) with that of the last release, which the revision must contain: what
it removes or narrows needs a `**Breaking:**` entry or a `### Removed` section since then, what it
adds or changes an entry that makes the next release at least a minor one
(docs/development.md#compatibility); the changelog sections since the last release stay as
tagged. A release is tagged a day after the commit date of its version commit on main, a
pre-release (a release candidate too) at once (SUP-001). release.yml publishes a release tag, also
one pushed by hand, only where the schedule would put it (`admit` asks `due` on main), and a
pre-release only on a commit of main that carries its version (#84). It reads the commit, not the
working tree.
Standard library only; git runs as a subprocess in the current directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

GIT = shutil.which("git") or "/usr/bin/git"
VERSION_FILE = "src/meshcore_home_gateway/__init__.py"
CHANGELOG = "CHANGELOG.md"
# what a release ships: the program, the unit and the installer, the dependencies, the zipapp build
SHIPPED = ("src", "deploy", "pyproject.toml", "tools/build_pyz.py")
_VERSION_LINE = re.compile(r'^__version__ = "([^"]*)"$', re.MULTILINE)
_FORM = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(?:(a|b|rc)(0|[1-9][0-9]*))?")
_SECTION = re.compile(r"^## \[([^\]]+)\]", re.MULTILINE)
_DATED = re.compile(r"^## \[([^\]]+)\] - \d{4}-\d{2}-\d{2}", re.MULTILINE)
_SUBSECTION = re.compile(r"^### (.+?)[ \t]*$", re.MULTILINE)
_UNRELEASED = re.compile(r"^## \[Unreleased\][^\n]*\n(.*?)(?=^## |\Z)", re.MULTILINE | re.DOTALL)
_STAGES = ("a", "b", "rc", "")  # in order; "" is the release
# a release waits this long after its version commit reached main: time to stop a merged change
# before it is signed and published for good (SUP-001); a pre-release reaches only who names it
COOLDOWN_S = 24 * 3600
# main as release.yml's checkout knows it (fetch-depth 0), by its full ref: git takes a tag named
# origin/main before the remote branch
MAIN = "refs/remotes/origin/main"
KINDS = ("patch", "minor", "major")
_KIND_OF = {
    "added": "minor",
    "changed": "minor",
    "deprecated": "minor",
    "fixed": "patch",
    "security": "patch",
    "removed": "major",
}
_HEADINGS = "Added, Changed, Deprecated, Removed, Fixed, Security"
_ENTRY = re.compile(r"\s*[-*] ")
_BREAKING = re.compile(r"\s*[-*] \*\*Breaking:\*\*")
# the public interface of a revision, written by `make interface` (docs/development.md)
INTERFACE = "tests/public_interface.json"
# false alarms, for one release each: {"X.Y.Z": {"<a finding as the refusal shows it>": "why"}}
WAIVERS = "tests/public_interface_waivers.json"
# what the check reads at the revision, which uncommitted changes do not reach
_READ = (CHANGELOG, "tests/public_interface*.json", VERSION_FILE)
# a release's snapshot that the comparator no longer reads
_RELEASED = (
    " (the snapshot of the last release: an attribute dropped or narrowed in"
    " tools/public_interface.py needs a new FORMAT, with a converter of the older ones)"
)


class PlanError(Exception):
    """The change cannot release as written."""


@dataclass(frozen=True, order=True, slots=True)
class Version:
    """A version of the forms the release policy uses: X.Y.Z, X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN."""

    major: int
    minor: int
    patch: int
    rank: int  # index in _STAGES: a < b < rc < the release
    number: int

    @classmethod
    def parse(cls, text: str) -> Version:
        match = _FORM.fullmatch(text)
        if match is None:
            msg = f"version {text!r} is not one of X.Y.Z, X.Y.ZaN, X.Y.ZbN, X.Y.ZrcN"
            raise PlanError(msg)
        major, minor, patch, stage, number = match.groups()
        rank = _STAGES.index(stage or "")
        return cls(int(major), int(minor), int(patch), rank, int(number or 0))

    @property
    def base(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"

    @property
    def stage(self) -> str:
        return _STAGES[self.rank]

    @property
    def final(self) -> bool:
        return self.stage == ""

    @property
    def waits(self) -> bool:
        """A release: tagged only COOLDOWN_S after its version commit; a pre-release at once."""
        return self.final

    def __str__(self) -> str:
        return self.base + (f"{self.stage}{self.number}" if self.stage else "")


def git(*args: str) -> str:
    run = subprocess.run(  # noqa: S603 - fixed git commands on this repository
        [GIT, *args], capture_output=True, text=True, check=False
    )
    if run.returncode != 0:
        msg = f"git {' '.join(args)}: {run.stderr.strip()}"
        raise PlanError(msg)
    return run.stdout


def version_text(rev: str) -> str:
    match = _VERSION_LINE.search(git("show", f"{rev}:{VERSION_FILE}"))
    if match is None:
        msg = f"no __version__ line in {VERSION_FILE} at {rev}"
        raise PlanError(msg)
    return match.group(1)


def tags() -> dict[Version, str]:
    """The release tags vX.Y.Z… and the commits they point at; other tags are ignored."""
    found = {}
    listing = git(
        "tag", "--list", "v*", "--format=%(refname:lstrip=2) %(objectname) %(*objectname)"
    )
    for line in listing.splitlines():
        name, obj, *peeled = line.split()
        try:
            found[Version.parse(name[1:])] = peeled[0] if peeled else obj  # annotated: peeled
        except PlanError:
            continue
    return found


def kind(changelog: str, last: Version | None) -> str | None:
    """The highest kind among the entries above the section of `last` (all if None)."""
    end = len(changelog)
    for match in _SECTION.finditer(changelog):
        if last is not None and match.group(1) == str(last):
            end = match.start()
            break
    heads = sorted(
        [(m.start(), m.end(), m.group(1)) for m in _SUBSECTION.finditer(changelog, 0, end)]
        + [(m.start(), m.end(), None) for m in _SECTION.finditer(changelog, 0, end)]
    )
    found: str | None = None
    for i, (_, body, name) in enumerate(heads):
        stop = heads[i + 1][0] if i + 1 < len(heads) else end
        lines = changelog[body:stop].splitlines()
        entries = [e for e in lines if _ENTRY.match(e)]
        if name is None:
            loose = [e for e in entries if not e[:1].isspace()]  # a section's text may say anything
            if loose:
                msg = f"{CHANGELOG}: an entry outside a subsection: {loose[0].strip()!r}"
                raise PlanError(msg)
            continue
        this = _KIND_OF.get(name.strip().lower())
        if this is None:
            msg = f"{CHANGELOG}: unknown heading '### {name}' (Keep a Changelog: {_HEADINGS})"
            raise PlanError(msg)
        if not entries:
            continue
        if any(_BREAKING.match(e) for e in lines):
            this = "major"
        if found is None or KINDS.index(this) > KINDS.index(found):
            found = this
    return found


def next_base(last_final: Version | None, change: str | None) -> str:
    if last_final is None:
        return "1.0.0"  # the first release
    if change is None:
        msg = f"nothing in {CHANGELOG} since {last_final}: no release"
        raise PlanError(msg)
    major, minor, patch = last_final.major, last_final.minor, last_final.patch
    if change == "major":
        return f"{major + 1}.0.0"
    if change == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def ships_alike(rev: str, candidate: str) -> bool:
    """`rev` ships the program of `candidate`, but for the version line."""
    changed = git("diff", "--name-only", candidate, rev, "--", *SHIPPED).split()
    if any(path != VERSION_FILE for path in changed):
        return False
    old, new = (git("show", f"{r}:{VERSION_FILE}") for r in (candidate, rev))
    return _VERSION_LINE.sub("", old) == _VERSION_LINE.sub("", new)


def tool(name: str) -> ModuleType:
    """tools/<name>.py, next to this file (python -I leaves its directory off the path)."""
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).resolve().parent / f"{name}.py"
    )
    if spec is None or spec.loader is None:
        msg = f"tools/{name}.py cannot be loaded"
        raise PlanError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # before it runs: its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


def last_release(
    changelog: str,
    known: Collection[Version],
    releasing: Version | None = None,
    rev: str = "HEAD",
) -> Version | None:
    """The newest release among the tags `known`. Refuses a clone without tags whose changelog
    has releases, which would take every version for the first, and one without the tag of the
    newest release that the changelog dates, but for the one the revision `releasing` writes:
    release-due.yml has not tagged it yet (it waits a day after the version commit of `rev`'s
    history), or a fork lacks the tags (syncing copies none)."""
    if not known and len(_DATED.findall(changelog)) > 1:
        msg = "no release tags v* here, though the changelog has releases: git fetch --tags"
        raise PlanError(msg)
    dated = [Version.parse(name) for name in _DATED.findall(changelog) if _FORM.fullmatch(name)]
    newest = max((v for v in dated if v.final and v != releasing), default=None)
    if known and newest is not None and newest not in known:
        wrote = version_commit(newest, rev)
        due = "" if wrote is None else f", from {_utc(wrote[1] + COOLDOWN_S)} on"
        msg = (
            f"{CHANGELOG} has {newest}, but there is no tag v{newest} here: if {newest} was merged"
            f" lately, release-due.yml tags it a day after its version commit{due}, once CI on"
            " main passed on it; run again then. Otherwise fetch the tags of the main repository"
        )
        raise PlanError(msg)
    return max((v for v in known if v.final), default=None)


def version_commit(version: Version, rev: str) -> tuple[str, int] | None:
    """The commit of `rev`'s history that wrote `version` into __version__, and its commit time,
    as release-due.yml counts the day from (`due`); None if the history lacks it."""
    log = git("log", "--format=%H %ct", "-G^__version__ = ", rev, "--", VERSION_FILE)
    for line in log.splitlines():
        sha, committed = line.split()
        if version_text(sha) == str(version):
            return sha, int(committed)
    return None


def _utc(seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(seconds))


def has(rev: str, path: str) -> bool:
    return not git_fails("cat-file", "-e", f"{rev}:{path}")


def _json(rev: str, path: str) -> dict[str, Any]:
    """The JSON object in `path` at `rev`."""
    try:
        found = json.loads(git("show", f"{rev}:{path}"))
    except ValueError as exc:
        msg = f"{path} at {rev}: not JSON ({exc})"
        raise PlanError(msg) from None
    if not isinstance(found, dict):
        msg = f"{path} at {rev}: not a JSON object"
        raise PlanError(msg)
    return found


def _waivers(rev: str, last: Version) -> dict[str, str]:
    """The waivers for the release `last`, each naming one finding as the refusal shows it;
    the next release makes them void."""
    if not has(rev, WAIVERS):
        return {}
    waivers = _json(rev, WAIVERS)
    for key in waivers:  # a tag's name or a pre-release would be ignored without a word
        if _FORM.fullmatch(key) is None or not Version.parse(key).final:
            msg = (
                f'{WAIVERS}: the key "{key}" is not a final release written X.Y.Z; findings'
                f' against v{last} go under "{last}"'
            )
            raise PlanError(msg)
    mine = waivers.get(str(last), {})
    if not isinstance(mine, dict) or not all(
        isinstance(reason, str) and reason.strip() for reason in mine.values()
    ):
        msg = (
            f'{WAIVERS}: expected {{"{last}": {{"<a finding as the refusal shows it>": "<why it'
            ' is a false alarm>"}}'
        )
        raise PlanError(msg)
    return mine


def interface(rev: str, releasing: Version | None = None) -> str:
    """Refuse `rev` if its public interface breaks that of the last release while the changelog
    since then does not make the next release a major one, or grows or changes it while the
    changelog makes it a patch; also if it changed a changelog section since the last release
    (that one's included) that it contains. Says what it compared, with the breaks a major release
    takes. `releasing`: the version `rev` writes, whose tag does not exist yet."""
    changelog = git("show", f"{rev}:{CHANGELOG}")
    known = tags()
    last = last_release(changelog, known, releasing, rev)
    if last is None:
        return "public interface: no release yet to compare with"
    if git_fails("merge-base", "--is-ancestor", f"v{last}", rev):  # its additions: no removals
        if git("rev-parse", "--is-shallow-repository").strip() == "true":
            msg = (
                "a shallow clone: git fetch --unshallow and run again (it does not show whether"
                f" {rev} contains v{last})"
            )
        else:
            msg = f"{rev} does not contain v{last}: merge origin/main and run again"
        raise PlanError(msg)
    _released_sections(rev, changelog, [v for v in known if v >= last])
    if not has(f"v{last}", INTERFACE):  # a release made before validate() required it
        return f"public interface: v{last} has no {INTERFACE}, nothing to compare"
    if not has(rev, INTERFACE):
        msg = f"{INTERFACE} is missing, but v{last} has one: make interface"
        raise PlanError(msg)
    comparator = tool("public_interface")
    old, new = _json(f"v{last}", INTERFACE), _json(rev, INTERFACE)
    for at, doc, hint in ((f"v{last}", old, _RELEASED), (rev, new, "")):
        try:
            comparator.check(doc)
        except comparator.FormatError as exc:
            msg = f"{INTERFACE} at {at}: {exc}{hint}"
            raise PlanError(msg) from None
    found = comparator.unwaived(old, new, _waivers(rev, last))
    breaks = [str(f) for f in found if f.kind == comparator.BREAKING]
    change = kind(changelog, last)
    waiver = f'a false alarm is waived in {WAIVERS} under "{last}"'
    if breaks and change != "major":
        msg = (
            f"the public interface of v{last} breaks, but {CHANGELOG} has no **Breaking:** entry"
            f" and no ### Removed section since (docs/development.md#compatibility; {waiver}):"
        )
        raise PlanError(_listed(msg, breaks))
    if breaks:
        count = f"{len(breaks)} finding" + ("s" if len(breaks) > 1 else "")
        return _listed(
            f"public interface: breaks v{last} ({count}), for {last.major + 1}.0.0:", breaks
        )
    if found and change in (None, "patch"):
        msg = (
            f"the public interface of v{last} grows or changes, but {CHANGELOG} has"
            f" {'only fixes' if change else 'no entry'} since: describe it under ### Added or"
            f" ### Changed (the next release is then a minor one; {waiver}):"
        )
        raise PlanError(_listed(msg, [str(f) for f in found]))
    return f"public interface: compatible with v{last}"


def _listed(head: str, findings: list[str]) -> str:
    return "\n  ".join([head, *findings])


def _released_sections(rev: str, changelog: str, released: list[Version]) -> None:
    """Refuse a changelog section of these releases that `rev` contains but changed since the
    tag: merging a release into a branch can move the branch's entries of [Unreleased] into the
    release's section without a conflict, where they count for no later release."""
    notes = tool("release_notes")
    for version in sorted(released):
        tag = f"v{version}"
        if git_fails("merge-base", "--is-ancestor", tag, rev):  # a newer one: not merged yet
            continue
        try:
            tagged = notes.section(git("show", f"{tag}:{CHANGELOG}"), str(version))
        except ValueError:  # tagged before validate() required the section
            continue
        try:
            now = notes.section(changelog, str(version))
        except ValueError:
            now = None
        if now != tagged:
            msg = (
                f"{CHANGELOG}: the section of {version} differs from the one at {tag} (a merge"
                " can move entries of [Unreleased] into it): entries since go above it"
            )
            raise PlanError(msg)


def validate(written: Version, rev: str) -> None:
    """Refuse `written` at `rev` unless it is the next version, with its changelog section."""
    changelog = git("show", f"{rev}:{CHANGELOG}")
    try:
        tool("release_notes").section(changelog, str(written))
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    unreleased = _UNRELEASED.search(changelog)
    names = [m.group(1) for m in _SECTION.finditer(changelog)]
    pending = unreleased is None or unreleased.group(1).strip()
    if pending or names[:2] != ["Unreleased", str(written)]:
        msg = f"{CHANGELOG}: an empty [Unreleased], then the section of {written}"
        raise PlanError(msg)
    known = tags()
    last_final = last_release(changelog, known, written, rev)
    base = next_base(last_final, kind(changelog, last_final) if last_final else None)
    if written.base != base:
        msg = f"{written}: the next release is {base} (or a pre-release of it)"
        raise PlanError(msg)
    if known and written <= max(known):
        msg = f"{written} is not newer than v{max(known)}"
        raise PlanError(msg)
    if written.final and not has(rev, INTERFACE):  # else the guard has nothing to compare with
        msg = f"{written}: a release needs {INTERFACE}, which later releases are compared with"
        raise PlanError(msg)
    if not written.final:
        same = [v.number for v in known if v.base == base and v.rank == written.rank]
        number = max(same, default=0) + 1
        if written.number != number:
            msg = f"{written}: the next {written.stage} of {base} is {base}{written.stage}{number}"
            raise PlanError(msg)
        return
    candidates = sorted(v for v in known if v.base == base and v.stage == "rc")
    if not candidates:
        msg = f"{written}: a release needs a release candidate v{base}rcN first"
        raise PlanError(msg)
    if not ships_alike(rev, f"v{candidates[-1]}"):
        msg = (
            f"{written} does not ship the program of v{candidates[-1]}, which the release"
            f" check ran with: in {', '.join(SHIPPED)} only __version__ may change"
        )
        raise PlanError(msg)


def start(base: str) -> str:
    """The revision a change starts from: `base` (a pull request's base, or the commit before a
    push), or HEAD^ for none, a new branch (all zeros) or a commit no longer known (force push)."""
    if base.strip("0") and not git_fails("cat-file", "-e", f"{base}^{{commit}}"):
        return base
    return "HEAD^"


def git_fails(*args: str) -> bool:
    try:
        git(*args)
    except PlanError:
        return True
    return False


def planned(base: str) -> tuple[Version, bool] | None:
    """The version HEAD releases and whether its tag is on HEAD already (a re-run); None if
    HEAD keeps the version of `base`. A tag on another commit is an error."""
    text = version_text("HEAD")
    if text == version_text(start(base)):
        return None
    written = Version.parse(text)
    existing = tags().get(written)
    head = git("rev-parse", "HEAD").strip()
    if existing is not None and existing != head:
        msg = f"v{written} already tags {existing}, not {head}"
        raise PlanError(msg)
    if existing is None:
        validate(written, "HEAD")
    return written, existing is not None


def check(base: str = "") -> str:
    """What HEAD releases, then what the public interface was compared with."""
    plan = planned(base)
    compared = interface("HEAD", plan[0] if plan else None)  # a break shows in its pull request
    if plan is None:
        return f"no release: __version__ stays {version_text('HEAD')}\n{compared}"
    written, tagged = plan
    if tagged:
        return f"v{written} tags this commit already\n{compared}"
    return f"releases v{written}" + ("" if written.final else " as a pre-release") + f"\n{compared}"


def uncommitted() -> list[str]:
    """The files the check reads that have changes not committed (a note: it reads HEAD)."""
    status = git("status", "--porcelain", "--", *_READ)
    return sorted({line[3:] for line in status.splitlines()})


def tag(base: str = "") -> tuple[str, bool, bool]:
    """For HEAD on main: its tag if it changed the version since `base` ('' if not), whether that
    tag points at HEAD already (a re-run) and whether it is a release."""
    plan = planned(base)
    if plan is None:
        return "", False, False
    written, tagged = plan
    return f"v{written}", tagged, written.final


def due(now: float, main: str = "HEAD") -> tuple[str, str, bool, int]:
    """For main on a schedule: the tag of the version `main` carries, the commit that wrote it,
    whether the tag is on that commit already, and the seconds until the commit is due: at once for
    a pre-release (a release candidate too), COOLDOWN_S after the commit date for a release. A tag
    on another commit is an error."""
    found = git("log", "-1", "--format=%H %ct", "-G^__version__ = ", main, "--", VERSION_FILE)
    sha, committed = found.split()
    written = Version.parse(version_text(sha))
    existing = tags().get(written)
    if existing is not None and existing != sha:
        msg = f"v{written} already tags {existing}, not {sha}"
        raise PlanError(msg)
    if existing is None:
        validate(written, sha)
    left = int(committed) + COOLDOWN_S - int(now) if written.waits else 0
    return f"v{written}", sha, existing is not None, max(0, left)


def admit(name: str, now: float, main: str = MAIN) -> tuple[str, bool]:
    """For release.yml on the tag `name` (#84): the commit it tags and whether it is a release.
    Refuses a commit that is not on `main` and a version other than the tag's. A release must be
    what release-due.yml tags, asked the same way (due): main's version, on the commit that wrote
    it, COOLDOWN_S after that commit. Whether CI passed on it, and whether release-due.yml is
    enabled, is the workflow's to ask."""
    written = Version.parse(name.removeprefix("v"))
    if name != f"v{written}":
        msg = f"{name!r} is not a release tag vX.Y.Z"
        raise PlanError(msg)
    sha = git("rev-parse", "--verify", f"refs/tags/{name}^{{commit}}").strip()
    if git_fails("merge-base", "--is-ancestor", sha, main):
        msg = f"{name} tags {sha}, not a commit of main ({main}): CI on main makes the tags"
        raise PlanError(msg)
    if (text := version_text(sha)) != str(written):
        msg = f"{name} tags a commit of {text}"
        raise PlanError(msg)
    if written.waits:
        try:
            mains, wrote, _, left = due(now, main)
        except PlanError as exc:
            msg = f"{name}: release-due.yml would tag none on {main}: {exc}"
            raise PlanError(msg) from None
        if (mains, wrote) != (name, sha):
            msg = (
                f"{name} tags {sha}; main's version is {mains}, of {wrote}:"
                " release-due.yml tags only that"
            )
            raise PlanError(msg)
        if left > 0:
            at = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(now + left))
            msg = (
                f"{name} is due in {left} s, at {at}, when release-due.yml starts it on this tag:"
                " a release waits a day after its version commit"
            )
            raise PlanError(msg)
    return sha, written.final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("check", help="refuse a version that is not the next one")
    tagging = commands.add_parser(
        "tag", help="print tag=vX.Y.Z or tag=, exists=, final= and waits="
    )
    for command in (checking, tagging):
        command.add_argument(
            "--base", default="", help="the revision the change starts from (default: HEAD^)"
        )
    commands.add_parser("due", help="print tag=, sha=, exists=, final=, due= and left= for main")
    admitting = commands.add_parser(
        "admit", help="refuse a tag CI would not have made; print sha= and final="
    )
    admitting.add_argument("tag", help="the tag release.yml runs on, vX.Y.Z")
    admitting.add_argument("--main", default=MAIN, help=f"main's revision (default: {MAIN})")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            if changed := uncommitted():
                note = f"the check reads HEAD; commit first ({', '.join(changed)} changed)"
                print(f"release_plan: {note}", file=sys.stderr)
            print(check(args.base))
        elif args.command == "admit":
            sha, final = admit(args.tag, time.time(), args.main)
            print(f"sha={sha}\nfinal={str(final).lower()}")
        elif args.command == "due":
            name, sha, exists, left = due(time.time())
            final = Version.parse(name[1:]).final
            print(
                f"tag={name}\nsha={sha}\nexists={str(exists).lower()}\nfinal={str(final).lower()}"
            )
            print(f"due={str(left == 0).lower()}\nleft={left}")
        else:
            name, exists, final = tag(args.base)
            waits = bool(name) and not exists and Version.parse(name[1:]).waits
            print(f"tag={name}\nexists={str(exists).lower()}\nfinal={str(final).lower()}")
            print(f"waits={str(waits).lower()}")
    except PlanError as exc:
        print(f"release_plan: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
