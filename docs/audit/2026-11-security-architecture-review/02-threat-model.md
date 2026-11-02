# 02 Threat model

The project's own threat model is design §7 (assets, boundaries, a threat table, the command
authorisation, residual risks). This document builds one independently from the model in `01-…`,
then compares the two in section 7. Findings that come out of it are written in
`03-security-findings.md`; this document only names them by ID.

## 1 Assets

| ID | Asset | Why it matters | Where it lives |
| --- | --- | --- | --- |
| A1 | The command path into the home | An accepted `command` event runs whatever automation the household attached: locks, alarms, heating, doors | `command` topic, Home Assistant automations |
| A2 | The alert path out of the home | The household relies on alerts arriving when the internet is down (the project's founding use case); a false "confirmed" is worse than a reported failure | the outbox, `delivery` events, the air |
| A3 | Positions of the household's people | Presence automations act on them; they are personal data; they persist (broker retained topics, Home Assistant recorder, `history.sqlite3`, `state.json`) | O5, O10, O12, O13 |
| A4 | Message texts, who talks to whom | Household communication; public-channel chatter of the neighbourhood | O3, O10, O12 |
| A5 | The radio's identity (its Ed25519 key) | Whoever holds it is the gateway to every contact; it decrypts every direct message to the gateway | the radio's flash; exportable over the companion protocol by default |
| A6 | The radio's contact table and settings | Decides what the radio decrypts, whom it answers, whether it leaks its position, how much it transmits | the radio's flash |
| A7 | Airtime and regulatory compliance | The operator is liable for the radio's transmissions; the mesh is shared | the budgets, the radio's airtime factor |
| A8 | Broker credentials and the broker itself | The broker is the home's bus; the gateway's credentials may be the only ones with write access to its topics | `config.toml`, the installer's answers, the broker |
| A9 | The gateway host (container) and the Proxmox host | A foothold in the home network | the LXC, the host the installer runs on as root |
| A10 | The integrity of what users install | A release reaches a machine that controls a transmitter and a home | GitHub releases, the installer address, the workflows |
| A11 | Availability of the gateway | Alerts and presence stop with it; a restart drops the queue | the process, its state files, the watchdog |
| A12 | Trust in the documents | Automations act on `contacts/<key>`, `command`, `telemetry` without further checks (by design, ADR 0015) | the broker's retained topics |

## 2 Actors

| ID | Actor | Position | Capabilities | Typical motivation |
| --- | --- | --- | --- | --- |
| T1 | Stranger in radio range | outside the home, with any MeshCore-capable board (20–40 €) or an SDR | send any packet: adverts under any name and any key of their own, channel messages on public or hashtag channels, direct messages to the radio only if they are a contact (otherwise dropped undecrypted), acknowledgements and path packets in the clear, telemetry requests; receive everything in the clear that is in the clear; jam | mischief, surveillance of the household, denial of the alert path, hiding a burglary |
| T2 | Stranger with a 48-bit prefix collision | as T1, after about 2^47 Ed25519 key generations for a chosen prefix | pose as an allowed sender's prefix in frames; still cannot decrypt or sign as the real key | targeted attack on the command path |
| T3 | A contact of the radio who is not household | a neighbour the household added, a former member, a compromised handheld | direct messages the radio decrypts, telemetry answers, adverts the radio trusts the name of | blocking a sender, spoofing a position of themselves, starving the radio's duty cycle |
| T4 | Holder of an allowed sender's handheld or key | theft, loss, a child, a compromised phone | send accepted commands | control of the home |
| T5 | Host on the radio's network (TCP transport) | the WiFi the radio joins | become the radio (ARP/DNS spoofing, or when the radio is off), or be a second client of the radio: read and write everything, export the private key, factory reset | full compromise of A5, A6, A1 |
| T6 | A broker client other than the gateway | any device with the broker's address and a credential, or the network if the broker is unauthenticated | publish to any topic the ACL allows: forge events and documents, send messages, manage contacts, clear retained topics, take the gateway's client id; read what the ACL allows | control of the home, surveillance, denial |
| T7 | A Home Assistant user who is not an administrator | a household member, a guest with an account, a compromised companion app | every entity: send to any contact or target, add or remove contacts, toggle telemetry of anyone, see every position and message | privacy within the household, mischief |
| T8 | A Home Assistant administrator or the Home Assistant host | | all of T7, plus automations and the recorder | not an attacker of the gateway; a victim of what reaches Home Assistant |
| T9 | A process in the container as the service user | a bug exploited through the radio or the broker | the state directory, the device node, the broker credentials in memory (the config file is readable by the group) | pivot |
| T10 | Root in the container, the Proxmox host | | everything in the container | not an attacker of the gateway |
| T11 | The supply chain | GitHub, the release workflow, the development dependencies, the installer's download addresses, the maintainer's account, the AI agent that writes most of the code | ship code to every installation on the next update | mass compromise |
| T12 | The operator, by mistake | | wrong clock, wrong ACL, a restored backup, a copied configuration | none; the system should fail safe |

## 3 Entry points by boundary

### B1 Air → radio firmware (I1)

What the firmware guarantees, as read in the pinned source (`11-research-notes.md` has the citations):

* Adverts are signed; a contact's advert with a timestamp not newer than the stored one is dropped (`BaseChatMesh.cpp:131-136`). An advert from an unknown key is accepted as a *new* node with whatever name it claims.
* Direct messages are decrypted only with the shared secret of a contact; the first contact whose key starts with the packet's 1-byte source hash and whose MAC verifies is the sender (`BaseChatMesh.cpp:203-211`, `Mesh.cpp:147-160`).
* Channel messages are decrypted with the channel's secret; the public channel's secret is public; the sender's name is text.
* Acknowledgements are a 4-byte hash in the clear; the sender matches the first 4 bytes against its table of 8 expected codes (`Mesh.cpp:560-571`, `MyMesh.cpp:424-434`). → **SEC-001**.
* The packet log hands every packet to the client before any check (`MyMesh.cpp:logRxRaw`).
* The radio's clock is written only forward over the protocol; at boot it is bootstrapped from the newest contact `lastmod` (`BaseChatMesh.cpp:60-70`), and on boards with a hardware RTC it persists. → **SEC-002**.

### B2 Radio → gateway (I2, I3)

* Frames are length-delimited, unchecked, unsigned; the gateway's decoder is strict about layout and drops what it does not expect.
* Over TCP there is no authentication either way and the release firmware exports the private key on request (`platformio.ini:33-34`, default flags). → **SEC-003**.
* Over USB the device node is the boundary: `dialout` in the container.

### B3 Gateway ↔ broker (I4–I10)

* The gateway subscribes to nine topic patterns and trusts the broker's ACL to limit who writes there. It has no notion of a sender identity on the bus.
* It accepts an unauthenticated, unencrypted broker without a warning. → **SEC-004**.
* Its retained documents are overwritten only when its own document changes. → **SEC-005**.
* Its client id is predictable. → **SEC-006**.

### B4/B5 Broker → Home Assistant → users

* Home Assistant renders Jinja templates that the gateway wrote (discovery) over data the mesh wrote (documents): the templates are trusted, the data is not evaluated.
* Markdown cards of the guide and the dashboard escape mesh text; the escaping is tested.
* Device names and the select's labels are mesh-controlled text shown to the household. → **SEC-007**.
* Every Home Assistant user can use every control. → **SEC-008**.

### B6 Service ↔ container ↔ host (I11–I13, I17)

* The service user owns the state directory (0700) and may read the configuration (0640); the password is in the file. → **SEC-009**.
* The systemd sandbox is tight; device access is by group.

### B7 Project → users (I14–I16, I19, I20)

* The installer is fetched and executed as root in one step; releases are attested; `main` is not.
* The code is mostly written by an AI agent and merged by the maintainer's account within minutes; releases are tagged automatically on merge. → **SUP-001** in `08-…`.

## 4 STRIDE by boundary

S spoofing, T tampering, R repudiation, I information disclosure, D denial of service, E elevation of privilege. "Covered" names the existing control; "gap" names a finding.

| Boundary | S | T | R | I | D | E |
| --- | --- | --- | --- | --- | --- | --- |
| B1 air | advert names: anyone can claim any name (covered: labels show 20 key digits; gap SEC-007); sender of a DM: a look-alike needs the household to add it (covered: ADR 0009, look-alike refusal); **ACK: forgeable from a guessed text (SEC-001)**; telemetry answer: a look-alike in the table (covered: not asked) | the packet log is unverified (covered: ADR 0025 rules); channel texts and names are plain (covered: documented) | the gateway logs names repr'd and keys shortened; message texts only at debug (covered) | adverts of the household with positions (covered: the check and warning); the gateway's own adverts never carry a position (covered: profile); the gateway's name and key are public by design | advert floods: 50-slot eviction, unbounded `node` rows, table re-reads (SEC-010, SEC-011); channel spam into Home Assistant's recorder (SEC-012); DM spam by a contact starves the radio's duty cycle (SEC-013); jamming (out of scope) | none at this boundary |
| B2 link | over TCP: anyone can be the radio (covered: documented, warned; gap: key export SEC-003) | a second client can change the table unseen (covered: documented) | – | over TCP the frames are plain: every message text in and out, the table, the keys' prefixes | a second TCP client replaces the gateway's connection (documented) | over TCP: factory reset, radio parameters, CLI over the air to any contact (SEC-003) |
| B3 broker | any client can forge any event or document under the ACL (covered: documented ACL; gaps: presence spoof chain SEC-005, no insecure-config guard SEC-004) | retained documents can be overwritten and stay (SEC-005) | the gateway does not sign or stamp what it publishes; a consumer cannot tell the gateway's document from a forged one (IMP) | a client with read access sees positions, texts, keys; without TLS so does the network (SEC-004) | client-id squatting (SEC-006); 1 KiB check after paho read the whole message (documented, broker limit); flooding the send topics (covered: rate limits, dedup, queue) | a forged `command` event is an accepted command to every automation (covered: documented as requiring the ACL) |
| B4/B5 HA | a device named like the gateway or like a household member (SEC-007) | a user changes the radio's table (SEC-008) | the recorder keeps everything (covered: documented) | every user sees every position (SEC-008) | a user removes a companion: presence gone | a non-admin sends mesh messages: airtime (covered: budget) |
| B6 host | – | a corrupted or restored `state.json` is validated per key (covered); a restored backup replays command timestamps (documented) | – | the password is group-readable (SEC-009) | the event loop runs blocking I/O under a 60 s watchdog (CODE/OPS) | the sandbox (covered); `dialout` reaches every serial device of the container (minor) |
| B7 project | a release is attested; `main` is not (documented) | a merged PR releases within minutes with no second reviewer (SUP) | commits are signed? (not verifiable here) | – | the installer address is GitHub (documented) | the installer runs as root from a download (documented, SUP) |

## 5 Attack trees

Each leaf names what stands in the way. Trees are written for the five outcomes an attacker would
want; they do not rate likelihood.

### 5.1 Run an automation in the home (A1)

```text
GOAL: an accepted `command` event with a chosen word
├─ 1. Send a direct message the gateway accepts as a command
│  ├─ 1a. Hold an allowed sender's private key (T4)                     [no technical control]
│  ├─ 1b. Make the radio hold a look-alike (T2) AND have the household add it
│  │      AND keep the real sender's commands rejected meanwhile          [refused as look-alike
│  │                                                                       at add; ADR 0009 memory]
│  ├─ 1c. Replay a recorded command                                        [timestamp ledger;
│  │                                                                       exact-repeat memory]
│  └─ 1d. Be the radio over TCP (T5) and hand the gateway a forged frame   [documented; no
│                                                                           authentication exists]
├─ 2. Publish a `command` event on the broker (T6)                         [the ACL, if any;
│                                                                           SEC-004: no guard]
├─ 3. Publish a discovery document that makes Home Assistant read a command
│     entity from a topic the attacker controls (T6 with write on homeassistant/#)
│                                                                          [Home Assistant's
│                                                                           discovery model; the
│                                                                           ACL]
└─ 4. Make an automation act on a non-command event the attacker controls
   ├─ 4a. A `received` event: automations are told not to act on it      [documented]
   └─ 4b. A forged `contacts/<key>` position: a presence automation       [SEC-005]
```

### 5.2 Make the household believe an alert was delivered when it was not (A2)

```text
GOAL: `delivery: confirmed` without the recipient having the message
├─ 1. Forge the acknowledgement (T1)
│  ├─ 1a. Know the text (fixed alert texts; the `delivery` topic if readable) AND the
│  │      timestamp (the second the gateway sent it; observable on air ±1 s) AND the attempt
│  │      (0 first) AND the radio's public key (in every advert and the contact link)
│  │      → SHA256(ts ‖ attempt ‖ text ‖ key)[:4] in a clear ACK packet   [SEC-001: nothing]
│  └─ 1b. Replay an old ACK                                                [timestamp differs]
├─ 2. Be the radio over TCP and answer SEND_CONFIRMED (T5)                 [documented]
└─ 3. Publish a `delivery` event on the broker (T6)                        [the ACL]
```

### 5.3 Learn where a household member is (A3)

```text
GOAL: a position of a companion
├─ 1. Read it where the gateway put it
│  ├─ 1a. The broker's retained `contacts/<key>` (T6 with read)            [the ACL; TLS]
│  ├─ 1b. Home Assistant as any user (T7)                                  [SEC-008]
│  ├─ 1c. `state.json`, `history.sqlite3` (T9, T10)                        [0700, the sandbox]
│  └─ 1d. A generated dashboard file passed around                         [documented]
├─ 2. Read it on the air
│  ├─ 2a. The companion shares its position in adverts                     [the check warns]
│  ├─ 2b. Ask the companion yourself (T1/T3): it allows everyone, or you are a contact it
│  │      allows                                                           [the companion's
│  │                                                                        settings; documented]
│  └─ 2c. Decrypt the companion's telemetry answer to the gateway          [AES; the key]
└─ 3. Make the gateway reveal it
   ├─ 3a. Ask the gateway's radio for telemetry                            [profile denies]
   └─ 3b. The gateway's advert                                             [profile: no position]
```

### 5.4 Take over the radio (A5, A6)

```text
GOAL: control the radio's identity, table or settings
├─ 1. TCP transport (T5)
│  ├─ 1a. Connect to port 5000: export the private key (default build), import another,
│  │      factory reset, set radio parameters, add contacts with the remote-CLI flag
│  │                                                                        [SEC-003: documented
│  │                                                                         in general terms]
│  └─ 1b. Take the radio's address while it is off and answer the gateway  [documented]
├─ 2. USB (T9, T10)                                                        [the container]
└─ 3. Over the air
   ├─ 3a. Make the radio add a contact: automatic adding                   [profile: manual]
   └─ 3b. Remote CLI over the air                                          [the gateway never
                                                                            sets the flag; warns]
```

### 5.5 Deny alerts or presence (A2, A3, A11)

```text
GOAL: the household gets no alerts, or presence is wrong
├─ 1. Jam the channel (T1)                                                 [physics; out of scope]
├─ 2. Forge ACKs so retries stop (T1)                                      [SEC-001]
├─ 3. Starve the radio's own duty cycle with DMs it must ack (T3)          [SEC-013]
├─ 4. Flood adverts: evict real nodes, grow the history, keep the gateway re-reading the table
│     (T1)                                                                 [SEC-010, SEC-011]
├─ 5. Take the gateway's MQTT client id (T6)                               [SEC-006]
├─ 6. Clear the gateway's retained availability or documents (T6)          [the ACL]
├─ 7. Remove a companion or exclude it from telemetry (T6, T7)             [SEC-008; the ACL]
├─ 8. Crash or hang the gateway through its inputs                         [none found; the
│                                                                           decoders are total]
└─ 9. Push the radio's clock into the future through the gateway (T12)     [SEC-002]
```

## 6 Where the data goes: a privacy view

| Data | Created by | Stored at | Retained for | Who can read |
| --- | --- | --- | --- | --- |
| Household positions | companions' telemetry answers | broker (retained), HA recorder, `history.sqlite3`, `state.json` (`current_contacts`) | broker: until overwritten or cleared; HA: 10 days default, statistics for ever; history: 30 days; state: while current | T6 (read ACL), T7, T9, T10 |
| Message texts in and out | the mesh, the automations | broker (`history` retained, 50 messages), HA recorder (event attributes), `history.sqlite3` | HA: 10 days; history: 30 days / 10,000 | the same |
| Who is near (nodes heard) | strangers' adverts | broker (`nodes` retained), HA recorder (`nodes` count, `node` events), `history.sqlite3` `node` table | history: 30 days | the same |
| Readings (batteries, temperatures) | companions | broker, HA recorder and statistics, history | HA statistics for ever | the same |
| The radio's key, name, settings | the radio | broker `state`, `settings`, HA attributes | retained | the same |
| Raw packets | everyone in range | broker `rx` (opt-in) | not retained | the same |

Nothing leaves the home network unless the broker, Home Assistant or the journal are reachable
from outside. The gateway itself makes no outbound connection but to the broker.

## 7 The project's threat table compared

Design §7.2 lists 15 threats. The reviewer's model covers each; the table says what the model
adds.

| Design §7.2 row | Covered by the model | What the model adds |
| --- | --- | --- |
| Spoofed command over the air | 5.1 / 1a–1c | – |
| Look-alike contact | 5.1 / 1b | the cost of a 48-bit collision is stated in §7.4; 11-research RES-003 quantifies it |
| Replayed command packet | 5.1 / 1c | – |
| Command injection into the automation system | 5.1 / 4 | 4b: a forged position is a command to a presence automation (SEC-005) |
| Abuse of the send topic | B3 D | – |
| Retained send request | B3 | – |
| Forged events by another broker client | 5.1 / 2 | the retained-document variant and the presence chain (SEC-005); the absence of a guard against an unauthenticated broker (SEC-004) |
| Unauthenticated TCP companion interface | 5.4 / 1 | the private key export in the default build (SEC-003) |
| Credential disclosure | B6 | the group-readable password and the alternative of a systemd credential (SEC-009) |
| Compromised dependency | B7 | the agent-authored, same-account-merged, auto-released pipeline (SUP) |
| Tampered release download | B7 | the unattested `main` installer path, `--ref` (OPS/SUP) |
| Privilege escalation in the service | B6 | – |
| Telemetry of someone outside the household | 5.3 / 2b | – |
| Presence decided on a position nobody confirmed | A3 | the forged-document variant (SEC-005) |
| A household companion shares its position in adverts | 5.3 / 2a | – |
| A forged advert in the packet log | B1 T | – |
| *not in the table* | 5.2 / 1a | **forged acknowledgements** (SEC-001) |
| *not in the table* | 5.5 / 9 | **the radio's clock pushed forward** (SEC-002) |
| *not in the table* | 5.5 / 3, 4 | **floods**: adverts, DMs from a contact, public-channel spam (SEC-010 to SEC-013) |
| *not in the table* | 5.5 / 5 | client-id squatting (SEC-006) |
| *not in the table* | B5 | the Home Assistant user as an actor (SEC-008) |
| *not in the table* | B4 | mesh-controlled names in the Home Assistant UI (SEC-007) |

The project's table is unusually complete for its age; the gaps are at the edges the project did
not draw as boundaries (the air as a source of acknowledgements and floods, the broker as a writer
of documents, Home Assistant's users, the operator's clock) rather than inside the command path,
which is where its attention went.

## 8 Assumptions the project makes, listed

Each is reasonable; each is also a place where the review looked for what happens when it fails.

1. The radio firmware is trusted (design §7.4). It is: but its interfaces (TCP, key export, the clock) are the gateway's problem once the gateway recommends and automates it.
2. The broker is secured by the operator. The gateway neither checks nor insists (SEC-004).
3. Only the gateway writes its topics. Nothing in the documents lets a consumer verify that (SEC-005, IMP).
4. Home Assistant's users are the household. Any user can act on any entity (SEC-008).
5. The host's clock is right (SEC-002).
6. The mesh is mostly benign and physics bounds the floods. The bounds in the gateway are partial (SEC-010 to SEC-013).
7. Texts the automations send are not secret. They are not: but they must not be *predictable* for the acknowledgement to mean anything (SEC-001).
