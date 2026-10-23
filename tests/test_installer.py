"""The Proxmox installer, run with stub Proxmox commands (pct, pveam, pvesm, pvesh, whiptail).

The stubs record every call and answer like a Proxmox VE 9 host would (output formats from the
Proxmox sources, see docs/deployment.md). The tests check the container that would be created,
the commands run inside it, and that the answers sent into the container render to a valid
configuration. The `pct exec` stub refuses commands that do not start from a clean environment,
and keeps what a command reads from its input as the file it writes (`cat >/root/NAME`).
The scripts sent into the container run on their own, against a directory that stands in for the
container's file system.
"""

from __future__ import annotations

import functools
import grp
import hashlib
import http.server
import io
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import textwrap
import threading
import time
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from meshcore_home_gateway import __version__
from meshcore_home_gateway.config import Config, parse_config, render_config

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = ROOT / "deploy" / "proxmox" / "meshcore-home-gateway-lxc.sh"
ALICE = "a1" * 32
RELAY = "b2" * 32
TEMPLATE = "debian-13-standard_13.6-1_amd64.tar.zst"
SEP = "\x1f"  # between arguments in the call log
NL = "\x1e"  # stands for a newline inside an argument
LIB = "/usr/local/lib/meshcore-home-gateway"
CONFIG = "/etc/meshcore-home-gateway/config.toml"
ANSWERS = "/root/meshcore-home-gateway-answers"

STUBS = {
    "pct": r"""
        log pct "$@"
        state="$STUB/ct-$2"
        case $1 in
          status)
            [[ -f $state ]] || { echo "Configuration file does not exist" >&2; exit 2; }
            echo "status: $(cat "$state")" ;;
          create) echo stopped >"$state" ;;
          start|reboot) echo running >"$state" ;;
          push) mkdir -p "$STUB/pushed"; cp "$3" "$STUB/pushed/$(basename "$4")" ;;
          exec)
            shift 3
            if [[ ${1-} != env || ${2-} != -i ]]; then echo "stub: no env -i" >&2; exit 97; fi
            shift 2
            while [[ ${1-} == *=* ]]; do shift; done
            if [[ -p /dev/stdin ]]; then  # files sent into the container through the input
              mkdir -p "$STUB/pushed"
              cat >"$STUB/input"
              written='cat >/root/([^[:space:])]+)'
              if [[ $* =~ $written ]]; then
                cp "$STUB/input" "$STUB/pushed/${BASH_REMATCH[1]}"
              fi
            fi
            [[ -z ${SLEEP_AT-} || $* != *"$SLEEP_AT"* ]] || sleep 60
            case "$*" in
              *'--output $dir/config.toml'*)  # the settings check
                [[ -z ${CHECK_FAILS-} ]] || { echo "bad" >&2; exit 78; } ;;
              *"daemon-reload"*) echo "${STUB_VERSION:-0.0.0}" ;;  # the installation step
              "meshcore-home-gateway --version")
                echo "meshcore-home-gateway ${STUB_VERSION:-0.0.0}" ;;
              *"--list channels"*)
                [[ -z ${PROBE_FAILS-} ]] || exit 1
                printf '%b' "${PROBE_CHANNELS:-0\\tPublic\\n1\\tAlerts\\n2\\tpublic\\n}" ;;
              *"--list contacts"*)
                [[ -n ${PROBE_ONLY_REPEATER-} ]] || printf '%s\tAlice Example\tchat\n' "$ALICE"
                printf '%s\tRelay\trepeater\n' "$RELAY" ;;
              "getent group dialout") printf 'dialout:x:%s:\n' "${CT_DIALOUT_GID:-20}" ;;
              *"config answers"*) cat "$STUB/current" ;;
              "test -f "*) [[ -e $STUB/current ]] ;;
            esac ;;
        esac
    """,
    "pvesh": r"""
        log pvesh "$@"
        [[ -z ${PVESH_FAILS-} ]] || exit 1
        if [[ $* == *--vmid* ]]; then
          id=${!#}
          if [[ -e $STUB/ct-$id || -e $STUB/vm-$id ]]; then
            echo "VM $id already exists" >&2; exit 255
          fi
          echo "$id"
        else
          echo 105
        fi
    """,
    "pvesm": r"""
        log pvesm "$@"
        [[ -z ${PVESM_FAILS-} ]] || exit 1
        echo "Name         Type     Status     Total (KiB)    Used (KiB) Available (KiB)    %"
        if [[ $* == *rootdir* ]]; then
          echo "local-zfs    zfspool  inactive          0             0               0  N/A"
          echo "local-lvm    lvmthin  active      1048576        104857          943719 10.00%"
        fi
        if [[ $* == *vztmpl* ]]; then
          echo "local        dir      active      1048576        104857          943719 10.00%"
        fi
    """,
    "pveam": r"""
        log pveam "$@"
        case $1 in
          update)
            if [[ -n ${PVEAM_UPDATE_FAILS-} ]]; then
              echo "update failed - see /var/log/pveam.log for details" >&2
            else
              echo "update successful"
            fi ;;
          available)
            printf '%-15s %-60s %s\n' \
              system debian-12-standard_12.12-1_amd64.tar.zst amd64 \
              system debian-13-standard_13.1-2_amd64.tar.zst amd64 \
              system debian-13-standard_13.6-1_amd64.tar.zst amd64 \
              system debian-13-standard_13.6-1_arm64.tar.zst arm64 ;;
          download)
            if [[ -n ${PVEAM_DOWNLOAD_FAILS-} ]]; then
              echo "TASK ERROR: download failed" >&2
            else
              : >"$STUB/tmpl-$3"
            fi ;;
          list)
            printf '%-60s %-6s\n' NAME SIZE
            for file in "$STUB"/tmpl-*; do
              if [[ -e $file ]]; then printf '%-60s %s\n' "$2:vztmpl/${file##*/tmpl-}" 123.93MB; fi
            done ;;
        esac
    """,
    "pveversion": 'echo "pve-manager/${PVE_VERSION:-9.0.10}/0123abcd (running kernel: 6.14)"',
    "dpkg-query": 'printf "%s" "${PVE_CONTAINER:-6.1.14}"',
    "id": 'echo "${STUB_UID:-0}"',
    "whiptail": r"""
        log whiptail "$@"
        positional=0 ended=0  # like popt: after the box type, '-' starts an option unless after --
        for arg in "$@"; do
          if (( positional && ! ended )); then
            if [[ $arg == -- ]]; then ended=1; continue; fi
            if [[ $arg == -* ]]; then echo "$arg: unknown option" >&2; exit 1; fi
          fi
          case $arg in
            --inputbox|--passwordbox|--yesno|--menu|--checklist|--msgbox) positional=1 ;;
          esac
        done
        printf '\033[H[screen]'  # the real one draws on stdout; captured, it would corrupt answers
        [[ $* == *--msgbox* ]] && exit 0
        answer=$(head -n 1 "$STUB/dialog"); sed -i 1d "$STUB/dialog"
        if [[ $* == *--yesno* ]]; then [[ $answer == yes ]]; exit; fi
        printf '%s' "$answer" | tr '|' '\n' >&2
    """,
}

CURRENT = f"""\
radio.transport=serial
radio.device={{serial}}
mqtt.host=old.example.org
mqtt.username=bridge
mqtt.password=old-secret
mqtt.node_id=gw1
messages.queue_size=50
commands.senders={ALICE}
commands.words=status
target.my-alerts=channel:1
target.alice=contact:{ALICE}
"""


@dataclass
class Host:
    root: Path
    serial: Path

    def calls(self, tool: str | None = None) -> list[list[str]]:
        log = self.root / "calls.log"
        text = log.read_text(encoding="utf-8") if log.exists() else ""
        lines = [line for line in text.split("\n") if line]  # not splitlines(): it splits at NL
        records = [[arg.replace(NL, "\n") for arg in line.split(SEP)] for line in lines]
        return [r for r in records if tool is None or r[0] == tool]

    def pct(self, action: str) -> list[list[str]]:
        return [c for c in self.calls("pct") if c[1] == action]

    def inside_args(self) -> list[list[str]]:
        """Commands run in the container, without the `env -i NAME=value ...` prefix."""
        commands = []
        for call in self.pct("exec"):
            args = call[6:]  # pct exec ID -- env -i
            while args and "=" in args[0]:
                args = args[1:]
            commands.append(args)
        return commands

    def inside(self) -> list[str]:
        return [" ".join(args) for args in self.inside_args()]

    def script(self, marker: str) -> str:
        """The `bash -c` script run in the container that contains `marker`."""
        (script,) = [a[2] for a in self.inside_args() if a[:2] == ["bash", "-c"] and marker in a[2]]
        return script

    def dialogs(self) -> list[list[str]]:
        return [c for c in self.calls("whiptail") if "--msgbox" not in c]

    def pushed(self, name: str) -> str:
        return (self.root / "pushed" / name).read_text(encoding="utf-8")

    def config(self) -> Config:
        return parse_config(render_config(self.pushed("meshcore-home-gateway-answers")))

    def log_text(self) -> str:
        return (self.root / "calls.log").read_text(encoding="utf-8")


def install_stubs(root: Path, real: frozenset[str] = frozenset()) -> Host:
    """Write the stub commands to `root/bin`, except those in `real`, and a serial device."""
    bin_dir = root / "bin"
    bin_dir.mkdir()
    serial_dir = root / "by-id"
    serial_dir.mkdir()
    serial = serial_dir / "usb-Example_Radio_0001-if00"
    serial.write_text("", encoding="utf-8")
    prelude = (
        "#!/usr/bin/env bash\nset -eu\n"
        f"log() {{ local IFS=$'{SEP}'; local line=\"$*\"; line=${{line//$'\\n'/$'{NL}'}}; "
        'printf "%s\\n" "$line" >>"$STUB/calls.log"; }\n'
    )
    for name, body in STUBS.items():
        if name not in real:
            path = bin_dir / name
            path.write_text(prelude + textwrap.dedent(body).lstrip(), encoding="utf-8")
            path.chmod(0o755)
    return Host(root, serial)


@pytest.fixture
def host(tmp_path: Path) -> Host:
    return install_stubs(tmp_path)


def installer_env(host: Host, env: dict[str, str] | None = None) -> dict[str, str]:
    return {
        "PATH": f"{host.root / 'bin'}:{os.environ['PATH']}",
        "STUB": str(host.root),
        "ALICE": ALICE,
        "RELAY": RELAY,
        "MESHCORE_HOME_GATEWAY_SERIAL_DIR": str(host.serial.parent),
        "STUB_VERSION": __version__,
        **(env or {}),
    }


def run_installer(
    host: Host,
    *args: str,
    dialog: list[str] | None = None,
    env: dict[str, str] | None = None,
    script: Path = SCRIPT,
) -> subprocess.CompletedProcess[str]:
    if dialog is not None:
        (host.root / "dialog").write_text("\n".join(dialog) + "\n", encoding="utf-8")
    return subprocess.run(  # noqa: S603 - our script with fixed arguments
        [BASH, str(script), *args], env=installer_env(host, env), capture_output=True, text=True,
        stdin=subprocess.DEVNULL, timeout=60, check=False,
    )  # fmt: skip


def interrupt_installer(
    host: Host, sig: signal.Signals, during: str, *args: str
) -> subprocess.CompletedProcess[str]:
    """Run the installer and send `sig` to its process group during a command in the container.

    Like Ctrl-C or a closed SSH session, which signal the whole foreground process group.
    """
    process = subprocess.Popen(  # noqa: S603 - our script with fixed arguments
        [BASH, str(SCRIPT), *args], env=installer_env(host, {"SLEEP_AT": during}),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 30
        while not any(during in c for c in host.inside()):
            assert process.poll() is None, process.communicate()[1]
            assert time.monotonic() < deadline, f"the installer did not reach {during!r}"
            time.sleep(0.05)
        os.killpg(process.pid, sig)
        stdout, stderr = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def path_without(host: Host, *commands: str) -> str:
    """The stubs and the test's PATH without `commands`: directories that hold one become links."""
    for command in commands:
        (host.root / "bin" / command).unlink(missing_ok=True)
    directories = [host.root / "bin"]
    for number, entry in enumerate(os.environ["PATH"].split(os.pathsep)):
        directory = Path(entry)
        if any((directory / command).exists() for command in commands):
            directory, original = host.root / f"path-{number}", directory
            directory.mkdir()
            for path in original.iterdir():
                if path.name not in commands:
                    (directory / path.name).symlink_to(path)
        directories.append(directory)
    return os.pathsep.join(str(d) for d in directories)


def answers_file(host: Host, text: str | None = None) -> Path:
    path = host.root / "answers.txt"
    if text is None:
        text = (
            f"radio.choice={host.serial}\nmqtt.host=broker.example.org\nmqtt.password=s3cret=x\n"
            f"targets.channels=1\ntargets.contacts={ALICE}\ncommands.senders={ALICE}\n"
            "commands.words=status,arm\n"
        )
    path.write_text(text, encoding="utf-8")
    return path


def existing_container(host: Host, state: str = "running") -> None:
    (host.root / "ct-105").write_text(state, encoding="utf-8")
    (host.root / "current").write_text(CURRENT.format(serial=host.serial), encoding="utf-8")


def create_options(host: Host) -> dict[str, str]:
    (create,) = host.pct("create")
    return dict(zip(create[4::2], create[5::2], strict=True))


@dataclass
class Container:
    """A container's file system below `root`, to run the scripts the installer sends into it.

    Absolute paths in a script move below `root`. `python3` and the container's /usr/bin/python3
    are the Python running the tests; `systemctl` and the commands added with `stub` record their
    arguments.
    """

    root: Path

    def path(self, absolute: str) -> Path:
        return self.root / absolute.lstrip("/")

    def stub(self, name: str, body: str = "") -> None:
        path = self.root / "stubs" / name
        path.write_text(
            f'#!/bin/sh\nprintf "%s\\n" "$*" >>"{self.root}/{name}.log"\n{body}\n', encoding="utf-8"
        )
        path.chmod(0o755)

    def calls(self, name: str) -> list[str]:
        log = self.root / f"{name}.log"
        return log.read_text(encoding="utf-8").splitlines() if log.exists() else []

    def receive(self, host: Host, path: str) -> None:
        """Put the file the installer sent to `path` (through a command's input) there."""
        shutil.copy(host.root / "pushed" / Path(path).name, self.path(path))

    def run(self, script: str, stdin: str = "") -> subprocess.CompletedProcess[str]:
        absolute = r"(?<![\w.-])/(?=(?:etc|root|usr/local)/)"
        script = re.sub(absolute, lambda _: f"{self.root}/", script)
        script = re.sub(r"(?<=\s)/usr/bin/python3\b", lambda _: sys.executable, script)
        return subprocess.run(  # noqa: S603 - a script of the installer
            [BASH, "-c", script], env={"PATH": f"{self.root / 'stubs'}:{os.environ['PATH']}"},
            input=stdin, capture_output=True, text=True, timeout=60, check=False,
        )  # fmt: skip


@pytest.fixture
def container(tmp_path: Path) -> Container:
    root = tmp_path / "container"
    for directory in ("stubs", "root", "etc/meshcore-home-gateway", "etc/systemd/system",
                      "usr/local/bin"):  # fmt: skip
        (root / directory).mkdir(parents=True)
    container = Container(root)
    container.stub("python3", f'exec "{sys.executable}" "$@"')
    container.stub("systemctl")
    return container


# --- installation ------------------------------------------------------------------------------


def test_unattended_install_creates_and_configures_the_container(host: Host) -> None:
    result = run_installer(host, "--answers", str(answers_file(host)))
    assert result.returncode == 0, result.stderr

    (create,) = host.pct("create")
    assert create[2:4] == ["105", f"local:vztmpl/{TEMPLATE}"]  # newest Debian 13, amd64
    options = create_options(host)
    assert options["--unprivileged"] == "1"  # not the default on Proxmox VE 8
    assert options["--features"] == "nesting=1"  # systemd 257 needs it
    assert options["--rootfs"] == "local-lvm:2"  # first *active* storage for containers
    assert options["--net0"] == "name=eth0,bridge=vmbr0,ip=dhcp"
    assert (options["--onboot"], options["--timezone"]) == ("1", "host")
    assert ["pveam", "download", "local", TEMPLATE] in host.calls()
    assert ["pct", "set", "105", "--dev0", f"path={host.serial},gid=20,mode=0660"] in host.calls()
    assert not host.pct("reboot")  # dialout has GID 20 in the container

    order = [c[1] for c in host.calls("pct")]
    assert order.index("create") < order.index("set") < order.index("start") < order.index("exec")
    assert all(c[3:6] == ["--", "env", "-i"] for c in host.pct("exec"))
    assert not host.pct("push")  # files go through the input of commands: none on the host

    inside = host.inside()
    assert any("apt-get install" in c and "python3-paho-mqtt" in c for c in inside)
    assert any("build_pyz.py" in c and "/usr/bin/python3 -I" in c for c in inside)
    assert any("config render" in c and "--group meshcore-home-gateway" in c for c in inside)
    assert inside[-2:] == [
        "systemctl enable --now meshcore-home-gateway",
        "systemctl is-active --quiet meshcore-home-gateway",
    ]

    config = host.config()
    assert config.radio.device == str(host.serial)
    assert config.mqtt.password == "s3cret=x"
    assert {t.alias: t.channel_index for t in config.targets if t.channel_index is not None} == {
        "alerts": 1
    }
    assert {t.alias: t.public_key for t in config.targets if t.public_key} == {
        "alice-example": ALICE
    }
    assert config.commands.senders == frozenset({ALICE})
    assert config.commands.words == frozenset({"status", "arm"})
    assert "s3cret" not in result.stderr + host.log_text()  # the password is never logged


def test_device_group_is_corrected_when_dialout_differs(host: Host) -> None:
    result = run_installer(host, "--answers", str(answers_file(host)), env={"CT_DIALOUT_GID": "30"})
    assert result.returncode == 0, result.stderr
    assert ["pct", "set", "105", "--dev0", f"path={host.serial},gid=30,mode=0660"] in host.calls()
    assert ["pct", "reboot", "105"] in host.calls()


def test_radio_over_tcp_needs_no_device(host: Host) -> None:
    answers = answers_file(
        host, "radio.choice=tcp\nradio.host=192.0.2.30\nradio.port=5001\nmqtt.host=broker\n"
    )
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    assert not host.pct("set")
    assert any("probe --tcp 192.0.2.30:5001 --list channels" in c for c in host.inside())
    config = host.config()
    assert (config.radio.transport, config.radio.host, config.radio.port) == (
        "tcp", "192.0.2.30", 5001,
    )  # fmt: skip


def test_an_ipv6_radio_address_reaches_probe_in_brackets(host: Host) -> None:
    answers = answers_file(
        host, "radio.choice=tcp\nradio.host=2001:db8::30\nradio.port=5001\nmqtt.host=broker\n"
    )
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    assert any("probe --tcp [2001:db8::30]:5001 --list channels" in c for c in host.inside())
    assert host.config().radio.host == "2001:db8::30"


def test_interactive_default_setup_asks_only_what_has_no_default(host: Host) -> None:
    dialog = [
        "default",  # setup
        str(host.serial),  # radio
        "", "broker.example.org",  # broker address: required, asked again
        "bridge", "pw-secret",  # user, password
        "yes",  # create the container
        "0|1|2",  # channels
        ALICE,  # contacts
        "",  # command senders: none
    ]  # fmt: skip
    result = run_installer(host, dialog=dialog)
    assert result.returncode == 0, result.stderr
    assert len(host.dialogs()) == len(dialog)
    options = create_options(host)
    assert (options["--rootfs"], options["--memory"], options["--hostname"]) == (
        "local-lvm:2", "256", "meshcore-home-gateway",
    )  # fmt: skip
    config = host.config()
    assert config.mqtt.node_id == "radio1"
    assert {t.alias for t in config.targets} == {"public", "alerts", "public-2", "alice-example"}
    assert config.commands.senders == frozenset()
    assert "pw-secret" not in result.stderr + host.log_text()


def test_interactive_advanced_setup_asks_for_everything(host: Host) -> None:
    dialog = [
        "advanced",
        # id, name, disk, memory, bridge
        "106", "meshcore-home-gateway", "local-lvm", "4", "512", "vmbr1",
        "192.0.2.20/24", "192.0.2.1", "local",  # address, gateway, template storage
        str(host.serial),
        "broker.example.org", "no", "1883", "bridge", "pw", "meshcore", "gw1",  # broker, topics
        "yes", "Mesh radio", "yes",  # discovery, device name, ASCII only
        "yes",  # create the container
        "0|1", ALICE, ALICE, "status,arm",  # channels, contacts, senders, words
    ]  # fmt: skip
    result = run_installer(host, dialog=dialog)
    assert result.returncode == 0, result.stderr
    options = create_options(host)
    assert host.pct("create")[0][2] == "106"
    assert options["--net0"] == "name=eth0,bridge=vmbr1,ip=192.0.2.20/24,gw=192.0.2.1"
    assert (options["--rootfs"], options["--memory"]) == ("local-lvm:4", "512")
    config = host.config()
    assert (config.mqtt.node_id, config.homeassistant.device_name) == ("gw1", "Mesh radio")
    assert config.messages.ascii_only is True
    assert config.commands.words == frozenset({"status", "arm"})
    checklists = [c for c in host.dialogs() if "--checklist" in c]
    assert all("Relay" not in c for c in checklists)  # repeaters are not offered as targets


def test_without_a_user_name_no_password_is_asked(host: Host) -> None:
    """The broker takes a password only with a user name."""
    dialog = ["default", str(host.serial), "broker.example.org", "", "yes", "1", "", ""]
    result = run_installer(host, dialog=dialog)
    assert result.returncode == 0, result.stderr
    assert len(host.dialogs()) == len(dialog)
    assert not [c for c in host.dialogs() if "--passwordbox" in c]
    config = host.config()
    assert (config.mqtt.username, config.mqtt.password) == (None, None)


def test_declining_the_confirmation_creates_nothing(host: Host) -> None:
    dialog = ["default", str(host.serial), "broker.example.org", "bridge", "pw", "no"]
    result = run_installer(host, dialog=dialog)
    assert result.returncode == 1
    assert "cancelled" in result.stderr
    assert not host.pct("create")
    assert not [c for c in host.calls("whiptail") if "--msgbox" in c]  # no error box


def test_names_that_look_like_options_are_offered_safely(host: Host) -> None:
    dialog = ["default", str(host.serial), "broker", "bridge", "pw", "yes", "0", "", ""]
    env = {"PROBE_CHANNELS": "0\\t-=Ops=-\\n"}
    result = run_installer(host, dialog=dialog, env=env)
    assert result.returncode == 0, result.stderr
    assert {t.alias: t.channel_index for t in host.config().targets} == {"ops": 0}


def test_the_settings_check_writes_no_bytecode_into_the_checkout(
    host: Host, tmp_path: Path
) -> None:
    """The checkout may belong to a user who could not remove root's __pycache__ directories."""
    checkout = tmp_path / "checkout"
    for part in ("src", "tools", "deploy"):
        shutil.copytree(ROOT / part, checkout / part, ignore=shutil.ignore_patterns("__pycache__"))
    script = checkout / SCRIPT.relative_to(ROOT)
    result = run_installer(host, "--answers", str(answers_file(host)), script=script)
    assert result.returncode == 0, result.stderr
    assert not list(checkout.rglob("__pycache__"))


def test_answers_file_with_windows_line_ends(host: Host) -> None:
    answers = answers_file(host)
    answers.write_bytes(answers.read_bytes().replace(b"\n", b"\r\n"))
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    assert host.config().radio.device == str(host.serial)


# --- refusals and failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"PVE_VERSION": "7.4-3"}, "too old"),
        ({"PVE_CONTAINER": "5.2.0"}, "pve-container 5.2.0"),
        ({"STUB_UID": "1000"}, "run as root"),
    ],
)
def test_preflight_refuses_unsuitable_hosts(host: Host, env: dict[str, str], message: str) -> None:
    result = run_installer(host, "--answers", str(answers_file(host)), env=env)
    assert result.returncode == 1
    assert message in result.stderr
    assert not host.calls("pct")


def test_invalid_settings_are_refused_before_anything_is_created(host: Host) -> None:
    answers = answers_file(host, f"radio.choice={host.serial}\nmqtt.host=b\nmqtt.node_id=Bad Id\n")
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 1
    assert "mqtt.node_id" in result.stderr
    assert not host.calls("pveam")
    assert not host.pct("create")


def test_missing_broker_address_is_refused(host: Host) -> None:
    result = run_installer(host, "--answers", str(answers_file(host, "radio.choice=tcp\n")))
    assert result.returncode == 1
    assert "Radio address" in result.stderr
    result = run_installer(
        host, "--answers", str(answers_file(host, f"radio.choice={host.serial}\n"))
    )
    assert result.returncode == 1
    assert "MQTT broker address" in result.stderr


def test_id_used_by_a_virtual_machine_is_refused(host: Host) -> None:
    (host.root / "vm-105").write_text("", encoding="utf-8")
    result = run_installer(host, "--answers", str(answers_file(host)))
    assert result.returncode == 1
    assert "already used" in result.stderr
    assert not host.pct("create")


def test_failed_template_list_update_uses_the_cached_list(host: Host) -> None:
    (host.root / f"tmpl-{TEMPLATE}").write_text("", encoding="utf-8")
    result = run_installer(
        host, "--answers", str(answers_file(host)), env={"PVEAM_UPDATE_FAILS": "1"}
    )
    assert result.returncode == 0, result.stderr
    assert "template list update failed" in result.stderr
    assert not [c for c in host.calls("pveam") if c[1] == "download"]  # already there


def test_failed_template_download_stops_before_creating(host: Host) -> None:
    env = {"PVEAM_DOWNLOAD_FAILS": "1"}
    result = run_installer(host, "--answers", str(answers_file(host)), env=env)
    assert result.returncode == 1
    assert "could not download" in result.stderr
    assert not host.pct("create")


@pytest.mark.parametrize(
    ("env", "message"),
    [({"PVESH_FAILS": "1"}, "next free container ID"), ({"PVESM_FAILS": "1"}, "storages")],
)
def test_a_failing_query_for_a_default_stops_before_anything_is_created(
    host: Host, env: dict[str, str], message: str
) -> None:
    """The answers give the storages; still the failed query is not reported and then ignored."""
    answers = answers_file(host)
    storages = "ct.storage=local-lvm\nct.template_storage=local\n"
    answers.write_text(answers.read_text(encoding="utf-8") + storages, encoding="utf-8")
    result = run_installer(host, "--answers", str(answers), env=env)
    assert result.returncode == 1
    assert result.stderr.count("error:") == 1
    assert message in result.stderr
    assert not host.pct("create")


def test_failure_after_creation_is_reported_once_and_names_the_container(host: Host) -> None:
    result = run_installer(host, "--answers", str(answers_file(host)), env={"PROBE_FAILS": "1"})
    assert result.returncode == 1
    assert result.stderr.count("error:") == 1
    assert "cannot read the radio" in result.stderr
    assert "pct destroy 105" in result.stderr


def test_unknown_answers_are_refused(host: Host) -> None:
    answers = answers_file(host)
    answers.write_text(answers.read_text(encoding="utf-8") + "ct.memroy_mb=512\n", encoding="utf-8")
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 1
    assert "ct.memroy_mb" in result.stderr
    assert not host.pct("create")


def test_answers_pass_every_setting_through(host: Host) -> None:
    extra = (
        "messages.queue_size=50\nairtime.budget_s_per_hour=18\ndiagnostics.log_level=debug\n"
        "homeassistant.prefix=ha\ncommands.max_age_s=600\n"
    )
    answers = answers_file(host)
    answers.write_text(answers.read_text(encoding="utf-8") + extra, encoding="utf-8")
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    config = host.config()
    assert (config.messages.queue_size, config.airtime.budget_s_per_hour) == (50, 18.0)
    assert (config.diagnostics.log_level, config.homeassistant.prefix) == ("debug", "ha")
    assert config.commands.max_age_s == 600


def test_a_malformed_answers_line_is_reported_without_its_content(host: Host) -> None:
    """The line may hold the broker password."""
    answers = answers_file(host, f"radio.choice={host.serial}\nmqtt.password: s3cret\n")
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 1
    assert "line 2" in result.stderr
    assert "s3cret" not in result.stderr


@pytest.mark.parametrize("separator", [": ", " "])
def test_a_password_with_equals_signs_in_a_malformed_line_stays_secret(
    host: Host, separator: str
) -> None:
    secret = "Zm9vYmFyYmF6cXV4MTIzNA=="  # base64: the line has an '=' after all
    lines = f"radio.choice={host.serial}\nmqtt.password{separator}{secret}\n"
    result = run_installer(host, "--answers", str(answers_file(host, lines)))
    assert result.returncode == 1
    assert "line 2" in result.stderr
    assert secret.rstrip("=") not in result.stderr


def test_an_answers_file_that_is_not_utf8_is_reported_clearly(host: Host) -> None:
    answers = answers_file(host)
    with answers.open("ab") as file:
        file.write(b"homeassistant.device_name=K\xfcche\r\n")
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 1
    assert "UTF-8" in result.stderr
    assert "Traceback" not in result.stderr
    assert not host.pct("create")


def test_a_container_id_below_100_is_refused(host: Host) -> None:
    answers = answers_file(host)
    answers.write_text(answers.read_text(encoding="utf-8") + "setup=advanced\nct.id=50\n",
                       encoding="utf-8")  # fmt: skip
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 1
    assert "at least 100" in result.stderr
    assert "already used" not in result.stderr
    assert not host.pct("create")


@pytest.mark.parametrize(
    "answer", ["targets.channels=one", "targets.channels=256", f"targets.contacts={ALICE[:-2]}"]
)
def test_malformed_targets_are_refused_before_anything_is_created(host: Host, answer: str) -> None:
    """The targets are chosen after the container is created; their answers are checked first."""
    answers = answers_file(host, f"radio.choice={host.serial}\nmqtt.host=broker\n{answer}\n")
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 1
    assert answer.partition("=")[0] in result.stderr
    assert not host.pct("create")


def test_targets_the_radio_does_not_offer_are_added_with_a_warning(host: Host) -> None:
    """For example a channel set up on the radio later; keys are matched in any case."""
    other = "c3" * 32
    answers = answers_file(
        host,
        f"radio.choice={host.serial}\nmqtt.host=broker\ntargets.channels=1, 7\n"
        f"targets.contacts={ALICE.upper()},{other}\n",
    )
    result = run_installer(host, "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    assert {t.alias: (t.channel_index, t.public_key) for t in host.config().targets} == {
        "alerts": (1, None),
        "channel-7": (7, None),
        "alice-example": (None, ALICE),
        "contact": (None, other),
    }
    warnings = [line for line in result.stderr.splitlines() if "warning:" in line]
    assert len(warnings) == 2
    assert "channel 7 " in warnings[0]
    assert f"contact {other} " in warnings[1]


def test_unattended_install_needs_the_radio_named(host: Host) -> None:
    """Without it the first serial device would be taken, possibly a Zigbee stick."""
    result = run_installer(host, "--answers", str(answers_file(host, "mqtt.host=broker\n")))
    assert result.returncode == 1
    assert "radio.choice" in result.stderr
    assert not host.pct("create")


# --- update and reconfigure --------------------------------------------------------------------


def test_update_installs_this_checkout_and_restarts_the_service(
    host: Host, container: Container
) -> None:
    existing_container(host)
    result = run_installer(host, "--update", "105")
    assert result.returncode == 0, result.stderr
    assert host.inside()[-1] == "systemctl restart meshcore-home-gateway"
    assert not host.pct("create")

    container.receive(host, "/root/meshcore-home-gateway-src.tar.gz")
    config = render_config(CURRENT.format(serial=host.serial))
    container.path(CONFIG).write_text(config, encoding="utf-8")
    run = container.run("umask 077\n" + host.script("build_pyz.py"))  # pct exec keeps the host's
    assert run.returncode == 0, run.stderr
    lib = container.path(LIB)
    assert stat.S_IMODE(lib.stat().st_mode) == 0o755  # the service user reads the program
    assert zipfile.is_zipfile(lib / f"meshcore-home-gateway-{__version__}.pyz")
    assert (lib / "current.pyz").readlink() == Path(f"meshcore-home-gateway-{__version__}.pyz")
    command = container.path("/usr/local/bin/meshcore-home-gateway")
    assert command.readlink() == lib / "current.pyz"
    unit = container.path("/etc/systemd/system/meshcore-home-gateway.service")
    assert unit.stat().st_mode & 0o777 == 0o644
    assert container.calls("systemctl") == ["daemon-reload"]
    assert not list(container.path("/root").iterdir())  # the sources are removed
    assert sorted(p.name for p in lib.iterdir()) == [
        "current.pyz", f"meshcore-home-gateway-{__version__}.pyz",
    ]  # fmt: skip


def test_update_keeps_the_active_version_when_the_configuration_does_not_pass(
    host: Host, container: Container
) -> None:
    """Also when the active zipapp has the same version: it is replaced only after the check."""
    existing_container(host)
    assert run_installer(host, "--update", "105").returncode == 0
    container.receive(host, "/root/meshcore-home-gateway-src.tar.gz")
    config = render_config(CURRENT.format(serial=host.serial)) + "[future]\nsetting = 1\n"
    container.path(CONFIG).write_text(config, encoding="utf-8")
    lib = container.path(LIB)
    lib.mkdir(parents=True)
    active = lib / f"meshcore-home-gateway-{__version__}.pyz"
    active.write_text("the active zipapp", encoding="utf-8")
    (lib / "current.pyz").symlink_to(active.name)
    run = container.run(host.script("build_pyz.py"))
    assert run.returncode != 0
    assert "future" in run.stderr  # check-config's message
    assert active.read_text(encoding="utf-8") == "the active zipapp"
    assert sorted(p.name for p in lib.iterdir()) == ["current.pyz", active.name]  # no .new
    assert not list(container.path("/root").iterdir())
    assert container.calls("systemctl") == []


def test_update_needs_no_whiptail(host: Host) -> None:
    """It shows no dialogs, and no Proxmox VE package depends on whiptail."""
    existing_container(host)
    result = run_installer(host, "--update", "105", env={"PATH": path_without(host, "whiptail")})
    assert result.returncode == 0, result.stderr


def test_update_needs_a_running_container(host: Host) -> None:
    existing_container(host, state="stopped")
    result = run_installer(host, "--update", "105")
    assert result.returncode == 1
    assert "not running" in result.stderr


def test_update_refuses_a_container_without_an_installation(host: Host) -> None:
    """A mistyped container ID must not get packages, a user and a service installed."""
    (host.root / "ct-106").write_text("running", encoding="utf-8")
    result = run_installer(host, "--update", "106")
    assert result.returncode == 1
    assert "no meshcore-home-gateway installation" in result.stderr
    assert not [c for c in host.inside() if "apt-get" in c]


def test_reconfigure_keeps_the_settings_it_does_not_ask_for(host: Host) -> None:
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    config = host.config()
    assert config.mqtt.host == "new.example.org"
    assert (config.mqtt.node_id, config.mqtt.password) == ("gw1", "old-secret")
    assert config.messages.queue_size == 50
    assert {t.alias: (t.channel_index, t.public_key) for t in config.targets} == {
        "my-alerts": (1, None),
        "alice": (None, ALICE),
    }
    assert (config.commands.senders, config.commands.words) == (
        frozenset({ALICE}), frozenset({"status"}),
    )  # fmt: skip
    inside = host.inside()
    assert (
        inside[0]
        == "meshcore-home-gateway config answers --config /etc/meshcore-home-gateway/config.toml"
    )
    assert inside.index("systemctl stop meshcore-home-gateway") < next(
        i for i, c in enumerate(inside) if "--list channels" in c
    )


def test_reconfigure_keeps_an_anonymous_broker_and_the_default_port(host: Host) -> None:
    """Settings missing from the file stay at the program's defaults, not the installer's."""
    existing_container(host)
    current = host.root / "current"
    text = current.read_text(encoding="utf-8")
    text = text.replace("mqtt.username=bridge\n", "").replace("mqtt.password=old-secret\n", "")
    current.write_text(text + "mqtt.tls=true\n", encoding="utf-8")
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    config = host.config()
    assert (config.mqtt.username, config.mqtt.password) == (None, None)
    assert (config.mqtt.tls, config.mqtt.port) == (True, 1883)


def test_reconfigure_without_a_user_name_drops_the_password(host: Host) -> None:
    existing_container(host)
    answers = answers_file(host, "mqtt.username=\n")
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    config = host.config()
    assert (config.mqtt.username, config.mqtt.password) == (None, None)


def test_reconfigure_keeps_every_alias_of_a_channel_or_contact(host: Host) -> None:
    existing_container(host)
    current = host.root / "current"
    current.write_text(
        current.read_text(encoding="utf-8")
        + f"target.alarm=channel:1\ntarget.alice-direct=contact:{ALICE}\n",
        encoding="utf-8",
    )
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    assert {t.alias: (t.channel_index, t.public_key) for t in host.config().targets} == {
        "my-alerts": (1, None),
        "alarm": (1, None),
        "alice": (None, ALICE),
        "alice-direct": (None, ALICE),
    }


@pytest.mark.parametrize("answer", ["radio.choice=tcp", "ct.memory_mb=512"])
def test_reconfigure_refuses_answers_it_cannot_apply(host: Host, answer: str) -> None:
    existing_container(host)
    answers = answers_file(host, f"{answer}\n")
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers))
    assert result.returncode == 1
    assert answer.partition("=")[0] in result.stderr
    assert "systemctl stop meshcore-home-gateway" not in host.inside()


def test_interactive_reconfigure_offers_the_current_settings(host: Host) -> None:
    existing_container(host)
    dialog = [
        "default",
        "old.example.org", "bridge", "",  # broker, user, password: empty keeps the current one
        "1|2", ALICE, ALICE, "status",  # channels, contacts, senders, words
    ]  # fmt: skip
    result = run_installer(host, "--reconfigure", "105", dialog=dialog)
    assert result.returncode == 0, result.stderr
    dialogs = host.dialogs()
    assert "old.example.org" in dialogs[1]  # the current value is the default
    channels = next(c for c in dialogs if "Channels to send to" in c)
    assert channels[-9:] == ["0", "Public", "off", "1", "Alerts", "on", "2", "public", "off"]
    config = host.config()
    assert config.mqtt.password == "old-secret"
    assert {t.alias for t in config.targets} == {"my-alerts", "public", "alice"}


def test_interactive_reconfigure_keeps_targets_the_radio_does_not_offer(host: Host) -> None:
    existing_container(host)
    current = host.root / "current"
    current.write_text(
        current.read_text(encoding="utf-8").replace(
            f"commands.senders={ALICE}", f"commands.senders={ALICE},{RELAY}"
        )
        + f"target.relay=contact:{RELAY}\ntarget.old=channel:7\n",
        encoding="utf-8",
    )
    dialog = ["default", "old.example.org", "bridge", "", "1", ALICE, ALICE, "status"]
    result = run_installer(host, "--reconfigure", "105", dialog=dialog)
    assert result.returncode == 0, result.stderr
    config = host.config()
    targets = {t.alias: (t.channel_index, t.public_key) for t in config.targets}
    assert targets["relay"] == (None, RELAY)  # a repeater: not offered, kept
    assert targets["old"] == (7, None)  # no longer named on the radio: not offered, kept
    assert config.commands.senders == frozenset({ALICE, RELAY})
    assert "kept" in result.stderr


def test_unattended_reconfigure_lists_a_kept_sender_once(host: Host) -> None:
    existing_container(host)
    current = host.root / "current"
    senders = f"commands.senders={ALICE},{RELAY}"
    current.write_text(
        current.read_text(encoding="utf-8").replace(f"commands.senders={ALICE}", senders),
        encoding="utf-8",
    )
    answers = answers_file(host, senders + "\n")
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers))
    assert result.returncode == 0, result.stderr
    lines = host.pushed("meshcore-home-gateway-answers").splitlines()
    assert [line for line in lines if line.startswith("commands.senders=")] == [senders]


def test_failed_reconfigure_starts_the_service_again(host: Host) -> None:
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    env = {"PROBE_FAILS": "1"}
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers), env=env)
    assert result.returncode == 1
    assert host.inside()[-1] == "systemctl start meshcore-home-gateway"


@pytest.mark.parametrize(
    ("sig", "during"),
    [
        (signal.SIGINT, "--list channels"),
        (signal.SIGTERM, "--list channels"),
        (signal.SIGHUP, "--list channels"),
        (signal.SIGINT, "systemctl stop"),
    ],
)
def test_interrupted_reconfigure_starts_the_service_again(
    host: Host, sig: signal.Signals, during: str
) -> None:
    """Ctrl-C, a kill or a closed SSH session while the service is stopped, or stopping."""
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = interrupt_installer(
        host, sig, during, "--reconfigure", "105", "--answers", str(answers)
    )
    assert result.returncode == 1
    assert "cancelled" in result.stderr
    assert host.inside()[-1] == "systemctl start meshcore-home-gateway"


def test_interrupted_install_names_the_container_to_remove(host: Host) -> None:
    answers = str(answers_file(host))
    result = interrupt_installer(host, signal.SIGINT, "--list channels", "--answers", answers)
    assert result.returncode == 1
    assert "pct destroy 105" in result.stderr


@pytest.mark.skipif(not Path("/dev/full").exists(), reason="needs /dev/full")
def test_failed_reconfigure_starts_the_service_again_when_messages_cannot_be_written(
    host: Host,
) -> None:
    """After a closed SSH session, writes to the terminal fail."""
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    with Path("/dev/full").open("w") as full:  # every write fails
        result = subprocess.run(  # noqa: S603 - our script with fixed arguments
            [BASH, str(SCRIPT), "--reconfigure", "105", "--answers", str(answers)],
            env=installer_env(host, {"PROBE_FAILS": "1"}), stdout=subprocess.DEVNULL,
            stderr=full, timeout=60, check=False,
        )  # fmt: skip
    assert result.returncode == 1
    assert host.inside()[-1] == "systemctl start meshcore-home-gateway"


@pytest.mark.parametrize("status", [0, 1])
def test_the_answers_are_private_and_removed_after_rendering(
    host: Host, container: Container, status: int
) -> None:
    """They hold the broker password, also when the configuration is not accepted."""
    result = run_installer(host, "--answers", str(answers_file(host)))
    assert result.returncode == 0, result.stderr
    script = host.script(f"--output {CONFIG}")
    record = f'umask >>"{container.root}/umask.log"; stat -c %a "$4" >>"{container.root}/mode.log"'
    container.stub("meshcore-home-gateway", f"{record}\nexit {status}")
    answers = host.pushed("meshcore-home-gateway-answers")
    run = container.run(f"umask 077\n{script}", stdin=answers)  # pct exec keeps the host's
    assert run.returncode == status
    (render,) = container.calls("meshcore-home-gateway")
    assert render == (
        f"config render --answers {container.path(ANSWERS)} --output {container.path(CONFIG)} "
        "--group meshcore-home-gateway"
    )
    assert container.calls("mode") == ["600"]  # the answers, while they exist
    assert container.calls("umask") == ["0022"]  # not 077 for the configuration's directory
    assert not container.path(ANSWERS).exists()


def test_the_service_user_can_reach_the_written_configuration(
    host: Host, container: Container
) -> None:
    """With the real program: the directory it creates is not only root's."""
    result = run_installer(host, "--answers", str(answers_file(host)))
    assert result.returncode == 0, result.stderr
    group = grp.getgrgid(os.getgid()).gr_name  # the container's service group stands in for it
    script = host.script(f"--output {CONFIG}")
    assert script.endswith("--group meshcore-home-gateway")
    script = script.removesuffix("meshcore-home-gateway") + group
    program = f'PYTHONPATH="{ROOT / "src"}" exec "{sys.executable}" -m meshcore_home_gateway "$@"'
    container.stub("meshcore-home-gateway", program)
    container.path(CONFIG).parent.rmdir()  # the first installation creates it
    run = container.run(f"umask 077\n{script}", stdin=host.pushed("meshcore-home-gateway-answers"))
    assert run.returncode == 0, run.stderr
    assert stat.S_IMODE(container.path(CONFIG).parent.stat().st_mode) == 0o755
    config = container.path(CONFIG).stat()
    assert (stat.S_IMODE(config.st_mode), config.st_gid) == (0o640, os.getgid())


def test_help_and_unknown_options(host: Host) -> None:
    assert "--update CTID" in run_installer(host, "--help").stdout
    result = run_installer(host, "--bogus")
    assert result.returncode == 1
    assert "unknown option" in result.stderr


def test_reconfigure_starts_the_service_again_when_its_output_pipe_is_gone(host: Host) -> None:
    """`... 2>&1 | tee log` and Ctrl-C: tee is gone too, and a write would raise SIGPIPE."""
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    read, write = os.pipe()
    os.close(read)  # nobody reads what the installer writes
    try:
        result = subprocess.run(  # noqa: S603 - our script with fixed arguments
            [BASH, str(SCRIPT), "--reconfigure", "105", "--answers", str(answers)],
            env=installer_env(host, {"PROBE_FAILS": "1"}), stdout=write, stderr=write,
            timeout=60, check=False,
        )  # fmt: skip
    finally:
        os.close(write)
    assert result.returncode == 1  # not killed by SIGPIPE (-13)
    assert host.inside()[-1] == "systemctl start meshcore-home-gateway"


@pytest.mark.parametrize("kind", ["contacts", "channels"])
def test_target_answers_count_also_when_the_radio_offers_none(host: Host, kind: str) -> None:
    """A new radio without chat contacts, or without named channels."""
    lines = f"radio.choice={host.serial}\nmqtt.host=broker\n"
    lines += f"targets.contacts={ALICE}\n" if kind == "contacts" else "targets.channels=3\n"
    env = {"PROBE_ONLY_REPEATER": "1"} if kind == "contacts" else {"PROBE_CHANNELS": "\\n"}
    result = run_installer(host, "--answers", str(answers_file(host, lines)), env=env)
    assert result.returncode == 0, result.stderr
    targets = {(t.channel_index, t.public_key) for t in host.config().targets}
    assert ((None, ALICE) if kind == "contacts" else (3, None)) in targets
    assert sum("warning:" in line for line in result.stderr.splitlines()) == 1


# --- run from a URL: nothing on the host, the program is downloaded in the container ------------

ARCHIVE = "https://github.com/dkuenzel/meshcore-home-gateway/archive/main.tar.gz"
RELEASES = "https://github.com/dkuenzel/meshcore-home-gateway/releases/download/"
MAIN_INSTALLER = (
    "https://raw.githubusercontent.com/dkuenzel/meshcore-home-gateway/main/"
    "deploy/proxmox/meshcore-home-gateway-lxc.sh"
)
STAGE = "/root/meshcore-home-gateway-src"


def run_from_url(
    host: Host, *args: str, release: str = "", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Like `bash -c "$(wget -qO- URL)" -- ARGS`: the script is no file, so there is no checkout.
    `release` is the version that `make release` writes into the published copy."""
    text = SCRIPT.read_text(encoding="utf-8")
    if release:
        text = text.replace('\nRELEASE=""', f'\nRELEASE="{release}"', 1)
    temporary = host.root / "tmp"
    temporary.mkdir(exist_ok=True)
    return subprocess.run(  # noqa: S603 - our script with fixed arguments
        [BASH, "-c", text, "--", *args],
        env=installer_env(host, {"TMPDIR": str(temporary), **(env or {})}),
        capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=60, check=False,
    )  # fmt: skip


def fetches(host: Host) -> list[list[str]]:
    """The downloads in the container: the arguments after `python3 -I -c FETCH_PY`."""
    return [a[4:] for a in host.inside_args() if a[:3] == ["python3", "-I", "-c"]]


def test_from_a_url_the_source_of_main_is_built_in_the_container(host: Host) -> None:
    """Before the first release; the host needs neither python3 nor tar, and keeps no file."""
    env = {"PATH": path_without(host, "python3", "tar")}
    result = run_from_url(host, "--answers", str(answers_file(host)), env=env)
    assert result.returncode == 0, result.stderr
    assert fetches(host) == [["source", ARCHIVE, STAGE]]
    assert not [c for c in host.inside() if f"cat >{STAGE}.tar.gz" in c]  # nothing from the host
    assert not host.pct("push")
    assert not list((host.root / "tmp").iterdir())
    inside = host.inside()
    install = next(i for i, c in enumerate(inside) if "build_pyz.py" in c)
    check = next(i for i, c in enumerate(inside) if "--output $dir/config.toml" in c)
    probe = next(i for i, c in enumerate(inside) if "--list channels" in c)
    assert install < check < probe  # the settings are checked with the installed program
    assert host.config().mqtt.host == "broker.example.org"
    assert f'wget -qO- {MAIN_INSTALLER})" -- --update 105' in result.stderr
    assert f"meshcore-home-gateway {__version__} runs in container 105" in result.stderr


def test_from_a_release_its_checked_files_are_downloaded_in_the_container(host: Host) -> None:
    result = run_from_url(host, "--answers", str(answers_file(host)), release="1.2.3",
                          env={"STUB_VERSION": "1.2.3"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert fetches(host) == [["release", f"{RELEASES}v1.2.3/", "1.2.3", STAGE]]
    assert "meshcore-home-gateway 1.2.3 in container 105" in result.stderr  # known in advance
    assert 'releases/latest/download/meshcore-home-gateway-lxc.sh)" -- --update 105' in (
        result.stderr
    )


def test_a_pre_release_names_its_own_installer(host: Host) -> None:
    """releases/latest skips pre-releases: it would not find one, or install an older release."""
    result = run_from_url(host, "--answers", str(answers_file(host)), release="0.2.0rc1",
                          env={"STUB_VERSION": "0.2.0rc1"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    installer = f"{RELEASES}v0.2.0rc1/meshcore-home-gateway-lxc.sh"
    assert f'wget -qO- {installer})" -- --reconfigure 105' in result.stderr
    assert "the installer of a newer release" in result.stderr
    assert "releases/latest" not in result.stderr


def test_another_ref_is_installed_again_with_the_installer_of_main(host: Host) -> None:
    """The installer at an older ref may not run from an address."""
    result = run_from_url(host, "--ref", "v0.1.0", "--answers", str(answers_file(host)))
    assert result.returncode == 0, result.stderr
    assert fetches(host) == [["source", ARCHIVE.replace("main", "v0.1.0"), STAGE]]
    assert f'wget -qO- {MAIN_INSTALLER})" -- --ref v0.1.0 --update 105' in result.stderr


@pytest.mark.parametrize("release", ["", "9.9.9"])
def test_reconfigure_names_the_version_that_runs_in_the_container(host: Host, release: str) -> None:
    """Not the version of the installer: nothing is installed."""
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = run_from_url(host, "--reconfigure", "105", "--answers", str(answers),
                          release=release, env={"STUB_VERSION": "1.2.3"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "meshcore-home-gateway 1.2.3 runs in container 105" in result.stderr
    assert not fetches(host)


def test_reconfigure_names_the_installer_by_its_own_release(host: Host) -> None:
    """Not by the version in the container: main's 0.1.0.dev0 was never published."""
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = run_from_url(host, "--reconfigure", "105", "--answers", str(answers),
                          release="0.1.0", env={"STUB_VERSION": "0.1.0.dev0"})  # fmt: skip
    assert result.returncode == 0, result.stderr
    assert "meshcore-home-gateway 0.1.0.dev0 runs in container 105" in result.stderr
    assert 'releases/latest/download/meshcore-home-gateway-lxc.sh)" -- --update 105' in (
        result.stderr
    )
    assert "v0.1.0.dev0" not in result.stderr


def test_reconfigure_with_refused_settings_leaves_the_service_running(host: Host) -> None:
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    result = run_from_url(host, "--reconfigure", "105", "--answers", str(answers),
                          env={"CHECK_FAILS": "1"})  # fmt: skip
    assert result.returncode == 1
    assert "invalid settings" in result.stderr
    assert not [c for c in host.inside() if "systemctl" in c or "--group" in c]


@pytest.mark.parametrize(("answers", "status"), [("mqtt.port=1884\n", 0), ("mqtt.port=abc\n", 1)])
def test_the_settings_check_in_the_container_renders_the_answers(
    host: Host, container: Container, answers: str, status: int
) -> None:
    """With the installed program, into temporary files that hold the password only meanwhile;
    the configuration stays as it is."""
    existing_container(host)
    reconfigure = answers_file(host, "mqtt.host=new.example.org\n")
    assert run_from_url(host, "--reconfigure", "105", "--answers", str(reconfigure)).returncode == 0
    script = host.script("--output $dir/config.toml")
    assert script.index("umask 077") < script.index("mktemp")
    program = f'PYTHONPATH="{ROOT / "src"}" exec "{sys.executable}" -m meshcore_home_gateway "$@"'
    container.stub("meshcore-home-gateway", program)
    temporary = container.path("/tmp")
    temporary.mkdir()
    current = CURRENT.format(serial=host.serial)
    run = container.run(f"export TMPDIR={temporary}\n{script}", stdin=current + answers)
    assert (run.returncode != 0) == bool(status), run.stderr
    assert ("mqtt.port" in run.stderr) == bool(status)
    (render,) = container.calls("meshcore-home-gateway")
    assert render.startswith(f"config render --answers {temporary}/")
    assert not list(temporary.iterdir())
    assert not container.path(CONFIG).exists()


def test_from_a_url_refused_settings_are_reported_before_the_radio_is_read(host: Host) -> None:
    result = run_from_url(host, "--answers", str(answers_file(host)), env={"CHECK_FAILS": "1"})
    assert result.returncode == 1
    assert "invalid settings" in result.stderr
    assert "pct destroy 105" in result.stderr
    assert not [c for c in host.inside() if "--list channels" in c]


def test_update_from_a_url_downloads_into_the_container(host: Host) -> None:
    existing_container(host)
    result = run_from_url(host, "--update", "105", release="1.2.3")
    assert result.returncode == 0, result.stderr
    assert fetches(host)[0][:3] == ["release", fetches(host)[0][1], "1.2.3"]
    assert host.inside()[-1] == "systemctl restart meshcore-home-gateway"


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (["--release", "2.0.0"], ["release", f"{RELEASES}v2.0.0/", "2.0.0", STAGE]),
        (["--ref", "v0.1.0"], ["source", ARCHIVE.replace("main", "v0.1.0"), STAGE]),
    ],
)  # fmt: skip
def test_a_release_or_a_ref_can_be_chosen_also_from_a_checkout(
    host: Host, args: list[str], expected: list[str]
) -> None:
    existing_container(host)
    result = run_installer(host, *args, "--update", "105")
    assert result.returncode == 0, result.stderr
    assert fetches(host) == [expected]


@pytest.mark.parametrize(
    "args",
    [["--release", "1.0;rm"], ["--release"], ["--ref", "-x"], ["--ref", "a b"],
     ["--release", "1.0.0", "--ref", "main"]],
)  # fmt: skip
def test_bad_release_and_ref_options_are_refused(host: Host, args: list[str]) -> None:
    result = run_installer(host, *args, "--update", "105")
    assert result.returncode == 1
    assert "--release" in result.stderr or "--ref" in result.stderr
    assert not host.pct("exec")


def test_the_published_copy_names_its_release() -> None:
    """`make release` writes the version into the one line it replaces."""
    lines = SCRIPT.read_text(encoding="utf-8").splitlines()
    assert lines.count('RELEASE=""     # the copy published with a release names its version here '
                       "(make release)") == 1  # fmt: skip
    makefile = (ROOT / "Makefile").read_text(encoding="utf-8")
    assert "meshcore-home-gateway-lxc.sh" in makefile
    assert 's/^RELEASE=\\"\\"/RELEASE=\\"$$version\\"/' in makefile


def test_help_needs_no_script_file(host: Host) -> None:
    result = run_from_url(host, "--help")
    assert result.returncode == 0
    assert "--release VERSION" in result.stdout
    assert "wget -qO- URL" in result.stdout


# --- the download inside the container ---------------------------------------------------------

FETCH = re.search(r"\nFETCH_PY='(.*?)'\n", SCRIPT.read_text(encoding="utf-8"), re.DOTALL)
UNIT = "deploy/systemd/meshcore-home-gateway.service"


@pytest.fixture
def server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A web server for the files below the directory it yields, and its base URL."""
    files = tmp_path / "www"
    files.mkdir()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(files))
    handler.log_message = lambda *_: None  # type: ignore[attr-defined]
    web = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=web.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{web.server_address[1]}/", files
    finally:
        web.shutdown()
        web.server_close()


def fetch(*args: str) -> subprocess.CompletedProcess[str]:
    assert FETCH is not None
    return subprocess.run(  # noqa: S603 - the installer's download step
        [sys.executable, "-I", "-c", FETCH.group(1), *args],
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip


def publish_release(files: Path, version: str, pyz: bytes = b"PK zipapp") -> None:
    release = files / f"v{version}"
    release.mkdir()
    (release / f"meshcore-home-gateway-{version}.pyz").write_bytes(pyz)
    (release / "meshcore-home-gateway.service").write_bytes((ROOT / UNIT).read_bytes())
    sums = "".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(release.iterdir())
    )
    (release / "SHA256SUMS").write_text(sums, encoding="utf-8")


def test_the_files_of_a_release_are_downloaded_and_checked(
    server: tuple[str, Path], tmp_path: Path
) -> None:
    base, files = server
    publish_release(files, "1.2.3")
    target = tmp_path / "stage"
    result = fetch("release", f"{base}v1.2.3/", "1.2.3", str(target))
    assert result.returncode == 0, result.stderr
    assert (target / "meshcore-home-gateway.pyz").read_bytes() == b"PK zipapp"
    assert (target / UNIT).read_bytes() == (ROOT / UNIT).read_bytes()


def test_a_release_file_that_does_not_match_its_checksum_is_not_installed(
    server: tuple[str, Path], tmp_path: Path
) -> None:
    base, files = server
    publish_release(files, "1.2.3")
    (files / "v1.2.3" / "meshcore-home-gateway-1.2.3.pyz").write_bytes(b"PK tampered")
    target = tmp_path / "stage"
    result = fetch("release", f"{base}v1.2.3/", "1.2.3", str(target))
    assert result.returncode == 1
    assert "does not match the SHA256SUMS" in result.stderr
    assert not (target / "meshcore-home-gateway.pyz").exists()


def test_a_missing_release_is_reported(server: tuple[str, Path], tmp_path: Path) -> None:
    base, _ = server
    result = fetch("release", f"{base}v9.9.9/", "9.9.9", str(tmp_path / "stage"))
    assert result.returncode == 1
    assert "cannot download" in result.stderr
    assert "404" in result.stderr
    assert "Traceback" not in result.stderr


def source_archive(files: Path, *extra: tarfile.TarInfo) -> None:
    """Like GitHub's archive of a ref: everything below one top directory."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in ("src/meshcore_home_gateway", "tools/build_pyz.py", UNIT, "README.md"):
            archive.add(ROOT / path, f"meshcore-home-gateway-main/{path}",
                        filter=lambda i: None if "__pycache__" in i.name else i)  # fmt: skip
        for member in extra:
            archive.addfile(member, io.BytesIO(b"x" * member.size))
    (files / "main.tar.gz").write_bytes(buffer.getvalue())


def test_the_source_of_a_ref_is_extracted_without_the_rest(
    server: tuple[str, Path], tmp_path: Path
) -> None:
    base, files = server
    escape = tarfile.TarInfo("meshcore-home-gateway-main/../escaped")
    escape.size = 1
    source_archive(files, escape)
    target = tmp_path / "stage"
    result = fetch("source", f"{base}main.tar.gz", str(target))
    assert result.returncode == 0, result.stderr
    assert (target / "src/meshcore_home_gateway/__init__.py").exists()
    assert (target / "tools/build_pyz.py").exists()
    assert (target / UNIT).exists()
    assert not (target / "README.md").exists()
    assert not (tmp_path / "escaped").exists()
    build = subprocess.run(  # noqa: S603 - the build the installation step runs
        [sys.executable, str(target / "tools/build_pyz.py"), "--source", str(target / "src"),
         "--output", str(tmp_path / "gw.pyz")], capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert build.returncode == 0, build.stderr


def test_a_source_archive_with_a_link_out_of_the_tree_is_refused(
    server: tuple[str, Path], tmp_path: Path
) -> None:
    base, files = server
    link = tarfile.TarInfo("meshcore-home-gateway-main/src/meshcore_home_gateway/passwd")
    link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
    source_archive(files, link)
    result = fetch("source", f"{base}main.tar.gz", str(tmp_path / "stage"))
    assert result.returncode == 1
    assert "not a usable source archive" in result.stderr
    assert "Traceback" not in result.stderr


def test_an_archive_without_the_source_is_refused(server: tuple[str, Path], tmp_path: Path) -> None:
    base, files = server
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        archive.add(ROOT / "README.md", "other-main/README.md")
    (files / "main.tar.gz").write_bytes(buffer.getvalue())
    result = fetch("source", f"{base}main.tar.gz", str(tmp_path / "stage"))
    assert result.returncode == 1
    assert "holds no meshcore-home-gateway source" in result.stderr


def test_a_release_file_is_installed_as_it_was_published(
    host: Host, container: Container, tmp_path: Path
) -> None:
    """No build: the zipapp of the release, checked against its SHA256SUMS, is the one used.

    Also when an interrupted run from a checkout left its archive behind."""
    existing_container(host)
    assert run_from_url(host, "--update", "105", release=__version__).returncode == 0
    stage = container.path(STAGE)
    container.path(f"{STAGE}.tar.gz").write_bytes(b"left behind")
    (stage / "deploy/systemd").mkdir(parents=True)
    shutil.copy(ROOT / UNIT, stage / UNIT)
    build = subprocess.run(  # noqa: S603 - a zipapp like the release one
        [sys.executable, str(ROOT / "tools/build_pyz.py"), "--source", str(ROOT / "src"),
         "--output", str(stage / "meshcore-home-gateway.pyz")], capture_output=True, check=False,
    )  # fmt: skip
    assert build.returncode == 0
    published = (stage / "meshcore-home-gateway.pyz").read_bytes()
    config = render_config(CURRENT.format(serial=host.serial))
    container.path(CONFIG).write_text(config, encoding="utf-8")
    run = container.run(host.script("build_pyz.py"))
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == __version__  # reported to the installer
    installed = container.path(LIB) / f"meshcore-home-gateway-{__version__}.pyz"
    assert installed.read_bytes() == published
    assert not stage.exists()
    assert not container.path(f"{STAGE}.tar.gz").exists()


def test_the_source_of_a_ref_is_built_in_the_container(host: Host, container: Container) -> None:
    """Also when an interrupted run from a checkout left its archive behind."""
    existing_container(host)
    assert run_from_url(host, "--update", "105").returncode == 0
    stage = container.path(STAGE)
    for path in ("src/meshcore_home_gateway", "tools/build_pyz.py", UNIT):  # as FETCH_PY leaves it
        copy = shutil.copytree if (ROOT / path).is_dir() else shutil.copy
        (stage / path).parent.mkdir(parents=True, exist_ok=True)
        copy(ROOT / path, stage / path)
    container.path(f"{STAGE}.tar.gz").write_bytes(b"left behind")
    config = render_config(CURRENT.format(serial=host.serial))
    container.path(CONFIG).write_text(config, encoding="utf-8")
    run = container.run(host.script("build_pyz.py"))
    assert run.returncode == 0, run.stderr
    assert run.stdout.strip() == __version__
    assert zipfile.is_zipfile(container.path(LIB) / f"meshcore-home-gateway-{__version__}.pyz")
    assert not stage.exists()
    assert not container.path(f"{STAGE}.tar.gz").exists()


def test_dialogs_refuse_a_script_piped_into_bash(host: Host) -> None:
    """`wget -qO- URL | bash`: the dialogs would read the rest of the script as answers."""
    result = subprocess.run(  # noqa: S603 - our script, fed like `wget -qO- URL | bash`
        [BASH, "-s"], input=SCRIPT.read_text(encoding="utf-8"), env=installer_env(host),
        capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert result.returncode == 1
    assert 'bash -c "$(wget -qO- URL)"' in result.stderr
    assert not host.calls("whiptail")
