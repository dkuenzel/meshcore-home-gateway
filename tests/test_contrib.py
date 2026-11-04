"""contrib/mqtt2prometheus (EXT-013): every field the exporter's configurations read is a field of
the gateway's documents as the interface snapshot has them, every value of a mapped field has a
number, and each topic path is one the gateway publishes."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
CONTRIB = ROOT / "contrib" / "mqtt2prometheus"
MQTT = json.loads((ROOT / "tests" / "public_interface.json").read_text(encoding="utf-8"))["mqtt"]
FIELD = re.compile(r"^    (\w+): (.+)$")
MAPPED = re.compile(r"^        (\"?[\w-]+\"?): (-?\d+(?:\.\d+)?)$")
PROMETHEUS_NAME = re.compile(r"meshcore_[a-z0-9_]+")


def metrics(name: str) -> list[dict[str, object]]:
    """The entries under `metrics:`, each its fields and `map` the string values it maps. The
    files keep this flat form; the project reads YAML nowhere (ADR 0005)."""
    text = (CONTRIB / name).read_text(encoding="utf-8")
    entries: list[dict[str, object]] = []
    for line in text.split("\nmetrics:\n", 1)[1].splitlines():
        if line.startswith("  - prom_name: "):
            entries.append({"prom_name": line.removeprefix("  - prom_name: "), "map": {}})
        elif match := FIELD.match(line):
            entries[-1][match.group(1)] = match.group(2).strip('"')
        elif match := MAPPED.match(line):
            mapping = entries[-1]["map"]
            assert isinstance(mapping, dict)
            mapping[match.group(1).strip('"')] = float(match.group(2))
    return entries


def setting(name: str, key: str) -> str:
    match = re.search(
        rf"^  {key}: (.+)$", (CONTRIB / name).read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match is not None, key
    return match.group(1).strip('"')


@pytest.mark.parametrize("name", ["gateway.yaml", "contacts.yaml"])
def test_every_metric_has_a_prometheus_name_a_help_and_a_type(name: str) -> None:
    entries = metrics(name)
    assert len(entries) >= 10
    names = [str(e["prom_name"]) for e in entries]
    assert len(set(names)) == len(names)
    for entry in entries:
        assert PROMETHEUS_NAME.fullmatch(str(entry["prom_name"])), entry
        assert entry.get("help"), entry
        assert entry.get("type") in {"gauge", "counter"}, entry


def test_the_gateway_metrics_read_fields_of_state_and_stats() -> None:
    documents = MQTT["documents"]
    for entry in metrics("gateway.yaml"):
        document = {"^state$": "state", "^stats$": "stats"}[str(entry["sensor_name_filter"])]
        assert entry["mqtt_name"] in documents[document], entry
    assert setting("gateway.yaml", "topic_path") == "meshcore/radio1/+"
    for document in ("state", "stats"):
        assert f"<base_topic>/<node_id>/{document}" in MQTT["publish"]


def test_the_contact_metrics_read_fields_of_a_contacts_document() -> None:
    fields = MQTT["documents"]["contacts/<key>"]
    entries = metrics("contacts.yaml")
    for entry in entries:
        assert entry["mqtt_name"] in fields, entry
        assert entry.get("omit_timestamp") == "true", entry  # its document comes on changes
    assert {"latitude", "longitude", "altitude_m"}.isdisjoint(e["mqtt_name"] for e in entries)
    assert setting("contacts.yaml", "topic_path") == "meshcore/radio1/contacts/+"
    assert "<base_topic>/<node_id>/contacts/<key>" in MQTT["publish"]


def test_every_value_of_a_mapped_field_has_a_number() -> None:
    """A value the gateway adds later fails here first: mqtt2prometheus refuses the whole
    document over one value it cannot map."""
    values = MQTT["values"]
    mapped = {
        ("gateway.yaml", "radio"): values["state:radio"],
        ("contacts.yaml", "telemetry_state"): values["contacts/<key>:telemetry_state"],
    }
    for (name, field), documented in mapped.items():
        (entry,) = [e for e in metrics(name) if e["mqtt_name"] == field]
        mapping = entry["map"]
        assert isinstance(mapping, dict)
        assert set(mapping) == set(documented)
