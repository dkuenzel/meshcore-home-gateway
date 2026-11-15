"""Manual check: the model of whiptail in tests/dialog_texts.py against the real whiptail.

`tests/test_dialog_texts.py` checks every text of the installer against its box with a model of
newt's line wrapping, since CI has no whiptail. This script runs the real whiptail over the same
texts, in tmux's pseudo terminal under `LC_ALL=C.UTF-8` as the installer calls it, reads the
screen and compares it with the model, line by line:

1. every text of every dialog in every language (each candidate with its worst-case values), in
   the dialog's kind and width and tall enough for the whole text: every line as the model wraps
   it, and nothing after it;
2. the same text in the dialog's own box on a terminal of 80 x 24: the lines the model says the
   box shows, and nothing after them;
3. the measured cases of `tests/test_dialog_texts.py` (MEASURED), which pin the model's corners.

    .venv/bin/python -I tests/manual/dialog_texts.py [--jobs N] [--report FILE]

It needs whiptail and tmux (`apt install whiptail tmux`); without them it says so and exits with
2. Exit code 0 when whiptail and the model agree on every text, 1 otherwise.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import itertools
import os
import shlex
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from tests import dialog_texts as model  # noqa: E402
from tests.test_dialog_texts import MEASURED  # noqa: E402

SOCKET = f"mhg-dialog-texts-{os.getpid()}"
TITLE = "meshcore-home-gateway"
MESSAGE_KINDS = frozenset({"msgbox", "yesno", "infobox"})  # their text starts a row lower
LIST_ITEMS = {"menu": ["a", "A", "b", "B"], "checklist": ["a", "A", "OFF", "b", "B", "OFF"]}
QUIET_S = 0.05
_names = itertools.count()


def tmux(*args: str) -> str:
    return subprocess.run(  # noqa: S603 - tmux with our arguments
        ["tmux", "-L", SOCKET, "-f", "/dev/null", *args],  # noqa: S607 - the user's tmux
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def screen(box: model.Box, text: str, columns: int, rows: int) -> list[str]:
    """The terminal's rows once whiptail has drawn `box` with `text` and waits for a key."""
    kind = ["--scrolltext"] if box.scroll else []
    kind += ["--fb"] if box.full_buttons else []
    args = ["--title", TITLE, *kind, f"--{box.kind}", "--", text, str(box.height), str(box.width)]
    if box.kind in LIST_ITEMS:
        args += [str(box.list_height), *LIST_ITEMS[box.kind]]
    command = shlex.join(["env", "LC_ALL=C.UTF-8", "whiptail", *args])
    session = f"s{next(_names)}"
    tmux("new-session", "-d", "-s", session, "-x", str(columns), "-y", str(rows), command)
    try:
        previous, deadline = "", time.monotonic() + 10
        while time.monotonic() < deadline:
            time.sleep(QUIET_S)
            shown = tmux("capture-pane", "-p", "-t", session)
            if shown == previous and "└" in shown:
                return shown.splitlines()
            previous = shown
        raise TimeoutError(f"whiptail did not settle: {args[:6]}")
    finally:
        tmux("kill-session", "-t", session)


def text_rows(lines: Sequence[str], box: model.Box) -> list[str]:
    """The rows of the box's text area, as shown: its window's rows from the text's first row,
    each without the border and the column before the text."""
    top = next(index for index, line in enumerate(lines) if "┌" in line)
    bottom = next(index for index, line in enumerate(lines) if "└" in line)
    first = top + 1 + (1 if box.kind in MESSAGE_KINDS else 0)
    return [line.split("│")[1][1:].rstrip() for line in lines[first:bottom]]


def shows(box: model.Box, text: str, columns: int, rows: int, count: int) -> tuple[bool, list[str]]:
    """Whether whiptail shows the first `count` lines as the model wraps them, and no more."""
    expected = [line.rstrip() for line in box.layout(text)][:count]
    shown = text_rows(screen(box, text, columns, rows), box)
    agrees = shown[:count] == expected and (len(shown) == count or not shown[count])
    return agrees, shown


@dataclass(frozen=True)
class Result:
    language: str
    where: str
    box: str
    needed: int
    capacity: int
    whole: bool
    boxed: bool
    shown: tuple[str, ...]


def check(language: str, dialog: model.Dialog, text: str) -> Result:
    box = dialog.box
    needed = len(box.layout(text))
    taller = box.height + max(needed + 1 - box.lines, 0)
    tall = model.Box(box.kind, taller, box.width, box.list_height, box.scroll, box.full_buttons)
    whole, _ = shows(tall, text, max(80, box.width + 2), taller + 2, needed)
    boxed, shown = shows(box, text, 80, 24, min(needed, box.lines))
    return Result(
        language, dialog.where(), box.describe(), needed, box.lines, whole, boxed, tuple(shown)
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--jobs", type=int, default=8, help="whiptails at a time (default 8)")
    parser.add_argument("--report", type=Path, help="write the results as Markdown to FILE")
    args = parser.parse_args(argv)
    missing = [name for name in ("whiptail", "tmux") if shutil.which(name) is None]
    if missing:
        print(f"not installed: {', '.join(missing)}", file=sys.stderr)
        return 2
    version = subprocess.run(
        ["whiptail", "--version"],  # noqa: S607 - the user's whiptail
        capture_output=True,
        text=True,
        check=False,
    )
    tasks = [
        (language, dialog, text)
        for language, catalog in model.catalogs().items()
        for dialog in model.dialogs()
        for text in dialog.text.render(model.translator(catalog))
    ]
    started = time.monotonic()
    try:
        with concurrent.futures.ThreadPoolExecutor(args.jobs) as pool:
            results = list(pool.map(lambda task: check(*task), tasks))
            measured = list(pool.map(lambda case: measure(*case), MEASURED))
    finally:
        subprocess.run(  # noqa: S603 - our server
            ["tmux", "-L", SOCKET, "kill-server"],  # noqa: S607
            capture_output=True,
            check=False,
        )
    report = summary(results, measured, (version.stdout or version.stderr).strip())
    report += f"\n{len(tasks) * 2 + len(MEASURED)} runs of whiptail in "
    report += f"{time.monotonic() - started:.0f} s.\n"
    print(report)
    if args.report:
        args.report.write_text(report, encoding="utf-8")
    agree = all(r.whole and r.boxed for r in results) and all(ok for ok, _ in measured)
    return 0 if agree else 1


def measure(box: model.Box, text: str, expected: Sequence[str]) -> tuple[bool, str]:
    """A measured case: whiptail shows `expected` in `box`, and nothing after it."""
    shown = text_rows(screen(box, text, 80, max(24, box.height + 2)), box)
    count = len(expected)
    ok = shown[:count] == list(expected) and (len(shown) == count or not shown[count])
    return ok, f"{box.describe()} {text[:40]!r}: {'agrees' if ok else shown}"


def summary(results: Sequence[Result], measured: Sequence[tuple[bool, str]], version: str) -> str:
    lines = [f"# whiptail and the model ({version})", ""]
    for language in dict.fromkeys(r.language for r in results):
        mine = [r for r in results if r.language == language]
        dialogs = len({r.where for r in mine})
        cut = sorted({r.where for r in mine if r.needed > r.capacity})
        lines.append(
            f"- {language}: {dialogs} dialogs, {len(mine)} texts; the whole text agrees in "
            f"{sum(r.whole for r in mine)}, the box's lines in {sum(r.boxed for r in mine)}; "
            f"cut in their box: {', '.join(cut) or 'none'}"
        )
    disagree = [r for r in results if not (r.whole and r.boxed)]
    lines += ["", f"Disagreements: {len(disagree)}"]
    lines += [
        f"- {r.language} {r.where} {r.box}: model {r.needed} lines (box {r.capacity}); "
        f"whole {r.whole}, box {r.boxed}; whiptail shows {list(r.shown)}"
        for r in disagree
    ]
    lines += ["", f"Measured cases: {sum(ok for ok, _ in measured)} of {len(measured)} agree"]
    lines += [f"- {text}" for _, text in measured]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
