---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# Internationalisation with gettext catalogs; English source, German, Dutch, Polish, French

## Context and Problem Statement

The project is English ([ADR 0011](0011-neutral-english-language-project-under-mit.md)), but the
people who use the dashboards, the installer and the command line live in a household, mostly
outside the English-speaking world. German comes first; the next languages should be those where
MeshCore is most widespread. Each component has its own conventions: Python programs, Home
Assistant entity names sent over MQTT, dashboards, and a bash installer that runs from a URL on a
Proxmox host. How do we translate all of them with one workflow, no new runtime dependency, and
without breaking anything that machines read?

## Decision Drivers

* No runtime dependency beyond the standard library ([ADR 0005](0005-runtime-dependencies-from-the-distribution.md)).
* One translator workflow with standard files (Weblate, Poedit).
* Machine interfaces never change with the language: MQTT, JSON output, entity IDs, event types,
  logs.
* The installer stores nothing on the host and runs with `LC_ALL=C.UTF-8` for its dialogs.
* Reproducible builds ([ADR 0013](0013-signed-releases-with-github-artifact-attestations.md)).

## Considered Options

* GNU gettext PO catalogs for everything, compiled by our own standard-library tooling; installer
  language packs generated from them
* Ad-hoc string tables per component (Python dictionaries, a YAML table for the dashboard, an
  array in the installer)
* Only the Home Assistant names translated, the rest English

## Decision Outcome

Chosen option: "gettext catalogs for everything", because PO files are the standard every
translation tool reads, Python's `gettext` reads compiled catalogs from bytes (so they can live in
the zipapp), and one source of messages keeps all components consistent.

* **Languages**: English (source, the msgid), German, Dutch, Polish, French. The three after German
  lead every measure of MeshCore's spread outside English and German: distinct contributors to the
  public node map (Dutch 7.5 %, Polish 6.0 %, French 5.1 %), nodes active in the last 30 days, and
  nodes added in the last 90 days; they also have large Home Assistant audiences, and the official
  MeshCore app and the Home Assistant MeshCore card already ship them. Italian and Russian follow
  closely and are the next candidates. A language is offered only when its installer, Home
  Assistant and dashboard texts are complete.
* **Python** (command line, Home Assistant names, dashboard texts): PO sources in
  `src/meshcore_home_gateway/locale/`, compiled at build time into `.mo` catalogs inside the zipapp
  by a small compiler of our own (byte-identical to GNU `msgfmt --no-hash`; CPython's
  `Tools/i18n/msgfmt.py` is not in the container and mixes catalogs when given several), loaded
  with `importlib.resources` into `gettext.GNUTranslations`. No `setlocale`, no installed locales.
* **Who decides the language**: the command line follows the environment (`LANGUAGE`, `LC_ALL`,
  `LC_MESSAGES`, `LANG`, as Python's `gettext` reads them); the service and the dashboard
  generator follow `[homeassistant] language` (default `en`), because the gateway cannot see Home
  Assistant's language.
* **Home Assistant**: MQTT entities have no translation keys, so the gateway sends localized names;
  `default_entity_id` keeps every entity ID English and stable, names a user changed stay, and a
  change of the language renames only the display names (verified in Home Assistant 2026.10.0).
  Event types, select options, attribute keys and units stay machine values; device classes are
  chosen so the frontend translates states.
* **Dashboards**: one generated dashboard per language; templates cannot see the user's language,
  and their date words are always English, so numbers and dates are left to cards that the
  frontend formats.
* **Installer**: neither GNU gettext (needs files and an installed locale on the host) nor bash's
  `$"..."` (expands the translated text as shell). Its strings are extracted into their own PO
  catalog; plain-text language packs are generated from it, pinned in the script by SHA-256,
  fetched from the release or the checkout and read without `eval`; a missing or wrong pack means
  English. Button labels are passed to whiptail; the chosen language becomes the default of
  `[homeassistant] language`.
* **Never translated**: logs and the journal, machine output (`probe --json`, `--list`, `listen`,
  `config answers`), MQTT topics and payloads, configuration keys, command and option names, mesh
  and radio data, developer documentation.

### Consequences

* Good, because a household sees its language in Home Assistant, the dashboard, the installer and
  the command line, while automations, logs and the API stay the same everywhere.
* Good, because translators work with standard PO files, and the checks (placeholders, plurals,
  completeness, canonical files) run in `make check`.
* Bad, because about 400 lines of tooling (extract, merge, check, compile, packs) are ours to keep;
  they are standard-library Python and tested against GNU gettext where it is installed.
* Bad, because argparse's own words ("usage", "options") are translated through a module attribute
  that is not documented; if it breaks, they fall back to English.
* Neutral, because the user guides stay English; translated guides can follow.

### Confirmation

Tests: every msgid translated in shipped languages, placeholders identical, plural forms valid,
catalogs compile and round-trip, the template is current, no translation in log calls, machine
outputs identical under every language, entity IDs identical in every language in the Home
Assistant contract tests, installer packs in sync with their pins and read back exactly.

## Pros and Cons of the Options

### gettext catalogs

* Good, because standard, tool-supported, with plurals and contexts.
* Bad, because PO files need a build step; it is part of the zipapp build.

### Ad-hoc tables

* Good, because no tooling.
* Bad, because three formats, no translator tools, and placeholder or plural mistakes go unnoticed.

### Home Assistant names only

* Good, because small.
* Bad, because the installer and the dashboard are what a household sees first.

## More Information

* Amended on 2026-10-31 by [ADR 0027](0027-message-catalogs-compiled-when-loaded.md): the
  catalogs are compiled when they are loaded, not when the zipapp is built.
* Spread of MeshCore by language: public node map `map.meshcore.io/api/v1/nodes` (68,204 nodes,
  fetched 2026-10-23, assigned to countries offline; only aggregates were kept), Home Assistant
  analytics (`analytics.home-assistant.io`, 2026-10-23), translations of the MeshCore app 1.50.0
  and of meshcore-card.
* Python: `gettext` documentation (docs.python.org/3.13/library/gettext.html); GNU gettext manual,
  "Plural forms"; ruff's `INT` rules catch f-strings inside `_()`.
* Revisit the language list before each minor release with the same measures; add Italian or
  Russian when translators offer them.
