"""The Proxmox installer, run with stub Proxmox commands (pct, pveam, pvesm, pvesh, whiptail).

The stubs record every call and answer like a Proxmox VE 9 host would (output formats from the
Proxmox sources, see docs/deployment.md). The tests check the container that would be created,
the commands run inside it, and that the answers pushed into the container render to a valid
configuration. The `pct exec` stub refuses commands that do not start from a clean environment.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

from meshcore_bus.config import Config, parse_config, render_config

ROOT = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash") or "/bin/bash"
SCRIPT = ROOT / "deploy" / "proxmox" / "meshcore-bus-lxc.sh"
ALICE = "a1" * 32
RELAY = "b2" * 32
TEMPLATE = "debian-13-standard_13.6-1_amd64.tar.zst"
SEP = "\x1f"  # between arguments in the call log
NL = "\x1e"  # stands for a newline inside an argument

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
            case "$*" in
              *"--list channels"*)
                [[ -z ${PROBE_FAILS-} ]] || exit 1
                printf '%b' "${PROBE_CHANNELS:-0\\tPublic\\n1\\tAlerts\\n2\\tpublic\\n}" ;;
              *"--list contacts"*)
                printf '%s\tAlice Example\tchat\n%s\tRelay\trepeater\n' "$ALICE" "$RELAY" ;;
              "getent group dialout") printf 'dialout:x:%s:\n' "${CT_DIALOUT_GID:-20}" ;;
              *"config answers"*) cat "$STUB/current" ;;
            esac ;;
        esac
    """,
    "pvesh": r"""
        log pvesh "$@"
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

    def inside(self) -> list[str]:
        """Commands run in the container, without the `env -i NAME=value ...` prefix."""
        commands = []
        for call in self.pct("exec"):
            args = call[6:]  # pct exec ID -- env -i
            while args and "=" in args[0]:
                args = args[1:]
            commands.append(" ".join(args))
        return commands

    def dialogs(self) -> list[list[str]]:
        return [c for c in self.calls("whiptail") if "--msgbox" not in c]

    def pushed(self, name: str) -> str:
        return (self.root / "pushed" / name).read_text(encoding="utf-8")

    def config(self) -> Config:
        return parse_config(render_config(self.pushed("meshcore-bus-answers")))

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


def run_installer(
    host: Host, *args: str, dialog: list[str] | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    if dialog is not None:
        (host.root / "dialog").write_text("\n".join(dialog) + "\n", encoding="utf-8")
    environment = {
        "PATH": f"{host.root / 'bin'}:{os.environ['PATH']}",
        "STUB": str(host.root),
        "ALICE": ALICE,
        "RELAY": RELAY,
        "MESHCORE_BUS_SERIAL_DIR": str(host.serial.parent),
        **(env or {}),
    }
    return subprocess.run(  # noqa: S603 - our script with fixed arguments
        [BASH, str(SCRIPT), *args], env=environment, capture_output=True, text=True, timeout=60,
        check=False,
    )  # fmt: skip


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
    assert order.index("create") < order.index("set") < order.index("start") < order.index("push")
    assert all(c[3:6] == ["--", "env", "-i"] for c in host.pct("exec"))

    inside = host.inside()
    assert any("apt-get install" in c and "python3-paho-mqtt" in c for c in inside)
    assert any("build_pyz.py" in c and "/usr/bin/python3 -I" in c for c in inside)
    assert any("config render" in c and "--group meshcore-bus" in c for c in inside)
    assert inside[-2:] == [
        "systemctl enable --now meshcore-bus",
        "systemctl is-active --quiet meshcore-bus",
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
        "local-lvm:2", "256", "meshcore-bus",
    )  # fmt: skip
    config = host.config()
    assert config.mqtt.node_id == "radio1"
    assert {t.alias for t in config.targets} == {"public", "alerts", "public-2", "alice-example"}
    assert config.commands.senders == frozenset()
    assert "pw-secret" not in result.stderr + host.log_text()


def test_interactive_advanced_setup_asks_for_everything(host: Host) -> None:
    dialog = [
        "advanced",
        "106", "meshcore-bus", "local-lvm", "4", "512", "vmbr1",  # id, name, disk, memory, bridge
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


def test_unattended_install_needs_the_radio_named(host: Host) -> None:
    """Without it the first serial device would be taken, possibly a Zigbee stick."""
    result = run_installer(host, "--answers", str(answers_file(host, "mqtt.host=broker\n")))
    assert result.returncode == 1
    assert "radio.choice" in result.stderr
    assert not host.pct("create")


# --- update and reconfigure --------------------------------------------------------------------


def test_update_checks_the_configuration_before_switching_versions(host: Host) -> None:
    existing_container(host)
    result = run_installer(host, "--update", "105")
    assert result.returncode == 0, result.stderr
    (build,) = [c for c in host.inside() if "build_pyz.py" in c]
    assert build.index("check-config") < build.index("ln -sfn")
    assert host.inside()[-1] == "systemctl restart meshcore-bus"
    assert not host.pct("create")


def test_update_needs_a_running_container(host: Host) -> None:
    existing_container(host, state="stopped")
    result = run_installer(host, "--update", "105")
    assert result.returncode == 1
    assert "not running" in result.stderr


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
    assert inside[0] == "meshcore-bus config answers --config /etc/meshcore-bus/config.toml"
    assert inside.index("systemctl stop meshcore-bus") < next(
        i for i, c in enumerate(inside) if "--list channels" in c
    )


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


def test_update_with_the_same_version_checks_before_replacing_it(host: Host) -> None:
    existing_container(host)
    result = run_installer(host, "--update", "105")
    assert result.returncode == 0, result.stderr
    (build,) = [c for c in host.inside() if "build_pyz.py" in c]
    assert ".new" in build[: build.index("check-config")]  # built beside the active zipapp
    assert build.index("check-config") < build.index("mv -f") < build.index("ln -sfn")


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
    lines = host.pushed("meshcore-bus-answers").splitlines()
    assert [line for line in lines if line.startswith("commands.senders=")] == [senders]


def test_failed_reconfigure_starts_the_service_again(host: Host) -> None:
    existing_container(host)
    answers = answers_file(host, "mqtt.host=new.example.org\n")
    env = {"PROBE_FAILS": "1"}
    result = run_installer(host, "--reconfigure", "105", "--answers", str(answers), env=env)
    assert result.returncode == 1
    assert host.inside()[-1] == "systemctl start meshcore-bus"


def test_help_and_unknown_options(host: Host) -> None:
    assert "--update CTID" in run_installer(host, "--help").stdout
    result = run_installer(host, "--bogus")
    assert result.returncode == 1
    assert "unknown option" in result.stderr
