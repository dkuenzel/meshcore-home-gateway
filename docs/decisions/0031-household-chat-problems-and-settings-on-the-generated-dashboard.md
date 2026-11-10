---
status: accepted
date: 2026-11-07
decision-makers: maintainers
---

# Household chat, problems and settings on the generated dashboard

## Context and Problem Statement

The dashboard of 2.0 ([ADR 0021](0021-generated-dashboards-for-desktop-and-mobile.md),
[design §6.13](../design-home-assistant.md#613-dashboard)) used only the entities the gateway had
anyway: four tabs with icons only, a tile with the guide's script *Mesh message* to write, which
Home Assistant 2026.10 and later never showed (#110), the last message received and the last one
sent but no history, the problem sensors as tiles, and no view of the settings. The household
could not read a conversation, could not write from the dashboard without an administrator adding
a script, and did not see why something failed or what the administrator had set. Home Assistant
offers no dashboard variables, no per-user state for a card, no per-entity permissions in its
interface, and the frontend shows only its own cards
([ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md)). What does the
gateway give the dashboard, how are its pages laid out, and how does a household member write a
message?

## Decision Drivers

* The household reads and writes its chats as in a messenger, on a phone, without help from the
  administrator and without anything installed besides the gateway's entities.
* Only Home Assistant's own cards; no custom card, no helper the administrator must create.
* Nothing that is sent by accident: a half-written text never goes out, a press sends what was
  written just before it, once.
* A Home Assistant user gets no power over the gateway that the configuration does not give
  already ([threat model](../threat-model.md), T7).
* What the gateway publishes stays provider-neutral where it can
  ([ADR 0015](0015-provider-neutral-documents-on-the-bus.md)), and its public interfaces only grow
  ([ADR 0023](0023-public-interfaces-kept-by-a-snapshot.md)).
* Three tabs fit a phone of 360 px in English and in German; every other page is one tap away.
* Tested like code on both Home Assistant series the release supports, and in a browser.

## Considered Options

* The gateway announces what the dashboard needs: the message history, the messages on their way,
  a draft and a *Send* button for each chat, and the configuration
* A text field that sends what is typed
* The script form *Mesh message* as the dashboard's way to write
* A shared select of the chat, Home Assistant helpers, or custom cards
* The recorder `options` loophole for the message history
* Absolute links between the pages
* Icon-only tabs
* A wait in the gateway between a stored text and a press
* Two topics, one for the draft and one for the press
* A limiter that coalesces the draft's echoes
* A `back_path` on a page

## Decision Outcome

Chosen option: "the gateway announces what the dashboard needs", because it is the only one that
lets a household member read a conversation and send a message with Home Assistant's own cards,
without an administrator's script or helper, and without sending a half-written text.

* **Entities for the dashboard.** Since 2.1 the gateway announces *Message history* (the
  retained `history`, with its own availability, so it stays readable while the gateway is
  offline), *Messages on their way* (the retained `pending`, likewise) and *Configuration* (the
  retained `config`, a diagnostic sensor), and for each target not in `hidden_targets` *Write to
  …* and *Send what is written to …*, for each companion that is no contact target *Write* and
  *Send what is written* on its device
  ([design §6.7](../design-home-assistant.md#67-home-assistant)).
  `history`, `pending` and `config` are documents of every front end. This lifts the rule of
  2.0's dashboard, "no new entity, topic or setting"; no setting is added.
* **One draft per chat, held by the gateway.** The gateway keeps each chat's draft in memory and
  publishes it retained on `homeassistant/draft/<kind>/<chat>`. The text entity stores a text
  (Return, or leaving the field), the button sends it: both publish on the chat's one request
  topic at QoS 0, so the broker keeps their order, and the gateway takes them in that order on its
  event loop, with no wait. A queued message empties the draft; a refused one or a duplicate stays,
  to be changed. A draft unchanged for 15 minutes is emptied (decided by the maintainer on
  2026-11-07), and a clean stop empties every draft. The household shares a chat's draft: what
  one leaves in the field, the next *Send* sends.
* **Draft requests are Home Assistant's controls**, under `homeassistant/` like `show_hidden` and
  `add_node`, not requests of every front end. Their outcome: a stored text ends in no event, its
  outcome is the draft published back; a press ends in a `delivery` event, `rejected` with
  `empty` for a blank draft too, except a blank press within 2 s after a queued press of the same
  chat (a double tap's second), whose outcome is that send's events; a retained request is a
  left-over and is ignored. The draft
  is a document for Home Assistant only, built in `drafts.py`, not in `views`, as both say
  ([mqtt-api.md](../mqtt-api.md#drafts-of-home-assistants-chats-homeassistantdraftkindchat)).
* **Three tabs with their titles alone**: *Overview*, *Chats* and *Contacts*, each without an icon,
  so the frontend shows the title. A page for each chat, *Map*, *Gateway*, *Settings* and *Help*
  are pages without a tab (`subview: true`), one tap away from *Overview* and *Contacts*. The 2.1
  design's rule DR-26, "tabs show their titles" (four bare icons are a guessing game), meant to
  keep the icons and add `show_icon_and_title: true`; measured at 360, 390 and 412 px in English
  and German on both frontends, five titled tabs fit nowhere and five icons miss 360 px, while
  three titled tabs fit everywhere with 7.5–10 px to spare. The maintainer chose the three titled
  tabs on 2026-11-07, keeping the rule's point and departing from its means.
* **Relative links.** Every link stays inside the dashboard, whatever its address:
  `<a href="./contact-alice">` in a card, `navigation_path: ./help` in a button. No page has a
  `back_path`: the back arrow goes back in the browser's history, from a bookmark to the first
  tab.
* **Every fixed ID of a contact is guarded.** The generator writes what it knows when it runs (the
  chat pages, each contact's switch, *Remove*, field and *Send*); each shows only while a
  template finds that entity, not `unavailable`, on a device whose via device is the gateway's and
  whose serial number is the key the dashboard was made for. A renamed, removed or reused entity
  ID never operates another contact.
* **Settings are shown, not changed.** *Settings* reads *Configuration*, an allowlist of the
  settings in effect without hosts, passwords, keys, codes or command words. Home Assistant
  changes nothing new: sending, asking a person for position and battery, adding and removing
  contacts were already its controls; `controls`, `hidden_targets`, `confirm_nonce`, the channels,
  the heard nodes, telemetry's consent, airtime, the radio profile, commands and the broker stay
  in the configuration file, which the administrator changes. No card uses `condition: state` or
  `state_not` on an entity that may be missing, which since 2026.10 hides the card for a household
  user (#110); the conditions are templates.
* **The dashboard needs Home Assistant 2026.10 or later**, whose frontend evaluates template
  conditions; `meshcore-home-gateway dashboard` says so after every dashboard it writes. The
  gateway and its entities keep the floor of 2.x, Home Assistant 2026.8: the dashboard is not a
  public interface and follows the frontend features it uses
  ([compatibility](../development.md#compatibility)).
* **Why a *Send* was refused stands under the thread**, not in the view's footer with the field
  and *Send*: the frontend caps the footer at a quarter of the screen, and the line on top of the
  field pushed *Send* 15 px below a 360x800 screen
  ([platform-notes.md](../platform-notes.md#home-assistant-dashboards)).

### Consequences

* Good, because a household member reads each chat as a thread, writes and sends from its page,
  sees what is wrong and what to do, and reads the settings, all without an administrator's step.
* Good, because `history`, `pending` and `config` serve every front end, and the draft rules are
  plain enough for another client of the broker.
* Good, because nothing typed is sent before *Send*, and *Send* sends the text stored just before
  it, through the same checks, queue and events as a send request.
* Bad, because every Home Assistant user reads and can send another user's unsent draft, as they
  can already send to every target; the 15 minutes bound how old such a draft gets.
* Bad, because Home Assistant's recorder stores the history, the messages on their way and every
  draft unless the administrator excludes them ([guide](../home-assistant.md#privacy)).
* Bad, because the broker keeps three more kinds of retained topics, which a downgrade to 2.0
  leaves behind until they are cleared
  ([deployment.md](../deployment.md#update-reconfigure-remove)).
* Bad, because the dashboard needs a newer Home Assistant than the gateway, and the chat pages and
  each contact's controls exist only for what the dashboard was made with: it is made again after
  a target or a contact was added.
* Bad, because on a desktop screen with a long thread the refused line can stand behind the footer
  until the page is scrolled: at 1280x800 with a full thread, up to 32 of its 49 px were behind
  the field ([platform-notes.md](../platform-notes.md#home-assistant-dashboards)). On a phone it
  stands above the footer.

### Trust boundaries

B3 the broker: Home Assistant's user writes the new request topics
`homeassistant/draft/<kind>/<chat>/request` (the documented ACL gains that line); a retained,
malformed or oversized request, and one for a chat the gateway does not have, is ignored; a press
goes through the checks of a send request. The drafts are retained, readable by every client that
may read `meshcore/<node>/#`, and in the broker's persistence file until they are sent, expire or
the gateway stops cleanly; the gateway undoes what another client writes on a draft topic, as on
its documents. B4 Home Assistant and B5 its users: every user reads every chat's draft and the
settings in effect, the hidden targets' names and prefixes included, never a secret, host or key;
no setting changes from Home Assistant. The cards read states and Home Assistant's device registry
and print every value through one escaping macro. The threat model names the drafts in A4, T6 and
T7.

### Confirmation

`tests/test_drafts.py` (the drafts' requests, order, outcomes, expiry, clean stop and cut),
`tests/test_views.py`, `tests/test_pending.py` and `tests/test_history.py` (the documents and their
bounds), `tests/test_retained.py` (the guard of the draft topics) and the snapshot of the public
interfaces; `tests/test_dashboard.py` with the rules of `tests/dashboard_rules.py`
([development.md](../development.md#translations)): the guards (rule 3), no `state_not` (rule 4),
relative links (rule 6), the three tabs (rule 12), the refused line under the thread (rule 18) and
no `back_path` (rule 20); the contract tests in `home-assistant/tests/` (`test_chats.py`,
`test_drafts.py`, `test_dashboard.py`, `test_overview.py`, `test_contacts.py`) on Home Assistant
2026.10 and 2026.11; and the release's browser check against a real gateway on the emulated
radio (`tools/browser_check.py` and `tools/emulated_radio.py`: every page at a phone's, a tablet's
and a desktop's size, in each language the dashboard ships, and one draft sent through Home
Assistant, [testing.md](../testing.md#the-dashboard-in-a-browser)).

## Pros and Cons of the Options

### The gateway announces what the dashboard needs

* Good, because Home Assistant's own text, button and sensor entities do the work.
* Good, because the order of a stored text and a press is kept by the broker on one topic.
* Bad, because the gateway grows entities, topics and a part (`drafts.py`) for one front end.

### A text field that sends what is typed

* Good, because one entity, no draft.
* Bad, because Home Assistant's text row sends its value on blur, Tab and back: leaving the field
  would send a half-written text.

### The script form *Mesh message*

* Good, because it exists and the guide keeps it for automations.
* Bad, because an administrator must add it, Home Assistant 2026.10 and later never showed 2.0's
  tile with it (#110), and a form in a dialog is no chat.

### A shared select, helpers or custom cards

* Good, because a select or helper could switch one page between chats.
* Bad, because a select is shared by every user, so one user switches everyone's chat; helpers
  need the administrator; custom cards are ruled out
  ([ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md)).

### The recorder `options` loophole

* Good, because a sensor never records an attribute named `options`, so the history would stay
  out of the recorder without the administrator.
* Bad, because it is undocumented and fragile: the frontend may read `options` as the options of
  an enum. The guide's recorder block is the administrator's choice instead.

### Absolute links

* Good, because a link names the page it opens.
* Bad, because they break when the dashboard is added under another address, and the generator
  would need that address.

### Icon-only tabs

* Good, because four icons fit 360 px.
* Bad, because the names are tooltips only, which a touch screen does not show, and five icons
  miss 360 px.

### A wait in the gateway between a stored text and a press

* Good, because a press would find the stored text even if it came first.
* Bad, because one topic keeps the order already, so the wait guards against no order seen in a
  test, and it widens the window below in which the clear cuts the next text being typed.

### Two topics

* Good, because the text entity and the button keep separate topics.
* Bad, because MQTT keeps order only within one topic: a press could overtake the text stored
  just before it and send the older draft.

### A limiter that coalesces the echoes

* Good, because it bounds the echoes of fast typing.
* Bad, because it delayed the clear after a press by up to 0.2 s, and a clear that comes late
  empties the next text being typed, so that its next *Send* sent a fragment, in every try in a
  browser on both Home Assistant series. The limiter takes the leading edge instead, and the
  clear goes out at once.

### A `back_path`

* Good, because a page names where its back arrow leads.
* Bad, because a page opens from several places, and the frontend replaces the page's history
  entry with the target, so the history holds the target twice and the browser's back lands on it
  again.

## More Information

* The pages, guards, template language and departures from ADR 0021:
  [design §6.13](../design-home-assistant.md#613-dashboard); the entities:
  [design §6.7](../design-home-assistant.md#67-home-assistant); the topics:
  [mqtt-api.md](../mqtt-api.md); for the household:
  [home-assistant.md](../home-assistant.md#dashboard).
* Revisit when Home Assistant offers state per user for a card (a field per user), permissions per
  entity in its interface, or dashboard variables; when the frontend's footer cap changes; and
  when the use after the release shows stale drafts being sent (a change time in the draft
  document).
