"""The release plan of a change: does it release, and is the version it writes the next one?

    python3 -I tools/release_plan.py check [--base REV]   # CI on a pull request (base: HEAD^)
    python3 -I tools/release_plan.py tag                  # CI on main: prints tag=, exists=, final=

A change releases when it changes `__version__` (docs/development.md, ADR 0022). It must then
write exactly the next version: the kind of change comes from the changelog entries since the
last release (a `**Breaking:**` entry or a `### Removed` section: major; Added, Changed or
Deprecated: minor; Fixed or Security only: patch), the stage is the author's (aN, bN or rcN for a
pre-release, none for a release). Until 1.0.0 is out the next release is 1.0.0. A release needs a
release candidate of the same version that ships the same program: only `__version__` may
differ. Standard library only; git runs as a subprocess in the current directory.
"""

from __future__ import annotations

import argparse
import importlib.util
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

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
KINDS = ("patch", "minor", "major")
_KIND_OF = {
    "added": "minor", "changed": "minor", "deprecated": "minor",
    "fixed": "patch", "security": "patch", "removed": "major",
}  # fmt: skip


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
    listing = git("tag", "--list", "v*", "--format=%(refname:short) %(objectname) %(*objectname)")
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
        if name is None:
            continue
        stop = heads[i + 1][0] if i + 1 < len(heads) else end
        entries = [e for e in changelog[body:stop].splitlines() if e.startswith("- ")]
        if not entries:
            continue
        this = _KIND_OF.get(name.strip().lower(), "minor")
        if any(e.startswith("- **Breaking:**") for e in entries):
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


def release_notes() -> ModuleType:
    """tools/release_notes.py, next to this file (python -I leaves its directory off the path)."""
    spec = importlib.util.spec_from_file_location(
        "release_notes", Path(__file__).resolve().parent / "release_notes.py"
    )
    if spec is None or spec.loader is None:
        msg = "tools/release_notes.py cannot be loaded"
        raise PlanError(msg)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate(written: Version, rev: str) -> None:
    """Refuse `written` at `rev` unless it is the next version, with its changelog section."""
    changelog = git("show", f"{rev}:{CHANGELOG}")
    try:
        release_notes().section(changelog, str(written))
    except ValueError as exc:
        raise PlanError(str(exc)) from None
    unreleased = _UNRELEASED.search(changelog)
    names = [m.group(1) for m in _SECTION.finditer(changelog)]
    pending = unreleased is None or unreleased.group(1).strip()
    if pending or names[:2] != ["Unreleased", str(written)]:
        msg = f"{CHANGELOG}: an empty [Unreleased], then the section of {written}"
        raise PlanError(msg)
    known = tags()
    finals = [v for v in known if v.final]
    last_final = max(finals) if finals else None
    base = next_base(last_final, kind(changelog, last_final))
    if written.base != base:
        msg = f"{written}: the next release is {base} (or a pre-release of it)"
        raise PlanError(msg)
    if known and written <= max(known):
        msg = f"{written} is not newer than v{max(known)}"
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
            f"{written} does not ship the program of v{candidates[-1]}, which the hardware"
            f" checklist ran with: in {', '.join(SHIPPED)} only __version__ may change"
        )
        raise PlanError(msg)


def check(base: str) -> str:
    text = version_text("HEAD")
    if text == version_text(base):
        return f"no release: __version__ stays {text}"
    written = Version.parse(text)
    validate(written, "HEAD")
    return f"releases v{written}" + ("" if written.final else " as a pre-release")


def tag() -> tuple[str, bool, bool]:
    """For HEAD on main: its tag if it changed the version ('' if not), whether that tag already
    points at HEAD (a re-run) and whether it is a release. A tag on another commit is an error."""
    text = version_text("HEAD")
    if text == version_text("HEAD^"):
        return "", False, False
    written = Version.parse(text)
    existing = tags().get(written)
    head = git("rev-parse", "HEAD").strip()
    if existing is not None and existing != head:
        msg = f"v{written} already tags {existing}, not {head}"
        raise PlanError(msg)
    if existing is None:
        validate(written, "HEAD")
    return f"v{written}", existing is not None, written.final


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    commands = parser.add_subparsers(dest="command", required=True)
    checking = commands.add_parser("check", help="refuse a version that is not the next one")
    checking.add_argument("--base", default="HEAD^", help="the revision the change starts from")
    commands.add_parser("tag", help="print tag=vX.Y.Z or tag=, exists= and final=")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            print(check(args.base))
        else:
            name, exists, final = tag()
            print(f"tag={name}\nexists={str(exists).lower()}\nfinal={str(final).lower()}")
    except PlanError as exc:
        print(f"release_plan: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
