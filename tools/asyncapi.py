"""The MQTT API as an AsyncAPI 3.1.0 document (EXT-004, issue #23).

docs/mqtt-api.asyncapi.yaml is generated from the interface snapshot, tests/public_interface.json,
and the package's version, so that it cannot drift from the code (`make interface` writes both):
every topic the gateway publishes (operations `send`) and reads (`receive`), with QoS and retain,
the parameters of the topics, and the keys of each JSON document. docs/mqtt-api.md stays
normative: it gives what each key means and its type.

What the snapshot does not record, the document leaves open rather than guess: a key without a
type, since every value may be null ("Unknown values are null"), and no `enum`, since every list
of values grows within a major version. `x-values` lists the values the gateway uses at this
release, in a key or in the items of a list. The document is JSON, which every YAML 1.2 parser
reads, so neither this tool nor its tests need a YAML library (ADR 0005).

Standard library only; it reads the version from the package's source, without importing it.

Usage: python3 tools/asyncapi.py [--write] [--release X.Y.Z]  (without --write: exit 1 if the
document is stale; `make release` names the version it builds)
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "tests" / "public_interface.json"
DOCUMENT = ROOT / "docs" / "mqtt-api.asyncapi.yaml"
PACKAGE = ROOT / "src" / "meshcore_home_gateway" / "__init__.py"
SPECIFICATION = "3.1.0"
MQTT_BINDING = "0.2.0"
REPOSITORY = "https://github.com/dkuenzel/meshcore-home-gateway"
OURS = "{base_topic}/{node_id}/"  # the gateway's own topics hang below it
JSON, TEXT = "application/json", "text/plain"

# the parameters of the addresses, as the configuration names them
PARAMETERS: dict[str, dict[str, Any]] = {
    "base_topic": {"description": "`[mqtt] base_topic`", "default": "meshcore"},
    "node_id": {"description": "`[mqtt] node_id`", "default": "radio1"},
    "key": {"description": "a contact's full public key: 64 hex digits in lower case"},
    "alias": {"description": "the alias of a target, `[targets.<alias>]`"},
    "prefix": {"description": "`[homeassistant] prefix`", "default": "homeassistant"},
    "device": {
        "description": "the gateway's device in Home Assistant, `meshcore_home_gateway_<node_id>`",
        "examples": ["meshcore_home_gateway_radio1"],
    },
    "component": {
        "description": "a device on the gateway's: `contact_<key>` or `manage_contacts`",
        "examples": ["manage_contacts"],
    },
    "status_topic": {
        "description": "`[homeassistant] status_topic`: Home Assistant's birth message",
        "default": "homeassistant/status",
    },
    "homie_device": {
        "description": "the gateway's Homie device, `meshcore-<node_id>` with `_` as `-`",
        "examples": ["meshcore-radio1"],
    },
}
# what the requests carry, as the topic table of docs/mqtt-api.md gives it: (content, summary);
# none of them may be retained
REQUESTS: dict[str, tuple[str | None, str]] = {
    "send/{alias}": (TEXT, "UTF-8 text: a message to the target"),
    "contacts/{key}/send": (TEXT, "UTF-8 text for a companion in the contact table"),
    "contacts/{key}/remove": (
        None,
        'any payload, or JSON {"id": "..."}: remove that contact from the radio',
    ),
    "contacts/{key}/telemetry": (
        None,
        (
            '`on` or `off`, or JSON {"telemetry": "off", "id": "...", "forget": true}: ask a '
            "companion for its telemetry, or exclude it"
        ),
    ),
    "add_contact": (
        None,
        (
            'a public key, contact link or contact card, or JSON {"contact": "...", "id": "..."}: '
            "add a contact to the radio"
        ),
    ),
    "homeassistant/add_node": (
        TEXT,
        "the label of a heard node, as Home Assistant's select offers it: add it to the radio",
    ),
    "homeassistant/show_hidden": (
        None,
        "any payload: show the contacts deleted in Home Assistant again",
    ),
    "homie/{homie_device}/{key}/send/set": (
        TEXT,
        "UTF-8 text for that companion, as on `contacts/<key>/send`",
    ),
}
BIRTH = "Home Assistant's birth message: after `online` the gateway announces its devices again"
READ_BACK = "the gateway reads its own topic, to undo what another client writes there"
WATCHED = "the gateway reads it, to notice when Home Assistant clears it"
DISCOVERY = "Home Assistant's device discovery (docs/home-assistant.md)"
CLEARED = {
    "contacts/{key}": "an empty retained message clears it when the contact leaves the table",
    "{prefix}/device/{device}/{component}/config": "an empty message removes the device",
}
HOMIE = "homie/{homie_device}/"  # the topics of the Homie device (docs/mqtt-api.md, Homie)
HOMIE_CLEARED = "an empty retained message: none, an unknown value, or a node that is gone"


def version() -> str:
    """`__version__` of the package, read from its source."""
    for node in ast.parse(PACKAGE.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and [getattr(t, "id", "") for t in node.targets] == [
            "__version__"
        ]:
            value = ast.literal_eval(node.value)
            if isinstance(value, str):
                return value
    raise SystemExit(f"{PACKAGE}: no __version__")


def address(topic: str) -> str:
    """A topic of the snapshot as a channel address: `<base_topic>/<node_id>/contacts/+` is
    `{base_topic}/{node_id}/contacts/{key}`; below the gateway's device in Home Assistant, the
    device on it is the parameter `component`."""
    parts = topic.split("/")
    out = []
    for index, part in enumerate(parts):
        before = parts[index - 1] if index else ""
        if part == "+" and parts[0] == "homie":  # a node of the Homie device: a contact's key
            out.append("{key}")
        elif part == "+":
            out.append({"contacts": "{key}", "send": "{alias}"}.get(before, "{component}"))
        elif before == "<device>" and part != "config":
            out.append("{component}")
        else:
            out.append(re.sub(r"<(\w+)>", r"{\1}", part))
    return "/".join(out)


def channel_id(path: str) -> str:
    """`{base_topic}/{node_id}/contacts/{key}/received` is `contacts_key_received`; the
    discovery topics are `discovery` and `discovery_component`, the Homie device's `homie_...`."""
    if path.startswith("{prefix}/device/{device}/"):
        path = "discovery/" + path.removeprefix("{prefix}/device/{device}/")
        path = path.removesuffix("/config")
    elif path.startswith(HOMIE):
        path = "homie/" + path.removeprefix(HOMIE)
    return "_".join(re.findall(r"[a-z0-9]+", path.removeprefix(OURS)))


def schema(paths: list[str], values: dict[str, list[str]], name: str) -> dict[str, Any]:
    """The schema of a document from its key paths (`a.b`, `a[].b`) and its lists of values
    (`a.b`, `a.*` for each key of the object `a`)."""
    root: dict[str, Any] = {"type": "object", "properties": {}}
    for path in sorted(paths):
        node = root
        for step in path.split("."):
            if "type" not in node:  # an object in a document; one in a list is never null
                node["type"] = ["object", "null"]
            node = node.setdefault("properties", {}).setdefault(step.removesuffix("[]"), {})
            if step.endswith("[]"):
                node["type"] = ["array", "null"]
                node = node.setdefault("items", {"type": "object"})
    for path, listed in sorted(values.items()):
        targets = _resolve(root, path)
        if not targets:
            raise SystemExit(f"{name}: values of {path}, a key the snapshot does not list")
        for target in targets:
            if "properties" not in target:  # the keys of an object are its properties
                target["x-values"] = sorted(listed)
    return root


def _resolve(root: dict[str, Any], path: str) -> list[dict[str, Any]]:
    """The schemas at a value path: `a.b` one, `a.*` each property of `a`; a list's items
    stand for the list."""
    nodes = [root]
    for step in path.split("."):
        found = []
        for node in nodes:
            properties = node.get("items", node).get("properties", {})
            found += list(properties.values()) if step == "*" else [properties.get(step)]
        nodes = [n for n in found if n is not None]
    return nodes


def _published(
    topic: str, entry: dict[str, Any], mqtt: dict[str, Any], short: str
) -> tuple[dict[str, Any], str]:
    """The payload of a topic the gateway publishes, and a summary of what it is."""
    documents: dict[str, list[str]] = mqtt["documents"]
    values: dict[str, list[str]] = mqtt["values"]
    known = topic.removeprefix("<base_topic>/<node_id>/")  # as the snapshot names it
    if known in documents:
        prefix = f"{known}:"
        listed = {k.removeprefix(prefix): v for k, v in values.items() if k.startswith(prefix)}
        payload = schema(documents[known], listed, known)
        summary = "a document" if entry["retained"] else "an event"
    elif entry["payload"] == "json":
        payload, summary = {"type": "object"}, DISCOVERY
    else:
        listed_text = {"x-values": sorted(values[known])} if known in values else {}
        payload, summary = {"type": "string"} | listed_text, "text"
    why = CLEARED.get(short) or (HOMIE_CLEARED if short.startswith(HOMIE) else "")
    if entry.get("cleared") and why:
        summary += f"; {why}"
    return payload, summary


def document(snapshot: dict[str, Any], release: str) -> dict[str, Any]:
    mqtt = snapshot["mqtt"]
    channels: dict[str, dict[str, Any]] = {}
    operations: dict[str, dict[str, Any]] = {}

    def channel(topic: str) -> tuple[str, dict[str, Any], str]:
        path = address(topic)
        name = channel_id(path)
        found = channels.setdefault(name, {"address": path, "messages": {}})
        if found["address"] != path:
            raise SystemExit(f"two channels named {name}: {found['address']}, {path}")
        if parameters := re.findall(r"\{(\w+)\}", path):
            found["parameters"] = {p: PARAMETERS[p] for p in parameters}
        return name, found, path.removeprefix(OURS)

    def message(found: dict[str, Any], name: str, content: str | None, **fields: Any) -> None:
        found["messages"][name] = (
            {"name": name} | ({"contentType": content} if content else {}) | fields
        )

    for topic, entry in sorted(mqtt["publish"].items()):
        name, found, short = channel(topic)
        payload, summary = _published(topic, entry, mqtt, short)
        if not found["messages"]:  # the first of the topics a channel stands for
            content = TEXT if entry["payload"] == "text" else JSON
            message(found, name, content, summary=summary, payload=payload)
        operation: dict[str, Any] = {
            "action": "send",
            "channel": {"$ref": f"#/channels/{name}"},
            "bindings": {
                "mqtt": {
                    "qos": entry["qos"],
                    "retain": entry["retained"],
                    "bindingVersion": MQTT_BINDING,
                }
            },
        }
        if operations.setdefault(f"send_{name}", operation) != operation:
            raise SystemExit(f"{topic}: published unlike the other topics of {found['address']}")
    for topic, entry in sorted(mqtt["subscribe"].items()):
        name, found, short = channel(topic)
        if short == "{status_topic}":
            message(found, name, TEXT, summary=BIRTH, payload={"type": "string"})
            summary = "Home Assistant comes online"
        elif found["messages"]:
            summary = WATCHED if short.startswith("{prefix}/") else READ_BACK
        else:
            kind, text = REQUESTS[short]
            message(found, name, kind, summary=f"{text}; never retained", payload={})
            summary = "a request"
        operations[f"receive_{name}"] = {
            "action": "receive",
            "channel": {"$ref": f"#/channels/{name}"},
            "summary": summary,
            "bindings": {"mqtt": {"qos": entry["qos"], "bindingVersion": MQTT_BINDING}},
        }
    for will_topic, will in mqtt["last_will"].items():  # the gateway's, the Homie device's
        channels[channel_id(address(will_topic))]["description"] = (
            f"The last will of a connection: `{will['payload']}`, "
            f"{'retained' if will['retained'] else 'not retained'}, QoS {will['qos']}."
        )
    for each in operations.values():  # each operation carries its channel's messages
        name = each["channel"]["$ref"].removeprefix("#/channels/")
        each["messages"] = [
            {"$ref": f"#/channels/{name}/messages/{m}"} for m in channels[name]["messages"]
        ]
    return {
        "asyncapi": SPECIFICATION,
        "id": "urn:meshcore-home-gateway:mqtt-api",
        "info": {
            "title": "meshcore-home-gateway MQTT API",
            "version": release,
            "description": (
                "The topics the gateway publishes and reads, generated from its interface "
                "snapshot by tools/asyncapi.py. docs/mqtt-api.md is normative: it gives what "
                "each key means and its type. Every value may be null. `x-values` lists the "
                "values the gateway uses at this release, in a key or in the items of a list; "
                "within a major version keys and values are only added: ignore a key you do "
                "not know, and treat a value you do not know as other."
            ),
            "license": {"name": "MIT", "url": f"{REPOSITORY}/blob/main/LICENSE"},
            "externalDocs": {"url": f"{REPOSITORY}/blob/v{release}/docs/mqtt-api.md"},
        },
        "servers": {
            "broker": {
                "host": "{host}",
                "protocol": "mqtt",
                "protocolVersion": "3.1.1",
                "description": 'The broker of `[mqtt]`; with `protocol = "5"` it speaks MQTT 5.',
                "variables": {
                    "host": {
                        "description": "`[mqtt] host` and `port`",
                        "examples": ["broker.example.org:8883"],
                    }
                },
            }
        },
        "channels": dict(sorted(channels.items())),
        "operations": dict(sorted(operations.items())),
    }


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=True) + "\n"


def generated(release: str | None = None) -> str:
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    return render(document(snapshot, release or version()))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write or check docs/mqtt-api.asyncapi.yaml.")
    parser.add_argument("--write", action="store_true", help="write the document")
    parser.add_argument(
        "--release", metavar="X.Y.Z", help="the version it is for (default: the package's)"
    )
    args = parser.parse_args(argv)
    text = generated(args.release)
    shown = DOCUMENT.relative_to(ROOT)
    if args.write:
        DOCUMENT.write_text(text, encoding="utf-8")
        print(f"wrote {shown}")
    elif not DOCUMENT.exists() or DOCUMENT.read_text(encoding="utf-8") != text:
        print(f"{shown} is not the snapshot's: make interface", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
