"""Release notes come from the version's section of CHANGELOG.md, which must exist and be dated."""

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_notes", ROOT / "tools" / "release_notes.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


release_notes = _load_tool()

CHANGELOG = """\
# Changelog

Intro.

## [Unreleased]

### Added

- Something new.

## [0.2.0] - 2026-12-01

### Fixed

- A bug.

## [0.1.0] - 2026-11-02

### Added

- The first release.

[unreleased]: https://github.com/example/project/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/example/project/compare/v0.1.0...v0.2.0
"""


def test_the_section_of_a_version_is_returned_without_its_heading() -> None:
    assert release_notes.section(CHANGELOG, "0.2.0") == "### Fixed\n\n- A bug."


def test_the_last_section_ends_before_the_link_definitions() -> None:
    assert release_notes.section(CHANGELOG, "0.1.0") == "### Added\n\n- The first release."


@pytest.mark.parametrize("version", ["0.3.0", "0.2", "Unreleased"])
def test_a_version_without_a_dated_section_is_refused(version: str) -> None:
    with pytest.raises(ValueError, match="no section"):
        release_notes.section(CHANGELOG, version)


def test_an_undated_or_empty_section_is_refused() -> None:
    with pytest.raises(ValueError, match="no section"):
        release_notes.section("## [1.0.0]\n\n- Undated.\n", "1.0.0")
    with pytest.raises(ValueError, match="empty"):
        release_notes.section("## [1.0.0] - 2026-11-02\n\n## [0.9.0] - 2026-10-01\n", "1.0.0")


def test_the_command_line_prints_the_section(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text(CHANGELOG, encoding="utf-8")
    assert release_notes.main(["0.2.0", "--changelog", str(changelog)]) == 0
    assert capsys.readouterr().out == "### Fixed\n\n- A bug.\n"
    with pytest.raises(SystemExit, match="no section"):
        release_notes.main(["9.9.9", "--changelog", str(changelog)])


@pytest.mark.parametrize(
    "version",
    ["0.1.0", "1.2.3a1", "1.2.3b2", "1.2.3rc1", "1.2.3.dev4", "1.2.3rc1.dev2", "1.2.3.post1"],
)
def test_the_documented_version_forms_are_accepted(version: str) -> None:
    release_notes.check_version(version)


@pytest.mark.parametrize(
    "version",
    ["1.0.0rc1.post1", "1.0.0a1.post2", "1.0.0rc1+build", "1.0.0c1", "1.0.0rc", "1.0", "v1.0.0"],
)
def test_other_version_forms_are_refused(version: str) -> None:
    """The release workflow tells pre-releases by the end of the tag; these would be published as
    the latest full release, or are not canonical."""
    with pytest.raises(ValueError, match="version"):
        release_notes.check_version(version)


def test_the_command_line_checks_the_version_first(tmp_path: Path) -> None:
    changelog = tmp_path / "CHANGELOG.md"
    changelog.write_text("## [1.0.0rc1.post1] - 2026-11-02\n\n- Notes.\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="version"):
        release_notes.main(["1.0.0rc1.post1", "--changelog", str(changelog)])
