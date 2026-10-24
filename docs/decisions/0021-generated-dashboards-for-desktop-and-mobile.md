---
status: accepted
date: 2026-10-23
decision-makers: maintainers
---

# Generated dashboards for desktop and mobile

## Context and Problem Statement

The gateway's entities are useful only when someone puts them on a dashboard, and a good mesh
dashboard needs care: an overview, the messages with a compose form, contacts and heard nodes, a
map with the household's traces, the radio's health. It must work on a phone and on a desktop,
in the household's language, for any node ID, and texts from the mesh must never become markup.
Home Assistant dashboards have no translations and no variables. How do we ship one?

## Decision Drivers

* No custom cards: only cards of the pinned Home Assistant frontend
  ([ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md)).
* One layout for desktop and mobile where the frontend can do it; DRY and KISS.
* Any node ID, any set of contacts, five languages ([ADR 0019](0019-internationalisation-with-gettext.md)).
* Texts from the mesh are hostile input (every node in range can send them).
* Tested like code: a dashboard that breaks with a new Home Assistant release must fail CI.

## Considered Options

* A generator in the command line that writes a dashboard per node ID and language from a template
  in the repository; released as files
* One static file for the raw configuration editor
* A YAML-mode dashboard only

## Decision Outcome

Chosen option: "a generator", because entity IDs contain the node ID and dashboards have no
variables, and because one template rendered per language keeps a single source.

* **Template** in the repository (`home-assistant/dashboard/`), with translatable strings marked
  and extracted into the Python catalog; `meshcore-home-gateway dashboard [--language L]` renders
  it for the configured node ID. Release assets for the default node ID in every language, listed
  in `SHA256SUMS` and attested ([ADR 0013](0013-signed-releases-with-github-artifact-attestations.md)).
* **Sections views**, which reflow from one column on a phone to several on a desktop; cards that
  only make sense on one of them get a `visibility` condition on the screen width. One dashboard
  serves both; a second, generated variant is made only where a condition cannot express the
  difference.
* **Map** with the household's contacts and their traces (the device trackers' history) and the
  heard repeaters with known positions; the gateway's own position never.
* **Mesh texts** are shown only escaped and inside HTML blocks in Markdown cards, never in Markdown
  context or tables; numbers and dates go into cards the frontend formats.
* **Versioned**: the dashboard names the gateway version it needs, and a card tells when the
  gateway is older or newer.
* **Installed** through the raw configuration editor (storage mode) or as a YAML-mode dashboard;
  an update replaces the file, which the guide says before any local customisation.

### Consequences

* Good, because every household gets a tested dashboard in its language without writing YAML.
* Good, because the contract test loads the generated dashboards into the pinned Home Assistant,
  checks that every entity they reference exists, and renders every Markdown card strictly with an
  attack corpus.
* Bad, because local edits are lost when the file is replaced; the guide recommends a copy for
  customising.
* Bad, because a dashboard per node ID and language means many release files; they are generated
  and checked, never edited by hand.

### Confirmation

Contract tests per language: the dashboard loads in YAML mode, all referenced entities exist after
discovery, Markdown cards render strictly in every situation (no gateway, gateway offline, no
contacts, many contacts, hostile names and texts), the card types are in the pinned frontend's
list, and the generator substitutes the node ID.

## Pros and Cons of the Options

### Generator

* Good, because one template, any node ID, any language.
* Bad, because it is code to maintain, with its own tests.

### One static file

* Good, because simple.
* Bad, because it works only for the default node ID and one language.

### YAML mode only

* Good, because updates are a file copy.
* Bad, because most users run dashboards in storage mode.

## More Information

* Platform facts (Markdown pipeline, sanitiser, card options): [platform-notes.md](../platform-notes.md),
  "Home Assistant dashboards".
* Revisit when Home Assistant offers translatable dashboards or dashboard variables.
