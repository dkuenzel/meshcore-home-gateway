"""The names in another language against the real Home Assistant MQTT integration (ADR 0019).

`[homeassistant] language` changes the names the gateway sends and nothing else. Two gateways
side by side, one English and one in another language, give Home Assistant the same entity IDs,
unique IDs, classes, units, options, event types and attributes; the names are the catalog's, a
component without a name keeps the one Home Assistant gives it, and a change of language renames
only the display names (docs/home-assistant.md, "Language of the names").

Until the gateway ships a language, the tests use a pseudo-language `xx`, compiled from the
template as the package compiles its catalogs: every name becomes `‹xx name›`, whose slug differs
from the English name's, so an entity ID made from a name would differ too.
"""

from __future__ import annotations

import dataclasses
import gettext
import io
import json
import re
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from test_contract import RADIO, fire, make_config, problems, state_document

from meshcore_home_gateway import catalog, i18n
from meshcore_home_gateway.config import Config
from meshcore_home_gateway.core.mesh import Mesh, Node
from meshcore_home_gateway.mqtt.contact_devices import ContactDevices, slugify
from meshcore_home_gateway.mqtt.discovery import device_id, discovery_payload, discovery_topic
from meshcore_home_gateway.mqtt.manage_entities import manage_payload, manage_topic
from meshcore_home_gateway.mqtt.topics import Topics
from meshcore_home_gateway.state import ContactDeviceRecords, JsonStateFile
from test_contact_devices import ALICE, ALICE_NODE, HILL_NODE, Broker, mesh_of
from test_telemetry import BATTERY_4_12, BME280, EVERY_TYPE, MCU, answer, document

NOW = 1_800_000_000.0
ROOM, SENSOR = "b2" * 32, "d4" * 32
NODES = (ALICE_NODE, HILL_NODE, Node(ROOM, "Hall", 3), Node(SENSOR, "Shed", 4))
REMOVED = {"send_old": "notify", "old_sensor": "sensor"}  # components of an earlier run
OPTIONS = ["e5e5e5e5e5e5…e5e5e5e5 Hilltop", "b2b2b2b2b2b2…b2b2b2b2 Hall (room)"]
FIRST, SECOND = "radio1", "radio2"  # the node IDs of the English gateway and the other one
# what may differ between the two gateways' entries: the names and what Home Assistant keeps
# about the entry itself, not about the entity
OWN = {"id", "entity_id", "unique_id", "device_id", "config_entry_id", "created_at",
       "modified_at", "original_name", "original_name_unprefixed", "_cache"}  # fmt: skip
SPOKEN = ["xx", *(code for code in i18n.LANGUAGES if code != "en")]


def pseudo(language: str = "xx", mark: str = "‹{language} {text}›") -> i18n.Translator:
    """Every text of the template marked, `‹xx text›` unless `mark` says otherwise, compiled in
    memory (ADR 0027)."""
    template = i18n.CATALOGS / "meshcore-home-gateway.pot"
    _, entries = catalog.read_po(template.read_text(encoding="utf-8"), template.name)
    marked = [
        dataclasses.replace(
            entry,
            msgstr=tuple(
                mark.format(language=language, text=text)
                for text in (entry.msgid, entry.msgid_plural)
                if text
            ),
        )
        for entry in entries
    ]
    header = {
        "Language": language,
        "Content-Type": "text/plain; charset=UTF-8",
        "Plural-Forms": "nplurals=2; plural=(n != 1);",
    }
    mo = catalog.write_mo(header, marked)
    return i18n.Translator(language, gettext.GNUTranslations(io.BytesIO(mo)))


def offer(
    language: str, monkeypatch: pytest.MonkeyPatch, mark: str = "‹{language} {text}›"
) -> i18n.Translator:
    """The language's translator; the pseudo-language offered as a language of this version
    first: the configuration and the builders look the languages up through the module."""
    if language != "xx":
        return i18n.translator(language)
    offered, original = pseudo(mark=mark), i18n.translator
    languages = MappingProxyType({**i18n.LANGUAGES, offered.language: "Pseudo"})
    monkeypatch.setattr(i18n, "LANGUAGES", languages)

    def translator(language: str) -> i18n.Translator:
        return offered if language == offered.language else original(language)

    monkeypatch.setattr(i18n, "translator", translator)
    return offered


def gateway(node_id: str = FIRST, language: str = "en", **settings: Any) -> Config:
    """Every component of the gateway (targets, command words, the packet sensors) with its
    names in `language`."""
    config = make_config(node_id=node_id)
    names = dataclasses.replace(config.homeassistant, language=language, **settings)
    return dataclasses.replace(config, homeassistant=names)


def answered() -> Mesh:
    """A companion, a repeater with a position, a room server and a sensor. The companion's
    answer has every reading type on channel 3, and a temperature on channel 2 too."""
    mesh = mesh_of(*NODES)
    answer(mesh, NOW, BATTERY_4_12, MCU, EVERY_TYPE, BME280[:8])
    return mesh


async def discover(
    hass: HomeAssistant, config: Config, store: JsonStateFile, mesh: Mesh,
    slug: Callable[[str], str] = slugify,
) -> dict[str, str]:  # fmt: skip
    """What the gateway announces when it starts: its document (the components of an earlier
    run bound, then removed), its controls, its contacts' devices, and the documents they show.
    The names it sent, by unique ID; `slug` makes its contacts' entity IDs."""
    topics = Topics(config.mqtt.base_topic, config.mqtt.node_id)
    sent = [
        discovery_payload(config, topics, RADIO, REMOVED, bind=True),
        discovery_payload(config, topics, RADIO, REMOVED),
    ]
    for gateway_document in sent:
        await fire(hass, discovery_topic(config), gateway_document)
    sent.append(manage_payload(config, topics, OPTIONS))
    await fire(hass, manage_topic(config), sent[-1])
    broker, records = Broker(), ContactDeviceRecords(store)
    records.slugs.update({node.public_key: slug(node.name) for node in NODES})
    ContactDevices(config, topics, broker, records, wall_clock=lambda: NOW).update(mesh)
    sent += [json.loads(payload) for _, payload in broker.sent if payload]
    await broker.deliver(hass)
    await fire(hass, topics.availability, "online", retain=True)
    await fire(hass, topics.state, state_document(known=True), retain=True)
    await fire(hass, topics.contact(ALICE), document(mesh), retain=True)
    return {
        component["unique_id"]: component["name"]
        for document_sent in sent
        for component in document_sent["cmps"].values()
        if isinstance(component.get("name"), str)
    }


def entries(hass: HomeAssistant, node_id: str) -> dict[str, er.RegistryEntry]:
    """The entities of one gateway, its controls and its contacts, by entity ID."""
    prefix = device_id(gateway(node_id)) + "_"
    (entry,) = hass.config_entries.async_entries("mqtt")
    found = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    return {e.entity_id: e for e in found if e.unique_id.startswith(prefix)}


def devices(hass: HomeAssistant, node_id: str) -> dict[str, dr.DeviceEntry]:
    """The devices of one gateway: its own and its contacts', by their ID."""
    uid = device_id(gateway(node_id))
    (entry,) = hass.config_entries.async_entries("mqtt")
    found = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    return {name: d for d in found for _, name in d.identifiers if name.startswith(uid)}


def as_first(text: str) -> str:
    """An ID of the second gateway as the first gateway's: its node ID, its contacts' slugs."""
    return re.sub(r"(^|\.)mesh_two_", r"\1mesh_", text.replace(f"_{SECOND}", f"_{FIRST}"))


def facts(hass: HomeAssistant, node_id: str) -> dict[str, Any]:
    """What Home Assistant keeps of one gateway's entities, and the states they show, but the
    names, by entity ID as the first gateway's."""
    registry = dr.async_get(hass)
    found: dict[str, Any] = {}
    for entity_id, entry in entries(hass, node_id).items():
        kept = {
            field.name: getattr(entry, field.name)
            for field in type(entry).__attrs_attrs__
            if field.name not in OWN
        }
        device = registry.async_get(entry.device_id) if entry.device_id else None
        kept["unique_id"] = as_first(entry.unique_id)
        kept["suggested_object_id"] = as_first(entry.suggested_object_id or "")
        kept["device"] = as_first(next(iter(device.identifiers))[1]) if device else None
        if (state := hass.states.get(entity_id)) is not None:  # none while disabled
            attributes = {k: v for k, v in state.attributes.items() if k != "friendly_name"}
            kept["state"] = (state.state, attributes)
        found[as_first(entity_id)] = kept
    return found


# --- the same entities in every language ---------------------------------------------------------


@pytest.mark.parametrize("language", SPOKEN)
async def test_entity_ids_are_the_same_in_every_language(
    hass: HomeAssistant, mqtt: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture, *, language: str,
) -> None:  # fmt: skip
    """An English gateway and one in `language` side by side; the second one's contacts get
    other slugs, so that their entity IDs stay apart."""
    translator = offer(language, monkeypatch)
    mesh = answered()
    english = await discover(hass, gateway(), JsonStateFile(tmp_path / "one.json"), mesh)
    spoken = await discover(
        hass, gateway(SECOND, language), JsonStateFile(tmp_path / "two.json"), mesh,
        slug=lambda name: f"two_{slugify(name)}",
    )  # fmt: skip
    first, second = facts(hass, FIRST), facts(hass, SECOND)
    assert len(first) > 70
    assert second == first

    names = {as_first(uid): name for uid, name in spoken.items()}
    first_entries = {as_first(e.unique_id): e for e in entries(hass, FIRST).values()}
    for entry in entries(hass, SECOND).values():
        uid, shown = as_first(entry.unique_id), first_entries[as_first(entry.unique_id)]
        assert (uid in names) == (uid in english), uid  # named in both languages, or in none
        if uid in names:  # named by the gateway: the catalog's translation of the English name
            assert (shown.original_name, entry.original_name) == (english[uid], names[uid])
        else:  # no name sent: Home Assistant's own, in its language (a device class) or none
            assert entry.original_name == shown.original_name
    if language == "xx":
        assert all(name.startswith("‹xx ") for name in names.values())

    contacts = {as_first(key): d for key, d in devices(hass, SECOND).items()}
    for key, device in devices(hass, FIRST).items():
        other = contacts[key]
        if key == device_id(gateway()):  # the default device name is a name
            default = translator.ha_name("MeshCore radio")
            assert (device.name, other.name) == ("MeshCore radio", default)
            assert other.model == device.model == RADIO.device.model  # the radio's: data
        else:  # the contact's name is data, its model a name
            assert other.name == device.name
            assert other.model == translator.ha_name(device.model)
    for entity_id, entry in entries(hass, SECOND).items():
        device = dr.async_get(hass).async_get(entry.device_id or "")
        assert device is not None
        parts = [device.name, *([entry.original_name] if entry.original_name else [])]
        if (state := hass.states.get(entity_id)) is not None:
            assert state.attributes["friendly_name"] == " ".join(parts)
    assert problems(caplog) == []


# --- a change of language ------------------------------------------------------------------------


async def test_a_language_change_renames_only_display_names(
    hass: HomeAssistant, mqtt: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Set in the configuration, a language renames the display names at the next start; the
    entity IDs, the names the user gave and every entity stay, as Home Assistant keeps a
    known entity's ID and the user's name when its discovery changes."""
    translator = offer("xx", monkeypatch)
    store, mesh = JsonStateFile(tmp_path / "state.json"), answered()
    await discover(hass, gateway(), store, mesh)
    radio = f"binary_sensor.{device_id(gateway())}_radio"
    entities = er.async_get(hass)
    entities.async_update_entity(radio, name="Radio in the hall")
    own = devices(hass, FIRST)[device_id(gateway())]
    dr.async_get(hass).async_update_device(own.id, name_by_user="Greenhouse gateway")
    before = facts(hass, FIRST)

    await discover(hass, gateway(language="xx"), store, mesh)
    assert facts(hass, FIRST) == before  # no entity added or removed, nothing else changed
    entry = entities.async_get(radio)
    assert entry is not None
    ha_name = translator.ha_name
    assert (entry.name, entry.original_name) == ("Radio in the hall", ha_name("Radio link"))
    own = devices(hass, FIRST)[device_id(gateway())]
    assert (own.name, own.name_by_user) == (ha_name("MeshCore radio"), "Greenhouse gateway")
    battery = hass.states.get(f"sensor.{device_id(gateway())}_battery")
    assert battery is not None
    assert battery.attributes["friendly_name"] == f"Greenhouse gateway {ha_name('Battery')}"
    temperature = hass.states.get("sensor.mesh_alice_ch3_temperature")
    assert temperature is not None
    assert temperature.attributes["friendly_name"] == f"Alice {ha_name('Temperature')} 3"
    assert problems(caplog) == []


async def test_a_chosen_device_name_stays(
    hass: HomeAssistant, mqtt: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:  # fmt: skip
    """Only the default device name is translated: a name the household chose stays."""
    translator = offer("xx", monkeypatch)
    config = gateway(language="xx", device_name="Greenhouse")
    await discover(hass, config, JsonStateFile(tmp_path / "state.json"), answered())
    assert devices(hass, FIRST)[device_id(config)].name == "Greenhouse"
    battery = hass.states.get(f"sensor.{device_id(config)}_battery")
    assert battery is not None
    assert battery.attributes["friendly_name"] == f"Greenhouse {translator.ha_name('Battery')}"
    assert problems(caplog) == []
