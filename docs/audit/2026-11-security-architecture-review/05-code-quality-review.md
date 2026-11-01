# 05 Code quality review

The code was read in full (`src/`, `tools/`, the installer, the test layout and the tests that
back claims made elsewhere in this review). Static checks: `ruff check` and `ruff format --check`
pass on `main`; `mypy --strict` passes in the project's own environment (in the reviewer's
sandbox it reported only a missing stub for `paho.mqtt.enums`, an artefact of the sandbox's
paho install). The unit, component and emulator suites pass (about 900 tests run here; the
broker, gettext, firmware and Home Assistant suites could not run in the sandbox). Items are
`CODE-nnn`.

## 1 General impression

The code is unusually consistent: one voice, one style of comment (a reason for every bound), one
way of handling errors, names that say what a thing is for. Modules have single responsibilities
and the pure core is really pure (`tests/test_architecture.py`). Types are complete and strict.
The tests are named as sentences and read as a specification. The weaknesses are those of a
program written fast by one author: the application modules are large, reasoning lives in
comments that reference documents outside the repository, and a few mechanisms that were right
for a small bridge (blocking file I/O on the loop, a multi-writer JSON state) have been carried
into a larger program.

## 2 Items

### CODE-001 A stall of the event loop longer than 5 s during a radio command ends the session although the answer arrived

| | |
| --- | --- |
| Category | code (robustness) |
| Component | `radio/session.py:738-757` (`_next`: `asyncio.wait({closed, frame}, timeout=self._timeout)`), `session.py:44` (`COMMAND_TIMEOUT_S = 5.0`), everything that blocks the loop (CODE-002) |
| Related | CODE-002, ARC-004, OPS-011 (CPU starvation in a container is an operational cause), IMP-020, RES-009 |

**Description.** `_next` waits for the answer frame with `asyncio.wait(..., timeout=5)`. If the
loop is blocked for longer than the timeout while the answer sits in the socket or the serial
buffer, both the I/O readiness and the timer are due when the loop resumes. asyncio runs the
I/O callback first (it feeds the transport's buffer and wakes the reader task) and the timer
callback second (it releases the waiter), both in one iteration; in the next iteration the reader
task runs and puts the frame into `pending.frames`, which wakes the getter task *after* the
waiter's task; the waiter resumes, sees the getter task not yet done, and `_next` raises "no
answer from the radio", ending the session. The answer is then discarded with the session, the
bridge reconnects (1 s back-off, flush, handshake, table read: several seconds), and whatever was
in flight (a send, a telemetry request) is retried or lost.

**Evidence.** Experiment (`experiments/loop_stall_session.py`, recorded in `11-research-notes.md`
RES-009): a `CompanionSession` over a socket pair to a thread that answers 1 s after each
command; `loop.call_later(0.2, time.sleep, stall_s)` stalls the loop while `get_battery` waits:

```text
stall 0.0 s, answer after 1.0 s: battery 4012 mV; closed False
stall 6.0 s, answer after 1.0 s: SESSION FAILED: no answer from the radio
stall 4.0 s, answer after 1.0 s: battery 4012 mV; closed False
stall 6.0 s, answer after 4.0 s: SESSION FAILED: no answer from the radio
```

What can stall the loop for 5 s: the hourly `history.purge` on large tables on slow storage
(the `reading` cap uses a correlated subquery over up to 200,000 rows); a burst of `state.json`
writes, each with two `fsync`s, on a loaded host; and, outside the program, **CPU starvation of
the container** (a Proxmox host running `vzdump` or a heavy VM can leave a one-core container
without CPU for seconds), or a host swapping. The last two are the realistic ones and the
program cannot prevent them; it can avoid turning them into a session loss.

**Preconditions and attacker model.** none (an operational condition).

**Impact.** Radio reconnects under host load; messages in flight repeated (an attempt the radio
already sent is repeated as the identical packet, which earns no acknowledgement, `design §6.2`
step 7) or delayed; telemetry answers dropped; log noise that points at the radio. For a gateway
whose host is a shared Proxmox server, "the radio disconnects every night at backup time" is a
plausible bug report with a confusing cause.

**Existing mitigations.** The reconnect is fast and complete; an interrupted attempt is repeated.

**Suggested mitigation or implementation.**

1. **Give the reader one iteration after a timeout before failing.** In `_next`, when the waiter
   fired on the timeout, check whether the loop was late (compare `loop.time()` at the timeout
   with the deadline; a lateness above, say, 0.5 s means a stall) and, in any case, yield once
   (`await asyncio.sleep(0)` twice: once for the reader task, once for the getter) and re-check
   `frame.done()` or `pending.frames.qsize()` before raising. Sketch:

   ```python
   if frame not in done:
       for _ in range(2):          # a stalled loop: the reader and the getter run first
           await asyncio.sleep(0)
       if frame.done():
           return frame.result()
   ```

   This closes the window of the experiment; it does not help when the answer has genuinely not
   arrived, which is the timeout's purpose.
2. **Measure the stall and say so**: a watchdog-like check in the status loop that records the
   maximum lateness of its own tick (`loop.time()` after `sleep` minus the expected time) and
   logs a warning when it exceeds a second; publish it as `loop_lag_max_s` in the health section
   (ARC-012). Operators then see "the host starves the container" instead of "the radio drops".
3. **Move blocking work off the loop** (CODE-002).

Test: `tests/test_session.py` — a transport whose `read` returns the answer after the loop was
blocked for 6 s (a `time.sleep` in a callback) still yields the answer.

**References.** `asyncio` event loop internals (`BaseEventLoop._run_once`: I/O callbacks before due
timers); `docs/design.md` §5.2.

---

### CODE-002 Blocking I/O on the event loop: `fsync` per state write, SQLite per event, the hourly purge

| | |
| --- | --- |
| Category | code (concurrency) |
| Component | `state.JsonStateFile.save` (two `fsync`s, called by nine classes), `history.py` (every `add_message`, `add_position`, `add_readings`, `node_heard` is a synchronous `execute`; `purge` runs up to six statements including correlated subqueries), `observer.tick` (purge hourly), `observer._store_heard` (a write per advert) |
| Related | CODE-001, ARC-004, SEC-010, ADR 0016 ("if they ever stall it, they move to a worker thread") |

**Description.** All persistence runs synchronously on the loop that also serves the radio
(5 s command timeout), the broker and the watchdog. Each `state.json` write is read-modify-write
with `fsync(file)` and `fsync(dir)`; the ledger writes once per accepted command (necessarily
before the publish), the contact device records at every table read that changed something, the
current documents at every tick and at every change of currency, the telemetry schedule every
5 minutes and at every flood, the advert checks at every table read. SQLite writes happen per
message, per position, per answer (one transaction of up to 16 rows), per advert heard (an
upsert). On an SSD-backed LVM volume this is microseconds to a few milliseconds each; on a
Proxmox host with spinning disks and ZFS sync writes, an `fsync` is tens of milliseconds, and
the hourly purge over 300,000 rows is seconds. None of it is measured; the project has no timing
instrumentation for the loop.

**Evidence.** Reading; the experiment of CODE-001 shows what a 5 s stall does.

**Preconditions and attacker model.** none; an attacker can only raise the rate (SEC-010's advert
flood makes a SQLite write per advert).

**Impact.** Latency of everything on the loop in proportion to disk latency; CODE-001 beyond 5 s.

**Existing mitigations.** Writes are few at rest (a companion at rest produces no position rows;
documents are stored only when changed); the WAL and `synchronous = NORMAL` keep SQLite's own
cost low; `JsonStateFile.save` is used sparingly by most classes.

**Suggested mitigation or implementation.**

1. **Purge in a thread**: `await asyncio.to_thread(history.purge, now)` with a second SQLite
   connection owned by the thread (SQLite connections are not shareable across threads with
   `check_same_thread=True`; opening a second connection for the purge is cheap, and WAL allows a
   writer and readers concurrently; two writers serialise on SQLite's lock with `busy_timeout`,
   which should be set, for example 5 s). The purge is the one operation with unbounded cost.
2. **Batch the state writes**: ARC-004's single store with one write per tick (and `save_now()`
   for the ledger) reduces the `fsync` count to the minimum; a write of the ledger could also
   use `fdatasync` and skip the directory `fsync` when the file already existed (the rename's
   durability needs the directory sync; keeping it is the conservative choice).
3. **A thread for all of `History`**: an `asyncio.Queue` of write operations drained by one
   worker thread with its own connection, as ADR 0016 anticipated; reads at start stay
   synchronous (before any task runs). This is more change than 1 and only needed if measurements
   show the per-event writes matter.
4. **Instrument first** (CODE-001's loop-lag measure) so that the team knows whether any of this
   is needed on the reference hardware.

Tests: `tests/test_history.py` — purge runs in a thread and the loop keeps serving (a timer fires
during a slow purge made slow by a large table).

**References.** `sqlite3.connect(check_same_thread=…)`, `PRAGMA busy_timeout`; ADR 0016.

---

### CODE-003 Two state sections grow without bound: `slugs` and `radio_profile`

| | |
| --- | --- |
| Category | code (resource bound) |
| Component | `state.ContactDeviceRecords.slugs` (`records.slugs[key] = slug` in `contact_devices._announce`, never removed: "also to contacts that left: they get theirs back"), `state.ProfileRecords.save` (one entry per radio key ever connected, never removed) |
| Related | ARC-004 |

**Description.** The slug map keeps one entry per contact that ever had a device, so that a
contact that returns gets its entity IDs back. Contacts come and go (the radio's table turns
over; a club's radio sees hundreds), and nothing prunes the map. `radio_profile` keeps an entry
per radio key, which grows only when radios are replaced (rare). Both are rewritten with every
save of `state.json`. At 100 bytes per slug entry, 10,000 contacts over the years are 1 MB in a
file that the ledger rewrites before every command.

**Evidence.** `contact_devices.py:574-575`, `state.py:199,239`; `state.py:534`.

**Impact.** Slow growth of the state file and of every write to it; no functional failure.

**Suggested mitigation or implementation.** Prune `slugs` of keys that are neither in the table
nor announced nor hidden, after a grace of, say, 90 days (store `since` with the slug, or prune
to the newest 1,000 by last use); prune `radio_profile` to the current radio's key plus the last
two. A test that 2,000 contacts passing through leave the map bounded.

---

### CODE-004 A section of the state file is re-saved only when it changes, so a replaced file loses the others' sections until then

| | |
| --- | --- |
| Category | code (persistence edge case) |
| Component | `state.JsonStateFile.load` (a corrupt file is moved aside, `{}` returned), every section's `save`/`replace` (loads from disk, writes its key, writes the file), `ContactDeviceRecords.save` and `CurrentDocuments.replace` (skip when unchanged) |
| Related | ARC-004, OPS-008 (restore from backup while running), IMP-021 |

**Description.** Each section keeps its own in-memory copy and re-reads the file from disk when
it saves. If the file on disk is replaced by something without the other sections (a corrupt
file moved aside by the first loader; a backup restored by an operator while the service runs;
a manual edit that drops keys), the next writer writes a file containing its own section plus
whatever the new file had. Sections that save only on change (`ha_contact_devices`,
`ha_components`, `current_contacts`, `advert_checks`) stay absent on disk until they next
change, which can be days; a restart meanwhile starts those sections empty: Home Assistant
devices are announced afresh (duplicates are prevented by unique IDs; removals pending in
`ha_removed` are lost, so leftover entities can stay), slugs are chosen anew (entity IDs change
for contacts whose slug was taken), the current documents are forgotten.

**Evidence.** `state.py:43-64,213-226,290-302`.

**Impact.** Rare; the consequences are cosmetic to mildly annoying (entity ID changes, leftover
entities). Listed because ARC-004's store would remove the class of problem.

**Suggested mitigation or implementation.** ARC-004 (one in-memory document, one writer). Short
of that: `JsonStateFile.save` could merge the caller's section into the *in-memory last-known
document* rather than the file's content when the file on disk is missing or corrupt; or each
section could write unconditionally at start once (a "touch" after load).

---

### CODE-005 Size and complexity hotspots

| | |
| --- | --- |
| Category | code (maintainability) |
| Component | `cli.py` 1,657 lines (29 handlers, the probe report, the contact session helper, the dashboard, render/answers), `bridge.py` 1,164, `telemetry_poller.py` 896, `observer.py` 844, `core/telemetry.py` 728, `config.py` 760, `state.py` 708, `mqtt/contact_devices.py` 633; `tests/test_telemetry_poller.py` 2,469, `test_observer.py` 2,324, `test_bridge.py` 2,268 |
| Related | ARC-007, ARC-009 |

**Description.** `ruff`'s `PLR0912`/`PLR0915` (branches, statements) are enabled and pass, so no
single function is extreme; the size is in the number of concerns per module. `core/telemetry.py`
holds 35 module constants, 7 dataclasses, a `Scheduler` with 30 methods and a state function with
eight outcomes; its docstring and design §6.11 are the only maps, and §6.11 is four pages of
prose without a diagram. The poller adds the asynchronous half (answer waiting by frame number,
held answers, late answers, the TCP allowances, the first-start hold). A newcomer needs a day to
read these two files; the tests (2,469 lines) are the real specification.

**Suggested mitigation or implementation.** (a) A state diagram and a sequence diagram for the
telemetry subsystem in `docs/design.md` §6.11 (Mermaid, as the rest of the document); (b)
split `cli.py` into `cli/` (`run.py`, `probe.py`, `contacts.py`, `config_cmd.py`, `dashboard.py`)
with the parser in `cli/__init__.py`; (c) split `bridge.py` as ARC-009 suggests; (d) split the
three largest test files by scenario group. None changes behaviour.

---

### CODE-006 Comments cite design identifiers that are not in the repository

| | |
| --- | --- |
| Category | code (documentation in code) |
| Component | 34 comments and docstrings in `observer.py`, `core/adverts.py`, `core/mesh.py`, `views.py`, `telemetry_poller.py`, `state.py` of the form "design 6c §3.1", "design 6c D4, D5", "D13", "D27", "D9"; `docs/design.md` contains no "6c", no "D13" |
| Related | DOC-011, IMP-030 |

**Description.** The telemetry and advert-check code references decisions of a design sprint
("6c" is the third telemetry slice in the roadmap, design §13; "D1…D27" are numbered decisions
of that slice's design) whose document is not in the repository. A reader of `observer._own_log`
("design 6c D13") has nothing to open. The ADRs 0024 and 0025 and design §6.6 contain the
substance, but not under those numbers.

**Suggested mitigation or implementation.** Either add the sprint design to `docs/` (as
`docs/design-telemetry.md` or an appendix of `design.md` with the D-numbers, if it exists as a
file) or replace each reference with the anchor that holds the rule today (`design §6.6`, ADR 0025
§"The check"). A grep-able convention (`[design §6.6]`, `[ADR 0025]`) and a test that every such
reference resolves to a heading would keep it true (the project already tests links in Markdown;
`tests/test_docs.py` could be extended to docstrings).

---

### CODE-007 `# fmt: skip` on 205 lines

| | |
| --- | --- |
| Category | code (style) |
| Component | `src/` (205 `# fmt: skip`, 49 `# noqa`), `pyproject.toml` (`ruff format`) |
| Related | none |

**Description.** The code uses `ruff format` and then opts out on a fifth of the non-trivial
statements to keep arguments on fewer lines (`log.warning("…", a, b)  # fmt: skip`, dict
literals, multi-line conditions). The result is two visual styles in one file and a maintenance
rule ("add `# fmt: skip` when the formatter would spread this") that only the author knows. It is
a choice, not a defect; the review notes it because every reader pays for it and because `ruff
format` has no setting that would give the compact style.

**Suggested mitigation or implementation.** Decide once: accept the formatter's output
(remove the markers in one commit; the diff is mechanical and the tests do not change), or
document the convention in `CONTRIBUTING.md` ("compact calls are kept with `# fmt: skip` when
the formatted form would exceed N lines"). The first is cheaper to keep.

---

### CODE-008 Twenty-eight broad `except Exception` handlers

| | |
| --- | --- |
| Category | code (error handling) |
| Component | `bridge._observe`, `bridge._radio_loop`, `bridge._outbox_loop`, `bridge._message_loop`, `bridge._telemetry_state`, `observer` (listeners, four places), `contact_manager.run` (nested), `contact_manager._excluded`, `telemetry_poller` (several), `session._apply_profile`, `session._maintenance`, `session._read_loop`, `state.TelemetryRecords.load`/`ran_before`, `mqtt/client` (none: good) |
| Related | ARC-012 |

**Description.** Each handler logs with `log.exception` (or at a limited rate) and continues,
which is the project's stated policy. Each is annotated with a reason. Two consequences: (1) a
programming error in a subsystem can repeat on every event (the poller limits its state-error
log to once a minute; the observer does not), and (2) `asyncio.CancelledError` is a
`BaseException` since 3.8 so the handlers do not swallow cancellation (correct), but
`KeyboardInterrupt`/`SystemExit` are also not caught (correct). The pattern is sound; what is
missing is a count that reaches the operator other than through the journal.

**Suggested mitigation or implementation.** A module-level counter per subsystem incremented in
each handler, exposed in the health section (ARC-012), and a rate limit on `log.exception` in the
observer's listeners like the poller's `STATE_ERROR_LOG_S`.

---

### CODE-009 The test strategy: what it covers well and what it does not

| | |
| --- | --- |
| Category | code (tests) |
| Component | `tests/` (1,470 test functions in the main suite, 90 % branch coverage gate), `home-assistant/tests/` (contract), `tests/firmware/` (real firmware), `tests/manual/` |
| Related | ADR 0026, IMP-017 |

**Description.** Strengths: every rule has a test named after it; the clock is injected everywhere
so timing tests are deterministic; the emulator covers the session and bridge against the
firmware's documented behaviour, and the firmware tests cover it against the firmware itself;
contract tests run the real Home Assistant; the installer runs against stubs of the Proxmox
tools; the snapshot guards the public interface; the docs' links and examples are tested. Gaps:

1. **No property-based or fuzz testing** of the parsers that face the air and the bus:
   `protocol/codec.decode`, `decode_message`, `protocol/packet.parse_packet`, `parse_advert`,
   `core/lpp.decode`, `protocol/links.parse_contact_text`, `core/text.cut_utf8`, `catalog.py`'s
   PO reader, `config.render_config`. The decoders are written to be total and the review found
   no crash, but a generator would find what reading cannot. Hypothesis is a development
   dependency (allowed by ADR 0005), hash-pinned like the others.
2. **No test of behaviour under loop stalls or slow I/O** (CODE-001).
3. **No test of resource bounds under floods** (SEC-010, SEC-011): the suites test that bounds
   exist where they exist, not that a flood stays within them.
4. **The emulator is a second implementation** of the firmware's answers (ADR 0026 names the
   risk: "a misreading passes in both places"); the firmware tests mitigate it for the scenarios
   they play (steps 29–43), not for the message path, which has the longest history and the
   hardware checklist.
5. **Coverage is measured, mutation is not**: with 1,470 tests and 90 %+ coverage, a mutation
   score would tell whether the tests check behaviour or only execute it (the sentences in the
   names suggest the former; `mutmut` or `cosmic-ray` would say).
6. **Test files of 2,000+ lines** (CODE-005).

**Suggested mitigation or implementation.** Add `hypothesis` to `requirements/dev.in` and
property tests for the parsers above (a fuzz-like `binary()` strategy for frames and packets,
`text()` for links and names, with the invariant "returns or raises `ProtocolError`/`LinkError`,
never anything else, in bounded time"); a stall test for `_next`; flood tests with the emulator;
a one-off mutation run on `core/` to calibrate.

---

### CODE-010 Minor observations

| ID | Observation | Where | Suggestion |
| --- | --- | --- | --- |
| CODE-010 | The `Bus` protocol in `bridge.py` restates `MqttBus`'s public surface; a change in one is caught only by mypy at the call sites | `bridge.py:126-140` | Move `Bus` to `ports.py` (ARC-003) and make `MqttBus` declare it |
| CODE-011 | `session.refresh_channels` reads every channel slot (`max_channels`, up to 40 on some builds) with one command each at every connect | `session.py:521-530` | Acceptable (connects are rare); note the cost in the docstring |
| CODE-012 | `AckWaiter._recent` keeps late acknowledgements for 120 s without a count bound; only matched codes reach it (the radio pushes only matched ones), so it is bounded by the radio's 8-entry table | `bridge.py:161-199` | None; a comment that the bound is the radio's table |
| CODE-013 | `history.purge` runs six statements, two with correlated subqueries, inside autocommit (each its own transaction) | `history.py:186-212` | Wrap the purge in one transaction; run it in a thread (CODE-002) |
| CODE-014 | `config._convert` accepts `"nan"` and `"inf"` for floats and relies on the range check to refuse them (it does, since comparisons with NaN are false) | `config.py:716-730` | A test that `airtime.burst_s=nan` is refused with a readable message |
| CODE-015 | `config._toml_string` writes U+2028/U+2029 and other non-ASCII unescaped, which TOML allows; a `check-config` of such a file is fine; an editor may not show them | `config.py:751-760` | Escape `Zl`/`Zp` as `\uXXXX` for the operator's sake |
| CODE-016 | The CLI imports heavy modules inside handlers for a fast `--version` (`# noqa: PLC0415`) | `cli.py` | Fine; the pattern is documented in the comment |
| CODE-017 | `bridge._deliver`'s `while sent < attempts` loop `continue`s on `RadioError` without a counter; it is bounded by `_radio_until(deadline)` | `bridge.py:607-626` | A comment naming the bound |
| CODE-018 | `observer._advert` publishes the `nodes` document and the history row on every stranger's advert (SEC-011) | `observer.py:646-649` | As SEC-011 |
| CODE-019 | `mqtt/client.MqttBus.publish` logs a warning per refused publish with paho's numeric error; during a disconnect paho returns `MQTT_ERR_NO_CONN`, which is handled, but a full paho queue (`max_queued_messages_set(0)` = unlimited) cannot happen; the 1000-event bound is the adapter's own | `client.py:176-192` | None; the design is documented |
| CODE-020 | `cli.main` decides the language before parsing and catches `KeyboardInterrupt` as exit 1 | `cli.py:72-85` | None |
| CODE-021 | The installer builds `bash -c` scripts for the container by string interpolation of shell constants (`$STAGE`, `$LIB`, `$CONFIG`, `$ANSWERS_FILE`) and one validated variable (`$SOURCE`) | `meshcore-home-gateway-lxc.sh:476-500,786-810,1187-1192` | Fine; a comment that only constants may be interpolated would keep it so |
| CODE-022 | `tools/release_plan.py` and `tools/public_interface.py` (nearly 1,000 lines) are run with `python3 -I` in CI and tested; they are the only tooling that decides a release | `tools/` | A note in `docs/development.md` that they are release-critical and reviewed as such |
| CODE-023 | The firmware test shims (`tests/firmware/shim`, `board`) are C++ stand-ins the project maintains per firmware release | `tests/firmware/` | A `README` in the directory naming what each stand-in replaces and why (the ADR says it; the directory does not) |
| CODE-024 | Comments and docstrings carry design reasoning at length (a strength for context, a cost for reading a function's body) | throughout | Keep; prefer references to the design where the paragraph exists there already |
| CODE-025 | `MqttBus.stop` publishes `offline` and waits up to 2.5 s; the unit's `TimeoutStopSec=20` and the bridge's 2 s drain add up to about 5 s at stop, leaving 15 s unused | `client.py:194-208`, the unit | Use the room for ARC-013's drain of the queue |

## 3 Numbers

| Measure | Value (`main`) |
| --- | --- |
| Source lines | 15,125 (`src/`) |
| Test lines | ≈ 43,000 (`tests/`, `home-assistant/tests/`) |
| Test functions | 1,609 (1,470 main suite, 60 Home Assistant, 79 firmware) |
| Modules in `src/` | 45 |
| Largest module | `cli.py`, 1,657 lines |
| `# fmt: skip` | 205; `# noqa` 49; `except Exception` 28 |
| Runtime dependencies | 1 (`paho-mqtt`) |
| Development dependencies (direct) | 9 (`requirements/dev.in`) |
| Coverage gate | 90 % branch (`pyproject.toml`); PR #3 reported 98 % |
