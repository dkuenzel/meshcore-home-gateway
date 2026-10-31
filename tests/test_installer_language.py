"""The installer's messages (ADR 0019): the block that loads a language pack and fills in texts, cut
out of the script and run on its own; the rules for its message calls; the texts that reach a
dialog through the pack; the host's settings check in the dialogs' language; the script's size.

A pack is plain text pinned by its SHA-256 (docs/development.md, Translations): these tests load
one made from the installer's template, with every text marked ‹…›, as the language menu will.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import NamedTuple

import pytest

from meshcore_home_gateway import __version__
from meshcore_home_gateway.catalog import Entry, read_po
from meshcore_home_gateway.config import ConfigError, render_config
from tests.test_installer import (
    ALICE,
    BASH,
    RELAY,
    SCRIPT,
    Host,
    answers_file,
    existing_container,
    install_stubs,
    installer_env,
    run_installer,
)
from tests.test_installer import RELEASES as DOWNLOADS  # the address of the release files

ROOT = Path(__file__).resolve().parent.parent
TEXT = SCRIPT.read_text(encoding="utf-8")
END_OF_MESSAGES = "# --- end of messages\n"
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


# the button labels the language menu will set from the pack: whiptail gets them before the rest
BUTTONS = ["--yes-button", "‹Yes›", "--no-button", "‹No›", "--ok-button", "‹Ok›",
           "--cancel-button", "‹Cancel›"]  # fmt: skip


def speaking(tmp_path: Path, release: str = "") -> Path:
    """A copy of the script that loads the pseudo-language's pack and its button labels right
    after its messages block, as the language menu (choose_language) will; `release` written in
    as `make release` writes it."""
    pack, pin = pinned(tmp_path, tool.pack("xx", marked(template())))
    buttons = " ".join(shlex.quote(label) for label in BUTTONS)
    load = f'load_messages "$(cat {pack})" {pin} || exit 99\nBUTTONS=({buttons})\n'
    assert TEXT.count(END_OF_MESSAGES) == 1
    text = TEXT.replace(END_OF_MESSAGES, END_OF_MESSAGES + load)
    if release:
        text = text.replace('\nRELEASE=""', f'\nRELEASE="{release}"', 1)
    return checkout(tmp_path, text)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    (tmp_path / "host").mkdir()
    return install_stubs(tmp_path / "host")


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


def default(host: Host, broker: list[str], again: tuple[str, ...] = ()) -> list[str]:
    """The answers of a default installation: the broker's, then (`again`) the answers after a
    refused login, which comes after the confirmation."""
    return ["default", str(host.serial), *broker, "yes", *again, "1", ALICE, ALICE, "status"]


class Scenario(NamedTuple):
    dialog: Callable[[Host], list[str]]
    env: dict[str, str]
    nested: tuple[str, ...] = ()  # translations shown as a value of another text, which hides them
    reconfigure: bool = False
    release: str = ""  # run from its address, published with this release


RELEASES = DOWNLOADS.removesuffix("/download/")  # the release page, as the script names it
SCENARIOS = {
    # a login refused, then the broker typed again: the user name it was; the plan; the summary
    "refused, typed again": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", "wrong"],
                          ("yes", "broker.example.org", "no", "1883", "bridge", "right")),
        {"BROKER_ANSWERS": "1 0", "PROBE_PLAN": PLAN_ALL},
        ("Login:    ‹user 'bridge' with a password›", "Broker:       ‹login accepted›"),
    ),
    "no user name, refused, settings refused": Scenario(
        lambda h: default(h, ["broker.example.org", ""],
                          ("yes", "broker.example.org", "no", "x", "",
                           "broker.example.org", "yes", "8883", "bridge", "")),
        {"BROKER_ANSWERS": "1 4 0"},
        ("Login:    ‹none (no user name)›",),
    ),
    "a login refused and kept": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", ""], ("no",)),
        {"BROKER_ANSWERS": "1"},
        ("Login:    ‹user 'bridge' without a password›",
         "Broker:       ‹login failed (see above), the service keeps trying›"),
    ),
    "advanced, a gateway, a radio over TCP": Scenario(
        lambda h: ["advanced", "106", "meshcore-home-gateway", "local-lvm", "4", "512", "vmbr1",
                   "192.0.2.20/24", "192.0.2.1", "local", "tcp", "192.0.2.30", "5000",
                   "broker.example.org", "yes", "8883", "bridge", "pw", "meshcore", "gw1", "yes",
                   "Mesh radio", "yes", "yes", "1", "", "", "x" * 32, "Mesh gateway"],
        {},
        ("Network:  ‹vmbr1, 192.0.2.20/24, gateway 192.0.2.1›",),
    ),
    "a failure after the dialogs": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", "pw"]),
        {"PVEAM_DOWNLOAD_FAILS": "1"},
        ("‹Error: ‹could not download debian-13-standard_13.6-1_amd64.tar.zst to local››",),
    ),
    "a pre-release from its address": Scenario(
        lambda h: default(h, ["broker.example.org", "bridge", "pw"]),
        {"STUB_VERSION": "0.2.0rc1"},
        (f"Update:       ‹the installer of a newer release ({RELEASES}) with -- --update 105›",),
        release="0.2.0rc1",
    ),
    "a version without the login check, a profile observed": Scenario(
        lambda h: ["default", "old.example.org", "bridge", "", "1", ALICE, ALICE, "status"],
        {"BROKER_ANSWERS": "3", "STUB_VERSION": "1.2.3", "PROBE_PLAN": PLAN_ALL},
        ("Broker:       ‹not checked (version 1.2.3 cannot; check after --update)›",),
        reconfigure=True,
    ),
    "the installed password kept": Scenario(
        lambda h: ["default", "old.example.org", "bridge", "new", "yes", "old.example.org", "no",
                   "1883", "bridge", "", "1", ALICE, ALICE, "status"],
        {"BROKER_ANSWERS": "1 0",
         "PROBE_PLAN": "name\\tGateway\\ntested\\tno\\nchange\\tautoadd\\t0\\t1\\treported\\n"},
        reconfigure=True,
    ),
}  # fmt: skip


def shown(host: Host) -> list[str]:
    """The text of every dialog, and the items of the menus that are ours."""
    texts = []
    for call in host.calls("whiptail"):
        text = call[call.index("--") + 1]
        texts.append(text)
        if "--menu" in call:  # tag, item, ...: our items say what a tag means
            items = call[call.index("--") + 6 :: 2]
            texts += [item for item in items if not item.startswith(("usb-", str(host.serial)))]
    return texts


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_dialog_text_goes_through_the_catalog(host: Host, tmp_path: Path, name: str) -> None:
    """With a pack loaded, every dialog, box and menu item shows its translation (values and the
    plan's setting lines aside), with the pack's button labels, while the installer's own log
    stays English."""
    scenario = SCENARIOS[name]
    script = speaking(tmp_path, scenario.release)
    args: tuple[str, ...] = ()
    if scenario.reconfigure:
        existing_container(host)
        current = (host.root / "current").read_text(encoding="utf-8")
        (host.root / "current").write_text(current + "radio.profile.manage=observe\n")
        args = ("--reconfigure", "105")
    if scenario.release:  # bash -c "$(wget -qO- URL)": no file, no checkout
        (host.root / "dialog").write_text("\n".join(scenario.dialog(host)) + "\n")
        result = subprocess.run(  # noqa: S603 - a copy of our script with fixed arguments
            [BASH, "-c", script.read_text(encoding="utf-8"), "--", *args],
            env=installer_env(host, scenario.env), capture_output=True, text=True,
            stdin=subprocess.DEVNULL, timeout=60, check=False,
        )  # fmt: skip
    else:
        result = run_installer(host, *args, dialog=scenario.dialog(host), env=scenario.env,
                               script=script)  # fmt: skip
    assert result.returncode == (name == "a failure after the dialogs"), result.stderr
    texts = shown(host)
    assert texts
    for text in texts:
        assert all(
            not line.strip() or SETTING_LINE.fullmatch(line) for line in bare(text).split("\n")
        ), text
    for nested in scenario.nested:
        assert any(nested in text for text in texts), nested
    assert all(call[1:9] == BUTTONS for call in host.calls("whiptail"))
    assert "‹" not in result.stderr
    assert "meshcore-home-gateway: " in result.stderr


LOGIN_STATES = [
    ("0", (), "login accepted"),
    ("1", ("no",), "login failed (see above), the service keeps trying"),
    ("3", (), "not checked (version 1.2.3 cannot; check after --update)"),
]


@pytest.mark.parametrize(("answers", "again", "words"), LOGIN_STATES)
def test_every_state_of_the_login_has_its_words(
    host: Host, tmp_path: Path, answers: str, again: tuple[str, ...], words: str
) -> None:
    """The summary turns the broker check's result into words, in the box's language."""
    script = speaking(tmp_path)
    if answers == "3":  # an installed version without the check: only on --reconfigure
        existing_container(host)
        dialog = ["default", "old.example.org", "bridge", "", "1", ALICE, ALICE, "status"]
        args: tuple[str, ...] = ("--reconfigure", "105")
    else:
        dialog = default(host, ["broker.example.org", "bridge", "pw"], again)
        args = ()
    env = {"BROKER_ANSWERS": answers, "STUB_VERSION": "1.2.3"}
    result = run_installer(host, *args, dialog=dialog, env=env, script=script)
    assert result.returncode == 0, result.stderr
    (summary,) = [c[-3] for c in host.calls("whiptail") if c[-2:] == ["22", "78"]]
    assert f"Broker:       {words}\n" in result.stderr
    assert f"Broker:       ‹{words}›\n" in summary
    assert summary.startswith("‹meshcore-home-gateway 1.2.3 runs in container 105.")


# --- the settings check on the host -------------------------------------------------------------


@pytest.fixture
def pseudo_program(tmp_path: Path) -> Path:
    """A copy of the package that offers the pseudo-language: i18n.LANGUAGES gains xx, and
    locale/xx.po marks every text ‹…›. The host's settings check runs it in a `python3 -I`
    subprocess, which no catalog of this process reaches."""
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
