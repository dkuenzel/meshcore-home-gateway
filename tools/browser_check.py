"""Screenshots of a dashboard in a running Home Assistant, as one of its users sees them.

The browser part of the dashboard's checks (docs/testing.md): it logs in through Home
Assistant's own login page as the user given, opens each view of the dashboard at a phone, a
tablet and a desktop size, each in a light and a dark colour scheme, and saves a screenshot of
each. It also lists what the browser's console reported as an error on each page. It is not part
of `make check`: it needs a running Home Assistant with its frontend, and a Chromium.

Set up once, in an environment of its own (Python 3.11 or newer):

    python3 -m venv .venv-browser
    .venv-browser/bin/pip install --require-hashes -r tools/browser_check-requirements.txt
    .venv-browser/bin/python -m playwright install chromium

With a Chromium of the same Playwright release already on the machine, point
PLAYWRIGHT_BROWSERS_PATH at it instead of the last step.

Run, with the user's password in the environment variable HA_PASSWORD (or in a file named by
--password-file; never on the command line):

    .venv-browser/bin/python tools/browser_check.py --url http://127.0.0.1:8123 \\
        --user household --dashboard mesh-radio1 --out build/browser overview messages

It writes <out>/<view>-<width>x<height>-<scheme>.png (with "@2x" before ".png" at --scale 2)
and <out>/console.json, the console errors by page. It prints one line per screenshot and exits
with 1 when a page reported an error. An error that matches a pattern of --allow-error stays in
console.json and fails no page: offline, the map's tile proxy answers 502 on every map page,
`--allow-error '502 \\(Bad Gateway\\)'`. A view that the dashboard does not have counts as an error
too: Home Assistant would show the first view in its place. A failed login ends the run at once.
A width under 600 pixels is shown as a phone: a mobile
viewport with touch. The colour scheme is the browser's (prefers-color-scheme), which Home
Assistant's default theme follows unless the user chose one in their profile.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SIZES = "390x844,1024x768,1280x800"
SCHEMES = ("light", "dark")
PHONE_WIDTH = 600  # narrower: a phone, with a mobile viewport and touch
TIMEOUT_MS = 30_000
# The dashboard's views as the frontend finds them in a URL, by path and by number
# (hui-root.ts); null when the configuration cannot be read, and then nothing is checked.
VIEWS = """async (dashboard) => {
  try {
    const config = await document.querySelector("home-assistant").hass.callWS(
      {type: "lovelace/config", url_path: dashboard});
    return (config.views || []).flatMap(
      (view, i) => view.path ? [view.path, String(i)] : [String(i)]);
  } catch (error) {
    return null;
  }
}"""


@dataclass(frozen=True, slots=True)
class Size:
    width: int
    height: int

    @property
    def phone(self) -> bool:
        return self.width < PHONE_WIDTH

    def __str__(self) -> str:
        return f"{self.width}x{self.height}"


def sizes(text: str) -> list[Size]:
    """`390x844,1280x800` as sizes."""
    found = []
    for part in text.split(","):
        width, _, height = part.strip().partition("x")
        if not (width.isdigit() and height.isdigit()):
            raise argparse.ArgumentTypeError(f"not a size such as 390x844: {part!r}")
        found.append(Size(int(width), int(height)))
    return found


def schemes(text: str) -> list[str]:
    found = [part.strip() for part in text.split(",")]
    for scheme in found:
        if scheme not in SCHEMES:
            raise argparse.ArgumentTypeError(f"not light or dark: {scheme!r}")
    return found


def pattern(text: str) -> re.Pattern[str]:
    try:
        return re.compile(text)
    except re.error as exc:
        raise argparse.ArgumentTypeError(f"not a regular expression: {text!r}: {exc}") from None


def failing(errors: list[str], allowed: list[re.Pattern[str]]) -> list[str]:
    """The errors that fail a page: those no pattern of --allow-error matches."""
    return [error for error in errors if not any(p.search(error) for p in allowed)]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="browser_check.py",
        description="Screenshots of a dashboard's views in a running Home Assistant.",
    )
    p.add_argument("views", nargs="+", help="view paths of the dashboard, such as overview")
    p.add_argument("--url", default="http://127.0.0.1:8123", help="Home Assistant's address")
    p.add_argument("--user", required=True, help="the user name to log in with")
    p.add_argument(
        "--password-env",
        default="HA_PASSWORD",
        metavar="NAME",
        help="the environment variable that holds the password (default: HA_PASSWORD)",
    )
    p.add_argument("--password-file", type=Path, help="a file that holds the password instead")
    p.add_argument(
        "--dashboard", required=True, help="the dashboard's URL path, such as mesh-radio1"
    )
    p.add_argument("--out", type=Path, default=Path("build/browser"), help="the folder to write to")
    p.add_argument("--sizes", type=sizes, default=sizes(SIZES), help=f"default: {SIZES}")
    p.add_argument(
        "--schemes", type=schemes, default=list(SCHEMES), help="light, dark or both (default)"
    )
    p.add_argument("--scale", type=float, default=1.0, help="device pixels per CSS pixel")
    p.add_argument("--full-page", action="store_true", help="the whole page, not the viewport")
    p.add_argument(
        "--allow-error",
        type=pattern,
        action="append",
        default=[],
        metavar="PATTERN",
        help="a console error that matches this regular expression fails no page (repeatable)",
    )
    p.add_argument(
        "--settle",
        type=float,
        default=2.0,
        metavar="SECONDS",
        help="time for the cards to render after the view is shown (default: 2)",
    )
    return p


def password(args: argparse.Namespace) -> str:
    if args.password_file is not None:
        return str(args.password_file.read_text(encoding="utf-8").strip())
    secret = os.environ.get(args.password_env, "")
    if not secret:
        raise SystemExit(f"no password: set {args.password_env} or give --password-file")
    return secret


def log_in(browser: Any, url: str, user: str, secret: str) -> Any:
    """The browser's storage after a login on Home Assistant's own page, "keep me logged in"
    ticked, so that every later context starts logged in."""
    context = browser.new_context()
    page = context.new_page()
    page.goto(url, timeout=TIMEOUT_MS)
    page.locator("input[name=username]").fill(user)
    page.locator("input[name=password]").fill(secret)
    remember = page.locator("input[type=checkbox]")
    if remember.count() == 1:
        remember.check()
    page.locator("input[name=password]").press("Enter")
    # the login page shows a wrong user name or password in an error alert and stays put; a
    # dashboard may show error alerts too, so only the login page's own alert counts
    refused = page.locator("ha-authorize ha-alert[alert-type=error]")
    page.locator("home-assistant-main").or_(refused).first.wait_for(timeout=TIMEOUT_MS)
    if refused.count():
        raise SystemExit(f"login as {user} failed: {refused.first.inner_text().strip()}")
    page.wait_for_url(lambda address: "/auth/" not in address, timeout=TIMEOUT_MS)
    page.locator("home-assistant-main").wait_for(timeout=TIMEOUT_MS)
    state = context.storage_state()
    context.close()
    return state


def shoot(
    browser: Any, state: Any, args: argparse.Namespace, size: Size, scheme: str
) -> dict[str, list[str]]:
    """Every view at one size and scheme: screenshots written, console errors by view."""
    context = browser.new_context(
        storage_state=state,
        viewport={"width": size.width, "height": size.height},
        device_scale_factor=args.scale,
        is_mobile=size.phone,
        has_touch=size.phone,
        color_scheme=scheme,
    )
    page = context.new_page()
    errors: list[str] = []

    def console(message: Any) -> None:
        if message.type == "error":
            errors.append(message.text)

    page.on("console", console)
    page.on("pageerror", lambda error: errors.append(f"uncaught: {error}"))
    found: dict[str, list[str]] = {}
    suffix = "@2x" if args.scale == 2 else "" if args.scale == 1 else f"@{args.scale:g}x"
    known: list[str] | None = None
    for index, view in enumerate(args.views):
        errors.clear()
        page.goto(f"{args.url}/{args.dashboard}/{view}", timeout=TIMEOUT_MS)
        page.locator("hui-view").wait_for(timeout=TIMEOUT_MS)
        if index == 0:
            known = page.evaluate(VIEWS, args.dashboard)
        page.wait_for_timeout(args.settle * 1000)
        if known is not None and view.split("/")[0] not in known:
            errors.append(f"no view {view!r} in the dashboard: Home Assistant shows its first view")
        name = f"{view.replace('/', '_')}-{size}-{scheme}{suffix}.png"
        page.screenshot(path=str(args.out / name), full_page=args.full_page)
        found[f"{view} {size} {scheme}"] = list(errors)
        allowed = len(errors) - len(failing(errors, args.allow_error))
        print(
            f"{name}: {len(errors) - allowed} console error(s)"
            + (f", {allowed} allowed" if allowed else "")
        )
    context.close()
    return found


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    args.url = args.url.rstrip("/")
    secret = password(args)
    # Playwright is not in the development environment (requirements/dev.txt), so `make lint`
    # checks this file without it: it is looked up when the check runs.
    playwright = importlib.import_module("playwright.sync_api")
    args.out.mkdir(parents=True, exist_ok=True)
    report: dict[str, list[str]] = {}
    with playwright.sync_playwright() as driver:
        browser = driver.chromium.launch()
        try:
            state = log_in(browser, args.url, args.user, secret)
            for scheme in args.schemes:
                for size in args.sizes:
                    report.update(shoot(browser, state, args, size, scheme))
        finally:
            browser.close()
    (args.out / "console.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    failed = sorted(page for page, errors in report.items() if failing(errors, args.allow_error))
    for page in failed:
        print(f"console errors on {page}: {report[page]}", file=sys.stderr)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
