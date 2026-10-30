"""The public interface (docs/development.md#compatibility): the committed snapshot is what the
code offers, and the comparator tells growth from breakage. The comparator's tests use small
synthetic snapshots, so that a legitimate removal from the real one never breaks them."""

from __future__ import annotations

import argparse
import asyncio
import copy
import dataclasses
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest

from meshcore_home_gateway import bridge, cli, views
from meshcore_home_gateway import config as configuration
from meshcore_home_gateway.core.mesh import Heard, Node, Signal, type_name
from meshcore_home_gateway.core.profile import ProfileReport
from meshcore_home_gateway.mqtt.contact_devices import contact_device_id
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.protocol.codes import NodeType, TelemetryMode
from meshcore_home_gateway.radio.session import AUTO_ADD_TYPES
from tests import public_interface as interface

if TYPE_CHECKING:
    from meshcore_home_gateway.radio.transport import Transport

tool = interface.tool


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return interface.surface()


# --- the snapshot is the code ---------------------------------------------------------------------


def test_the_snapshot_is_the_code(current: dict[str, Any]) -> None:
    committed = interface.SNAPSHOT.read_text(encoding="utf-8")
    if tool.render(current) != committed:  # as `python -m tests.public_interface` says it
        pytest.fail(interface._report(committed, current, written=False), pytrace=False)


def test_the_extractor_gives_the_same_snapshot_again(current: dict[str, Any]) -> None:
    """Nothing it patches stays patched, nothing depends on the order of a set."""
    assert tool.render(interface.surface()) == tool.render(current)


def _covered(topic: str, subscriptions: list[str]) -> bool:
    def matches(pattern: str) -> bool:
        levels, wanted = topic.split("/"), pattern.split("/")
        if wanted[-1] == "#":
            return levels[: len(wanted) - 1] == wanted[:-1]
        return len(levels) == len(wanted) and all(
            w in ("+", t) for w, t in zip(wanted, levels, strict=True)
        )

    return any(matches(pattern) for pattern in subscriptions)


def test_every_topic_of_the_layout_is_in_the_snapshot(current: dict[str, Any]) -> None:
    """A topic that needs a setting the fixture lacks would be missing silently otherwise."""
    topics = Topics(interface.BASE, interface.NODE)
    parsers = {"key_from_contact_topic", "alias_from_send_topic"}
    built = [value for name, value in vars(topics).items() if not name.startswith("_")]
    built += [
        getattr(topics, name)(interface.KEY)
        for name in dir(topics)
        if not name.startswith("_") and name not in parsers and callable(getattr(topics, name))
    ]
    mqtt = current["mqtt"]
    missing = [
        topic
        for topic in map(interface.placeholders, built)
        if topic != "<base_topic>/<node_id>"  # the root, not a topic
        and topic not in mqtt["publish"]
        and not _covered(topic, mqtt["subscribe"])
    ]
    assert not missing


def test_placeholders_never_meet_real_names() -> None:
    """The slug "s" once turned "sensor" into "<slug>ensor"."""
    assert interface.placeholders(f"sensor.{interface.SLUG}_snr") == "sensor.<slug>_snr"
    assert interface.placeholders(f"{interface.BASE}/{interface.NODE}/state") == (
        "<base_topic>/<node_id>/state"
    )


def _python(code: str) -> subprocess.CompletedProcess[str]:
    """`code` in a fresh interpreter: no cache or patch of this run reaches it."""
    return subprocess.run(  # noqa: S603 - the interpreter with a fixed program
        [sys.executable, "-c", code], cwd=interface.ROOT, capture_output=True, text=True,
        check=False, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )  # fmt: skip


def test_the_configuration_is_recorded_in_a_fresh_state() -> None:
    """`placeholders` runs inside the recorder on the parser: it must not parse a configuration
    itself, or a test run alone (-k, --lf) would recurse. In this run the snapshot was computed
    already, so only a fresh interpreter shows it."""
    code = "from tests import public_interface as pi; pi._reads(pi.MINIMAL); pi.config()"
    run = _python(code)
    assert run.returncode == 0, run.stderr[-2000:]


def test_the_extractor_imports_without_the_private_names_it_reads() -> None:
    """A renamed helper of the code fails the tests of the interface, not their collection."""
    run = _python(
        "from meshcore_home_gateway import cli; del cli._count; import tests.public_interface"
    )
    assert run.returncode == 0, run.stderr


LEFT = "add to placeholders(), or the code transforms a configured name"


def test_a_fixture_value_left_in_the_snapshot_is_refused() -> None:
    with pytest.raises(AssertionError, match=re.escape(f"['qmissedq']: {LEFT}")):
        interface.refuse_leftovers({"mqtt": {"documents": {"qmissedq": []}}})
    for transformed, left in (
        (interface.NODE.replace("-", "_"), "qno_de_q"),
        (interface.NODE.rstrip("0123456789"), "qno-de_q"),
        (interface.PREFIX.lower(), "qpre-fix_q"),
        (interface.WORD, "qWo-rd_q"),  # a command word no longer taken in lower case
    ):
        with pytest.raises(AssertionError, match=re.escape(f"['{left}']: {LEFT}")):
            interface.refuse_leftovers({"homeassistant": {"devices": {"x": {"id": transformed}}}})
    with pytest.raises(AssertionError, match=interface.KEY[:8]):
        interface.refuse_leftovers(
            {"homeassistant": {"devices": {"contact": {"id": interface.KEY}}}}
        )


def test_a_configured_name_the_code_transforms_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """A node ID such as radio-1 would get other topics: the fixture's node ID has a '-', so the
    snapshot keeps what the code made of it, and that is refused before anything else fails."""
    built = Topics.__init__

    def underscored(topics: Topics, base_topic: str, node_id: str) -> None:
        built(topics, base_topic, node_id.replace("-", "_"))

    monkeypatch.setattr(Topics, "__init__", underscored)
    with pytest.raises(AssertionError, match=re.escape(f"['qno_de_q']: {LEFT}")):
        interface.surface()


def test_an_error_logged_while_computing_is_refused() -> None:
    """Bridge._observe logs an exception instead of raising it: a broken path would only show
    as a missing topic."""
    with pytest.raises(AssertionError, match="observer failed"), interface.refusing_errors():
        logging.getLogger("meshcore_home_gateway.bridge").error("observer failed")


def test_an_error_logged_before_a_later_failure_is_shown_with_it() -> None:
    """A producer's bug is logged and swallowed; the guard that then fails names its effect."""

    def computing() -> None:
        with interface.refusing_errors():
            logging.getLogger("meshcore_home_gateway.observer").error("contact listener failed")
            raise AssertionError("contact:event_type: Home Assistant declares values no path emits")

    with pytest.raises(AssertionError, match="contact listener failed") as refused:
        computing()
    assert "no path emits" in str(refused.value.__context__)


def test_an_event_loop_that_fails_fails_the_snapshot_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The publish path of the packet log runs outside Bridge._observe: an exception there ends
    the bridge's event loop, and waiting for its queue would hang the test run."""

    def broken(*_: Any) -> dict[str, Any]:
        raise RuntimeError("rx_payload broke")

    monkeypatch.setattr(bridge, "rx_payload", broken)
    with pytest.raises(RuntimeError, match="rx_payload broke"):
        interface.surface()


def test_an_event_loop_that_never_gets_there_fails_after_a_bound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stuck(_: bridge.Bridge) -> None:
        await asyncio.Event().wait()

    monkeypatch.setattr(bridge.Bridge, "_event_loop", stuck)
    monkeypatch.setattr(interface, "EVENTS_S", 0.01)
    with pytest.raises(AssertionError, match=re.escape("did not get there in 0.01 s")):
        interface.surface()


def test_a_value_list_is_what_home_assistant_is_told_of() -> None:
    """Home Assistant refuses an event type or option it was not told of."""
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "x: the producer emits values Home Assistant does not know: emitted ['a', 'b'], "
            "declared ['a']"
        ),
    ):
        interface._declared("x", {"a", "b"}, ["a"])
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "x: Home Assistant declares values no path emits: emitted ['a'], declared ['a', 'b']:"
            " extend the scenario in tests/public_interface.py (_published, _statuses)"
        ),
    ):
        interface._declared("x", {"a"}, ["a", "b"])
    assert sorted(interface._declared("x", {"a", "b"}, ["b", "a"])) == ["a", "b"]


def test_a_value_that_no_path_emits_any_more_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """The settings' status is taken from views.settings_document, not from discovery's list."""
    real = views.settings_document

    def never_drift(report: ProfileReport, at: float) -> dict[str, Any]:
        document = real(report, at)
        return document | {"status": "written"} if document["status"] == "drift" else document

    monkeypatch.setattr(views, "settings_document", never_drift)
    with pytest.raises(
        AssertionError, match="settings:status: Home Assistant declares values no path emits"
    ):
        interface.surface()


def _nothing(_: str) -> dict[str, str | None]:
    return {}


def test_the_choices_are_what_the_parser_takes() -> None:
    manage = interface._choices("radio.profile.manage", ["auto", "never", "always"], _nothing)
    assert manage == ["auto", "always"]
    with pytest.raises(AssertionError, match=re.escape("mqtt.host: the parser checks no choices")):
        interface._choices("mqtt.host", ["broker.example.org"], _nothing)


def test_contact_slugs_are_recorded_as_the_code_gives_them(current: dict[str, Any]) -> None:
    """The entity IDs of a contact keep its slug: a slug made otherwise is another entity ID."""
    assert current["homeassistant"]["slugs"] == {
        "Alice": "alice",
        "Jürgen's Handy": "jurgen_s_handy",  # letters as ASCII, anything else as "_"
        "Groß": "gross",
        "Repeater on the hill behind the old mill": "repeater_on_the_hill_behind_the",  # 32 at most
        "🙂": "<key[:8]>",  # no letters: the key's start
        "Alice (a second contact)": "alice_<key[:4]>",  # a name taken
    }


def test_probe_prints_every_value_of_its_lists(current: dict[str, Any]) -> None:
    """The radios of the scenario cover each list of values that `probe` prints."""
    values = current["cli"]["values"]
    modes = [cli._mode_name(mode) for mode in TelemetryMode]
    assert {name: set(found) for name, found in values.items()} == {
        "probe --json:advert_location": set(cli.ADVERT_LOCATION.values()),
        "probe --json:auto_add.types": set(AUTO_ADD_TYPES.values()),
        "probe --json:contacts[].flags": set(cli._FLAG_NAMES.values()),
        "probe --json:contacts[].type": set(cli.NODE_TYPES.values()),
        "probe --json:telemetry.*": set(modes),
        "probe --list contacts:3": set(cli.NODE_TYPES.values()),
    }


def test_values_are_read_by_key_path() -> None:
    printed = {"contacts": [{"flags": ["a", "b"]}, {"flags": []}, {}], "telemetry": {"x": "c"}}
    assert interface._values_at(printed, "contacts[].flags") == ["a", "b"]
    assert interface._values_at(printed, "telemetry.*") == ["c"]
    assert interface._values_at(printed, "telemetry") == [{"x": "c"}]
    assert interface._values_at(printed, "advert_location") == []  # a key renamed: none
    assert interface._values_at({"contacts": {}}, "contacts[].type") == []


def test_contacts_have_the_node_types_of_the_wire(current: dict[str, Any]) -> None:
    """As `contacts/<key>:type` names them; the name of a Python enum member is no interface."""
    contact = current["homeassistant"]["components"]["contact"]
    assert {kind for c in contact.values() for kind in c["node_types"]} == {
        type_name(node_type) for node_type in NodeType
    }


# --- the command line (python -m tests.public_interface) ------------------------------------------


def _with_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, current: dict[str, Any], text: str | None
) -> Path:
    path = tmp_path / "public_interface.json"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(interface, "SNAPSHOT", path)
    monkeypatch.setattr(interface, "surface", lambda: copy.deepcopy(current))
    return path


def test_the_command_line_compares_and_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    current: dict[str, Any],
) -> None:
    older = copy.deepcopy(current)
    older["mqtt"]["documents"]["state"].append("battery_mv")
    path = _with_snapshot(monkeypatch, tmp_path, current, tool.render(older))
    assert interface.main([]) == 1
    err = capsys.readouterr().err
    assert 'mqtt > documents > state: no longer has "battery_mv"' in err
    assert "make interface" in err
    assert interface.main(["--write"]) == 0
    out = capsys.readouterr().out
    assert "Breaking" in out  # what the new snapshot drops stays visible
    assert out.endswith(f"wrote {path}\n")
    assert path.read_text(encoding="utf-8") == tool.render(current)
    assert interface.main([]) == 0
    assert capsys.readouterr().out == f"{path} is the public interface of the code\n"


def test_the_command_line_without_a_snapshot(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    current: dict[str, Any],
) -> None:
    path = _with_snapshot(monkeypatch, tmp_path, current, None)
    assert interface.main([]) == 1
    assert "make interface" in capsys.readouterr().err
    assert interface.main(["--write"]) == 0
    assert path.read_text(encoding="utf-8") == tool.render(current)


def test_the_command_line_with_a_snapshot_it_cannot_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    current: dict[str, Any],
) -> None:
    path = _with_snapshot(monkeypatch, tmp_path, current, '{"format": 0}\n')
    assert interface.main([]) == 1
    assert capsys.readouterr().err == (
        f"{path} cannot be compared with the code: snapshot format 0, this tool reads 1;"
        " `make interface` writes it again\n"
    )
    assert interface.main(["--write"]) == 0
    assert capsys.readouterr().out == (
        f"{path} cannot be compared with the code: snapshot format 0, this tool reads 1\n"
        f"wrote {path}\n"
    )
    assert path.read_text(encoding="utf-8") == tool.render(current)


def test_a_difference_in_form_only_is_explained() -> None:
    assert "only in its form" in interface.explain([])


def test_the_advice_names_the_release_each_kind_makes() -> None:
    found = [tool.Finding(kind, "x", "y") for kind in (tool.BREAKING, tool.CHANGED, tool.ADDED)]
    breaking, _, changed, _, added, _, against = interface._findings(found)
    assert "a major one; a false alarm: a waiver, docs/development.md#compatibility" in breaking
    assert changed.endswith("(the next release is then a minor one):")
    assert added.endswith("(the next release is then a minor one):")
    assert against == (  # a removal of what only main had needs no entry
        "These kinds are against the committed snapshot. What needs a changelog entry is what"
        " `git fetch --tags origin && make release-plan` reports against the last release: a"
        " removal of something no release had needs none."
    )
    assert interface._findings([]) == []


# --- the comparator, on a small synthetic snapshot ------------------------------------------------

B = "<base_topic>/<node_id>/"
SMALL: dict[str, Any] = {
    "format": 1,
    "mqtt": {
        "publish": {
            B + "state": {"retained": True, "qos": 1, "payload": "json"},
            B + "nodes": {"retained": True, "qos": 1, "payload": "json"},
            B + "rx": {"retained": False, "qos": 1, "payload": "json"},
            B + "command": {"retained": False, "qos": 0, "payload": "json"},
            B + "contacts/<key>": {"retained": True, "qos": 1, "payload": "json", "cleared": True},
        },
        "subscribe": {B + "send/+": {"qos": 1}, "<status_topic>": {"qos": 1}},
        "last_will": {B + "availability": {"payload": "offline", "retained": True, "qos": 1}},
        "documents": {"state": ["battery_v", "queue"], "contacts": ["contacts", "contacts[].key"]},
        "values": {"delivery:event_type": ["confirmed", "failed"]},
    },
    "config": {
        "airtime.burst_s": {"type": "number", "default": 10.0, "min": 5.0, "max": 3600.0},
        "commands.max_age_s": {"type": "number", "default": 900.0, "min": 0.0},
        "contacts.manage": {"type": "boolean", "default": True, "answer": "bool"},
        "diagnostics.log_level": {
            "type": "string",
            "default": "info",
            "choices": ["error", "info"],
        },
        "messages.queue_size": {"type": "integer", "default": 20, "min": 1, "max": 1000},
        "mqtt.base_topic": {"type": "string", "default": "meshcore", "identity": True},
        "mqtt.keepalive_s": {
            "type": "integer",
            "default": 60,
            "min": 10,
            "max": 3600,
            "answer": "int",
        },
        "mqtt.port": {"type": "integer", "default": 1883, "min": 1, "max": 65535, "answer": "int"},
        "mqtt.username": {"type": "string", "answer": "str"},
        "radio.transport": {"type": "string", "required": True, "choices": ["serial", "tcp"]},
        "targets": {"type": "table"},
        "targets.<alias>": {"type": "table", "conditional": True},
        "targets.<alias>.channel": {"type": "integer", "min": 0, "max": 255, "conditional": True},
    },
    "cli": {
        "commands": {
            "meshcore-home-gateway": {
                "options": {"--version": {"output": "meshcore-home-gateway {version}"}},
                "positionals": {"1": {"takes_value": True, "required": True, "name": "command"}},
            },
            "listen": {
                "options": {
                    "--count": {"takes_value": True, "type": "count", "min": 0, "default": 0},
                    "--topic": {"takes_value": True, "choices": ["all", "state"], "default": "all"},
                },
                "positionals": {},
            },
            "probe": {
                "options": {
                    "--json": {"takes_value": False},
                    "--list": {"takes_value": True, "choices": ["channels", "contacts"]},
                    "--serial": {"takes_value": True},
                    "--tcp": {"takes_value": True, "type": "host[:port]", "min": 1, "max": 65535},
                },
                "positionals": {},
                "groups": [{"options": [["--serial"], ["--tcp"]], "required": True}],
            },
            "run": {
                "options": {"--state-dir": {"takes_value": True, "type": "path"}},
                "positionals": {},
            },
            "send": {
                "options": {
                    "--config": {"takes_value": True, "type": "path", "default": "/etc/m.toml"},
                    "--label": {"takes_value": True, "nargs": "*"},
                    "--wait": {
                        "takes_value": True,
                        "type": "seconds",
                        "min": 0.0,
                        "max": 86400.0,
                        "default": 0.0,
                    },
                },
                "positionals": {
                    "1": {"takes_value": True, "required": True, "name": "alias"},
                    "2": {"takes_value": True, "required": True, "name": "text"},
                },
            },
        },
        "exit_codes": {"ok": 0, "failure": 1, "usage": 2, "config_error": 78},
        "paths": {"config": "/etc/meshcore-home-gateway/config.toml"},
        "columns": {"probe --list contacts": 3},
        "json": {"probe --json": ["battery_mv", "name"]},
        "values": {"probe --json:contacts[].type": ["chat", "room"]},
    },
    "homeassistant": {
        "devices": {
            "gateway": {
                "id": "meshcore_home_gateway_<node_id>",
                "topic": "<prefix>/device/<device>/config",
            },
        },
        "components": {
            "gateway": {
                "battery": {"platform": "sensor", "entity_id": "sensor.<device>_b", "unit": "V"},
                "contact": {
                    "platform": "event",
                    "entity_id": "event.<device>_contact",
                    "event_types": ["added", "refused"],
                    "attributes": "<document contact>",
                },
                "airtime": {
                    "platform": "sensor",
                    "entity_id": "sensor.<device>_airtime",
                    "attributes": ["budget_s_per_hour"],
                },
                "profile": {"platform": "sensor", "entity_id": "sensor.<device>_profile"},
            },
            "contact": {
                "send": {
                    "platform": "notify",
                    "entity_id": "notify.mesh_<slug>",
                    "node_types": ["chat"],
                },
            },
        },
        "slugs": {"Alice": "alice", "🙂": "<key[:8]>"},
    },
    "platforms": {"tested_firmware": [14]},
}

Mutation = Callable[[dict[str, Any]], object]


def at(doc: dict[str, Any], *path: str) -> Any:
    for key in path:
        doc = doc[key]
    return doc


PUBLISH, CONFIG = ("mqtt", "publish"), ("config",)
SEND, PROBE = ("cli", "commands", "send"), ("cli", "commands", "probe")
LISTEN, RUN = ("cli", "commands", "listen"), ("cli", "commands", "run")
GATEWAY = ("homeassistant", "components", "gateway")

POSITIONAL = (*SEND, "positionals", "1")
LAST_WILL = ("mqtt", "last_will", B + "availability")
TELEMETRY = {"type": "table"}  # 1.1.0a1 adds the table and its switch
ENABLED = {"type": "boolean", "default": False, "answer": "bool"}

BREAKING: dict[str, Mutation] = {
    "topic removed": lambda d: at(d, *PUBLISH).pop(B + "rx"),
    "retained flipped": lambda d: at(d, *PUBLISH, B + "state").update(retained=False),
    "qos changed": lambda d: at(d, *PUBLISH, B + "command").update(qos=1),
    "no longer cleared": lambda d: at(d, *PUBLISH, B + "contacts/<key>").pop("cleared"),
    "subscription removed": lambda d: d["mqtt"]["subscribe"].pop(B + "send/+"),
    "subscription qos": lambda d: d["mqtt"]["subscribe"][B + "send/+"].update(qos=0),
    "last will changed": lambda d: at(d, *LAST_WILL).update(payload="gone"),
    "last will not retained": lambda d: at(d, *LAST_WILL).update(retained=False),
    "last will qos": lambda d: at(d, *LAST_WILL).update(qos=0),
    "document key removed": lambda d: d["mqtt"]["documents"]["state"].remove("battery_v"),
    "nested key removed": lambda d: d["mqtt"]["documents"]["contacts"].remove("contacts[].key"),
    "value removed": lambda d: d["mqtt"]["values"]["delivery:event_type"].remove("confirmed"),
    "setting removed": lambda d: at(d, *CONFIG).pop("contacts.manage"),
    "maximum lowered": lambda d: at(d, *CONFIG, "messages.queue_size").update(max=500),
    "minimum raised": lambda d: at(d, *CONFIG, "airtime.burst_s").update(min=6.0),
    "bound added": lambda d: at(d, *CONFIG, "commands.max_age_s").update(max=60.0),
    "choice removed": lambda d: at(d, *CONFIG, "diagnostics.log_level", "choices").remove("error"),
    "choices introduced": lambda d: at(d, *CONFIG, "mqtt.username").update(choices=["a"]),
    "number to integer": lambda d: at(d, *CONFIG, "airtime.burst_s").update(type="integer"),
    "now required": lambda d: at(d, *CONFIG, "mqtt.port").update(required=True),
    "conditional now required": lambda d: at(d, *CONFIG, "targets.<alias>.channel").update(
        required=True
    ),
    "new and required": lambda d: at(d, *CONFIG).update(
        {"mqtt.realm": {"type": "string", "required": True}}
    ),
    "required in an existing table": lambda d: at(d, *CONFIG).update(
        {"targets.<alias>.priority": {"type": "string", "required": True, "conditional": True}}
    ),
    "identity default": lambda d: at(d, *CONFIG, "mqtt.base_topic").update(default="mesh"),
    "answer type": lambda d: at(d, *CONFIG, "mqtt.port").update(answer="str"),
    "answer gone": lambda d: at(d, *CONFIG, "mqtt.port").pop("answer"),
    "command removed": lambda d: d["cli"]["commands"].pop("listen"),
    "option removed": lambda d: at(d, *SEND, "options").pop("--wait"),
    "flag takes a value": lambda d: at(d, *PROBE, "options", "--json").update(takes_value=True),
    "new required option": lambda d: at(d, *SEND, "options").update(
        {"--to": {"takes_value": True, "required": True}}
    ),
    "new required positional": lambda d: at(d, *SEND, "positionals").update(
        {"3": {"takes_value": True, "required": True, "name": "channel"}}
    ),
    "positional renamed": lambda d: at(d, *POSITIONAL).update(name="target"),
    "cli choice removed": lambda d: at(d, *PROBE, "options", "--list", "choices").remove(
        "contacts"
    ),
    "cli type introduced": lambda d: at(d, *PROBE, "options", "--serial").update(type="path"),
    "cli type changed": lambda d: at(d, *SEND, "options", "--wait").update(type="count"),
    "two values": lambda d: at(d, *POSITIONAL).update(nargs=2),
    "at least one": lambda d: at(d, *SEND, "options", "--label").update(nargs="+"),
    "the rest": lambda d: at(d, *POSITIONAL).update(nargs="..."),
    "path default": lambda d: at(d, *SEND, "options", "--config").update(default="/etc/x.toml"),
    "option maximum lowered": lambda d: at(d, *SEND, "options", "--wait").update(max=5.0),
    "option bound added": lambda d: at(d, *LISTEN, "options", "--count").update(max=100),
    "port range narrowed": lambda d: at(d, *PROBE, "options", "--tcp").update(min=1024),
    "subcommands required": lambda d: at(d, *LISTEN, "positionals").update(
        {"1": {"takes_value": True, "required": True, "name": "source"}}
    ),
    "column appended": lambda d: d["cli"]["columns"].update({"probe --list contacts": 4}),
    "exit code": lambda d: d["cli"]["exit_codes"].update(config_error=1),
    "version format": lambda d: at(
        d, "cli", "commands", "meshcore-home-gateway", "options", "--version"
    ).update(output="{version}"),
    "default path": lambda d: d["cli"]["paths"].update(config="/etc/mhg.toml"),
    "probe json key removed": lambda d: d["cli"]["json"]["probe --json"].remove("battery_mv"),
    "probe value removed": lambda d: d["cli"]["values"]["probe --json:contacts[].type"].remove(
        "room"
    ),
    "component removed": lambda d: at(d, *GATEWAY).pop("battery"),
    "entity id changed": lambda d: at(d, *GATEWAY, "battery").update(entity_id="sensor.x"),
    "platform changed": lambda d: at(d, *GATEWAY, "battery").update(platform="number"),
    "unit changed": lambda d: at(d, *GATEWAY, "battery").update(unit="mV"),
    "unit added": lambda d: at(d, *GATEWAY, "profile").update(unit="x"),
    "unit dropped": lambda d: at(d, *GATEWAY, "battery").pop("unit"),
    "event type removed": lambda d: at(d, *GATEWAY, "contact", "event_types").remove("refused"),
    "attribute key removed": lambda d: at(d, *GATEWAY, "airtime").update(attributes=[]),
    "attributes of a document no longer": lambda d: at(d, *GATEWAY, "contact").pop("attributes"),
    "node type removed": lambda d: at(
        d, "homeassistant", "components", "contact", "send", "node_types"
    ).remove("chat"),
    "device id": lambda d: d["homeassistant"]["devices"]["gateway"].update(id="mhg_<node_id>"),
    "slug made otherwise": lambda d: d["homeassistant"]["slugs"].update({"🙂": "<key[:12]>"}),
    "slug example gone": lambda d: d["homeassistant"]["slugs"].pop("Alice"),
    "tested firmware dropped": lambda d: d["platforms"]["tested_firmware"].remove(14),
    "section removed": lambda d: d.pop("homeassistant"),
}

COMPATIBLE: dict[str, Mutation] = {
    "topic added": lambda d: at(d, *PUBLISH).update(
        {B + "telemetry": {"retained": True, "qos": 1, "payload": "json"}}
    ),
    "now cleared": lambda d: at(d, *PUBLISH, B + "nodes").update(cleared=True),
    "subscription added": lambda d: d["mqtt"]["subscribe"].update({B + "trace/+": {"qos": 1}}),
    "document key added": lambda d: d["mqtt"]["documents"]["state"].append("uptime_s"),
    "value added": lambda d: d["mqtt"]["values"]["delivery:event_type"].append("deferred"),
    "setting added": lambda d: at(d, *CONFIG).update({"mqtt.realm": {"type": "string"}}),
    "a table and its switch": lambda d: at(d, *CONFIG).update(
        {"telemetry": TELEMETRY, "telemetry.enabled": ENABLED}
    ),
    "required in a new table": lambda d: at(d, *CONFIG).update(
        {
            "telemetry": {**TELEMETRY, "conditional": True},
            "telemetry.endpoint": {"type": "string", "required": True, "conditional": True},
        }
    ),
    "maximum raised": lambda d: at(d, *CONFIG, "airtime.burst_s").update(max=7200.0),
    "minimum lowered": lambda d: at(d, *CONFIG, "airtime.burst_s").update(min=1.0),
    "bound dropped": lambda d: at(d, *CONFIG, "messages.queue_size").pop("max"),
    "integer to number": lambda d: at(d, *CONFIG, "mqtt.keepalive_s").update(
        type="number", answer="float"
    ),
    "answer tracked since": lambda d: at(d, *CONFIG, "airtime.burst_s").update(answer="float"),
    "choice added": lambda d: at(d, *CONFIG, "radio.transport", "choices").append("ble"),
    "choices dropped": lambda d: at(d, *CONFIG, "radio.transport").pop("choices"),
    "no longer required": lambda d: at(d, *CONFIG, "radio.transport").update(required=False),
    "default changed": lambda d: at(d, *CONFIG, "messages.queue_size").update(default=50),
    "command added": lambda d: d["cli"]["commands"].update(
        {"trace": {"options": {}, "positionals": {}}}
    ),
    "option added": lambda d: at(d, *SEND, "options").update({"--channel": {"takes_value": True}}),
    "optional positional": lambda d: at(d, *SEND, "positionals").update(
        {"3": {"takes_value": True, "nargs": "?", "name": "channel"}}
    ),
    "optional variadic positional": lambda d: at(d, *SEND, "positionals").update(
        {"3": {"takes_value": True, "nargs": "*", "name": "names"}}
    ),
    "option maximum raised": lambda d: at(d, *SEND, "options", "--wait").update(max=604800.0),
    "subcommands optional": lambda d: at(d, *LISTEN, "positionals").update(
        {"1": {"takes_value": True, "name": "source"}}
    ),
    "cli choice added": lambda d: at(d, *PROBE, "options", "--list", "choices").append("profile"),
    "cli default": lambda d: at(d, *SEND, "options", "--wait").update(default=5.0),
    "cli type dropped": lambda d: at(d, *SEND, "options", "--wait").pop("type"),
    "now optional": lambda d: at(d, *POSITIONAL).update(nargs="?"),
    "one or more": lambda d: at(d, *POSITIONAL).update(nargs="+"),
    "exactly one, as a list": lambda d: at(d, *POSITIONAL).update(nargs=1),
    "column list added": lambda d: d["cli"]["columns"].update({"probe --list nodes": 3}),
    "probe json key added": lambda d: d["cli"]["json"]["probe --json"].append("uptime_s"),
    "probe value added": lambda d: d["cli"]["values"]["probe --json:contacts[].type"].append(
        "sensor"
    ),
    "component added": lambda d: at(d, *GATEWAY).update(
        {"uptime": {"platform": "sensor", "entity_id": "sensor.<device>_uptime"}}
    ),
    "event type added": lambda d: at(d, *GATEWAY, "contact", "event_types").append("linked"),
    "attribute key added": lambda d: at(d, *GATEWAY, "airtime", "attributes").append("burst_s"),
    "node type added": lambda d: at(
        d, "homeassistant", "components", "contact", "send", "node_types"
    ).append("room"),
    "slug example added": lambda d: d["homeassistant"]["slugs"].update({"Groß": "gross"}),
    "firmware tested in addition": lambda d: d["platforms"]["tested_firmware"].append(15),
    "section added": lambda d: d.update({"installer": {"answers": ["setup"]}}),
}
# compatible, but users notice them: listed under Changed; the others are additions
CHANGED = {
    "maximum raised", "minimum lowered", "bound dropped", "integer to number", "choices dropped",
    "no longer required", "default changed", "cli default", "cli type dropped", "now optional",
    "one or more", "exactly one, as a list", "option maximum raised",
}  # fmt: skip


def mutated(change: Mutation) -> dict[str, Any]:
    doc = copy.deepcopy(SMALL)
    change(doc)
    return doc


def test_an_unchanged_snapshot_has_no_findings() -> None:
    assert tool.compare(SMALL, copy.deepcopy(SMALL)) == []


@pytest.mark.parametrize("change", BREAKING.values(), ids=BREAKING.keys())
def test_breaking(change: Mutation) -> None:
    found = tool.compare(SMALL, mutated(change))
    assert [f.kind for f in found] == ["breaking"], found


@pytest.mark.parametrize(("name", "change"), COMPATIBLE.items(), ids=COMPATIBLE.keys())
def test_compatible(name: str, change: Mutation) -> None:
    found = tool.compare(SMALL, mutated(change))
    assert found
    assert {f.kind for f in found} == {"changed" if name in CHANGED else "added"}, found


def test_positionals_in_another_order_break_at_both_places() -> None:
    def swap(d: dict[str, Any]) -> None:
        first, second = at(d, *SEND, "positionals").values()
        first["name"], second["name"] = second["name"], first["name"]

    found = tool.compare(SMALL, mutated(swap))
    assert [(f.kind, str(f)) for f in found] == [
        ("breaking", 'cli > commands > send > positionals > 1 > name: "alias" -> "text"'),
        ("breaking", 'cli > commands > send > positionals > 2 > name: "text" -> "alias"'),
    ]


# --- option groups and subcommands: a call that worked must still work ---------------------------

C = "cli > commands > "
FLAG = {"takes_value": False}


def grouped(*path: str, options: list[str], required: bool) -> Mutation:
    """A mutation that gives the command at `path` this group, a member's names joined by "/"
    (new options take no value)."""

    def change(d: dict[str, Any]) -> None:
        command = at(d, *path)
        members = sorted(sorted(member.split("/")) for member in options)
        for name in (name for names in members for name in names):
            command["options"].setdefault(name, FLAG)
        command.setdefault("groups", []).append({"options": members, "required": required})

    return change


def regrouped(options: list[str], required: bool = True) -> Mutation:
    """A mutation that makes probe's one group this one (new options take no value)."""

    def change(d: dict[str, Any]) -> None:
        at(d, *PROBE).pop("groups")
        grouped(*PROBE, options=options, required=required)(d)

    return change


def aliased(option: str, alias: str) -> Mutation:
    """A mutation that gives probe's `option` another name, in its group too."""

    def change(d: dict[str, Any]) -> None:
        command = at(d, *PROBE)
        command["options"][alias] = copy.deepcopy(command["options"][option])
        for group in command["groups"]:
            group["options"] = sorted(
                sorted([*names, alias]) if option in names else names for names in group["options"]
            )

    return change


def excluding(path: str, first: str, second: str) -> tuple[str, str]:
    return path, f'"{first}" and "{second}" exclude each other'


PROBE_GROUP = f"{C}probe > groups > --serial | --tcp"
SEND_GROUP = f"{C}send > groups > --direct | --flood"
WIDER = f"{C}probe > groups > --json | --serial | --tcp"
JOINED = f"{C}probe > groups > --ble | --serial | --tcp"
LISTEN_GROUP = f"{C}listen > groups > --count | --topic"
OUTPUT_GROUP = f"{C}probe > groups > --json | --list"
ALIASED = f"{C}probe > groups > --serial | --tcp/--tcp-address"
RENAMED = f"{C}probe > groups > --host/--tcp | --serial"
GROUPS: dict[str, tuple[Mutation, set[tuple[str, str]]]] = {
    # breaking
    "a required group of new options": (
        grouped(*SEND, options=["--direct", "--flood"], required=True),
        {
            ("added", f"{C}send > options > --direct: new"),
            ("added", f"{C}send > options > --flood: new"),
            ("added", ": ".join(excluding(SEND_GROUP, "--direct", "--flood"))),
            ("breaking", f"{SEND_GROUP}: new and required"),
        },
    ),
    "existing options made exclusive": (
        grouped(*PROBE, options=["--json", "--list"], required=False),
        {("breaking", ": ".join(excluding(OUTPUT_GROUP, "--json", "--list")))},
    ),
    "listen --topic and --count exclusive": (
        grouped(*LISTEN, options=["--count", "--topic"], required=False),
        {("breaking", ": ".join(excluding(LISTEN_GROUP, "--count", "--topic")))},
    ),
    "an option in a required group of its own": (
        grouped(*RUN, options=["--state-dir"], required=True),
        {("breaking", f"{C}run > groups > --state-dir: new and required")},
    ),
    "an option moved out of a required group": (
        regrouped(["--serial"]),
        {
            ("breaking", f'{C}probe > groups > --serial: no longer satisfied by "--tcp"'),
            ("changed", f'{PROBE_GROUP}: "--serial" and "--tcp" no longer exclude each other'),
        },
    ),
    "a required group widened by an existing option": (  # which it now excludes, too
        regrouped(["--json", "--serial", "--tcp"]),
        {
            ("breaking", ": ".join(excluding(WIDER, "--json", "--serial"))),
            ("breaking", ": ".join(excluding(WIDER, "--json", "--tcp"))),
            ("changed", f'{WIDER}: also satisfied by "--json"'),
        },
    ),
    # compatible
    "a new option joins a required group": (
        regrouped(["--ble", "--serial", "--tcp"]),
        {
            ("added", f"{C}probe > options > --ble: new"),
            ("added", ": ".join(excluding(JOINED, "--ble", "--serial"))),
            ("added", ": ".join(excluding(JOINED, "--ble", "--tcp"))),
            ("added", f'{JOINED}: also satisfied by "--ble"'),
        },
    ),
    "an alias of a grouped option": (  # still the same option: it excludes none of its names
        aliased("--tcp", "--tcp-address"),
        {
            ("added", f"{C}probe > options > --tcp-address: new"),
            ("added", ": ".join(excluding(ALIASED, "--serial", "--tcp-address"))),
            ("added", f'{ALIASED}: also satisfied by "--tcp-address"'),
        },
    ),
    "a rename that keeps the old name": (
        aliased("--tcp", "--host"),
        {
            ("added", f"{C}probe > options > --host: new"),
            ("added", ": ".join(excluding(RENAMED, "--host", "--serial"))),
            ("added", f'{RENAMED}: also satisfied by "--host"'),
        },
    ),
    "a group of new options, not required": (
        grouped(*SEND, options=["--direct", "--flood"], required=False),
        {
            ("added", f"{C}send > options > --direct: new"),
            ("added", f"{C}send > options > --flood: new"),
            ("added", ": ".join(excluding(SEND_GROUP, "--direct", "--flood"))),
        },
    ),
    "a group no longer required": (
        regrouped(["--serial", "--tcp"], required=False),
        {("changed", f"{PROBE_GROUP}: no longer required")},
    ),
    "a group dropped": (
        lambda d: at(d, *PROBE).pop("groups"),
        {
            ("changed", f'{PROBE_GROUP}: "--serial" and "--tcp" no longer exclude each other'),
            ("changed", f"{PROBE_GROUP}: no longer required"),
        },
    ),
    "an option of a required group removed": (  # its removal is the one finding
        lambda d: (at(d, *PROBE, "options").pop("--tcp"), regrouped(["--serial"])(d)),
        {("breaking", f"{C}probe > options > --tcp: removed")},
    ),
    "a new command with a required group": (
        lambda d: (
            d["cli"]["commands"].update({"trace": {"options": {}, "positionals": {}}}),
            grouped("cli", "commands", "trace", options=["--to"], required=True)(d),
        ),
        {("added", f"{C}trace: new")},
    ),
}  # fmt: skip


@pytest.mark.parametrize(("change", "expected"), GROUPS.values(), ids=GROUPS.keys())
def test_option_groups(change: Mutation, expected: set[tuple[str, str]]) -> None:
    found = tool.compare(SMALL, mutated(change))
    assert {(f.kind, str(f)) for f in found} == expected
    assert len(found) == len(expected)


def test_a_group_that_becomes_required_breaks() -> None:
    loose = mutated(regrouped(["--serial", "--tcp"], required=False))
    assert [(f.kind, str(f)) for f in tool.compare(loose, SMALL)] == [
        ("breaking", f"{PROBE_GROUP}: now required")
    ]


def test_an_option_required_alone_may_join_a_group() -> None:
    """Every call gave the option: it satisfies the group."""
    alone = mutated(
        lambda d: at(d, *SEND, "options").update({"--to": {"takes_value": True, "required": True}})
    )
    either = copy.deepcopy(alone)
    at(either, *SEND, "options", "--to").pop("required")
    grouped(*SEND, options=["--all", "--to"], required=True)(either)
    assert {(f.kind, str(f)) for f in tool.compare(alone, either)} == {
        ("added", f"{C}send > options > --all: new"),
        ("added", f'{C}send > groups > --all | --to: "--all" and "--to" exclude each other'),
        ("added", f'{C}send > groups > --all | --to: also satisfied by "--all"'),
        ("changed", f"{C}send > options > --to > required: true -> null"),
    }


def test_a_command_that_needs_a_subcommand_from_then_on_breaks() -> None:
    def subcommands(d: dict[str, Any]) -> None:
        BREAKING["subcommands required"](d)
        d["cli"]["commands"]["listen broker"] = {"options": {}, "positionals": {}}

    assert [(f.kind, str(f)) for f in tool.compare(SMALL, mutated(subcommands))] == [
        ("added", f"{C}listen broker: new"),
        ("breaking", f"{C}listen > positionals > 1: new and required"),
    ]


def test_the_extractor_records_groups_and_subcommands() -> None:
    parser = argparse.ArgumentParser(prog="x")
    where = parser.add_mutually_exclusive_group(required=True)
    where.add_argument("-s", "--serial")
    where.add_argument("--tcp")
    output = parser.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true")
    output.add_argument("--list")
    parser.add_argument("name")
    commands = parser.add_subparsers(required=True, metavar="action")
    commands.add_parser("run").add_subparsers(dest="what")
    out: dict[str, Any] = {}
    interface._commands(parser, "", out)
    assert out["x"]["groups"] == [  # an option by all its names
        {"options": [["--json"], ["--list"]], "required": False},
        {"options": [["--serial", "-s"], ["--tcp"]], "required": True},
    ]
    assert out["x"]["positionals"] == {
        "1": {"name": "name", "required": True, "takes_value": True},
        "2": {"name": "action", "required": True, "takes_value": True},  # at its place
    }
    assert out["run"] == {
        "options": {},
        "positionals": {"1": {"name": "what", "takes_value": True}},
    }


def test_names_of_one_length_in_another_order_are_no_change() -> None:
    """argparse calls the option by its first name; the snapshot lists every name."""

    def recorded(*names: str) -> dict[str, Any]:
        parser = argparse.ArgumentParser(prog="x")
        where = parser.add_mutually_exclusive_group(required=True)
        where.add_argument("--serial")
        where.add_argument(*names, dest="tcp")
        out: dict[str, Any] = {}
        interface._commands(parser, "", out)
        return {"format": 1, "cli": {"commands": out}}

    assert recorded("--tcp", "--net") == recorded("--net", "--tcp")
    assert tool.compare(recorded("--tcp", "--net"), recorded("--net", "--tcp")) == []


def test_an_alias_of_a_required_option_is_an_addition() -> None:
    """As `config render --answers` would gain `--answers-file`: every call still works. A rename
    without the old name breaks every call."""

    def recorded(*names: str) -> dict[str, Any]:
        parser = argparse.ArgumentParser(prog="render")
        parser.add_argument(*names, dest="answers", type=Path, required=True)
        out: dict[str, Any] = {}
        interface._commands(parser, "", out)
        return {"format": 1, "cli": {"commands": out}}

    options = f"{C}render > options > "
    aliased = tool.compare(recorded("--answers"), recorded("--answers", "--answers-file"))
    assert [(f.kind, str(f)) for f in aliased] == [
        ("added", f'{options}--answers > names: also has "--answers-file"'),
        ("added", f"{options}--answers-file: new"),
    ]
    renamed = tool.compare(recorded("--answers"), recorded("--answers-file"))
    assert [(f.kind, str(f)) for f in renamed] == [
        ("breaking", f"{options}--answers: removed"),
        ("breaking", f"{options}--answers-file: new and required"),
    ]
    dropped = tool.compare(recorded("--answers", "--answers-file"), recorded("--answers"))
    assert [(f.kind, str(f)) for f in dropped] == [
        ("breaking", f'{options}--answers > names: no longer has "--answers-file"'),
        ("breaking", f"{options}--answers-file: removed"),
    ]


def test_a_positional_without_values_is_not_required_on_any_python() -> None:
    """Before 3.13 argparse calls a `*` positional without a default required."""
    parser = argparse.ArgumentParser(prog="x")
    parser.add_argument("names", nargs="*").required = True  # as 3.11 and 3.12 set it
    parser.add_argument("more", nargs="?")
    parser.add_argument("pair", nargs=2)
    out: dict[str, Any] = {}
    interface._commands(parser, "", out)
    assert out["x"]["positionals"] == {
        "1": {"name": "names", "nargs": "*", "takes_value": True},
        "2": {"name": "more", "nargs": "?", "takes_value": True},
        "3": {"name": "pair", "nargs": 2, "required": True, "takes_value": True},
    }


def test_an_option_records_the_bounds_its_type_takes() -> None:
    parser = argparse.ArgumentParser(prog="x")
    parser.add_argument("--wait", type=cli._wait_seconds)
    parser.add_argument("--tcp", type=cli.tcp_address)
    parser.add_argument("--count", type=cli._count)
    out: dict[str, Any] = {}
    interface._commands(parser, "", out)
    options = out["x"]["options"]
    assert (options["--wait"]["min"], options["--wait"]["max"]) == (0.0, cli.MAX_WAIT_S)
    assert (options["--tcp"]["min"], options["--tcp"]["max"]) == (1, 65535)  # of the port
    assert options["--count"]["min"] == 0
    assert "max" not in options["--count"]


def test_a_required_setting_in_a_new_table_that_every_file_reads_is_breaking() -> None:
    """Read for every file, also one without the table: every file must set it."""

    def change(d: dict[str, Any]) -> None:
        at(d, *CONFIG).update(
            {"telemetry": TELEMETRY, "telemetry.endpoint": {"type": "string", "required": True}}
        )

    found = tool.compare(SMALL, mutated(change))
    assert [(f.kind, str(f)) for f in found] == [
        ("added", "config > telemetry: new"),
        ("breaking", "config > telemetry.endpoint: new and required"),
    ]


def test_each_value_of_a_set_is_a_finding_of_its_own() -> None:
    def change(d: dict[str, Any]) -> None:
        d["mqtt"]["documents"]["state"].clear()
        at(d, *GATEWAY, "contact")["event_types"] = ["linked"]

    found = tool.compare(SMALL, mutated(change))
    events = "homeassistant > components > gateway > contact > event_types"
    assert [(f.kind, str(f)) for f in found] == [
        ("added", f'{events}: also has "linked"'),
        ("breaking", f'{events}: no longer has "added"'),
        ("breaking", f'{events}: no longer has "refused"'),
        ("breaking", 'mqtt > documents > state: no longer has "battery_v"'),
        ("breaking", 'mqtt > documents > state: no longer has "queue"'),
    ]


def test_identity_is_not_compared() -> None:
    assert (
        tool.compare(
            SMALL, mutated(lambda d: at(d, *CONFIG, "mqtt.username").update(identity=True))
        )
        == []
    )


def test_a_required_setting_read_for_every_file_from_then_on_is_breaking() -> None:
    """Released with a table read only when present, then read for every file: each file needs
    the setting now, also one without the table."""
    optional = mutated(COMPATIBLE["required in a new table"])
    everywhere = copy.deepcopy(optional)
    for key in ("telemetry", "telemetry.endpoint"):
        everywhere["config"][key].pop("conditional")
    found = tool.compare(optional, everywhere)
    assert [(f.kind, str(f)) for f in found] == [
        ("breaking", "config > telemetry.endpoint > conditional: true -> null"),
        ("changed", "config > telemetry > conditional: true -> null"),
    ]
    at(everywhere, *CONFIG, "telemetry.endpoint").pop("required")  # optional, read everywhere
    assert {f.kind for f in tool.compare(optional, everywhere)} == {"changed"}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (  # its default now applies to every file
            lambda d: at(d, *CONFIG, "targets.<alias>.channel").pop("conditional"),
            "config > targets.<alias>.channel > conditional: true -> null",
        ),
        (  # read only when present from now on: what loaded still loads
            lambda d: at(d, *CONFIG, "mqtt.username").update(conditional=True),
            "config > mqtt.username > conditional: null -> true",
        ),
    ],
)
def test_a_setting_read_otherwise_is_a_change(change: Mutation, expected: str) -> None:
    assert [(f.kind, str(f)) for f in tool.compare(SMALL, mutated(change))] == [
        ("changed", expected)
    ]


def test_a_list_in_another_order_is_no_change() -> None:
    def reorder(d: dict[str, Any]) -> None:
        d["mqtt"]["documents"]["state"].reverse()
        d["config"]["radio.transport"]["choices"].reverse()
        at(d, *GATEWAY, "contact", "event_types").reverse()

    assert tool.compare(SMALL, mutated(reorder)) == []


def test_a_rename_is_a_removal_and_an_addition() -> None:
    def rename(d: dict[str, Any]) -> None:
        d["config"]["mqtt.keepalive"] = d["config"].pop("mqtt.keepalive_s")

    found = tool.compare(SMALL, mutated(rename))
    assert [(f.kind, f.path) for f in found] == [
        ("added", "config > mqtt.keepalive"),
        ("breaking", "config > mqtt.keepalive_s"),
    ]


def test_a_finding_names_its_path_and_both_values() -> None:
    [finding] = tool.compare(SMALL, mutated(BREAKING["unit changed"]))
    assert str(finding) == 'homeassistant > components > gateway > battery > unit: "V" -> "mV"'


def test_an_attribute_without_a_rule_is_refused() -> None:
    doc = mutated(lambda d: at(d, *GATEWAY, "battery").update(state_class="measurement"))
    with pytest.raises(tool.FormatError, match=re.escape("battery: no rule for the attribute")):
        tool.compare(SMALL, doc)


def test_a_record_that_is_no_object_is_refused() -> None:
    doc = mutated(lambda d: at(d, *PUBLISH).update({B + "rx": "json"}))
    with pytest.raises(tool.FormatError, match="a record must be an object"):
        tool.compare(doc, SMALL)


@pytest.mark.parametrize(
    ("path", "value", "expected"),
    [
        (
            (*CONFIG, "mqtt.port", "min"),
            "1",
            'config > mqtt.port > min: expected a number, got "1"',
        ),
        ((*CONFIG, "mqtt.port", "max"), True, "max: expected a number, got true"),
        ((*CONFIG, "mqtt.port", "default"), {"a": 1}, "default: expected a JSON scalar or list"),
        ((*CONFIG, "mqtt.port", "required"), None, "required: expected true or false, got null"),
        ((*CONFIG, "radio.transport", "choices"), [{"a": 1}], "expected a list of strings and"),
        ((*PUBLISH, B + "state", "qos"), True, "qos: expected an integer, got true"),
        ((*PUBLISH, B + "state", "payload"), 1, "payload: expected a string, got 1"),
        ((*POSITIONAL, "nargs"), 1.5, "nargs: expected a string or an integer, got 1.5"),
        ((*GATEWAY, "contact", "event_types"), {"added": 1}, "expected a list of strings, got {"),
        ((*GATEWAY, "airtime", "attributes"), [1], "attributes: expected a string or a list of"),
        ((*PROBE, "groups"), [{"options": "--tcp", "required": True}], "probe > groups: expected"),
        ((*PROBE, "groups"), [{"options": ["--tcp"]}], 'groups: expected a list of {"options"'),
        ((*PROBE, "groups"), {"options": ["--tcp"], "required": True}, "groups: expected a list"),
        (
            (*PROBE, "groups"),
            [{"options": ["--tcp"], "required": True}],
            "probe > groups: expected",
        ),
        ((*PROBE, "groups"), [{"options": [[]], "required": True}], "probe > groups: expected"),
    ],
)
def test_a_value_of_another_shape_is_refused(
    path: tuple[str, ...], value: object, expected: str
) -> None:
    doc = mutated(lambda d: at(d, *path[:-1]).update({path[-1]: value}))
    with pytest.raises(tool.FormatError, match=re.escape(expected)):
        tool.compare(SMALL, doc)


def test_another_format_is_refused() -> None:
    with pytest.raises(tool.FormatError, match="format 2, this tool reads 1"):
        tool.compare(SMALL, {**SMALL, "format": 2})


def test_a_waiver_names_a_finding_as_the_refusal_shows_it() -> None:
    new = mutated(BREAKING["topic removed"])
    [finding] = tool.unwaived(SMALL, new, {})
    assert str(finding) == f"mqtt > publish > {B}rx: removed"
    assert tool.unwaived(SMALL, new, {str(finding): "never published"}) == []
    assert tool.unwaived(SMALL, new, {finding.path: "never published"}) == [finding]
    grown = mutated(COMPATIBLE["topic added"])  # growth too: a fix may add what was missing
    assert tool.unwaived(SMALL, grown, {f"mqtt > publish > {B}telemetry: new": "a fix"}) == []


def test_a_waiver_covers_its_one_fact_only() -> None:
    """A waived removal from a list lets no second removal pass, a waived narrowing no other."""
    one = mutated(BREAKING["document key removed"])
    waived = {str(f): "never published" for f in tool.unwaived(SMALL, one, {})}
    assert list(waived) == ['mqtt > documents > state: no longer has "battery_v"']
    assert tool.unwaived(SMALL, one, waived) == []
    both = mutated(lambda d: d["mqtt"]["documents"]["state"].clear())
    assert [str(f) for f in tool.unwaived(SMALL, both, waived)] == [
        'mqtt > documents > state: no longer has "queue"'
    ]
    lowered = mutated(BREAKING["maximum lowered"])
    waived = {str(f): "a typo, never released" for f in tool.unwaived(SMALL, lowered, {})}
    assert list(waived) == ["config > messages.queue_size > max: 1000 -> 500"]
    further = mutated(lambda d: at(d, *CONFIG, "messages.queue_size").update(max=50))
    assert [str(f) for f in tool.unwaived(SMALL, further, waived)] == [
        "config > messages.queue_size > max: 1000 -> 50"
    ]
    refused = mutated(BREAKING["event type removed"])
    waived = {str(f): "never published" for f in tool.unwaived(SMALL, refused, {})}
    none = mutated(lambda d: at(d, *GATEWAY, "contact", "event_types").clear())
    assert [str(f) for f in tool.unwaived(SMALL, none, waived)] == [
        'homeassistant > components > gateway > contact > event_types: no longer has "added"'
    ]


def test_the_snapshot_renders_canonically() -> None:
    text = tool.render(SMALL)
    assert text.startswith('{\n "cli": {\n  "columns": {')  # sorted keys, indent 1
    assert text.endswith("}\n")
    assert tool.render(json.loads(text)) == text


# --- the extractor's guards: each fails loudly instead of recording less --------------------------


def _option(*names: str, **kwargs: Any) -> argparse.Action:
    return argparse.ArgumentParser().add_argument(*names, **kwargs)


def _sensor(unique_id: str, platform: str = "sensor") -> dict[str, Any]:
    return {"p": platform, "unique_id": unique_id, "default_entity_id": f"{platform}.x"}


GUARDS: dict[str, tuple[Callable[[], object], str]] = {
    "a topic published in two ways": (
        lambda: interface._Topic({True, False}, {1}, {"json"}).record("t"),
        "t: published in more than one way",
    ),
    "a list with lines of two lengths": (
        lambda: interface._columns({"x": "a\tb\nc\n"}),
        "x: lines of 2, 1 fields",
    ),
    "an option type without a name": (
        lambda: interface._argument(_option("--n", type=int), required=False),
        "n: name the type",
    ),
    "a number of values without a count": (
        lambda: interface._fewest(_option("x", nargs=argparse.PARSER)),
        "x: name the nargs 'A...' in _FEWEST",
    ),
    "a bound the type function exceeds": (
        lambda: interface._bounds(cli._wait_seconds, interface._Type("seconds", 0.0, 60.0)),
        "seconds: its max is not 60.0",
    ),
    "a bound the type function refuses": (
        lambda: interface._bounds(cli._count, interface._Type("count", 1)),
        "count: its min is not 1",
    ),
    "a setting's bound the parser exceeds": (
        lambda: interface._parsed_bounds("mqtt.port", {"type": "integer", "min": 1, "max": 1000}),
        "mqtt.port: its max is not 1000: the parser takes 1001",
    ),
    "a setting's bound the parser refuses": (
        lambda: interface._parsed_bounds("airtime.burst_s", {"type": "number", "min": 4.0}),
        "airtime.burst_s: its min is not 4.0: the parser refuses it (airtime.burst_s: must be",
    ),
    "a bound of a list": (
        lambda: interface._parsed_bounds(
            "messages.ignore_channels", {"type": "list[integer]", "max": 7}
        ),
        "messages.ignore_channels: its max is not 7: the parser takes 8",
    ),
    "a bound of a setting read only when present": (  # in its table of MAXIMAL_TEXT
        lambda: interface._parsed_bounds(
            "targets.<alias>.channel", {"type": "integer", "max": 7, "conditional": True}
        ),
        "targets.<alias>.channel: its max is not 7: the parser takes 8",
    ),
    "a bound the table lacks": (
        lambda: interface._bounds(cli._wait_seconds, interface._Type("seconds", 0.0)),
        "seconds: refuses 1000000000: give it its max in _types()",
    ),
    "a unique ID not by key": (
        lambda: interface._components({"k": _sensor("other_k")}, "dev"),
        "k: the unique ID other_k is not by key",
    ),
    "two target aliases with other facts": (
        lambda: interface._components(
            {
                interface.ALIAS: _sensor(f"dev_{interface.ALIAS}"),
                interface.CHANNEL_ALIAS: _sensor(f"dev_{interface.CHANNEL_ALIAS}", "button"),
            },
            "dev",
        ),
        f"{interface.CHANNEL_ALIAS}: other facts than <alias>",
    ),
    "an attribute template without keys": (
        lambda: interface._component(
            _sensor("u") | {"json_attributes_template": "{{ value_json | tojson }}"}
        ),
        "u: no attribute keys in",
    ),
    "a field of the contact at its default": (
        lambda: interface._filled(Node(interface.KEY, "Alice", NodeType.CHAT)),
        (
            "fill Node.flags, Node.path_hops, Node.last_advert, Node.position in _tracked() in"
            " tests/public_interface.py"
        ),
    ),
    "a field inside the contact at its default": (
        lambda: interface._filled(
            dataclasses.replace(
                interface._tracked(NodeType.CHAT), heard=Heard(1.0, "message", Signal(hops=1))
            )
        ),
        "fill Signal.snr_db, Signal.rssi_dbm in _tracked()",
    ),
}


@pytest.mark.parametrize(("check", "expected"), GUARDS.values(), ids=GUARDS.keys())
def test_a_guard_of_the_extractor(check: Callable[[], object], expected: str) -> None:
    with pytest.raises(AssertionError, match=re.escape(expected)):
        check()


def test_a_range_the_parser_narrows_after_the_read_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The typed read takes 10 to 3600 s; a check after it would narrow that unseen."""
    read = configuration._mqtt

    def narrowed(section: Any) -> configuration.MqttConfig:
        mqtt = read(section)
        if mqtt.keepalive_s > 600:
            raise configuration.ConfigError("mqtt.keepalive_s: 600 at most")
        return mqtt

    monkeypatch.setattr(configuration, "_mqtt", narrowed)
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "mqtt.keepalive_s: its max is not 3600: the parser refuses it (mqtt.keepalive_s: 600"
            " at most)"
        ),
    ):
        interface.config()


def test_a_setting_that_another_one_allows_is_checked_with_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """radio.port only with the tcp transport, which MINIMAL lacks."""
    monkeypatch.delitem(interface._BESIDES, "radio.port")
    with pytest.raises(AssertionError, match=re.escape("set that in _BESIDES")):
        interface.config()


def test_the_snapshot_refuses_a_fixture_value_that_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(interface, "_client_id", lambda: "qleakq")
    with pytest.raises(AssertionError, match=re.escape("fixture values left in the snapshot")):
        interface.surface()


def test_the_snapshot_refuses_an_error_logged_on_the_way(monkeypatch: pytest.MonkeyPatch) -> None:
    def logging_client_id() -> str:
        logging.getLogger("meshcore_home_gateway.bridge").error("observer failed")
        return "<client_id>"

    monkeypatch.setattr(interface, "_client_id", logging_client_id)
    with pytest.raises(AssertionError, match="logged while computing the public interface"):
        interface.surface()


def test_contact_components_must_not_depend_on_the_node_type_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def payload(config: Any, topics: Any, tracked: Any, slug: str) -> dict[str, Any]:
        uid = contact_device_id(config, tracked.node.public_key)
        platform = "sensor" if tracked.node.node_type == NodeType.CHAT else "button"
        return {"cmps": {"snr": _sensor(f"{uid}_snr", platform)}}

    monkeypatch.setattr(interface, "contact_payload", payload)
    with pytest.raises(AssertionError, match="contact component snr: other facts for"):
        interface.homeassistant()


def test_an_answer_type_needs_a_typed_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(configuration._ANSWER_TYPES, "history.vacuum_days", int)
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "history.vacuum_days: has an answer type, but neither MINIMAL nor MAXIMAL_TEXT reads"
            " it through a typed method of config._Section: set it in MAXIMAL_TEXT"
        ),
    ):
        interface.config()


def test_a_setting_read_in_two_ways_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def twice(_: str) -> None:
        section = configuration._Section({}, "t")
        section.integer("n", 1, 0, 9)
        section.integer("n", 2, 0, 9)

    monkeypatch.setattr(interface, "parse_config", twice)
    with pytest.raises(AssertionError, match=re.escape("t.n: read as")):
        interface._reads("")


def test_a_parameter_the_recorder_reads_must_stay(monkeypatch: pytest.MonkeyPatch) -> None:
    """Renamed, a bound or the required flag would vanish from every setting as "compatible"."""

    def integer(self: Any, name: str, default: int, low: int, high: int) -> int:
        return default

    monkeypatch.setattr(configuration._Section, "integer", integer)
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "config._Section.integer no longer takes lo: update _KINDS and _setting in"
            " tests/public_interface.py"
        ),
    ):
        interface._reads(interface.MINIMAL)


def _probing_elsewhere(monkeypatch: pytest.MonkeyPatch, *commands: str, sub: str = "") -> None:
    """These commands, as the code calls them, keep their state in another directory: `sub`
    below $STATE_DIRECTORY if it is set and `sub` given, else one of their own."""
    for name in commands:
        command = getattr(cli, name)

        def elsewhere(args: argparse.Namespace, command: Callable[..., int] = command) -> int:
            if not sub:
                args.state_dir = Path("/srv/elsewhere")
            elif "STATE_DIRECTORY" in os.environ:
                args.state_dir = Path(os.environ["STATE_DIRECTORY"]) / sub
            return command(args)

        monkeypatch.setattr(cli, name, elsewhere)


def test_run_and_probe_must_use_one_state_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _probing_elsewhere(monkeypatch, "_probe")
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "run uses the state directory /var/lib/meshcore-home-gateway, probe /srv/elsewhere:"
            " record each"
        ),
    ):
        interface._state_directory(tmp_path, interface._probe_report(tmp_path))


def test_the_state_directory_must_be_the_one_the_variable_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _probing_elsewhere(monkeypatch, "_run", "_probe", sub="gateway")
    with pytest.raises(
        AssertionError,
        match=re.escape(f"STATE_DIRECTORY={tmp_path / 'named'}: state in {tmp_path}/named/gateway"),
    ):
        interface._state_directory(tmp_path, interface._probe_report(tmp_path))


def test_config_answers_must_print_every_setting_with_an_answer_type(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The installer writes the configuration back from them (`--reconfigure`)."""
    monkeypatch.setitem(configuration._ANSWER_TYPES, "history.vacuum_days", int)
    with pytest.raises(
        AssertionError,
        match=re.escape(
            "config answers prints no line for ['history.vacuum_days']: set each in ANSWERED in"
            " tests/public_interface.py"
        ),
    ):
        interface._answers(tmp_path)
    monkeypatch.delitem(configuration._ANSWER_TYPES, "history.vacuum_days")
    monkeypatch.delitem(configuration._ANSWER_TYPES, "mqtt.password")
    with pytest.raises(
        AssertionError,
        match=re.escape("config answers prints ['mqtt.password'], which have no answer type"),
    ):
        interface._answers(tmp_path)


def _refused(_: list[str]) -> int:
    raise SystemExit(cli.EX_USAGE)  # as argparse does


@pytest.mark.parametrize(
    ("main", "status"),
    [(lambda _: cli.EX_FAILURE, cli.EX_FAILURE), (lambda _: cli.EX_OK, cli.EX_OK), (_refused, 2)],
)
def test_a_command_must_succeed_and_print(
    monkeypatch: pytest.MonkeyPatch, main: Callable[[list[str]], int], status: int
) -> None:
    monkeypatch.setattr(cli, "main", main)
    with pytest.raises(AssertionError, match=re.escape(f"x: exit {status}, 0 characters")):
        interface._printed(["x"])


def test_the_bus_must_set_a_last_will(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(interface._Paho, "will_set", lambda *_, **__: None)
    with pytest.raises(AssertionError, match="MqttBus set no last will"):
        interface.mqtt(tmp_path)


@dataclasses.dataclass
class _Annotated:
    factory: Transport | None  # a name for type checking only, as config.py may import some
    mode: Literal["auto", "always"] = "auto"


def test_a_choice_is_read_from_its_own_annotation_only() -> None:
    assert interface._literal(_Annotated, "mode") == ["auto", "always"]
