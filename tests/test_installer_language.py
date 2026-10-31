"""The installer's languages (ADR 0019): the block that loads a language pack and fills in texts,
cut out of the script and run on its own; the rules for its message calls; the language menu,
where its pack comes from and what a pack that cannot be loaded leaves; the texts that reach a
dialog through the pack; the program's messages in the dialogs' language; the language of the
names in Home Assistant; the script's size.

A pack is plain text pinned by its SHA-256 (docs/development.md, Translations): these tests offer
the pseudo-language xx, made from the installer's template with every text marked ‹…›, in a
copy of the script whose block of language packs lists it.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import NamedTuple

import pytest

from meshcore_home_gateway import __version__, i18n
from meshcore_home_gateway.catalog import Entry, read_po
from meshcore_home_gateway.config import ConfigError, render_config
from tests.test_installer import (
    ALICE,
    BASH,
    MAIN_INSTALLER,
    RELAY,
    SCRIPT,
    Host,
    answers_file,
    existing_container,
    install_stubs,
    installer_env,
    path_without,
    run_installer,
)
from tests.test_installer import RELEASES as DOWNLOADS  # the address of the release files

ROOT = Path(__file__).resolve().parent.parent
TEXT = SCRIPT.read_text(encoding="utf-8")
BLOCK = re.search(r"^# --- messages .*?^# --- end of messages\n", TEXT, re.MULTILINE | re.DOTALL)
CHECK = re.search(r"\nCHECK_SETTINGS_PY='(.*?)'\n", TEXT, re.DOTALL)
GERMANIC = "nplurals=2; plural=(n != 1);"
SPAN = re.compile(r"‹[^‹›]*›")
# a line of the profile plan without words: a setting, its value and the one wanted (a format)
SETTING_LINE = re.compile(r"  [\w.]+: .+ -> .+")


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("i18n_tool", ROOT / "tools" / "i18n.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["i18n_tool"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def template() -> list[Entry]:
    path = tool.Project(ROOT).installer_template
    return read_po(path.read_text(encoding="utf-8"), path.name)[1]


def marked(entries: list[Entry]) -> list[Entry]:
    """The pseudo-language: every text as ‹text›, its fields kept."""
    return [Entry(e.msgid, (f"‹{e.msgid}›",), e.msgctxt, None, e.flags) for e in entries]


def pinned(tmp_path: Path, text: str, name: str = "xx.messages") -> tuple[Path, str]:
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    return path, hashlib.sha256(text.encode("utf-8")).hexdigest()


def bare(text: str) -> str:
    """What is left of a text without its ‹…› spans, the innermost first."""
    while SPAN.search(text):
        text = SPAN.sub("", text)
    return text


def run_block(
    where: Path, driver: str, *args: str, payload: bytes = b""
) -> subprocess.CompletedProcess[bytes]:
    """The messages block alone, under the script's shell options, then `driver`; in `where`,
    so that a command a hostile text ran would leave its file there."""
    assert BLOCK is not None
    program = "set -Eeuo pipefail\n" + BLOCK.group() + driver
    return subprocess.run(  # noqa: S603 - the block of our script and a fixed driver
        [BASH, "-c", program, "block", *args], cwd=where, input=payload, capture_output=True,
        check=False, timeout=60,
    )  # fmt: skip


# --- the messages block, on its own ------------------------------------------------------------

# Calls on its input, NUL-separated: a function (t or tp), its arguments, then \x1e; each result
# is printed with a NUL after it.
CALLS = r"""
load_messages "$(cat "$1")" "$2" || exit 3
call=()
while IFS= read -r -d '' word; do
  if [[ $word != $'\x1e' ]]; then call+=("$word"); continue; fi
  case ${call[0]} in
    t|tp) "${call[@]}" ;;
    *) exit 4 ;;
  esac
  printf '\0'
  call=()
done
"""
HOSTILE = "$(touch ran) `touch ran` ${HOME} \\c \\\\ \\n %s %b & {other} 'q' \"dq\"\ttab\nline"


def calls(*asked: tuple[str, ...]) -> bytes:
    return "".join("".join(f"{word}\0" for word in (*call, "\x1e")) for call in asked).encode()


def test_messages_round_trip_in_bash(tmp_path: Path) -> None:
    """Every text of the template, a context, and hostile translations and keys come back exactly
    as the catalog holds them; nothing in them runs."""
    entries = [
        *marked(template()),
        Entry("Yes", ("‹Yes›",), "button"),
        Entry("hostile", (HOSTILE,)),
        Entry("a [key] * @ $(touch ran) {x}", ("‹key›",)),
    ]
    pack, pin = pinned(tmp_path, tool.pack("xx", entries))
    asked = [("tp", e.msgctxt, e.msgid) if e.msgctxt else ("t", e.msgid) for e in entries]
    result = run_block(tmp_path, CALLS, str(pack), pin, payload=calls(*asked))
    assert result.returncode == 0, result.stderr
    shown = result.stdout.decode("utf-8").split("\0")[:-1]
    assert shown == [entry.msgstr[0] for entry in entries]
    assert len(entries) > 100  # the template's texts are among them
    assert not (tmp_path / "ran").exists()


@pytest.mark.parametrize(
    "value",
    ["$(touch ran)", "`touch ran`", "A&B", "%s %b", "\\n \\c \\\\", "two\nlines", "{other}",
     "{message}", "‹›"],
)  # fmt: skip
def test_values_are_filled_in_as_given(tmp_path: Path, value: str) -> None:
    """A value is inserted once, never read again: not as a field, an escape or a command."""
    pack, pin = pinned(tmp_path, tool.pack("xx", marked(template())))
    asked = [("t", "Error: {message}", "message", value),
             ("t", "invalid settings: {reason}", "reason", value)]  # fmt: skip
    result = run_block(tmp_path, CALLS, str(pack), pin, payload=calls(*asked))
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode("utf-8").split("\0")[:-1] == [
        f"‹Error: {value}›", f"‹invalid settings: {value}›",
    ]  # fmt: skip
    assert not (tmp_path / "ran").exists()


def test_a_field_in_a_translation_runs_nothing(tmp_path: Path) -> None:
    """A translation's {…} names a value of the call, never code: the name is looked up as it is."""
    hostile = "{$(touch ran)} {a[$(touch ran)]} {`touch ran`} {message} {message"
    entries = [Entry("Error: {message}", (hostile,), flags=("python-brace-format",))]
    pack, pin = pinned(tmp_path, tool.pack("xx", entries))
    asked = ("t", "Error: {message}", "message", "x")
    result = run_block(tmp_path, CALLS, str(pack), pin, payload=calls(asked))
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode() == hostile.replace("{message} ", "x ") + "\0"
    assert not (tmp_path / "ran").exists()


def test_without_a_pack_the_texts_are_english(tmp_path: Path) -> None:
    """English needs no pack: t fills the msgid, tp gives it as it is."""
    driver = (
        "t 'Error: {message}' message 'x {y}'; printf '|'; tp button Cancel; printf '|'\n"
        "t 'not in any catalog {n}' n 1; printf '|%s' \"${#L10N[@]} ${#BUTTONS[@]} $L10N_LANG\"\n"
    )
    result = run_block(tmp_path, driver)
    assert result.returncode == 0, result.stderr
    assert result.stdout.decode() == "Error: x {y}|Cancel|not in any catalog 1|0 0 en"


# Loads a good pack, then the one tried: prints the entries before, its status, the entries after
TRY = r"""
load_messages "$(cat "$1")" "$2" || exit 3
before=${#L10N[@]}
status=0
load_messages "$(cat "$3")" "$4" || status=$?
printf '%s %s %s' "$before" "$status" "${#L10N[@]}"
"""


@pytest.mark.parametrize(
    "case",
    ["tampered", "another pin", "no pin", "an upper-case pin", "an unknown line", "an empty line",
     "a line without a text", "two final line breaks"],
)  # fmt: skip
def test_a_pack_that_does_not_match_is_refused_whole(tmp_path: Path, case: str) -> None:
    """Refused before or while it is read, and no entry stays behind: the dialogs stay English."""
    good, pin = pinned(tmp_path, tool.pack("xx", marked(template())))
    text = good.read_text(encoding="utf-8")
    other = text.replace("‹Error: {message}›", "‹Fehler: {message}›")
    tried, wanted = {  # the text tried, and the pin it is checked against (None: its own)
        "tampered": (other, pin),
        "another pin": (text, hashlib.sha256(other.encode()).hexdigest()),
        "no pin": (text, ""),
        "an upper-case pin": (text, pin.upper()),
        "an unknown line": (text + "plural\t2\tn != 1\n", None),
        "an empty line": (text + "\nmsg\ta\tb\n", None),
        "a line without a text": (text + "msg\ta\n", None),
        "two final line breaks": (text + "\n", None),
    }[case]
    path, own = pinned(tmp_path, tried, "tried")
    result = run_block(tmp_path, TRY, str(good), pin, str(path), own if wanted is None else wanted)
    assert result.returncode == 0, result.stderr
    before, status, after = result.stdout.decode().split()
    assert int(before) > 100
    assert (status, after) == ("1", "0")


# --- the rules for the script ------------------------------------------------------------------

BUDGET = 98_304  # bytes: 96 KiB, 75 % of the 131,071 that Linux takes in one argument


def published(version: str) -> bytes:
    """The installer as `make release` writes it: the release's version in its one line."""
    return re.sub(
        rb'^RELEASE=""', b'RELEASE="' + version.encode() + b'"', SCRIPT.read_bytes(), count=1,
        flags=re.MULTILINE,
    )  # fmt: skip


def fits(script: bytes) -> bool:
    script.decode("utf-8")  # valid UTF-8: the endonyms of the language menu
    return len(script) <= BUDGET


def test_the_script_fits_one_argument() -> None:
    """`bash -c "$(wget -qO- URL)"` passes the whole script, as `make release` writes it, as one
    argument."""
    copy = published(__version__)
    assert f'\nRELEASE="{__version__}"'.encode() in copy
    assert fits(copy), len(copy)
    assert fits(b"x" * BUDGET)
    assert not fits(b"x" * (BUDGET + 1))
    with pytest.raises(UnicodeDecodeError):
        fits(b"\xff")
    assert BUDGET <= 131_071 * 3 // 4 + 1


def test_message_calls_use_literals() -> None:
    """t, tp, die and $say take a literal, so the template holds every text."""
    texts = {entry.msgid for entry in tool.extract_shell(SCRIPT, ROOT)}
    dies = len(re.findall(r"(?<![\w-])die '", TEXT)) + len(re.findall(r'(?<![\w-])die "', TEXT))
    assert len(texts) > 100
    assert dies > 60  # every die passes a msgid


def test_no_eval_or_source() -> None:
    """A language pack is data: the script runs no text it reads."""
    assert tool.shell_hazards(SCRIPT, ROOT) == []


# --- what the dialogs show ----------------------------------------------------------------------

PINS = re.compile(
    rf"^{re.escape(tool.PINS_START)}\n.*?^{re.escape(tool.PINS_END)}\n", re.MULTILINE | re.DOTALL
)
PSEUDO = {"en": "English", "xx": "Pseudo"}  # what the copies of the script offer
LANGUAGE_MENU = "Language / Sprache / Taal / Język / Langue"  # the first menu's one text
NAMES_MENU = "‹Language of the names in Home Assistant›"
RELEASES = DOWNLOADS.removesuffix("/download/")  # the release page, as the script names it
MAIN_PACK = MAIN_INSTALLER.rpartition("/")[0] + "/locale/xx.messages"  # main's file


def release_pack(version: str) -> str:
    """Where a release publishes the pseudo-language's pack."""
    return f"{DOWNLOADS}v{version}/meshcore-home-gateway-lxc.xx.messages"


def pseudo_pack() -> str:
    return str(tool.pack("xx", marked(template())))


def serve(host: Host, address: str, text: str) -> None:
    """Put `text` at `address` of the stub web server, which the wget stub reads."""
    path = host.root / "www" / address.removeprefix("https://")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def checkout(tmp_path: Path, script: str, source: Path | None = None) -> Path:
    """A checkout of its own for a copy of the script: `source` as its src (else the real one),
    tools and the unit linked. Returns the script's path."""
    root = tmp_path / "checkout"
    (root / "deploy" / "proxmox").mkdir(parents=True)
    if source is None:
        (root / "src").symlink_to(ROOT / "src")
    elif source != root / "src":
        shutil.move(source, root / "src")
    (root / "tools").symlink_to(ROOT / "tools")
    (root / "deploy" / "systemd").symlink_to(ROOT / "deploy" / "systemd")
    path = root / "deploy" / "proxmox" / SCRIPT.name
    path.write_text(script, encoding="utf-8")
    return path


def offering(
    host: Host,
    tmp_path: Path,
    source: Path | None = None,
    *,
    release: str = "",
    served: str | None = None,
    pinned: str | None = None,
    languages: Mapping[str, str] = PSEUDO,
) -> Path:
    """A copy of the script whose block of language packs offers `languages`, as `make
    translations` writes it, in a checkout of its own (`source` as its src); the pseudo-language's
    pack next to it in locale/, at main's address and, with `release` written in as `make
    release` writes it, at that release's. `served` is the pack's text (the pseudo-language's),
    `pinned` the text whose digest the block holds (the one served)."""
    text = pseudo_pack() if served is None else served
    digest = text if pinned is None else pinned
    block = tool.pin_block(languages, {code: digest for code in languages if code != "en"})
    script, count = PINS.subn(lambda _: block, TEXT)
    assert count == 1
    if release:
        script = script.replace('\nRELEASE=""', f'\nRELEASE="{release}"', 1)
        serve(host, release_pack(release), text)
    serve(host, MAIN_PACK, text)
    path = checkout(tmp_path, script, source)
    (path.parent / "locale").mkdir()
    (path.parent / "locale" / "xx.messages").write_bytes(text.encode("utf-8"))
    return path


def run(
    host: Host,
    script: Path,
    *args: str,
    dialog: list[str] | None = None,
    env: dict[str, str] | None = None,
    address: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run a copy of the script as a file, or (`address`) as `bash -c "$(wget -qO- URL)"` runs
    it: no file, so no checkout and nothing next to it. TMPDIR is an empty directory."""
    temporary = host.root / "tmp"
    temporary.mkdir(exist_ok=True)
    env = {"TMPDIR": str(temporary), **(env or {})}
    if not address:
        return run_installer(host, *args, dialog=dialog, env=env, script=script)
    if dialog is not None:
        (host.root / "dialog").write_text("\n".join(dialog) + "\n", encoding="utf-8")
    return subprocess.run(  # noqa: S603 - a copy of our script with fixed arguments
        [BASH, "-c", script.read_text(encoding="utf-8"), "--", *args],
        env=installer_env(host, env), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60, check=False,
    )  # fmt: skip


def text_of(call: list[str]) -> str:
    return call[call.index("--") + 1]


def answered(host: Host) -> dict[str, str]:
    """The answers the installer sent to `config render` for the configuration."""
    text = host.pushed("meshcore-home-gateway-answers")
    return dict(line.split("=", 1) for line in text.splitlines())


def environment_of(call: list[str]) -> list[str]:
    """The assignments of a `pct exec ID -- env -i NAME=value ... COMMAND` call."""
    assignments = []
    for arg in call[6:]:
        if "=" not in arg:
            break
        assignments.append(arg)
    return assignments


# the button labels the pack gives: whiptail gets them before the rest of each call
BUTTONS = ["--yes-button", "‹Yes›", "--no-button", "‹No›", "--ok-button", "‹Ok›",
           "--cancel-button", "‹Cancel›"]  # fmt: skip


@pytest.fixture
def host(tmp_path: Path) -> Host:
    (tmp_path / "host").mkdir()
    return install_stubs(tmp_path / "host")


@pytest.fixture
def pseudo_program(tmp_path: Path) -> Path:
    """A copy of the package that offers the pseudo-language: i18n.LANGUAGES gains xx, and
    locale/xx.po marks every text ‹…›. The installer's settings check on the host runs it in a
    `python3 -I` subprocess, which no catalog of this process reaches; it also accepts
    homeassistant.language = "xx", the menu's choice."""
    source = tmp_path / "pseudo" / "src"
    shutil.copytree(ROOT / "src", source, ignore=shutil.ignore_patterns("__pycache__"))
    package = source / "meshcore_home_gateway"
    module = package / "i18n.py"
    text, count = re.subn(
        r"MappingProxyType\(\{", 'MappingProxyType({"xx": "Pseudo", ',
        module.read_text(encoding="utf-8"), count=1,
    )  # fmt: skip
    assert count == 1
    module.write_text(text, encoding="utf-8")
    python = tool.Project(ROOT).template
    _, entries = read_po(python.read_text(encoding="utf-8"), python.name)
    translated = [
        Entry(e.msgid, tuple(f"‹{t}›" for t in (e.msgid, e.msgid_plural) if t is not None),
              e.msgctxt, e.msgid_plural, e.flags)
        for e in entries
    ]  # fmt: skip
    po = tool.write_po(tool.catalog_header("xx", GERMANIC), translated)
    (package / "locale" / "xx.po").write_text(po, encoding="utf-8")
    return source


PLAN_ALL = (
    "name\\tGateway\\ntested\\tyes\\nwrites\\tyes\\n"
    "change\\tmanual_add\\tfalse\\ttrue\\tset\\n"
    "change\\tautoadd\\t0\\t1\\treported\\n"
    'change\\tname\\t"Renamed"\\t"Gateway"\\tkept\\n'
    "change\\tautoadd\\t0\\t1\\tgiven_up\\n"
    "change\\tduty_cycle_percent\\t50\\t10\\tunsupported\\n"
    f"contact\\t{ALICE}\\tfavourite\\tAlice Example\\n"
    f"contact\\t{RELAY}\\tadd\\t\\n"
    f"contact\\t{ALICE}\\tmissing\\tAlice\\n"
    f"contact\\t{RELAY}\\tnot_favourite\\t\\n"
)


def default(
    host: Host, broker: list[str], again: tuple[str, ...] = (), language: str = "xx"
) -> list[str]:
    """The answers of a default installation: the language, the broker's, then (`again`) the
    answers after a refused login, which comes after the confirmation."""
    return [language, "default", str(host.serial), *broker, "yes", *again, "1", ALICE, ALICE,
            "status"]  # fmt: skip


def advanced(host: Host, names: str | None, language: str = "en") -> list[str]:
    """The answers of an advanced installation: `names` for the menu of the names' language,
    None where the installer does not ask it."""
    return [language, "advanced", "106", "meshcore-home-gateway", "local-lvm", "4", "512",
            "vmbr1", "dhcp", "local", str(host.serial), "broker.example.org", "no", "1883",
            "bridge", "pw", "meshcore", "gw1", "yes", "Mesh radio",
            *([] if names is None else [names]),
            "no", "yes", "1", ALICE, ALICE, "status", ""]  # fmt: skip


class Scenario(NamedTuple):
    dialog: Callable[[Host], list[str]]
    env: dict[str, str]
    nested: tuple[str, ...] = ()  # translations shown as a value of another text, which hides them
    reconfigure: bool = False
    release: str = ""  # run from its address, published with this release
    status: int = 0


SCENARIOS = {
    # a login refused, then the broker typed again: the user name it was; the plan; the summary
    "refused, typed again": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", "wrong"],
                          ("yes", "broker.example.org", "no", "1883", "bridge", "right")),
        {"BROKER_ANSWERS": "1 0", "PROBE_PLAN": PLAN_ALL},
        ("Login:    ‹user 'bridge' with a password›", "Broker:       ‹login accepted›",
         "‹broker refused the connection: Not authorized›"),
    ),
    "no user name, refused, settings refused": Scenario(
        lambda h: default(h, ["broker.example.org", ""],
                          ("yes", "broker.example.org", "no", "x", "",
                           "broker.example.org", "yes", "8883", "bridge", "")),
        {"BROKER_ANSWERS": "1 4 0"},
        ("Login:    ‹none (no user name)›", "‹configuration error: mqtt.port: not a number›"),
    ),
    "a login refused and kept": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", ""], ("no",)),
        {"BROKER_ANSWERS": "1"},
        ("Login:    ‹user 'bridge' without a password›",
         "Broker:       ‹login failed (see above), the service keeps trying›"),
    ),
    "advanced, a gateway, a radio over TCP": Scenario(
        lambda h: ["xx", "advanced", "106", "meshcore-home-gateway", "local-lvm", "4", "512",
                   "vmbr1", "192.0.2.20/24", "192.0.2.1", "local", "tcp", "192.0.2.30", "5000",
                   "broker.example.org", "yes", "8883", "bridge", "pw", "meshcore", "gw1", "yes",
                   "Mesh radio", "en", "yes", "yes", "1", "", "", "x" * 32, "Mesh gateway"],
        {},
        ("Network:  ‹vmbr1, 192.0.2.20/24, gateway 192.0.2.1›",),
    ),
    "settings refused on the host": Scenario(
        lambda h: ["xx", "default", str(h.serial), "broker example", "bridge", "pw"],
        {},
        ("‹Error: ‹invalid settings: ‹mqtt.host: ",),  # the program's reason, translated
        status=1,
    ),
    "a failure after the dialogs": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", "pw"]),
        {"PVEAM_DOWNLOAD_FAILS": "1"},
        ("‹Error: ‹could not download debian-13-standard_13.6-1_amd64.tar.zst to local››",),
        status=1,
    ),
    "a pre-release from its address": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", "pw"]),
        {"STUB_VERSION": "0.2.0rc1"},
        (f"Update:       ‹the installer of a newer release ({RELEASES}) with -- --update 105›",),
        release="0.2.0rc1",
    ),
    "a version without the login check, a profile observed": Scenario(
        lambda h: ["xx", "default", "old.example.org", "bridge", "", "1", ALICE, ALICE, "status"],
        {"BROKER_ANSWERS": "3", "STUB_VERSION": "1.2.3", "PROBE_PLAN": PLAN_ALL},
        ("Broker:       ‹not checked (version 1.2.3 cannot; check after --update)›",),
        reconfigure=True,
    ),
    "the installed password kept": Scenario(
        lambda h: ["xx", "default", "old.example.org", "bridge", "new", "yes", "old.example.org",
                   "no", "1883", "bridge", "", "1", ALICE, ALICE, "status"],
        {"BROKER_ANSWERS": "1 0",
         "PROBE_PLAN": "name\\tGateway\\ntested\\tno\\nchange\\tautoadd\\t0\\t1\\treported\\n"},
        reconfigure=True,
    ),
}  # fmt: skip


def shown(host: Host) -> list[str]:
    """The text of every dialog after the language menu, and the items of the menus that are
    ours (not the radio's devices, not the languages' names)."""
    texts = []
    for call in host.calls("whiptail")[1:]:
        texts.append(text_of(call))
        if "--menu" in call:  # tag, item, ...: our items say what a tag means
            items = call[call.index("--") + 6 :: 2]
            texts += [
                item for item in items
                if not item.startswith(("usb-", str(host.serial))) and item not in PSEUDO.values()
            ]  # fmt: skip
    return texts


def scenario_run(
    host: Host, tmp_path: Path, source: Path, scenario: Scenario
) -> subprocess.CompletedProcess[str]:
    script = offering(host, tmp_path, source, release=scenario.release)
    args: tuple[str, ...] = ()
    if scenario.reconfigure:
        existing_container(host)
        current = (host.root / "current").read_text(encoding="utf-8")
        (host.root / "current").write_text(current + "radio.profile.manage=observe\n")
        args = ("--reconfigure", "105")
    return run(host, script, *args, dialog=scenario.dialog(host), env=scenario.env,
               address=bool(scenario.release))  # fmt: skip


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_dialog_text_is_translated(
    host: Host, tmp_path: Path, pseudo_program: Path, name: str
) -> None:
    """The pseudo-language chosen in the first menu: every dialog, box and menu item after it
    shows its translation (values and the plan's setting lines aside), with the pack's button
    labels; the program's messages in the boxes are in it too."""
    scenario = SCENARIOS[name]
    result = scenario_run(host, tmp_path, pseudo_program, scenario)
    assert result.returncode == scenario.status, result.stderr
    menu, *calls = host.calls("whiptail")
    assert text_of(menu) == LANGUAGE_MENU
    assert menu[1] == "--title"  # before the pack: whiptail's own buttons
    assert calls
    assert all(call[1:9] == BUTTONS for call in calls)
    texts = shown(host)
    for text in texts:
        assert all(
            not line.strip() or SETTING_LINE.fullmatch(line) for line in bare(text).split("\n")
        ), text
    for nested in scenario.nested:
        assert any(nested in text for text in texts), nested


SCREEN = "\x1b[H[screen]"  # what the whiptail stub draws, on the installer's stderr
# what the program prints in the pseudo-language, quoted by the installer's log or passed on
QUOTED = (
    "‹broker refused the connection: Not authorized›",
    "‹bad›",
)
# each run: its answers, its stubs' settings, and the line that quotes the program
FRAMES: dict[str, tuple[Callable[[Host], list[str]], dict[str, str], str]] = {
    "a refused login kept": (
        lambda h: default(h, ["broker.example.org", "bridge", ""], ("no",)),
        {"BROKER_ANSWERS": "1"},
        ("meshcore-home-gateway: warning: the broker login failed: "
         "‹broker refused the connection: Not authorized›"),
    ),
    "settings refused on the host": (
        lambda h: ["xx", "default", str(h.serial), "broker example", "bridge", "pw"],
        {},
        "meshcore-home-gateway: error: invalid settings: ‹mqtt.host: ‹",
    ),
    "settings refused in the container": (
        lambda h: ["xx", "default", "old.example.org", "bridge", ""],
        {"CHECK_FAILS": "1", "STUB_VERSION": "1.2.3"},
        "meshcore-home-gateway: ‹bad›",  # its stderr, passed on
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", FRAMES)
def test_the_installer_log_frames_stay_english(
    host: Host, tmp_path: Path, pseudo_program: Path, name: str
) -> None:
    """The installer's own log lines stay English in every language; the program's messages that
    they quote, or that the installer passes on, are in the dialogs' language."""
    dialog, env, quoted = FRAMES[name]
    script = offering(host, tmp_path, pseudo_program)
    args: tuple[str, ...] = ()
    if name == "settings refused in the container":
        existing_container(host)
        args = ("--reconfigure", "105")
    result = run(host, script, *args, dialog=dialog(host), env=env)
    assert result.returncode == (name != "a refused login kept"), result.stderr
    lines = result.stderr.replace(SCREEN, "").split("\n")
    assert any(line.startswith(quoted) for line in lines), result.stderr
    for line in lines:
        own = line
        if own.startswith("meshcore-home-gateway: error: invalid settings: "):
            own = "meshcore-home-gateway: error: invalid settings: "  # then the program's reason
        for text in QUOTED:
            own = own.replace(text, "")
        assert "‹" not in own, line


@pytest.mark.parametrize(
    ("origin", "args", "wanted"),
    [
        ("release", (), release_pack("1.2.3")),
        ("release", ("--release", "1.1.0a2"), release_pack("1.2.3")),  # this copy's texts
        ("a release's copy in a checkout", (), release_pack("1.2.3")),  # not the files there
        ("checkout", (), None),  # the file next to the script
        ("a copy of main's script", (), MAIN_PACK),  # no locale/ next to it
        ("main's address", (), MAIN_PACK),
    ],
)
def test_packs_come_from_where_the_script_comes_from(
    host: Host, tmp_path: Path, origin: str, args: tuple[str, ...], wanted: str | None
) -> None:
    """The pack whose texts are the running script's: its release's, the one next to it in a
    checkout, else main's; read into memory, whatever --release installs."""
    script = offering(host, tmp_path, release="1.2.3" if "release" in origin else "")
    if origin == "a copy of main's script":
        lone = tmp_path / "copy" / SCRIPT.name
        lone.parent.mkdir()
        shutil.copy(script, lone)
        script = lone
    address = origin in {"release", "main's address"}
    result = run(host, script, *args, dialog=["xx", "<cancel>"], address=address)
    assert result.returncode == 1
    assert "meshcore-home-gateway: error: cancelled\n" in result.stderr
    _, setup = host.calls("whiptail")
    assert text_of(setup) == "‹Setup›"  # the pack is loaded
    assert setup[1:9] == BUTTONS
    expected = [] if wanted is None else [["wget", "-qO-", "--timeout=20", wanted]]
    assert host.calls("wget") == expected
    assert not list((host.root / "tmp").iterdir())  # nothing written


NOTICE = "Pseudo: the language pack could not be loaded; the installer continues in English."
BIG = 300 * 1024  # bytes: more than a pack may have


@pytest.mark.parametrize(
    "case", ["another digest", "a failed download", "no wget", "300 KiB", "a malformed line"]
)
def test_a_pack_that_cannot_be_loaded_leaves_english(host: Host, tmp_path: Path, case: str) -> None:
    """English dialogs and program messages after one log line and one English notice; the
    installation goes on, and the menu's choice is still the language of the names."""
    good = pseudo_pack()
    served, env = good, {"STUB_VERSION": "1.2.3"}
    if case == "another digest":
        served = good.replace("‹Setup›", "‹Einrichtung›")
    elif case == "a failed download":
        env["WGET_FAILS"] = "1"
    elif case == "no wget":
        env["PATH"] = path_without(host, "wget")
    elif case == "300 KiB":
        served = good + "# " + "x" * BIG + "\n"
    else:
        served = good + "plural\t2\tn != 1\n"
    pinned = good if case == "another digest" else served
    script = offering(host, tmp_path, release="1.2.3", served=served, pinned=pinned)
    dialog = default(host, ["broker.example.org", "bridge", "pw"])
    result = run(host, script, dialog=dialog, env=env, address=True)
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("the language pack xx was not loaded") == 1
    logged = "meshcore-home-gateway: the language pack xx was not loaded: the dialogs stay in "
    assert logged + "English\n" in result.stderr
    calls = host.calls("whiptail")
    assert next(text_of(c) for c in calls if "--msgbox" in c) == NOTICE
    assert text_of(calls[2]) == "Setup"
    assert all(call[1] == "--title" for call in calls)  # no button labels
    assert not any("‹" in arg for call in calls for arg in call)
    assert all(not any(a.startswith("LANGUAGE=") for a in environment_of(c))
               for c in host.pct("exec"))  # fmt: skip
    assert answered(host)["homeassistant.language"] == "xx"


OFFERED = {"en": "English", "de": "Deutsch", "pl": "Polski", "xx": "Pseudo"}
ENVIRONMENTS = [
    ({"LANG": "de_DE.UTF-8"}, "de"),
    ({"LC_ALL": "C.UTF-8"}, "en"),
    ({"LANGUAGE": "pl:de"}, "pl"),
    ({"LANGUAGE": "pt_BR:de", "LANG": "pl_PL.UTF-8"}, "de"),  # the first offered
    ({"LC_ALL": "C.UTF-8", "LANGUAGE": "de"}, "de"),  # Python's order, not GNU's
    ({"LC_ALL": "pl_PL.UTF-8", "LC_MESSAGES": "de_DE.UTF-8", "LANG": "de_DE.UTF-8"}, "pl"),
    ({"LC_MESSAGES": "pl_PL.UTF-8", "LANG": "de_DE.UTF-8"}, "pl"),
    ({"LANGUAGE": "", "LC_MESSAGES": "PL_PL.utf8@euro"}, "pl"),
    ({"LANGUAGE": "C:de"}, "en"),
    ({"LANG": "*"}, "en"),
    ({"LANGUAGE": "$(id)"}, "en"),
    ({"LANGUAGE": "xx[$(id)]:de"}, "de"),
    ({}, "en"),
]


@pytest.mark.parametrize(("environ", "wanted"), ENVIRONMENTS)
def test_language_default_follows_the_environment(
    host: Host,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environ: dict[str, str],
    wanted: str,
) -> None:
    """The menu offers the caller's language first, as the command line chooses it (Python's
    gettext order); a code is checked before it is looked up."""
    script = offering(host, tmp_path, languages=OFFERED)
    result = run(host, script, dialog=["<cancel>"], env=environ)
    assert result.returncode == 1
    (menu,) = host.calls("whiptail")
    assert menu[menu.index("--default-item") + 1] == wanted
    assert menu[menu.index("--") + 5 :] == [part for item in OFFERED.items() for part in item]
    monkeypatch.setattr(i18n, "LANGUAGES", MappingProxyType(OFFERED))
    assert i18n.choose(environ) == wanted


@pytest.mark.parametrize(
    "case", ["install", "reconfigure", "only English", "--answers", "--update", "--help",
             "an unknown option"],
)  # fmt: skip
def test_the_language_menu_comes_first(host: Host, tmp_path: Path, case: str) -> None:
    """The first dialog of an installation and a reconfiguration, while more than English is
    offered; none without dialogs. --help and errors before it stay English."""
    script = SCRIPT if case == "only English" else offering(host, tmp_path)
    args: tuple[str, ...] = ()
    if case in {"reconfigure", "--update"}:
        existing_container(host)
        args = ("--reconfigure" if case == "reconfigure" else case, "105")
    elif case == "--answers":
        args = (case, str(answers_file(host)))
    elif case == "--help":
        args = (case,)
    elif case == "an unknown option":
        args = ("--bogus",)
    dialog = ["<cancel>"] if case == "only English" else ["en", "<cancel>"]
    result = run(host, script, *args, dialog=dialog)
    texts = [text_of(call) for call in host.calls("whiptail")]
    assert texts == {
        "install": [LANGUAGE_MENU, "Setup"],
        "reconfigure": [LANGUAGE_MENU, "Setup"],
        "only English": ["Setup"],
        "an unknown option": ["Error: unknown option --bogus (see --help)"],
    }.get(case, [])
    assert result.returncode == (case not in {"--answers", "--update", "--help"}), result.stderr
    if case == "--help":
        assert result.stdout.startswith("Usage: meshcore-home-gateway-lxc.sh [OPTION...]")
    assert not host.calls("wget")


@pytest.mark.parametrize("case", ["xx", "en", "--update", "--answers"])
def test_container_commands_get_the_language(
    host: Host, tmp_path: Path, pseudo_program: Path, case: str
) -> None:
    """With the pack loaded, the program in the container speaks the dialogs' language; else it
    gets the environment it always had."""
    script = offering(host, tmp_path, pseudo_program)
    dialog = None
    args: tuple[str, ...] = ()
    if case == "--update":
        existing_container(host)
        args = (case, "105")
    elif case == "--answers":
        args = (case, str(answers_file(host)))
    else:
        dialog = default(host, ["broker.example.org", "bridge", "pw"], language=case)
    result = run(host, script, *args, dialog=dialog)
    assert result.returncode == 0, result.stderr
    calls = host.pct("exec")
    assert calls
    language = ["LANGUAGE=xx"] if case == "xx" else []
    assert all(environment_of(call)[1:] == ["LANG=C.UTF-8", *language] for call in calls)


# --- the language of the names in Home Assistant -------------------------------------------------


def test_a_new_installation_names_in_the_menus_language(
    host: Host, tmp_path: Path, pseudo_program: Path
) -> None:
    """The default setup asks nothing more: the names follow the dialogs' language."""
    script = offering(host, tmp_path, pseudo_program)
    dialog = default(host, ["broker.example.org", "bridge", "pw"])
    result = run(host, script, dialog=dialog)
    assert result.returncode == 0, result.stderr
    assert len(host.dialogs()) == len(dialog)
    assert answered(host)["homeassistant.language"] == "xx"


def test_the_advanced_setup_asks_for_the_names_language(
    host: Host, tmp_path: Path, pseudo_program: Path
) -> None:
    """A menu of the languages' names, the dialogs' language first."""
    script = offering(host, tmp_path, pseudo_program)
    result = run(host, script, dialog=advanced(host, "en", language="xx"))
    assert result.returncode == 0, result.stderr
    (menu,) = [call for call in host.calls("whiptail") if NAMES_MENU in call]
    assert menu[menu.index("--default-item") + 1] == "xx"
    assert menu[menu.index("--") + 5 :] == ["en", "English", "xx", "Pseudo"]
    assert answered(host)["homeassistant.language"] == "en"


@pytest.mark.parametrize(("configured", "written"), [("de", "de"), (None, "en")])
def test_a_reconfiguration_keeps_the_names_language(
    host: Host, tmp_path: Path, configured: str | None, written: str
) -> None:
    """Not the dialogs' language: changing only the broker must not rename every entity. A file
    without the setting gets English, its default."""
    script = offering(host, tmp_path)
    existing_container(host)
    if configured is not None:
        current = (host.root / "current").read_text(encoding="utf-8")
        (host.root / "current").write_text(current + f"homeassistant.language={configured}\n")
    dialog = ["xx", "default", "old.example.org", "bridge", "", "1", ALICE, ALICE, "status"]
    result = run(host, script, "--reconfigure", "105", dialog=dialog,
                 env={"STUB_VERSION": "1.2.3"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert answered(host)["homeassistant.language"] == written


@pytest.mark.parametrize("offered", [PSEUDO, {"en": "English"}])
@pytest.mark.parametrize(
    ("answers", "written"),
    [("", "en"), ("homeassistant.language=xx\n", "xx"),
     ("homeassistant.discovery=false\n", None)],
)  # fmt: skip
def test_the_answers_name_the_names_language(
    host: Host,
    tmp_path: Path,
    pseudo_program: Path,
    *,
    offered: dict[str, str],
    answers: str,
    written: str | None,
) -> None:
    """As given, whatever the installer offers; English without one; none without discovery."""
    script = offering(host, tmp_path, pseudo_program, languages=offered)
    path = answers_file(host, f"radio.choice={host.serial}\nmqtt.host=b\n{answers}")
    result = run(host, script, "--answers", str(path))
    assert result.returncode == 0, result.stderr
    assert answered(host).get("homeassistant.language") == written


OLDER = "meshcore-home-gateway 1.1.0a3 in container 105 has no setting homeassistant.language"


@pytest.mark.parametrize(
    ("installed", "answers", "written"),
    [("1.1.0a3", "", None), ("1.1.0a4", "", "en"), ("1.2.0", "", "en"),
     ("1.1.0a3", "homeassistant.language=en\n", "en")],
)  # fmt: skip
def test_an_older_version_gets_no_language_setting(
    host: Host, installed: str, answers: str, written: str | None
) -> None:
    """A version before 1.1.0a4 refuses the setting: learned only after the installation (--ref),
    the installer leaves its own value out and says so once; a value of the answers goes as
    given."""
    path = answers_file(host, f"radio.choice={host.serial}\nmqtt.host=b\n{answers}")
    result = run_installer(host, "--ref", "v1.1.0a3", "--answers", str(path),
                           env={"STUB_VERSION": installed})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert answered(host).get("homeassistant.language") == written
    logged = result.stderr.count(OLDER)
    assert logged == (installed == "1.1.0a3" and not answers)
    if logged:
        assert f"{OLDER}: the names in Home Assistant stay English\n" in result.stderr


def test_an_older_release_is_not_asked_for_the_names_language(host: Host, tmp_path: Path) -> None:
    """Its version is known before the installation."""
    script = offering(host, tmp_path)
    result = run(host, script, "--release", "1.1.0a3", dialog=advanced(host, None),
                 env={"STUB_VERSION": "1.1.0a3"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert not [call for call in host.calls("whiptail") if "Language of the names" in str(call)]
    assert "homeassistant.language" not in answered(host)


def test_a_reconfiguration_asks_what_the_installed_version_knows(
    host: Host, tmp_path: Path
) -> None:
    """From a checkout too: the installed program renders the configuration, not the
    checkout's."""
    script = offering(host, tmp_path)
    existing_container(host)
    dialog = ["en", "advanced", "old.example.org", "no", "1883", "bridge", "", "meshcore", "gw1",
              "yes", "MeshCore radio", "no", "1", ALICE, ALICE, "status", ""]  # fmt: skip
    result = run(host, script, "--reconfigure", "105", dialog=dialog,
                 env={"STUB_VERSION": "1.1.0a3"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert len(host.dialogs()) == len(dialog)
    assert not [call for call in host.calls("whiptail") if "Language of the names" in str(call)]
    assert "homeassistant.language" not in answered(host)


def test_a_checkout_always_gets_the_setting(host: Host, tmp_path: Path) -> None:
    """Its program comes from the script's own tree, which knows the setting while its version
    still names an older release."""
    source = tmp_path / "old" / "src"
    package = source / "meshcore_home_gateway"
    package.mkdir(parents=True)
    real = ROOT / "src" / "meshcore_home_gateway"
    for entry in real.iterdir():
        if entry.name not in {"__init__.py", "__pycache__"}:
            (package / entry.name).symlink_to(entry)
    init = (real / "__init__.py").read_text(encoding="utf-8")
    (package / "__init__.py").write_text(
        re.sub(r'__version__ = ".*"', '__version__ = "1.1.0a3"', init), encoding="utf-8"
    )
    script = offering(host, tmp_path, source)
    result = run(host, script, dialog=advanced(host, "en"), env={"STUB_VERSION": "1.1.0a3"})
    assert result.returncode == 0, result.stderr
    assert "meshcore-home-gateway 1.1.0a3" in result.stderr
    assert [call for call in host.calls("whiptail") if NAMES_MENU.strip("‹›") in call]
    assert answered(host)["homeassistant.language"] == "en"


LOGIN_STATES = [
    ("0", (), "login accepted"),
    ("1", ("no",), "login failed (see above), the service keeps trying"),
    ("3", (), "not checked (version 1.2.3 cannot; check after --update)"),
]


@pytest.mark.parametrize(("answers", "again", "words"), LOGIN_STATES)
def test_every_state_of_the_login_has_its_words(
    host: Host,
    tmp_path: Path,
    pseudo_program: Path,
    *,
    answers: str,
    again: tuple[str, ...],
    words: str,
) -> None:
    """The summary turns the broker check's result into words, in the box's language."""
    script = offering(host, tmp_path, pseudo_program)
    if answers == "3":  # an installed version without the check: only on --reconfigure
        existing_container(host)
        dialog = ["xx", "default", "old.example.org", "bridge", "", "1", ALICE, ALICE, "status"]
        args: tuple[str, ...] = ("--reconfigure", "105")
    else:
        dialog = default(host, ["broker.example.org", "bridge", "pw"], again)
        args = ()
    env = {"BROKER_ANSWERS": answers, "STUB_VERSION": "1.2.3"}
    result = run(host, script, *args, dialog=dialog, env=env)
    assert result.returncode == 0, result.stderr
    (summary,) = [c[-3] for c in host.calls("whiptail") if c[-2:] == ["22", "78"]]
    assert f"Broker:       {words}\n" in result.stderr
    assert f"Broker:       ‹{words}›\n" in summary
    assert summary.startswith("‹meshcore-home-gateway 1.2.3 runs in container 105.")


# --- the settings check on the host -------------------------------------------------------------


INVALID = "radio.transport=serial\nradio.device=/dev/ttyACM0\nmqtt.host=b\nmqtt.node_id=Bad Id\n"


@pytest.mark.parametrize("language", ["xx", "en"])
def test_the_host_check_speaks_the_language_it_is_given(
    pseudo_program: Path, language: str
) -> None:
    """The reason of a refused setting in the language of LANGUAGE, the installer's dialogs'
    (cut out of the script, as the installer runs it)."""
    assert CHECK is not None
    result = subprocess.run(  # noqa: S603 - the installer's check on a copy of our package
        [sys.executable, "-I", "-B", "-c", CHECK.group(1), str(pseudo_program)],
        input=INVALID, env={"LANGUAGE": language, "PATH": os.environ["PATH"]},
        capture_output=True, text=True, check=False, timeout=60,
    )  # fmt: skip
    assert result.returncode == 1
    with pytest.raises(ConfigError) as refused:
        render_config(INVALID)
    english = str(refused.value)
    assert "mqtt.node_id" in english
    if language == "en":
        assert result.stderr == english + "\n"
    else:
        assert result.stderr.startswith("‹")
        assert "mqtt.node_id" in result.stderr
        assert english not in result.stderr
    assert not list(pseudo_program.rglob("__pycache__"))


def test_the_host_check_follows_the_dialogs_not_the_caller(
    host: Host, tmp_path: Path, pseudo_program: Path
) -> None:
    """A LANGUAGE passed on by ssh does not reach the check: the dialogs are English here."""
    script = checkout(tmp_path, TEXT, pseudo_program)
    answers = answers_file(host, f"radio.choice={host.serial}\nmqtt.host=b\nmqtt.node_id=Bad Id\n")
    result = run_installer(host, "--answers", str(answers), env={"LANGUAGE": "xx"}, script=script)
    assert result.returncode == 1
    assert "error: invalid settings: mqtt.node_id" in result.stderr
    assert "‹" not in result.stderr
