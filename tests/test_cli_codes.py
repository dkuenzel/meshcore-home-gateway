"""`commands enrol`: a code secret for a command sender (IMP-002)."""

from __future__ import annotations

import base64
import os
import stat
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from meshcore_home_gateway.cli import EX_FAILURE, EX_OK, EX_USAGE, main
from meshcore_home_gateway.config import load_config
from meshcore_home_gateway.core.commands import CODE_STEP_S, code_at, evaluate_command
from meshcore_home_gateway.core.contacts import Contact, ContactBook
from meshcore_home_gateway.core.model import InboundMessage, MessageKind, TextType

ALICE, CAROL = "a1" * 32, "c3" * 32


def config_file(tmp_path: Path, codes: bool = True) -> Path:
    path = tmp_path / "config.toml"
    codes_file = f'codes_file = "{tmp_path / "command-codes"}"\n' if codes else ""
    path.write_text(
        '[radio]\ntransport = "serial"\ndevice = "/dev/ttyACM0"\n'
        '[mqtt]\nhost = "broker.example.org"\nnode_id = "radio1"\ninsecure = true\n'
        f'[commands]\nsenders = ["{ALICE}", "{CAROL}"]\nwords = ["arm"]\n{codes_file}',
        encoding="utf-8",
    )
    return path


def enrol(config: Path, key: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    status = main(["commands", "enrol", key, "--config", str(config)])
    printed = capsys.readouterr()
    return status, printed.out, printed.err


def test_enrol_writes_a_secret_and_prints_it_for_an_authenticator_app(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    status, out, err = enrol(config, ALICE.upper(), capsys)
    assert status == EX_OK
    uri = urlsplit(out.strip())
    query = {name: values[0] for name, values in parse_qs(uri.query).items()}
    assert (uri.scheme, uri.netloc, uri.path) == (
        "otpauth",
        "totp",
        f"/meshcore-home-gateway%20radio1:{ALICE[:12]}",
    )
    assert {name: query[name] for name in ("issuer", "algorithm", "digits", "period")} == {
        "issuer": "meshcore-home-gateway radio1",
        "algorithm": "SHA1",
        "digits": "6",
        "period": "30",
    }
    secret = base64.b32decode(query["secret"])
    assert len(secret) == 20
    assert "restart the service" in err
    assert query["secret"] in err  # to type in
    codes = tmp_path / "command-codes"
    assert stat.S_IMODE(os.stat(codes).st_mode) == 0o640
    assert os.stat(codes).st_gid == os.stat(config).st_gid
    assert codes.read_text(encoding="utf-8").startswith("# The code secrets")
    assert load_config(config).commands.secrets == ((ALICE, secret),)


def test_enrol_again_replaces_the_secret_and_keeps_the_others_and_the_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    enrol(config, CAROL, capsys)
    first = dict(load_config(config).commands.secrets)
    codes = tmp_path / "command-codes"
    codes.chmod(0o600)  # handed to the service as a systemd credential
    assert enrol(config, ALICE, capsys)[0] == EX_OK
    second = dict(load_config(config).commands.secrets)
    assert second[ALICE] != first[ALICE]
    assert second[CAROL] == first[CAROL]
    assert stat.S_IMODE(os.stat(codes).st_mode) == 0o600
    assert [p.name for p in tmp_path.iterdir() if p.name.startswith(".")] == []


@pytest.mark.parametrize(
    ("codes", "key", "refusal"),
    [
        (False, ALICE, (EX_FAILURE, "set [commands] codes_file first")),
        (True, "b2" * 32, (EX_FAILURE, "is not in [commands] senders")),
        (True, "a1b2", (EX_USAGE, "a1b2")),
    ],
    ids=["no-codes-file", "not-a-sender", "not-a-key"],
)
def test_enrol_refuses_what_it_cannot_enrol(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    codes: bool,
    key: str,
    refusal: tuple[int, str],
) -> None:
    result, out, err = enrol(config_file(tmp_path, codes), key, capsys)
    assert (result, out) == (refusal[0], "")
    assert refusal[1] in err
    assert not (tmp_path / "command-codes").exists()


def test_enrol_leaves_a_file_it_cannot_read_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    codes = tmp_path / "command-codes"
    codes.write_text("not toml\n", encoding="utf-8")
    result, out, err = enrol(config, ALICE, capsys)
    assert (result, out) == (EX_FAILURE, "")
    assert "cannot read" in err
    assert codes.read_text(encoding="utf-8") == "not toml\n"


def test_check_config_counts_the_senders_with_a_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    assert main(["check-config", "--config", str(config)]) == EX_OK
    assert "  codes      1 sender ends commands with a code" in capsys.readouterr().out.splitlines()


def test_enrol_that_cannot_replace_the_file_leaves_it_and_no_other(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    before = (tmp_path / "command-codes").read_text(encoding="utf-8")

    def refused(*_: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refused)
    result, out, err = enrol(config, ALICE, capsys)
    assert (result, out) == (EX_FAILURE, "")
    assert "Permission denied" in err
    assert (tmp_path / "command-codes").read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["command-codes", "config.toml"]


def without_sender(config: Path, key: str) -> None:
    """The configuration as after removing `key` from `[commands] senders` by hand."""
    text = config.read_text(encoding="utf-8")
    config.write_text(text.replace(f'"{key}", ', ""), encoding="utf-8")


def test_a_removed_sender_s_secret_is_ignored_and_named(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#80: removing a lost phone's sender ends its commands and nothing else; before, its
    secret made the configuration an error (exit 78), which systemd does not restart."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    enrol(config, CAROL, capsys)
    without_sender(config, ALICE)
    assert main(["check-config", "--config", str(config)]) == EX_OK
    printed = capsys.readouterr()
    assert "  codes      1 sender ends commands with a code" in printed.out.splitlines()
    assert f"{ALICE} has a code secret but is not in commands.senders" in printed.err
    assert f"'meshcore-home-gateway commands revoke {ALICE}' removes it" in printed.err
    assert [key for key, _ in load_config(config).commands.secrets] == [CAROL]


def test_a_sender_the_installer_drops_leaves_a_configuration_that_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#80: `--reconfigure` takes every setting as an answer, `codes_file` too, while unticking
    a sender or refusing the insecure broker drops the senders and words; `config render` does
    not read the codes file. The configuration it writes must still load."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    assert main(["config", "answers", "--config", str(config)]) == EX_OK
    answers = capsys.readouterr().out.splitlines()
    kept = [
        line for line in answers if not line.startswith(("commands.senders=", "commands.words="))
    ]
    assert any(line.startswith("commands.codes_file=") for line in kept)
    (tmp_path / "answers").write_text("\n".join(kept) + "\n", encoding="utf-8")
    rendered = tmp_path / "rendered.toml"
    render = ["config", "render", "--answers", str(tmp_path / "answers"), "--output", str(rendered)]
    assert main(render) == EX_OK
    assert main(["check-config", "--config", str(rendered)]) == EX_OK
    assert f"{ALICE} has a code secret" in capsys.readouterr().err


def revoke(config: Path, key: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    status = main(["commands", "revoke", key, "--config", str(config)])
    printed = capsys.readouterr()
    return status, printed.out, printed.err


def test_revoke_removes_a_secret_and_keeps_the_others_and_the_mode(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#80: the documented way to take a secret back, once its key is no sender any more."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    enrol(config, CAROL, capsys)
    carol = dict(load_config(config).commands.secrets)[CAROL]
    codes = tmp_path / "command-codes"
    codes.chmod(0o600)
    without_sender(config, ALICE)
    status, out, err = revoke(config, ALICE.upper(), capsys)
    assert (status, out) == (EX_OK, "")
    assert f"the code secret of {ALICE} is removed from {codes}" in err
    assert "without a code" not in err  # Carol, the sender left, has her secret
    assert ALICE not in codes.read_text(encoding="utf-8")
    commands = load_config(config).commands
    assert (commands.secrets, commands.ignored_secrets) == (((CAROL, carol),), ())
    assert stat.S_IMODE(os.stat(codes).st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["command-codes", "config.toml"]


def test_revoke_of_the_last_secret_leaves_a_file_that_loads(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing codes file is a configuration error: the file stays, with no secret."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    without_sender(config, ALICE)
    assert revoke(config, ALICE, capsys)[0] == EX_OK
    assert (tmp_path / "command-codes").read_text(encoding="utf-8").startswith("# The code")
    assert load_config(config).commands.secrets == ()


@pytest.mark.parametrize(
    ("setup", "key", "refusal"),
    [
        ("no-codes-file", ALICE, (EX_FAILURE, "[commands] codes_file is not set")),
        ("still-a-sender", ALICE, (EX_FAILURE, "is in [commands] senders")),
        ("former-sender", "b2" * 32, (EX_FAILURE, "has no code secret in")),
        ("former-sender", "a1b2", (EX_USAGE, "a1b2")),
    ],
    ids=["no-codes-file", "still-a-sender", "no-secret", "not-a-key"],
)
def test_revoke_refuses_what_it_cannot_revoke(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    key: str,
    refusal: tuple[int, str],
) -> None:
    """A sender without its secret would command without a code: it leaves the senders first,
    or `enrol` gives it a new secret."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    before = (tmp_path / "command-codes").read_text(encoding="utf-8")
    if setup != "still-a-sender":
        without_sender(config, ALICE)
    if setup == "no-codes-file":
        text = config.read_text(encoding="utf-8")
        config.write_text(text[: text.index("codes_file")], encoding="utf-8")
    result, out, err = revoke(config, key, capsys)
    assert (result, out) == (refusal[0], "")
    assert refusal[1] in err
    assert (tmp_path / "command-codes").read_text(encoding="utf-8") == before


def test_revoke_leaves_a_file_it_cannot_read_as_it_is(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config = config_file(tmp_path)
    without_sender(config, ALICE)
    codes = tmp_path / "command-codes"
    codes.write_text("not toml\n", encoding="utf-8")
    result, out, err = revoke(config, ALICE, capsys)
    assert (result, out) == (EX_FAILURE, "")
    assert "cannot read" in err
    assert codes.read_text(encoding="utf-8") == "not toml\n"


FIXED = "a2" * 32  # Alice's key as corrected by hand
NOW = 1_800_000_000.0


def verdict(config: Path, key: str, text: str) -> str:
    """The verdict on a command of `key` by the configuration as the service loads it at its
    start: `accepted` or the reason."""
    message = InboundMessage(
        kind=MessageKind.CONTACT,
        text=text,
        sender_timestamp=int(NOW) - 5,
        received_at=NOW,
        text_type=TextType.PLAIN,
        sender_prefix=key[:12],
    )
    book = ContactBook([Contact(k, "x") for k in (ALICE, CAROL, FIXED)])
    decision = evaluate_command(message, load_config(config).commands, book, {}, NOW)
    assert decision is not None
    return "accepted" if decision.command is not None else str(decision.rejection)


def test_a_corrected_key_commands_only_after_enrol_or_revoke(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#80: correcting a sender's key by hand leaves its secret under the old key. 2.0 then did
    not start (exit 78); now the commands of each sender without a secret are refused as
    code_missing, and check-config says so, until `enrol` gives the sender a secret or `revoke`
    removes the old one. The service reads the files at its start: each verdict loads them
    anew, as a restart does."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    config.write_text(config.read_text(encoding="utf-8").replace(ALICE, FIXED), encoding="utf-8")
    assert [verdict(config, key, "arm now") for key in (FIXED, CAROL)] == ["code_missing"] * 2
    assert main(["check-config", "--config", str(config)]) == EX_OK
    err = capsys.readouterr().err
    for sender in (FIXED, CAROL):
        refused = f"{sender} has no code secret while one is ignored: its commands are refused"
        assert refused in err
    enrol(config, FIXED, capsys)
    code = code_at(dict(load_config(config).commands.secrets)[FIXED], int(NOW // CODE_STEP_S))
    assert verdict(config, FIXED, f"arm now {code}") == "accepted"
    assert verdict(config, FIXED, "arm now") == "code_missing"
    assert verdict(config, CAROL, "arm now") == "code_missing"
    status, _, err = revoke(config, ALICE, capsys)
    assert status == EX_OK
    assert "The service reads the file at its start: restart it to apply this" in err
    assert f"after the restart, {CAROL} commands without a code" in err
    assert f"after the restart, {FIXED}" not in err
    assert verdict(config, CAROL, "arm now") == "accepted"
    assert verdict(config, FIXED, f"arm now {code}") == "accepted"


def test_revoke_names_the_senders_that_then_command_without_a_code(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """#80: `revoke` of the last ignored secret ends the refusal of the senders without one:
    after the restart they command without a code, also a key corrected by hand that was not
    enrolled. Revoke says so by name, and how to give one a secret; while another secret is
    still ignored they stay refused, and it says nothing of it."""
    config = config_file(tmp_path)
    enrol(config, ALICE, capsys)
    enrol(config, CAROL, capsys)
    text = config.read_text(encoding="utf-8").replace(ALICE, FIXED)
    config.write_text(text.replace(f', "{CAROL}"', ""), encoding="utf-8")  # Carol's phone lost
    status, _, err = revoke(config, CAROL, capsys)
    assert status == EX_OK
    assert "without a code" not in err
    assert verdict(config, FIXED, "arm now") == "code_missing"
    status, _, err = revoke(config, ALICE, capsys)
    assert status == EX_OK
    assert f"after the restart, {FIXED} commands without a code" in err
    assert f"'meshcore-home-gateway commands enrol {FIXED}' gives it one" in err
    assert verdict(config, FIXED, "arm now") == "accepted"
