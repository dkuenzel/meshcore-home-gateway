"""Fetch the firmware sources that the firmware tests build, pinned by tag and full commit.

Each source in the pin file (tests/firmware/sources.toml) names a git repository, the tags of a
release, the commit they must point to, and the paths to check out. The script fetches the tags,
refuses a tag that points to another commit, and checks out the paths of the pinned commit into
DEST/<name>. A stamp in that directory skips the fetch while the pin stays the same, so a second
run needs no network. `<NAME>_SOURCE` (for example `MESHCORE_SOURCE`) names a local git
repository to fetch from instead of the URL; the same checks run.

    python3 -I tools/fetch_firmware.py tests/firmware/sources.toml build/firmware/src
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TypeGuard

GIT = shutil.which("git") or "/usr/bin/git"
STAMP = ".fetched"
COMMIT = re.compile(r"[0-9a-f]{40}")
# git reads these to find a repository; a hook or a wrapper may have set them for another one
REPOSITORY_VARIABLES = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE",
)  # fmt: skip


class FetchError(Exception):
    pass


@dataclass(frozen=True)
class Pin:
    name: str
    url: str
    tags: tuple[str, ...]
    commit: str
    paths: tuple[str, ...]

    @property
    def stamp(self) -> str:
        """What a checkout of this pin holds; the URL is left out, a local source gives the same."""
        return f"commit {self.commit}\ntags {' '.join(self.tags)}\npaths {' '.join(self.paths)}\n"


def load_pins(path: Path) -> list[Pin]:
    with path.open("rb") as handle:
        table = tomllib.load(handle)
    return [_pin(name, entry) for name, entry in table.items()]


def _pin(name: str, entry: object) -> Pin:
    if not re.fullmatch(r"[a-z][a-z0-9]*", name) or not isinstance(entry, dict):
        raise FetchError(f"{name}: expected a table with a lower-case name")
    url, commit = entry.get("url"), entry.get("commit")
    tags, paths = entry.get("tags"), entry.get("paths")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise FetchError(f"{name}: url must be an https address")
    if not isinstance(commit, str) or not COMMIT.fullmatch(commit):
        raise FetchError(f"{name}: commit must be the full hash, 40 lower-case hex digits")
    if not _strings(tags) or not _strings(paths):
        raise FetchError(f"{name}: tags and paths must be lists of names")
    for item in paths:
        parts = PurePosixPath(item).parts
        if PurePosixPath(item).is_absolute() or ".." in parts or not parts:
            raise FetchError(f"{name}: path {item!r} must stay inside the repository")
    return Pin(name, url, tuple(tags), commit, tuple(paths))


def _strings(value: object) -> TypeGuard[list[str]]:
    return (
        isinstance(value, list)
        and bool(value)
        and all(isinstance(v, str) and v and not v.startswith("-") for v in value)
    )


def _git(*args: str | Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in REPOSITORY_VARIABLES}
    result = subprocess.run(  # noqa: S603 - git with arguments from the pin file
        [GIT, *map(str, args)], env=env, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        command = " ".join(map(str, args))
        raise FetchError(f"git {command}: {result.stderr.strip() or result.returncode}")
    return result.stdout.strip()


def fetch(pin: Pin, dest: Path, source: str | None = None) -> bool:
    """Check out the pin into dest/<name>; False when the checkout there is the pinned one."""
    target = dest / pin.name
    stamp = target / STAMP
    if stamp.is_file() and stamp.read_text(encoding="utf-8") == pin.stamp:
        return False
    dest.mkdir(parents=True, exist_ok=True)
    origin = source or pin.url
    # on dest's file system, so that the finished tree is renamed into place in one step
    with tempfile.TemporaryDirectory(prefix=f".{pin.name}-", dir=dest) as scratch:
        repo, tree = Path(scratch) / "git", Path(scratch) / "tree"
        _git("init", "--quiet", "--bare", repo)
        refs = [f"+refs/tags/{tag}:refs/tags/{tag}" for tag in pin.tags]
        _git("--git-dir", repo, "fetch", "--quiet", "--depth=1", "--no-tags", origin, *refs)
        for tag in pin.tags:
            found = _git("--git-dir", repo, "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}")
            if found != pin.commit:
                raise FetchError(
                    f"{pin.name}: tag {tag} at {origin} is commit {found}, pinned {pin.commit}"
                )
        tree.mkdir()
        _git("--git-dir", repo, "--work-tree", tree, "checkout", "--quiet", pin.commit, "--",
             *pin.paths)  # fmt: skip
        (tree / STAMP).write_text(pin.stamp, encoding="utf-8")
        if target.exists():
            shutil.rmtree(target)
        tree.rename(target)
    return True


def source_variable(pin: Pin) -> str:
    return f"{pin.name.upper()}_SOURCE"


def main(argv: list[str] | None = None, environ: Mapping[str, str] = os.environ) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("pins", type=Path, help="the pin file (TOML)")
    parser.add_argument("dest", type=Path, help="the directory that receives one tree per source")
    args = parser.parse_args(argv)
    try:
        for pin in load_pins(args.pins):
            source = environ.get(source_variable(pin)) or None
            fetched = fetch(pin, args.dest, source)
            where = f"from {source or pin.url}" if fetched else "up to date"
            print(f"{pin.name}: {', '.join(pin.tags)} = {pin.commit[:12]}, {where}")
    except (FetchError, OSError, tomllib.TOMLDecodeError) as exc:
        print(f"fetch_firmware: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
