"""docs/mqtt-api.asyncapi.yaml, the MQTT API for machines (EXT-004, issue #23): generated from the
interface snapshot, which tests/test_public_interface.py holds to the code, so neither can drift;
checked here against the snapshot by rules of its own and against the AsyncAPI document's rules."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import meshcore_home_gateway

ROOT = Path(__file__).resolve().parent.parent
DOCUMENT = ROOT / "docs" / "mqtt-api.asyncapi.yaml"
SNAPSHOT = json.loads((ROOT / "tests" / "public_interface.json").read_text(encoding="utf-8"))


def _load_tool() -> ModuleType:
    spec = importlib.util.spec_from_file_location("asyncapi", ROOT / "tools" / "asyncapi.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["asyncapi"] = module
    spec.loader.exec_module(module)
    return module


tool = _load_tool()


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    assert len(keys) == len(set(keys)), f"a key twice: {keys}"
    return dict(pairs)


DATA: dict[str, Any] = json.loads(DOCUMENT.read_text(encoding="utf-8"), object_pairs_hook=_unique)


def test_the_document_is_the_snapshots(capsys: pytest.CaptureFixture[str]) -> None:
    """After a change of the interface `make interface` writes it with the snapshot."""
    assert tool.main([]) == 0, capsys.readouterr().err


def test_a_stale_document_is_refused_and_written_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "mqtt-api.asyncapi.yaml"
    stale.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(tool, "DOCUMENT", stale)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    assert tool.main([]) == 1
    assert "mqtt-api.asyncapi.yaml is not the snapshot's: make interface" in capsys.readouterr().err
    assert tool.main(["--write"]) == 0
    assert stale.read_text(encoding="utf-8") == DOCUMENT.read_text(encoding="utf-8")
    stale.unlink()
    assert tool.main([]) == 1


def test_the_document_names_the_release() -> None:
    """A consumer pins it per tag: the tool reads `__version__` without importing the package."""
    assert DATA["asyncapi"] == "3.1.0"
    assert DATA["info"]["version"] == meshcore_home_gateway.__version__ == tool.version()
    assert DATA["info"]["externalDocs"]["url"].endswith(
        f"/blob/v{meshcore_home_gateway.__version__}/docs/mqtt-api.md"
    )


def test_it_is_yaml_as_every_parser_reads_it() -> None:
    """JSON, which YAML 1.2 contains: ASCII, no tabs, no key twice (DATA's hook)."""
    text = DOCUMENT.read_text(encoding="utf-8")
    assert text.isascii()
    assert "\t" not in text


def _matches(address: str, topic: str) -> bool:
    """Whether a topic of the snapshot (`<x>` and `+` as it writes them) is one of the address,
    each parameter one level or a placeholder of the snapshot."""
    pattern = re.sub(r"\\\{\w+\\\}", "[^/]+", re.escape(address))
    return re.fullmatch(pattern, topic) is not None


def _operations(action: str) -> dict[str, dict[str, Any]]:
    """The operations of an action by the address of their channel."""
    found = {}
    for operation in DATA["operations"].values():
        if operation["action"] == action:
            channel = operation["channel"]["$ref"].removeprefix("#/channels/")
            found[DATA["channels"][channel]["address"]] = operation
    return found


@pytest.mark.parametrize(("direction", "action"), [("publish", "send"), ("subscribe", "receive")])
def test_every_topic_of_the_code_has_its_operation(direction: str, action: str) -> None:
    """Each topic the gateway publishes is one channel with a `send` operation of its QoS and
    retain flag; each it subscribes to, one with `receive`; and no operation is more."""
    operations = _operations(action)
    used = set()
    for topic, entry in SNAPSHOT["mqtt"][direction].items():
        [address] = [a for a in operations if _matches(a, topic)] or [f"none for {topic}"]
        mqtt = operations[address]["bindings"]["mqtt"]
        assert mqtt["qos"] == entry["qos"], topic
        if direction == "publish":
            assert mqtt["retain"] is entry["retained"], topic
        used.add(address)
    assert used == set(operations)


def test_each_last_will_is_described_on_its_channel() -> None:
    """The gateway's (`availability`) and the Homie device's (`$state`)."""
    wills = SNAPSHOT["mqtt"]["last_will"].items()
    assert len(wills) == 2
    for topic, will in wills:
        [channel] = [c for c in DATA["channels"].values() if _matches(c["address"], topic)]
        assert f"`{will['payload']}`, retained, QoS {will['qos']}" in channel["description"]


def test_a_topic_the_gateway_clears_says_so() -> None:
    """An empty message clears a retained document, or removes a device from Home Assistant."""
    cleared = [t for t, entry in SNAPSHOT["mqtt"]["publish"].items() if entry.get("cleared")]
    assert cleared
    for topic in cleared:
        [channel] = [c for c in DATA["channels"].values() if _matches(c["address"], topic)]
        [message] = channel["messages"].values()
        assert "an empty" in message["summary"], topic


def _keys(schema: dict[str, Any], prefix: str = "") -> set[str]:
    """The key paths of a schema as the snapshot writes them: `a.b`, `a[].b`."""
    paths = set()
    for name, child in schema.get("properties", {}).items():
        path = f"{prefix}.{name}" if prefix else name
        paths.add(path)
        if "items" in child:
            paths |= _keys(child["items"], f"{path}[]")
        paths |= _keys(child, path)
    return paths


def _payload(topic: str) -> dict[str, Any]:
    [channel] = [
        c
        for c in DATA["channels"].values()
        if _matches(c["address"], f"<base_topic>/<node_id>/{topic}")
    ]
    [message] = channel["messages"].values()
    payload: dict[str, Any] = message["payload"]
    return payload


@pytest.mark.parametrize("name", sorted(SNAPSHOT["mqtt"]["documents"]))
def test_each_document_has_the_keys_of_the_snapshot(name: str) -> None:
    payload = _payload(name)
    assert payload["type"] == "object"
    assert _keys(payload) == set(SNAPSHOT["mqtt"]["documents"][name])


def _at(schema: dict[str, Any], path: str) -> list[dict[str, Any]]:
    nodes = [schema]
    for step in path.split("."):
        found = []
        for node in nodes:
            properties = node.get("items", node).get("properties", {})
            found += list(properties.values()) if step == "*" else [properties[step]]
        nodes = found
    return nodes


@pytest.mark.parametrize("name", sorted(SNAPSHOT["mqtt"]["values"]))
def test_each_list_of_values_is_in_the_document(name: str) -> None:
    """In `x-values` of its key, or of each key for `a.*`; the keys of an object are its
    properties."""
    listed = sorted(SNAPSHOT["mqtt"]["values"][name])
    document, _, path = name.partition(":")
    payload = _payload(document)
    for node in _at(payload, path) if path else [payload]:
        if "properties" in node:
            assert sorted(node["properties"]) == listed
        else:
            assert node["x-values"] == listed


def test_no_list_of_values_is_closed() -> None:
    """Never an `enum`: every list of values grows within a major version (docs/mqtt-api.md)."""
    assert '"enum"' not in DOCUMENT.read_text(encoding="utf-8")


def _references(value: Any) -> list[str]:
    if isinstance(value, dict):
        own = [value["$ref"]] if "$ref" in value else []
        return own + [r for v in value.values() for r in _references(v)]
    if isinstance(value, list):
        return [r for v in value for r in _references(v)]
    return []


def test_every_reference_resolves_and_an_operation_carries_its_channels_messages() -> None:
    for reference in _references(DATA):
        node: Any = DATA
        for step in reference.removeprefix("#/").split("/"):
            node = node[step]
    for operation in DATA["operations"].values():
        channel = operation["channel"]["$ref"]
        assert operation["action"] in {"send", "receive"}
        assert operation["messages"]
        assert all(m["$ref"].startswith(f"{channel}/messages/") for m in operation["messages"])


def test_every_parameter_of_an_address_is_declared() -> None:
    """The AsyncAPI rule: a channel's parameters are exactly those of its address."""
    for channel in DATA["channels"].values():
        named = set(re.findall(r"\{(\w+)\}", channel["address"]))
        assert set(channel.get("parameters", {})) == named, channel["address"]
        assert all(p["description"] for p in channel.get("parameters", {}).values())
    for server in DATA["servers"].values():
        assert set(re.findall(r"\{(\w+)\}", server["host"])) == set(server["variables"])


def test_a_request_is_described_as_the_topic_table_does() -> None:
    """Each topic the gateway only reads has a summary; tools/asyncapi.py has them from the
    table of docs/mqtt-api.md, which names each one."""
    table = (ROOT / "docs" / "mqtt-api.md").read_text(encoding="utf-8")
    for short in tool.REQUESTS:
        assert f"| `{short.replace('{', '<').replace('}', '>')}` | → gateway |" in table, short
