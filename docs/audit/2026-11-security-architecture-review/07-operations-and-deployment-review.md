# 07 Operations and deployment review

Scope: the Proxmox installer (`deploy/proxmox/meshcore-home-gateway-lxc.sh`), the systemd unit,
the container layout, update, rollback, reconfigure, backup and restore, logging and monitoring,
and what an operator meets in the first month. The reviewer could not run the installer against
a Proxmox host; the script was read in full and its tests (`tests/test_installer.py`,
`test_installer_language.py`, 3,000 lines against stubs) were read for what they cover. Items are
`OPS-nnn`.

## 1 General impression

The deployment story is complete and unusually careful: one command, dialogs that read the
radio, a clean environment in the container, the program downloaded inside the container and
checked against the release's checksums, a validated configuration written by the program itself,
a service with a systemd exposure score of 1.6, documented update, rollback and reconfigure
paths, and tests of the script against stubs of every Proxmox tool. The gaps are in what happens
*after* the first day: security updates of the container, the behaviour at host boot and USB
re-plug, crash loops, backups that carry secrets and positions, and the absence of a signal
that the gateway is degraded.

## 2 Items

### OPS-001 Security updates in the container are not automated

| | |
| --- | --- |
| Category | operations (patching) |
| Component | `install_service` (installs `python3 python3-paho-mqtt ca-certificates`, nothing else), `docs/deployment.md` (no mention of `apt upgrade` or `unattended-upgrades`), ADR 0005 (Decision Drivers: "Security updates without our involvement"; Consequences: "receive Debian security updates") |
| Related | SUP-005, ARC-007, IMP-023 |

**Description.** ADR 0005's main argument for taking `paho-mqtt` from Debian rather than PyPI is
that Debian ships security updates. Updates reach a Debian system only if something installs
them: `unattended-upgrades` (not part of Proxmox's Debian container templates by default) or an
operator running `apt upgrade`. The installer installs neither and the documentation never
mentions updates of the container's packages (only of the gateway, `--update`). A container
installed today will run the same `python3`, `openssl`, `libsqlite3` and `paho` for years, and
the argument that justified the dependency policy is not realised.

**Evidence.** `install_service`, lines 778–780; `grep -n unattended docs/` finds only the
"unattended installation" of the installer.

**Preconditions and attacker model.** T6, T5 or T1 with an exploit for a vulnerability fixed in
Debian after the installation (OpenSSL, the TLS stack used towards the broker; SQLite; CPython's
`json`, `ssl`, `tarfile`).

**Impact.** The container's attack surface ages. The gateway's own update mechanism keeps the
program fresh and the platform stale.

**Existing mitigations.** The container is unprivileged and the service sandboxed; the exposed
surface is small (one outbound TLS connection, one serial device).

**Suggested mitigation or implementation.**

1. Install and enable `unattended-upgrades` in `install_service` (`apt-get install -y
   unattended-upgrades`; Debian's default configuration applies security updates daily and needs
   no further setup; `Unattended-Upgrade::Automatic-Reboot` is off, which is right, since nothing
   the gateway uses needs a reboot but the kernel, which is the host's).
2. Say in `docs/deployment.md` §4 that the container is a Debian system that needs updates, how
   `unattended-upgrades` is configured, and that `--update` updates only the gateway.
3. Consider a `--update` that also runs `apt-get upgrade` in the container (one command for both),
   with the service restarted once.
4. A line in the health section (ARC-012) or the final installer message about pending updates
   is more than needed.

Test: `tests/test_installer.py` — the package list of the `apt-get install` call includes
`unattended-upgrades`.

**References.** Debian wiki "UnattendedUpgrades"; ADR 0005.

---

### OPS-002 The installer's integrity chain: checksums from the same release, no attestation in the container, an unverifiable `main` path

| | |
| --- | --- |
| Category | operations (integrity of what is installed) |
| Component | `stage_program` and `FETCH_PY` (download `SHA256SUMS` and the files from the release, compare), `pack_text` (the language pack from the release or `main`, checked against a digest in the script), `detect_source` (`ref`: `https://github.com/$REPOSITORY/archive/$REF.tar.gz`, "HTTPS is the only check"), `docs/deployment.md` §2 (the `gh attestation verify` path for the installer itself), `make release` (writes `RELEASE="X.Y.Z"` into the installer) |
| Related | SUP-003, ADR 0013, ADR 0022, IMP-023 |

**Description.** Three levels of assurance exist and the documentation names them; the review
adds what each proves:

1. **`bash -c "$(wget -qO- …releases/latest/download/…)"`** (the README's first command): the
   installer is fetched over HTTPS from GitHub and executed as root on the Proxmox host without
   any check. Its authenticity rests on GitHub's TLS and on nobody having replaced the asset.
   GitHub lets anyone with write access to the repository **edit or replace release assets**
   after publication without changing the tag (the ruleset protects tags, not assets); the
   attestation would then no longer match, but this path never checks it.
2. **Inside the container**, the zipapp and the unit are downloaded from the same release and
   compared with that release's `SHA256SUMS`. This detects a corrupt or partial download and a
   mismatch between files; it does not prove authenticity, since whoever can replace the zipapp
   can replace `SHA256SUMS` too (same place, same access).
3. **The documented verification** (download the installer, `gh attestation verify` with
   `--signer-workflow` and `--source-ref`, then run it) proves that *the installer* was built by
   the release workflow from the tag. The program it then downloads is checked only as in 2.

So a verified installer installs an unverified program, and the common path verifies nothing.
The `main` and `--ref` paths are documented as unverified. The language packs *are* pinned by
digest in the installer (good: a verified installer implies verified packs).

**Preconditions and attacker model.** T11: write access to the repository's releases (a
compromised maintainer account or token, or a malicious workflow in a merged PR with
`contents: write`: the `release` job has it), or a man-in-the-middle of GitHub's TLS (a CA
compromise; out of scope).

**Impact.** Every new installation and every `--update` runs what the attacker put in the release
asset, as root in a container with the radio and the broker credentials.

**Existing mitigations.** The documented verification path; HTTPS; the tag ruleset; attestations
(ADR 0013); `zizmor`; the reproducible build (a third party can rebuild and compare).

**Suggested mitigation or implementation.**

1. **Pin the program's digests in the published installer.** `make release` already writes the
   version into the installer; it can write `PYZ_SHA256="…"` and `UNIT_SHA256="…"` (it has the
   files in `dist/release/` before it writes `SHA256SUMS`). `FETCH_PY` compares the downloads
   with the pinned digests instead of (or in addition to) the downloaded `SHA256SUMS`. Then the
   verified installer (path 3) implies a verified program, and replacing the zipapp asset no
   longer works without replacing the installer asset too, which breaks the installer's own
   attestation. Cost: one `sed` more in `make release` and a test that the published installer
   pins the digests of the published files (`tests/test_installer*.py` already checks the
   language packs' pins the same way).
2. **Make the verified path the first one in the README** (download, verify, run), with the
   one-liner as the alternative "if you trust GitHub's TLS and the release assets".
3. **Verify the attestation inside the container** where possible: Debian 13 packages `gh`
   (`gh` 2.x); `gh attestation verify` needs a GitHub token even for public repositories (a
   limitation the operator would have to meet) — so this is optional and the digest pin (1) is
   the practical measure. Offline verification of the Sigstore bundle would need
   `sigstore-python` or `cosign`, neither in Debian; a note in the documentation that the bundle
   can be verified on another machine is enough.
4. **The `main` path**: pin by commit (`--ref <sha>`) in the documentation's examples instead of
   `main`, so that a reader who follows it gets what the documentation was written against.

Test: `tests/test_installer.py` — a release whose zipapp does not match the installer's pinned
digest is refused before anything is installed.

**References.** GitHub docs "Managing releases in a repository" (editing assets); SLSA v1.0
"verifying artifacts"; ADR 0013.

---

### OPS-003 The container does not start without the radio, and a re-plugged radio needs a container reboot

| | |
| --- | --- |
| Category | operations (availability) |
| Component | `create_container` (`pct set --dev0 path=…,gid=…,mode=0660`), ADR 0006 Consequences ("the container does not start while the radio is unplugged (also at host boot)"), `docs/deployment.md` §2 ("after re-plugging the radio, `pct reboot 105`"), §5 troubleshooting |
| Related | OPS-004 |

**Description.** Proxmox resolves a `dev0` device path when the container starts and refuses to
start the container when it does not exist; it binds the device node once, so a radio that
re-enumerates (a USB hiccup, a power cut on a hub, a firmware update of the radio) leaves the
container with a dead node until `pct reboot`. At host boot, USB enumeration and container
start race; a container with `onboot=1` whose radio is slow to enumerate fails to start and
stays stopped until an operator notices. The documentation is honest about both. For a gateway
whose purpose is to work when other things fail, "the whole container is down because the USB
device blinked" is the weakest point of the deployment.

**Evidence.** The ADR and the documentation; `pve-container`'s `Tools.pm` behaviour cited there.

**Preconditions and attacker model.** none (USB and power events; an attacker with physical
access needs nothing cleverer).

**Impact.** Total loss of the gateway (not only the radio link) until a manual `pct start` or
`pct reboot`; no alert of its own reaches anyone (the broker's last will marks it offline, and a
Home Assistant automation can notice that: the guide has no such example).

**Existing mitigations.** The documentation; `onboot=1`; the gateway's own reconnect loop handles
the case where the node stays valid.

**Suggested mitigation or implementation.**

1. **A host-side udev rule** installed by the installer (the host keeps nothing today, which
   ADR 0006 values; this would be the one file, and it is small): on `add` of the radio's USB
   device (matched by vendor, product and serial, which `/dev/serial/by-id` encodes), run
   `pct set <ctid> --dev0 path=…` and `pct start` or `pct reboot <ctid>` as appropriate. The
   installer knows the by-id path and the container id. This turns re-plug and boot races into
   automatic recovery. Document it as optional ("`--host-udev-rule`").
2. **Alternatively a host systemd timer** that checks the container every minute and starts it
   when it is stopped and the device exists (simpler, slower).
3. **Pass the device by its stable path and let the container handle re-enumeration**: Proxmox
   binds the *node*, not the symlink, so the stable path does not survive a new minor number;
   this is a Proxmox limitation (ADR 0006's revisit clause names hot-plug).
4. **A Home Assistant example** in `docs/home-assistant.md`: "notify me when the gateway has been
   unavailable for 5 minutes" (a state trigger on `binary_sensor.meshcore_home_gateway_radio1_radio`
   to `unavailable` with `for:`). This is the cheapest and belongs in the guide regardless.

Test: the installer tests can check the generated udev rule's content; the behaviour needs a
host.

**References.** `pct.conf(5)` `dev[n]`; `udev(7)`; the Proxmox forum thread ADR 0006 cites.

---

### OPS-004 A crash loop ends in a permanently failed unit with no notification

| | |
| --- | --- |
| Category | operations (availability, alerting) |
| Component | `deploy/systemd/meshcore-home-gateway.service` (`Restart=on-failure`, `RestartSec=5`, `StartLimitIntervalSec=600`, `StartLimitBurst=20`, no `OnFailure=`), `WatchdogSec=60` |
| Related | OPS-003 (the same silence), ARC-012 |

**Description.** Twenty failures within ten minutes (a crash at start on a corrupt state file
that is not JSON-corrupt but semantically bad, a radio that answers garbage, a bug) put the unit
into the `failed` state, from which systemd does not restart it until `systemctl reset-failed`.
The last will on the broker marks the gateway offline; nothing else happens. An operator who does
not watch Home Assistant learns of it when an alert does not come.

**Evidence.** The unit; `systemd.unit(5)` `StartLimitIntervalSec`.

**Impact.** Silent, lasting outage after a transient fault storm.

**Suggested mitigation or implementation.**

1. `OnFailure=meshcore-home-gateway-failed.service`, a tiny unit that logs loudly (`wall`-style
   journal line at `crit`) and, where the operator configured it, runs a hook script (the
   container has no mail; a `curl` to a webhook, or `mosquitto_pub` of a last-resort message with
   the credentials from the configuration, would be the gateway's own channel: an
   `OnFailure` unit running `meshcore-home-gateway send --wait 0 alerts "gateway failed"` cannot
   work because the gateway *is* the sender; a direct MQTT publish of a retained
   `<root>/failed` document could).
2. Reconsider the limits: `StartLimitBurst=20` within 600 s with `RestartSec=5` is 100 s of
   trying; a gateway might better try for ever with a growing `RestartSec` (`RestartSteps=` and
   `RestartMaxDelaySec=` exist since systemd 254, Debian 13 has 257) than stop.
3. The Home Assistant example of OPS-003 covers the detection side.

**References.** `systemd.service(5)` `OnFailure=`, `RestartSteps=`, `RestartMaxDelaySec=`.

---

### OPS-005 The unit's hardening: what else is available

| | |
| --- | --- |
| Category | operations (defence in depth) |
| Component | `deploy/systemd/meshcore-home-gateway.service` |
| Related | SEC-002, SEC-009, CODE-002 |

**Description.** The unit is already at the point where `systemd-analyze security` scores 1.6
("safe"); the remaining directives are small and each has a reason not to be there. For the
record:

| Directive | Value today | Consideration |
| --- | --- | --- |
| `After=time-sync.target` | absent | SEC-002: do not start before the clock is synchronised (helps outside LXC; harmless inside) |
| `LoadCredential=` | absent | SEC-009 |
| `MemoryMax=` | absent | The container has 256 MB; a leak in the gateway would make the container's OOM killer choose; `MemoryMax=192M` makes the gateway the one killed and restarted |
| `OOMPolicy=kill`, `OOMScoreAdjust=` | absent | With `MemoryMax`, `OOMPolicy=kill` is the default behaviour |
| `IPAddressDeny=any` + `IPAddressAllow=<broker>` | absent | Only when the broker has a fixed address; DNS is then not needed; makes the sandbox network-level too. Optional, documented, not default |
| `DevicePolicy=closed` + `DeviceAllow=` | absent | Documented why (unprivileged containers do not enforce it) |
| `PrivateUsers=yes` | absent | Not usable in an unprivileged container with a device node; correct to omit |
| `ProtectSystem=strict` | yes | The configuration directory is read-only to the service: good |
| `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | yes | `AF_UNIX` is needed for `sd_notify` |
| `SystemCallFilter=@system-service`, `~@privileged @resources` | yes | Good; `@obsolete` could be added |
| `RemoveIPC=yes`, `KeyringMode=private` | absent | Marginal |
| `TimeoutStopSec=20` | yes | ARC-013: a drain needs up to 60 s |

**Suggested mitigation or implementation.** Add `After=time-sync.target`, `MemoryMax=`, and
`@obsolete` to the filter; document `IPAddressAllow` as an option; the rest with the related
items.

---

### OPS-006 Restarts and updates drop the queue; the stop timeout is shorter than an acknowledgement wait

| | |
| --- | --- |
| Category | operations |
| Component | `update()` and `reconfigure()` (`systemctl restart`/`stop`), `TimeoutStopSec=20`, `bridge.run` (2 s drain of inbound, no drain of outbound), design §3 non-goal |
| Related | ARC-013 |

**Description.** Already discussed as an architecture item; operationally: `--update` restarts
the service at once, so an alert queued during the update is lost without a `delivery` event;
`--reconfigure` stops the service for the duration of the dialogs (minutes) with the radio
unattended (messages queue in the radio's 16 slots; fine) and the broker's requests lost (clean
session). The documentation says a restart drops the queue; the update path does not check
`state.queue` first.

**Suggested mitigation or implementation.** With ARC-013 (persist or drain): nothing more. Without
it: `--update` could read the retained `state` document (`queue`) before the restart and wait up
to a minute for 0, and `TimeoutStopSec` could be 90 s so that a drain can finish.

---

### OPS-007 A firmware update of the radio switches contact management off until the gateway is updated

| | |
| --- | --- |
| Category | operations (surprise) |
| Component | `core/profile.TESTED_FIRMWARE = {14}`, `contact_manager._firmware_allowed`, `radio/profile` (`auto` writes only on tested firmware), `docs/configuration.md` `[radio.profile] manage`, `docs/testing.md` "Testing another firmware release" |
| Related | ADR 0020, ARC-016 |

**Description.** The firmware gate is a good safety decision (unknown firmware is not written
blindly). Its operational effect: the day MeshCore releases 1.19 and the household updates the
radio (the app offers it), the gateway reports `untested_firmware`, the Home Assistant controls
turn unavailable, the profile stops enforcing the observer settings (a factory-reset radio would
keep automatic adding), and nothing says why in Home Assistant except the *Radio profile*
sensor's attributes. The documentation says it in `configuration.md`; the user who updated the
radio reads the troubleshooting table, which has no row for it.

**Suggested mitigation or implementation.** A troubleshooting row ("controls unavailable after a
firmware update of the radio"); a warning in the log at every connect on untested firmware
(there is one at info level in `_log_observing`, to check); a `contacts` index field that says
`manage: false` *and why* (`manage_reason: "untested_firmware"`), which the dashboard could show;
and a fast lane in the project for raising `TESTED_FIRMWARE` (the firmware tests make it "two
lines and a run", `docs/testing.md`), with the compatibility policy already treating it as an
addition.

---

### OPS-008 Backups carry the password and the household's positions; restore has three files that must stay together

| | |
| --- | --- |
| Category | operations (backup and restore) |
| Component | `docs/deployment.md` §4 "State and backup" (names the four files and what `state.json` holds), `docs/configuration.md` `[history]` |
| Related | SEC-009, SEC-015, CODE-004 |

**Description.** The backup instructions are correct and do not say: the configuration holds the
broker password; `state.json` and `history.sqlite3` hold positions and messages; the three state
files must be restored together (an old `state.json` with a new `telemetry-excluded.json` would
restore current documents of excluded contacts until the first table read; an old
`telemetry-excluded.json` polls contacts the household excluded since); a restore of
`state.json` while the service runs is overwritten by the next writer (CODE-004); a Proxmox
`vzdump` of the running container is crash-consistent for SQLite (WAL) and for `state.json`
(atomic replace), which is fine, and should be said.

**Suggested mitigation or implementation.** Four sentences in §4; a `meshcore-home-gateway
backup FILE` command (IMP) that stops nothing, uses SQLite's `.backup`, and writes a tarball with
the three state files and the configuration *without* the password line (or with it, flagged).

---

### OPS-009 Disk: the journal, the history and `state.json` share 2 GB with no limits set

| | |
| --- | --- |
| Category | operations (capacity) |
| Component | `create_container` (`--rootfs …:2` GB), the container's `journald.conf` (Debian default: `SystemMaxUse` 10 % of the file system, 200 MB here), `history.py` caps (tens of MB at the caps), SEC-010 (the `node` table without a cap) |
| Related | SEC-010, ARC-012 |

**Description.** The defaults are adequate for a quiet household. A flood (SEC-010) or verbose
logging (`log_level = "debug"` left on) can fill the disk; a full disk breaks the history, then
the state file (commands refused), then the journal. Nothing reports disk usage.

**Suggested mitigation or implementation.** The installer writes
`/etc/systemd/journald.conf.d/meshcore-home-gateway.conf` with `SystemMaxUse=100M`; the health
section reports free space of the state directory (`os.statvfs`), with a warning in the log below
10 %; SEC-010's row cap.

---

### OPS-010 The TCP radio in the installer: asked for without a word

| | |
| --- | --- |
| Category | operations (safe defaults in the dialogs) |
| Component | `ask_radio` (offers "TCP" beside the serial devices; asks host and port), `check_broker`, `summary_text` |
| Related | SEC-003, SEC-004 |

**Description.** The dialog that offers a TCP radio does not mention what SEC-003 describes; the
broker dialog accepts an empty user name without a warning (SEC-004). The final summary says
"login accepted" and nothing about the security posture chosen.

**Suggested mitigation or implementation.** A one-paragraph `--msgbox` before the TCP host dialog
("the radio's TCP interface has no password and lets any host on its network read its private
key; use a network only the gateway reaches") and before continuing without credentials or
without TLS; a line in the summary ("Broker: user X, no TLS" / "Radio: TCP, unauthenticated").

---

### OPS-011 Observability: what an operator sees and does not see

| | |
| --- | --- |
| Category | operations |
| Component | `systemd.notify` (`STATUS=radio …, queue …`), the `state` document, the journal, Home Assistant's diagnostic entities |
| Related | ARC-012, CODE-001 |

**Description.** Good: `systemctl status` shows the radio state and the queue; the `state`
document and the diagnostic entities show the link, the battery, the airtime, the queue, the
telemetry status and reason, the profile status, the radio's counters; the journal is clean at
`info`. Missing: a "degraded" signal (history off, exclusions unsaved, repeated observer errors,
loop lag), the radio's clock offset (SEC-002), a contested TCP link (SEC-003), disk space
(OPS-009), the count of dropped events while the broker was away (logged, not published), the
counts of refused requests (logged). A Prometheus endpoint is excluded by design (no listener);
the `state` document is the right place, and Home Assistant's long-term statistics then keep
them.

**Suggested mitigation or implementation.** The health section of ARC-012 with the fields above;
a `meshcore-home-gateway status` CLI command that prints the retained `state` and `stats` in a
readable form (the `listen` command prints JSON lines; a human summary is missing).

---

### OPS-012 Smaller operational notes

| ID | Where | Note | Suggestion |
| --- | --- | --- | --- |
| OPS-012 | The installer's `wait_ready` | Needs DNS to `deb.debian.org`; an offline host cannot install (documented as a requirement) | None; a `--offline` with a local template and `.deb`s is a feature request |
| OPS-013 | `install_service` | `apt-get update && apt-get install` without `--no-install-recommends`'s companion `-o APT::Install-Suggests=false` (fine) and without `apt-get clean` (a few MB) | `apt-get clean` |
| OPS-014 | The container's clock | `--timezone host`; the journal shows local time; the gateway's documents are UTC: a reader comparing them needs to know | A sentence in `docs/deployment.md` §4 |
| OPS-015 | `--reconfigure` | Stops the service for the duration of the dialogs; an interrupted run restarts it (good); the radio's inbound queue (16) may overflow in a long session | A note; nothing to fix |
| OPS-016 | Rollback | The unit file of the newer version stays (documented); a newer unit with `LoadCredential` or a new `After=` is harmless for an older program; a newer unit that drops a directive the older program needs is not (none today) | Keep the note; version the unit file with the program (a comment line with the version) |
| OPS-017 | Logging at `debug` | Logs message texts (documented); also every frame code and decision; a day of `debug` on a busy mesh is large | A warning at start when `log_level = "debug"` is set, naming what it logs |
| OPS-018 | The service user in `dialout` | Reaches every serial device of the container; only `dev0` exists; another device passed later would be reachable too | None; documented in the unit's comments |
| OPS-019 | `pct exec` from the host uses the host's `PATH` | Documented with the full path in the final message | None |
| OPS-020 | Proxmox 8 "with current updates" | The script checks `pve-container ≥ 5.3.1` and `pveversion ≥ 8`; good | None |
| OPS-021 | Two gateways on one host | Each its own container and radio; the installer picks the next free id; nothing prevents two containers claiming the same `dev0` (Proxmox allows it; the second gets `EBUSY` on open: `TIOCEXCL`) | A check in `ask_radio` whether another container already passes the device through |
| OPS-022 | Uninstall | `pct destroy`; the broker's retained topics and Home Assistant's devices stay (documented with the commands) | A `meshcore-home-gateway retire` command that clears the topics and sends the removals, run before the destroy (IMP) |

## 3 What the deployment does well

* Nothing is stored on the host; everything the installer does is visible in the log and
  reversible (`pct destroy`).
* Every command in the container runs with a clean environment; the configuration is written by
  the program's own validator with the right mode and group.
* The unit is as tight as a unit with a serial device in an unprivileged container can be, and
  the project measured it.
* Update swaps a symlink after validating the configuration with the new version; rollback is a
  symlink; both are documented with their limits.
* The installer is tested against stubs of the Proxmox tools in their real output formats, and
  once by hand against the real `whiptail` for the pitfalls the stubs cannot show.
