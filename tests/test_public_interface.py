"""The public interface (docs/development.md#compatibility): the committed snapshot is what the
code offers, and the comparator tells growth from breakage. The comparator's tests use small
synthetic snapshots, so that a legitimate removal from the real one never breaks them."""

from __future__ import annotations

import copy
import json
import logging
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from meshcore_home_gateway.mqtt.topics import Topics
from tests import public_interface as interface

tool = interface.tool


@pytest.fixture(scope="module")
def current() -> dict[str, Any]:
    return interface.surface()


# --- the snapshot is the code ---------------------------------------------------------------------


def test_the_snapshot_is_the_code(current: dict[str, Any]) -> None:
    committed = interface.SNAPSHOT.read_text(encoding="utf-8")
    if tool.render(current) != committed:
        pytest.fail(interface.explain(tool.compare(json.loads(committed), current)), pytrace=False)


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
    assert interface.placeholders("sensor.qslugq_snr") == "sensor.<slug>_snr"
    assert interface.placeholders(f"{interface.BASE}/{interface.NODE}/state") == (
        "<base_topic>/<node_id>/state"
    )


def test_a_fixture_value_left_in_the_snapshot_is_refused() -> None:
    with pytest.raises(AssertionError, match="qmissedq"):
        interface.refuse_leftovers({"mqtt": {"documents": {"qmissedq": []}}})
    with pytest.raises(AssertionError, match=interface.KEY[:8]):
        interface.refuse_leftovers(
            {"homeassistant": {"devices": {"contact": {"id": interface.KEY}}}}
        )


def test_an_error_logged_while_computing_is_refused() -> None:
    """Bridge._observe logs an exception instead of raising it: a broken path would only show
    as a missing topic."""
    with pytest.raises(AssertionError, match="observer failed"), interface.refusing_errors():
        logging.getLogger("meshcore_home_gateway.bridge").error("observer failed")


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
    assert 'mqtt > documents > state: no longer has ["battery_mv"]' in err
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
    assert "cannot be compared with the code: snapshot format 0" in capsys.readouterr().err
    assert interface.main(["--write"]) == 0
    assert "cannot be compared" in capsys.readouterr().out
    assert path.read_text(encoding="utf-8") == tool.render(current)


def test_a_difference_in_form_only_is_explained() -> None:
    assert "only in its form" in interface.explain([])


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
        "subscribe": [B + "send/+", "<status_topic>"],
        "last_will": {B + "availability": "offline"},
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
        "mqtt.keepalive_s": {"type": "integer", "default": 60, "min": 10, "max": 3600},
        "mqtt.port": {"type": "integer", "default": 1883, "min": 1, "max": 65535, "answer": "int"},
        "mqtt.username": {"type": "string", "answer": "str"},
        "radio.transport": {"type": "string", "required": True, "choices": ["serial", "tcp"]},
        "targets.<alias>.channel": {"type": "integer", "min": 0, "max": 255, "conditional": True},
    },
    "cli": {
        "commands": {
            "meshcore-home-gateway": {
                "options": {"--version": {"output": "meshcore-home-gateway {version}"}},
                "positionals": {},
                "commands": ["listen", "probe", "send"],
            },
            "listen": {"options": {}, "positionals": {}},
            "probe": {
                "options": {
                    "--json": {"takes_value": False},
                    "--list": {"takes_value": True, "choices": ["channels", "contacts"]},
                },
                "positionals": {},
            },
            "send": {
                "options": {"--wait": {"takes_value": True, "type": "seconds", "default": 0.0}},
                "positionals": {
                    "1": {"takes_value": True, "required": True},
                    "2": {"takes_value": True, "required": True},
                },
            },
        },
        "exit_codes": {"ok": 0, "failure": 1, "usage": 2, "config_error": 78},
        "paths": {"config": "/etc/meshcore-home-gateway/config.toml"},
        "columns": {"probe --list contacts": 3},
        "json": {"probe --json": ["battery_mv", "name"]},
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
    },
}

Mutation = Callable[[dict[str, Any]], object]


def at(doc: dict[str, Any], *path: str) -> Any:
    for key in path:
        doc = doc[key]
    return doc


PUBLISH, CONFIG = ("mqtt", "publish"), ("config",)
SEND, PROBE = ("cli", "commands", "send"), ("cli", "commands", "probe")
GATEWAY = ("homeassistant", "components", "gateway")

BREAKING: dict[str, Mutation] = {
    "topic removed": lambda d: at(d, *PUBLISH).pop(B + "rx"),
    "retained flipped": lambda d: at(d, *PUBLISH, B + "state").update(retained=False),
    "qos changed": lambda d: at(d, *PUBLISH, B + "command").update(qos=1),
    "no longer cleared": lambda d: at(d, *PUBLISH, B + "contacts/<key>").pop("cleared"),
    "subscription removed": lambda d: d["mqtt"]["subscribe"].remove(B + "send/+"),
    "last will changed": lambda d: d["mqtt"]["last_will"].update({B + "availability": "gone"}),
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
    "new and required": lambda d: at(d, *CONFIG).update(
        {"mqtt.realm": {"type": "string", "required": True}}
    ),
    "identity default": lambda d: at(d, *CONFIG, "mqtt.base_topic").update(default="mesh"),
    "answer type": lambda d: at(d, *CONFIG, "mqtt.port").update(answer="str"),
    "command removed": lambda d: d["cli"]["commands"].pop("listen"),
    "option removed": lambda d: at(d, *SEND, "options").pop("--wait"),
    "flag takes a value": lambda d: at(d, *PROBE, "options", "--json").update(takes_value=True),
    "new required option": lambda d: at(d, *SEND, "options").update(
        {"--to": {"takes_value": True, "required": True}}
    ),
    "new required positional": lambda d: at(d, *SEND, "positionals").update(
        {"3": {"takes_value": True, "required": True}}
    ),
    "cli choice removed": lambda d: at(d, *PROBE, "options", "--list", "choices").remove(
        "contacts"
    ),
    "column appended": lambda d: d["cli"]["columns"].update({"probe --list contacts": 4}),
    "exit code": lambda d: d["cli"]["exit_codes"].update(config_error=1),
    "version format": lambda d: at(
        d, "cli", "commands", "meshcore-home-gateway", "options", "--version"
    ).update(output="{version}"),
    "default path": lambda d: d["cli"]["paths"].update(config="/etc/mhg.toml"),
    "probe json key removed": lambda d: d["cli"]["json"]["probe --json"].remove("battery_mv"),
    "component removed": lambda d: at(d, *GATEWAY).pop("battery"),
    "entity id changed": lambda d: at(d, *GATEWAY, "battery").update(entity_id="sensor.x"),
    "platform changed": lambda d: at(d, *GATEWAY, "battery").update(platform="number"),
    "unit changed": lambda d: at(d, *GATEWAY, "battery").update(unit="mV"),
    "event type removed": lambda d: at(d, *GATEWAY, "contact", "event_types").remove("refused"),
    "attribute key removed": lambda d: at(d, *GATEWAY, "airtime").update(attributes=[]),
    "attributes of a document no longer": lambda d: at(d, *GATEWAY, "contact").pop("attributes"),
    "node type removed": lambda d: at(
        d, "homeassistant", "components", "contact", "send", "node_types"
    ).remove("chat"),
    "device id": lambda d: d["homeassistant"]["devices"]["gateway"].update(id="mhg_<node_id>"),
    "section removed": lambda d: d.pop("homeassistant"),
}

COMPATIBLE: dict[str, Mutation] = {
    "topic added": lambda d: at(d, *PUBLISH).update(
        {B + "telemetry": {"retained": True, "qos": 1, "payload": "json"}}
    ),
    "now cleared": lambda d: at(d, *PUBLISH, B + "nodes").update(cleared=True),
    "subscription added": lambda d: d["mqtt"]["subscribe"].append(B + "trace/+"),
    "document key added": lambda d: d["mqtt"]["documents"]["state"].append("uptime_s"),
    "value added": lambda d: d["mqtt"]["values"]["delivery:event_type"].append("deferred"),
    "setting added": lambda d: at(d, *CONFIG).update({"mqtt.realm": {"type": "string"}}),
    "maximum raised": lambda d: at(d, *CONFIG, "airtime.burst_s").update(max=7200.0),
    "minimum lowered": lambda d: at(d, *CONFIG, "airtime.burst_s").update(min=1.0),
    "bound dropped": lambda d: at(d, *CONFIG, "messages.queue_size").pop("max"),
    "integer to number": lambda d: at(d, *CONFIG, "mqtt.keepalive_s").update(type="number"),
    "choice added": lambda d: at(d, *CONFIG, "radio.transport", "choices").append("ble"),
    "choices dropped": lambda d: at(d, *CONFIG, "radio.transport").pop("choices"),
    "no longer required": lambda d: at(d, *CONFIG, "radio.transport").update(required=False),
    "default changed": lambda d: at(d, *CONFIG, "messages.queue_size").update(default=50),
    "command added": lambda d: d["cli"]["commands"].update(
        {"trace": {"options": {}, "positionals": {}}}
    ),
    "option added": lambda d: at(d, *SEND, "options").update({"--channel": {"takes_value": True}}),
    "optional positional": lambda d: at(d, *SEND, "positionals").update(
        {"3": {"takes_value": True, "nargs": "?"}}
    ),
    "cli choice added": lambda d: at(d, *PROBE, "options", "--list", "choices").append("profile"),
    "cli default": lambda d: at(d, *SEND, "options", "--wait").update(default=5.0),
    "column list added": lambda d: d["cli"]["columns"].update({"probe --list nodes": 3}),
    "probe json key added": lambda d: d["cli"]["json"]["probe --json"].append("uptime_s"),
    "component added": lambda d: at(d, *GATEWAY).update(
        {"uptime": {"platform": "sensor", "entity_id": "sensor.<device>_uptime"}}
    ),
    "event type added": lambda d: at(d, *GATEWAY, "contact", "event_types").append("linked"),
    "attribute key added": lambda d: at(d, *GATEWAY, "airtime", "attributes").append("burst_s"),
    "node type added": lambda d: at(
        d, "homeassistant", "components", "contact", "send", "node_types"
    ).append("room_server"),
    "unit tracked since": lambda d: at(d, *GATEWAY, "profile").update(unit="x"),
    "section added": lambda d: d.update({"platforms": {"python": "3.11"}}),
}
# compatible, but users notice them: listed under Changed; the others are additions
CHANGED = {
    "maximum raised", "minimum lowered", "bound dropped", "integer to number", "choices dropped",
    "no longer required", "default changed", "cli default",
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
    assert [f.kind for f in found] == ["changed" if name in CHANGED else "added"], found


def test_identity_and_condition_are_not_compared() -> None:
    def change(d: dict[str, Any]) -> None:
        d["config"]["targets.<alias>.channel"].pop("conditional")
        d["config"]["mqtt.username"]["identity"] = True

    assert tool.compare(SMALL, mutated(change)) == []


def test_a_list_in_another_order_is_no_change() -> None:
    def reorder(d: dict[str, Any]) -> None:
        d["mqtt"]["subscribe"].reverse()
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


def test_another_format_is_refused() -> None:
    with pytest.raises(tool.FormatError, match="format 2, this tool reads 1"):
        tool.compare(SMALL, {**SMALL, "format": 2})


def test_a_waiver_names_a_finding_by_its_path() -> None:
    new = mutated(BREAKING["topic removed"])
    [finding] = tool.breaking(SMALL, new, {})
    assert finding.path == f"mqtt > publish > {B}rx"
    assert tool.breaking(SMALL, new, {finding.path: "never published"}) == []


def test_the_snapshot_renders_canonically() -> None:
    text = tool.render(SMALL)
    assert text.startswith('{\n "cli": {\n  "columns": {')  # sorted keys, indent 1
    assert text.endswith("}\n")
    assert tool.render(json.loads(text)) == text
