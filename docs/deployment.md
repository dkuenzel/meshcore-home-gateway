# Deployment and operations

The recommended setup is an unprivileged Proxmox VE container created by the installer script
([ADR 0006](decisions/0006-deployment-in-an-unprivileged-proxmox-container.md)). Any other Linux with
systemd, Python 3.11+ and paho-mqtt 2.x works as well (section 3).

## 1 Before you start

* **Radio:** a MeshCore companion radio with USB firmware (or WiFi firmware for TCP), set up with
  the official app: name, radio preset, channels, contacts. Then disconnect the app; the gateway
  must be the radio's only client (protocol.md, section 1).
* **Radio settings:** the gateway sets the radio up as an observer at its first connect: contacts
  added manually only, no telemetry for anyone, no position in adverts, and the 10 % duty cycle in
  the EU band 869.40–869.65 MHz (configuration.md, `[radio.profile]`; ADR 0020). The installer
  shows what changes; in the advanced setup it also asks for the radio's name. With firmware other than companion 1.18 it
  only reports; check the settings in the app then, or set `radio.profile.manage = "always"`.
* **Broker:** an MQTT user for the gateway, with an ACL (mqtt-api.md, last section); the ACL is
  required when commands are configured. A standalone Mosquitto reads a user added with
  `mosquitto_passwd` only after `systemctl reload mosquitto`.
* **Home Assistant:** MQTT integration with discovery enabled (the default).
* **Telemetry:** the gateway asks the companions the household includes for their battery,
  sensors and position ([companions.md](companions.md)). With `[telemetry] ask = "all"` it asks
  every companion that is not excluded, and the first time it runs that way it waits an hour after
  the radio connects, so the household can exclude every companion that is not its own.

## 2 Proxmox VE

### Requirements

* Proxmox VE 9, or Proxmox VE 8 with current updates: Debian 13 containers need `pve-container`
  5.3.1 or newer, which the script checks. Device passthrough into containers exists since 8.1.
* An amd64 host with internet access for the Debian template and packages.
* `whiptail` for the dialogs. It belongs to a standard Debian installation, but no Proxmox package
  depends on it; if it is missing, install it (`apt install whiptail`) or use `--answers`.
  `--update` shows no dialogs and does not need it.

### Install

On the Proxmox VE host, as root. Download the installer, check that the release workflow built it
from the release's tag (on any machine with the GitHub CLI `gh`, logged in; the release attests
each of its files; `X.Y.Z` is the version on the releases page), then run it:

```sh
wget https://github.com/dkuenzel/meshcore-home-gateway/releases/latest/download/meshcore-home-gateway-lxc.sh
gh attestation verify meshcore-home-gateway-lxc.sh --repo dkuenzel/meshcore-home-gateway \
  --signer-workflow dkuenzel/meshcore-home-gateway/.github/workflows/release.yml \
  --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
bash meshcore-home-gateway-lxc.sh && rm meshcore-home-gateway-lxc.sh
```

The installer of a release carries the SHA-256 of that release's zipapp and unit and installs
only those files: the check of the installer covers the program it installs. Whoever could
replace a file of the release could replace its `SHA256SUMS` too, but not the installer's
attestation; the repository's releases are also immutable once published. If you trust GitHub's
TLS and the release's files as served, run the installer straight from its address instead:

```sh
bash -c "$(wget -qO- https://github.com/dkuenzel/meshcore-home-gateway/releases/latest/download/meshcore-home-gateway-lxc.sh)"
```

A pre-release has an installer of its own (`releases/download/vX.Y.Zrc1/meshcore-home-gateway-lxc.sh`,
see Update below); `main`'s copy
(`https://raw.githubusercontent.com/dkuenzel/meshcore-home-gateway/main/deploy/proxmox/meshcore-home-gateway-lxc.sh`)
installs the source of `main`, which no release attests.

The script runs from memory and stores nothing on the host besides the Debian template, which
lands in the template storage as for any container. It downloads the program inside the new
container:

* run from a release's address: that release's zipapp and unit, checked against the release's
  `SHA256SUMS` and the digests the installer carries before they are installed (`--release
  X.Y.Z` picks another release, checked against its `SHA256SUMS` alone);
* run from `main`'s address: the source archive of `main` (or of the branch, tag or commit that
  `--ref` names), built inside the container; HTTPS is the only check. A branch moves; to install
  exactly what you read, name a commit: `bash -c "$(wget -qO- URL)" -- --ref <commit>` (its full
  hash from the commit's page);
* run from a clone of this repository: that clone, packed and sent into the container (for
  development); with `--release` or `--ref`, a download as above.

Options follow a `--`, for example `bash -c "$(wget -qO- URL)" -- --answers FILE`; `--help` lists
them. If the command does nothing at all, the address could not be downloaded (`bash -c` gets an
empty script): `wget -O- URL` shows why.

The installer speaks English and German (since 2.1.0; 2.0.0 offered English only): the first
dialog chooses its language, preselected from your session (`LANGUAGE`, `LC_ALL`,
`LC_MESSAGES`, `LANG`). The script reads that language's pack from where the script comes from
(its release, the checkout next to it, else `main`) into memory, stores nothing, and uses it only
when its SHA-256 is the one the script carries (the release attests the script); else the dialogs
stay English and a box says so. The dialogs, their buttons and boxes, and the program's messages
they show then follow the language; on a new installation it becomes the default of the
language of the names in Home Assistant. The script's own log lines, `--help`, errors before the
menu, `--update` and runs with `--answers` stay English.

The next dialog chooses the setup. **Default** asks only what has no sensible default: the
radio, the broker address, user and password, and at the end the targets. **Advanced** asks for
every setting in the table.

| Question | Default | Setup |
| --- | --- | --- |
| Container ID | next free ID | Advanced |
| Host name, disk storage, disk size, memory, network bridge | `meshcore-home-gateway`, first active storage for containers, 2 GB, 256 MB, `vmbr0` | Advanced |
| IPv4 address with prefix, gateway | `dhcp` | Advanced |
| Storage for templates | first active storage for templates | Advanced |
| Radio connection | the devices in `/dev/serial/by-id/`, or TCP with address and port (5000) | both |
| TCP: accept that the interface has no authentication (`radio.tcp_unauthenticated`) | no: the radios again | both, only then |
| Broker address, TLS, user, password | –, no, none (suggested: `meshcore-home-gateway`; an empty one is confirmed), – (asked only with a user name) | both |
| Broker port | 1883 (8883 with TLS) | Advanced |
| Base topic, node id | `meshcore`, `radio1` | Advanced |
| Home Assistant discovery, device name | yes, `MeshCore radio` | Advanced |
| Language of the names in Home Assistant (asked while it offers more than English) | the dialogs' language; on `--reconfigure` the configured one, else `en` | Advanced |
| ASCII only | no | Advanced |
| Channels and contacts to send to (only the messages of these channels are published, `messages.publish_channels = targets`) | read from the radio | both |
| Contacts allowed to send commands, command words | read from the radio; `status` | both |
| Commands without a user name or TLS: accept (`mqtt.insecure`) | no: no commands | both, only then |

Without a question, an installation keeps of other people's radios their keys and types only
(the default `contacts.heard_nodes = keys`, [what the gateway keeps](privacy.md)) and asks only
the companions the household includes for telemetry (`telemetry.ask = household`); the answers
`contacts.heard_nodes=full`, `telemetry.ask=all` and `messages.publish_channels=all` in an
answers file keep more.

The script asks for confirmation before it creates anything. Then it downloads the newest Debian
13 template if needed, creates and starts the container, installs the program, checks the
settings with the program's own configuration code (when it installs a clone: already before the
container is created), logs in to the broker with the user name and password given, reads the
radio's channels and contacts for the last questions, writes the configuration and starts the
service. When the broker refuses the login, the script shows its answer and offers to enter the
broker settings again (without dialogs it warns and goes on: the service keeps trying). Once the
service runs, the script has the gateway check itself (`meshcore-home-gateway doctor`: the
configuration, the broker, the radio as the gateway reports it, the container) and repeats the
check for about a minute while the radio connects. The final message shows the result of the
login, the self-test's `passed` (or the problems it found) and how to read the logs; the
self-test's report stands above it, also when it passed, with the warnings it passed with, such
as a code secret the gateway ignores. If a step fails or the script is interrupted after the
container was created, the error message says how to remove it.

The dialogs run in the `C.UTF-8` locale: in a locale the host lacks (for example one passed on by
ssh), whiptail would drop every non-ASCII character typed, passwords included.

**"Already passed through to another container (ID …)".** The script refuses a radio that
another container of the host can open ([below](#what-the-script-does)). Containers made by setup
scripts, for zigbee2mqtt or Home Assistant for example, often get the host's serial devices by
their kernel names, whatever is plugged in (`lxc.mount.entry: /dev/ttyACM0 dev/ttyACM0 …`, the
same for `/dev/ttyUSB0` and `/dev/serial/by-id`): the radio, once the kernel numbers it
`ttyACM0`, is then that container's as well. When the container named needs no serial device
(zigbee2mqtt with a coordinator on the network, say), take those lines out of its configuration,
here for container 101:

```sh
cp /etc/pve/lxc/101.conf /root/101.conf.bak
sed -E '1,/^\[/{/^lxc\.mount\.entry: *\/dev\/(tty(USB|ACM)[0-9]+|serial\/by-id)[ \/]/d; /^lxc\.cgroup2\.devices\.allow: *c (166|188|189):/d}' \
  /root/101.conf.bak > /root/101.conf.new
diff /root/101.conf.bak /root/101.conf.new   # what goes
cp /root/101.conf.new /etc/pve/lxc/101.conf
pct reboot 101
```

This removes the bind mounts of `/dev/ttyUSB*`, `/dev/ttyACM*` and `/dev/serial/by-id` and the
permissions for USB serial devices (major numbers 166, 188 and 189) from the container's current
settings, not from its snapshots; `cp /root/101.conf.bak /etc/pve/lxc/101.conf` undoes it. The
copy through `/root` is on purpose: `sed -i` writes a temporary file beside the original, which
Proxmox's cluster file system (`/etc/pve`) may refuse. When the other container does use a serial
device of its own, give it that device's `/dev/serial/by-id/…` path instead of the kernel name,
which follows the order in which the devices were plugged in. Then run the installer again.

### What the script does

The equivalent manual steps, for review or for a setup the script does not cover, from a clone of
this repository. On the host:

```sh
pveam update          # prints "update successful"; the exit status is 0 even when it fails
pveam available --section system | grep debian-13-standard
pveam download local debian-13-standard_<version>_amd64.tar.zst
pct create 105 local:vztmpl/debian-13-standard_<version>_amd64.tar.zst \
  --hostname meshcore-home-gateway --cores 1 --memory 256 --swap 256 --rootfs local-lvm:2 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --timezone host --unprivileged 1 --features nesting=1 \
  --onboot 1 --ostype debian --tags meshcore-home-gateway
pct set 105 --dev0 path=/dev/serial/by-id/usb-<radio>-if00,gid=20,mode=0660
pct start 105
pct exec 105 -- getent group dialout    # 20 on Debian; otherwise use that number in dev0
tar -czf /tmp/meshcore-home-gateway-src.tar.gz src/meshcore_home_gateway tools/build_pyz.py \
  deploy/systemd/meshcore-home-gateway.service deploy/config.example.toml
pct push 105 /tmp/meshcore-home-gateway-src.tar.gz /root/meshcore-home-gateway-src.tar.gz
pct enter 105
```

Inside the container:

```sh
apt-get update && apt-get install -y --no-install-recommends python3 python3-paho-mqtt \
  ca-certificates unattended-upgrades && apt-get clean
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=100M\n' >/etc/systemd/journald.conf.d/meshcore-home-gateway.conf
systemctl try-restart systemd-journald
useradd --system --user-group --groups dialout --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin meshcore-home-gateway
mkdir -p /root/src /usr/local/lib/meshcore-home-gateway
tar -C /root/src -xzf /root/meshcore-home-gateway-src.tar.gz
python3 /root/src/tools/build_pyz.py --source /root/src/src --interpreter '/usr/bin/python3 -I' \
  --output /usr/local/lib/meshcore-home-gateway/meshcore-home-gateway-<version>.pyz
ln -sfn meshcore-home-gateway-<version>.pyz /usr/local/lib/meshcore-home-gateway/current.pyz
ln -sfn /usr/local/lib/meshcore-home-gateway/current.pyz /usr/local/bin/meshcore-home-gateway
install -m 0644 /root/src/deploy/systemd/meshcore-home-gateway.service /etc/systemd/system/
systemctl daemon-reload
meshcore-home-gateway probe --serial /dev/serial/by-id/usb-<radio>-if00
install -D -m 0640 -g meshcore-home-gateway /root/src/deploy/config.example.toml \
  /etc/meshcore-home-gateway/config.toml
editor /etc/meshcore-home-gateway/config.toml && meshcore-home-gateway check-config
systemctl enable --now meshcore-home-gateway
```

The script writes the configuration from its answers instead (`meshcore-home-gateway config render
--answers FILE --output /etc/meshcore-home-gateway/config.toml --group meshcore-home-gateway`, with
the keys of the next section), which gives the same file with the same mode.

* **Unprivileged and with nesting, both set explicitly:** on Proxmox VE 8, `pct create` makes a
  privileged container unless `--unprivileged 1` is given, and on both versions only the web
  wizard ticks *nesting* by default. Debian 13's systemd needs nesting for its own services
  (credentials, namespaces) and for the gateway's sandboxing; without it, units fail with
  `243/CREDENTIALS` or `226/NAMESPACE`
  ([pct.conf(5)](https://pve.proxmox.com/pve-docs/pct.conf.5.html), *nesting*;
  [Proxmox forum](https://forum.proxmox.com/threads/lxc-debian-13-with-nesting-disabled-no-console.173418/)).
* **Device passthrough:** `dev0` creates a device node at the same path inside the container (a
  device, not a link) with the group as seen inside the container and mode 0660, and allows
  exactly this device; the service user is in `dialout`. Proxmox resolves the path when the
  container starts, which has two consequences:
  * the container does not start while the radio is unplugged, also not at host boot;
  * if the radio comes back with a different device number after re-plugging (for example
    `ttyACM1` instead of `ttyACM0`), the container keeps the old one until it restarts. The
    simple rule: after re-plugging the radio, `pct reboot 105`. Until then the gateway reports the
    radio as disconnected and keeps retrying.

  The script writes `path=` explicitly because device names may contain `=`. It refuses a radio
  that another container of the host already passes through (a `devN` entry or an
  `lxc.mount.entry` of `pct config`, compared after following links, so `/dev/ttyACM0` and its
  `/dev/serial/by-id/` name are the same radio): a radio takes one client at a time, and the
  container that opens it second could not use it. With dialogs it says so and offers the
  radios again. A container that gets serial devices by their kernel names counts too
  ([Install](#install) says how to take them out).
* **Updates of the container:** the script installs `unattended-upgrades`, which installs
  Debian's updates every day (see [Operations](#4-operations)), and empties the package cache
  afterwards (`apt-get clean`). It limits the journal to 100 MB of the container's disk.
* **Clean environment:** `pct exec` passes the host's environment (locale, `PATH`) into the
  container; the script runs every command with `env -i` and a fixed `PATH`. When the host's
  `PATH` lacks `/usr/local/bin`, `pct exec 105 -- meshcore-home-gateway` fails with "Failed to
  exec"; name the program with its path, `pct exec 105 -- /usr/local/bin/meshcore-home-gateway
  check-config`, as the final message does.

### Unattended installation

`--answers FILE` skips all dialogs. The file has one `key=value` per line; empty lines and lines
starting with `#` are ignored. There are two kinds of keys:

* **Answers for the installer:** `setup`, `ct.id`, `ct.hostname`, `ct.storage`, `ct.disk_gb`,
  `ct.memory_mb`, `ct.bridge`, `ct.ip`, `ct.gateway`, `ct.template_storage`, `radio.choice` (a
  device path or `tcp`; required, the script never guesses a device), `targets.channels` and
  `targets.contacts` (comma-separated indices and keys as offered by `meshcore-home-gateway probe
  --list`). An unknown key of this kind, or a malformed index or key, stops the script before it
  creates anything. An index or key that the radio does not offer becomes a target all the same,
  with a warning in the log.
* **Every other key is a configuration setting** (`radio.host`, `mqtt.host`, `mqtt.password`,
  `airtime.budget_s_per_hour`, `commands.senders`, `commands.words`, …; see configuration.md) and
  goes into the configuration as given. The configuration check refuses unknown settings and
  invalid values: when the script installs a clone, before anything is created; otherwise in the
  new container right after the program is installed, before the radio is read (the message says
  how to remove the container). `homeassistant.language` sets the language of the names in Home
  Assistant (`en` when missing); a version before 1.1.0a4 refuses it.

Missing keys take the defaults above; without a broker address (or a radio address for TCP) the
script stops before it creates anything. Without `mqtt.username` the gateway logs in without a
user name, and `mqtt.password` alone is refused. The script logs in to the broker before it reads
the radio; a refused login is a warning, since the broker may be down meanwhile and the service
keeps trying. Keep the file private; it may contain the broker password. From a URL the file goes after `--`: `bash -c "$(wget -qO- URL)" -- --answers FILE`.

TLS without own files uses the container's system CA store. A file (`mqtt.ca_file`,
`mqtt.cert_file`, `mqtt.key_file`) must exist in the container when the configuration is written,
which a new container cannot: install first, then copy the files in (for example `pct push 105
ca.pem /etc/meshcore-home-gateway/ca.pem`) and set them with
`bash -c "$(wget -qO- URL)" -- --reconfigure 105 --answers FILE`.

### Update, reconfigure, remove

```sh
installer=https://github.com/dkuenzel/meshcore-home-gateway/releases/latest/download/meshcore-home-gateway-lxc.sh
# new version, same configuration
bash -c "$(wget -qO- $installer)" -- --update 105
# dialogs again
bash -c "$(wget -qO- $installer)" -- --reconfigure 105
# an earlier release again (X.Y.Z is its version)
bash -c "$(wget -qO- $installer)" -- --release X.Y.Z --update 105
# roll back to an earlier version that is still installed
pct exec 105 -- ls /usr/local/lib/meshcore-home-gateway/
pct exec 105 -- ln -sfn meshcore-home-gateway-<old version>.pyz \
  /usr/local/lib/meshcore-home-gateway/current.pyz
pct exec 105 -- systemctl restart meshcore-home-gateway
# remove: first out of Home Assistant and off the broker, then the container
pct exec 105 -- systemctl stop meshcore-home-gateway
pct exec 105 -- meshcore-home-gateway retire --yes
pct stop 105 && pct destroy 105
```

* **Update** downloads (or builds) the new zipapp under a temporary name next to the old one and
  checks the current configuration with it; only then it moves it to
  `meshcore-home-gateway-<version>.pyz`, switches the `current.pyz` link and restarts the service. If the configuration does not pass, the old
  version stays active, also when the version string did not change. A newer version can refuse
  a configuration that an older one accepted (see the changelog); fix the setting it names, for
  example with `--reconfigure`, and update again (a setting the installed version does not know
  yet: see the next point). The `latest` address serves the latest release,
  never a pre-release (`a`, `b`, `rc`, `dev`): it does not find one, or installs an older release
  over it. Install or update to a pre-release with its own installer
  (`releases/download/vX.Y.Zrc1/meshcore-home-gateway-lxc.sh`) or with `--release X.Y.Zrc1`; the
  final message of a pre-release names its own installer. An update also installs the packages
  and the journal limit of [What the script does](#what-the-script-does) again, so an older
  installation gets `unattended-upgrades` with it. After the restart it runs the same self-test
  as an installation; with problems the new version keeps running, and the log shows the report
  and points to [Roll back](#update-reconfigure-remove).
* **A setting that only the new version knows:** from 1.x to 2.0, a radio over TCP needs
  `tcp_unauthenticated = true` in `[radio]`, and commands over a broker connection without a user
  name, or without TLS to a broker beyond the gateway's host, need `insecure = true` in `[mqtt]`.
  `--reconfigure` cannot write them: it asks with the installed version, which does not know
  them, and `--update` asks nothing. Read first what each one accepts
  ([configuration.md](configuration.md#radio),
  [mqtt-api.md](mqtt-api.md#securing-the-broker)); a radio over USB, or a broker with credentials
  and TLS, needs neither. Then add the line by hand and update at once, because the installed
  version refuses the unknown setting at its next start:

  ```sh
  pct exec 105 -- sed -i '/^\[radio\]/a tcp_unauthenticated = true' /etc/meshcore-home-gateway/config.toml
  pct exec 105 -- sed -i '/^\[mqtt\]/a insecure = true' /etc/meshcore-home-gateway/config.toml
  bash -c "$(wget -qO- $installer)" -- --update 105
  ```

* **The privacy defaults after an update to 2.0.0:** only the messages of the channels that are a
  target are published, of other people's radios only their keys and types, and telemetry asks
  only the companions the household includes, and the contact targets and command senders, at
  once. Switch *Telemetry* on for each of the household's
  companions (or `meshcore-home-gateway contacts telemetry KEY on`); the changelog's
  **Breaking:** entries name the settings that keep the old behaviour. The messages that 1.x
  received on other channels stay in the history database (and in `history export` and `backup`)
  until they are `messages_days` old, 30 days by default; since 2.1 the `history` document and
  Home Assistant's *Message history* leave them out. To be rid of them at once, stop the service
  and delete `/var/lib/meshcore-home-gateway/history.sqlite3` with its `-wal` and `-shm` files:
  the gateway starts a new history, without the rest of the old one.
* **Telemetry after an update** from 1.0.0, or from 1.1.0a1 with telemetry off, with
  `[telemetry] ask = "all"`: telemetry is on from 1.1.0a2 on, and the first time it runs on the
  state directory it asks nobody for an hour
  after the radio connects (*Telemetry status* `paused`, reason `first_start`; the log names the
  number of companions it will ask). Exclude every companion that is not the household's in that
  hour ([companions.md](companions.md)). With a broker ACL, Home Assistant's user needs
  `topic write <base_topic>/<node_id>/contacts/+/telemetry` for the switch *Telemetry*
  ([mqtt-api.md](mqtt-api.md#securing-the-broker)): the example ACL of earlier versions lacks it,
  and without it the switch does nothing. To update from 1.0.0 without any request, switch
  telemetry off right before the update; 1.0.0 refuses the `[telemetry]` table when it starts, so
  update at once:

  ```sh
  pct exec 105 -- sh -c 'grep -q "^\[telemetry\]" /etc/meshcore-home-gateway/config.toml || printf "\n[telemetry]\nenabled = false\n" >> /etc/meshcore-home-gateway/config.toml'
  bash -c "$(wget -qO- $installer)" -- --update 105
  ```

  A configuration copied from the 1.1.0a1 example says `enabled = false`: remove that line to follow
  the new default.
* **Reconfigure** reads the current configuration (`meshcore-home-gateway config answers`) and
  offers its values as defaults: an empty password keeps the current one while the user name stays
  the same (with a new user name it means no password, so an answers file that changes
  `mqtt.username` gives `mqtt.password` too; an empty user name removes both), and settings the
  dialogs do not ask for are kept. The broker login is checked before the service is stopped,
  with the version installed in the container (one from before the check skips it). Selected targets keep their
  aliases, all of them when several aliases name the same channel or contact. Targets and command
  senders that the radio does not offer (a contact that is not a chat contact, a channel slot
  without a name) are kept as well, and the log names them. Unticking a sender with a code secret
  leaves the secret in the codes file, where the gateway ignores it: until
  `meshcore-home-gateway commands revoke <key>` in the container and a restart, the commands of
  the senders without a secret are refused as `code_missing`, and the self-test's report names
  them ([configuration.md](configuration.md#commands)). The service is stopped while the script
  reads the radio and started again afterwards, also when a step fails or the script is interrupted
  (Ctrl-C, a closed SSH session). Meanwhile the radio keeps up to 16 received messages for the
  gateway; more are lost, so do not leave the dialogs open for long on a busy mesh. The container and the radio connection stay as installed:
  answers for them (`radio.choice`, `ct.*`) stop the script. A TCP radio's address changes with
  the settings `radio.host` and `radio.port` in an answers file; a serial radio under a new name is
  passed through again by hand (see Troubleshooting).
* **Roll back** works for versions that are still in `/usr/local/lib/meshcore-home-gateway/`.
  An update keeps the files of other versions, but replaces the file of its own version: after an
  update with an unchanged version string (a checkout of `main` between two releases) there is
  nothing to roll back to. The unit file is not rolled back: the one the update installed stays;
  a release's unit names its version in its second line
  (`pct exec 105 -- head -2 /etc/systemd/system/meshcore-home-gateway.service`).
  Check the configuration with the old version first (`pct exec 105 -- python3 -I
  /usr/local/lib/meshcore-home-gateway/meshcore-home-gateway-<old version>.pyz check-config`):
  settings added later are refused by older versions.
* **Roll back to 2.0** from 2.1 leaves three kinds of retained topics on the broker that 2.0
  neither publishes nor clears: `pending`, `config` and the drafts of Home Assistant's chats,
  `homeassistant/draft/<kind>/<chat>`, which the stop of 2.1 empties but leaves retained. Once 2.0
  runs (2.1 would publish them again), clear them with an empty retained message on each, as the
  changelog of 2.1 gives them; for node `radio1`, with the broker's address and a user that may
  write them (`-h`, `-u`, `-P`):

  ```sh
  mosquitto_pub -r -n -t meshcore/radio1/pending
  mosquitto_pub -r -n -t meshcore/radio1/config
  mosquitto_sub -t 'meshcore/radio1/homeassistant/draft/#' --retained-only --remove-retained -W 2
  ```

  Then make the dashboard again with 2.0's `meshcore-home-gateway dashboard` and paste it
  ([home-assistant.md](home-assistant.md#dashboard)): the dashboard of 2.1 names entities that
  2.0 does not announce, and its version card asks to update the gateway. An entity of 2.1 that
  Home Assistant keeps afterwards can be deleted under *Settings → Entities*. The history
  database keeps working; 2.0 leaves the columns 2.1 added empty.
* **Roll back to 1.x** from 2.0 or later keeps the state directory, which 1.x reads in its own
  way (#95). 1.x knows no inclusions (`[telemetry] ask` is 2.0's): it asks every companion that
  is not excluded, other people's too. And the first exclusion made under 1.x writes
  `telemetry-excluded.json` without the household's inclusions, so that 2.0, back again, asks
  none of the household's companions (*Telemetry* off on each). Before the roll back, keep a
  copy of the file and switch telemetry off for 1.x (`enabled = false` in `[telemetry]`, removed
  again after the return; 1.0.0 refuses the table, and has no telemetry):

  ```sh
  pct exec 105 -- cp -p /var/lib/meshcore-home-gateway/telemetry-excluded.json /root/
  ```

  Back on 2.0 or later, with the service stopped, put the copy back (`cp -p
  /root/telemetry-excluded.json /var/lib/meshcore-home-gateway/`) if 1.x changed the file; an
  exclusion made under 1.x is gone then, and a companion the household did not include is not
  asked anyway.
  Messages that the stop kept for the next start (`outbox` in `state.json`, with their texts)
  stay there while 1.x runs, which ignores them; the next start of 2.0 or later ends those whose
  `queue_ttl_s` has passed, `expired` or `failed`, as their final delivery event.

## 3 Other Linux systems

Requirements: systemd, Python 3.11 or newer, paho-mqtt 2.x. On Debian 13 both come from the
distribution; on older systems install paho-mqtt from PyPI in a virtual environment and adjust the
interpreter line. Run the steps as root in a clone of this repository (at the tag of the release
you install, so that the unit and the example configuration match).

The zipapp comes from a release or from the clone. A release file can be verified before it is
installed: `SHA256SUMS` shows that the download is complete, the attestation that this repository's
release workflow built it from the tag (the command checks both, `--repo` alone would accept any
workflow and branch of the repository)
([ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md)). The second
command needs the GitHub CLI `gh`, logged in (`gh auth login`); the release also carries the
attestation as a Sigstore bundle (`meshcore-home-gateway-X.Y.Z.sigstore.json`) for other Sigstore
clients.

```sh
# either: the release file, verified (X.Y.Z is the version)
sha256sum --check --ignore-missing SHA256SUMS
gh attestation verify meshcore-home-gateway-X.Y.Z.pyz --repo dkuenzel/meshcore-home-gateway \
  --signer-workflow dkuenzel/meshcore-home-gateway/.github/workflows/release.yml \
  --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
# or: built from the clone
python3 tools/build_pyz.py --interpreter '/usr/bin/python3 -I' \
  --output meshcore-home-gateway-X.Y.Z.pyz
```

```sh
apt install python3 python3-paho-mqtt
install -d /usr/local/lib/meshcore-home-gateway
install -m 0755 meshcore-home-gateway-X.Y.Z.pyz /usr/local/lib/meshcore-home-gateway/
ln -sfn meshcore-home-gateway-X.Y.Z.pyz /usr/local/lib/meshcore-home-gateway/current.pyz
ln -sfn /usr/local/lib/meshcore-home-gateway/current.pyz /usr/local/bin/meshcore-home-gateway
useradd --system --user-group --groups dialout --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin meshcore-home-gateway
install -D -m 0640 -g meshcore-home-gateway deploy/config.example.toml \
  /etc/meshcore-home-gateway/config.toml
install -m 0644 deploy/systemd/meshcore-home-gateway.service /etc/systemd/system/
editor /etc/meshcore-home-gateway/config.toml && meshcore-home-gateway check-config
systemctl daemon-reload && systemctl enable --now meshcore-home-gateway
```

To update, check the configuration with the new version first, then switch to it and install
the unit of its tag too. The unit changes with releases: since 2.0 it gives a stop 90 s
(`TimeoutStopSec`), which the gateway needs to wait for the acknowledgement of a message in
flight; the unit of 1.x gives 20 s.

```sh
install -m 0755 meshcore-home-gateway-X.Y.Z.pyz /usr/local/lib/meshcore-home-gateway/
python3 -I /usr/local/lib/meshcore-home-gateway/meshcore-home-gateway-X.Y.Z.pyz check-config
ln -sfn meshcore-home-gateway-X.Y.Z.pyz /usr/local/lib/meshcore-home-gateway/current.pyz
install -m 0644 deploy/systemd/meshcore-home-gateway.service /etc/systemd/system/
systemctl daemon-reload && systemctl restart meshcore-home-gateway
```

The command line's texts follow `LANGUAGE`, `LC_ALL`, `LC_MESSAGES` and `LANG`
([configuration.md](configuration.md#language-of-the-command-line)); `LANGUAGE=en` keeps them
English. The service's log is always English.

## 4 Operations

| Task | Command (inside the container) |
| --- | --- |
| Logs | `journalctl -u meshcore-home-gateway -f` |
| Status | `systemctl status meshcore-home-gateway` (shows radio state and queue) |
| What the gateway reports | `meshcore-home-gateway status` (its retained documents; `--json`) |
| Check everything | `meshcore-home-gateway doctor` (configuration, broker, gateway, this host) |
| Check the configuration | `meshcore-home-gateway check-config` |
| Check the broker login | `meshcore-home-gateway check-config --broker` |
| Watch the bus | `meshcore-home-gateway listen` |
| Send a test message | `meshcore-home-gateway send --wait 60 <alias> "test"` |
| Inspect the radio | `systemctl stop meshcore-home-gateway && meshcore-home-gateway probe --serial <device>` |
| Back up | `meshcore-home-gateway backup /root/gateway-$(date +%F).tar.gz` (below) |
| Export the history | `meshcore-home-gateway history export --format csv > messages.csv` |
| Before the container goes | `systemctl stop meshcore-home-gateway && meshcore-home-gateway retire --yes` |

`status` exits with 1 while the gateway is offline or its health section degraded, `doctor` when
a check finds a problem; both suit a monitoring script. `doctor` checks what is easily missed:
the broker login and whether the broker takes clients without one,
TLS, a radio over TCP, the radio's clock, a contested TCP link, firmware the gateway is not
tested with, the health section, the free space, automatic security updates and a restart that
updates wait for.

* **Service:** `Type=notify` with a 60 s watchdog; restart after 5 s on failure, not on
  configuration errors (exit code 78). Each further automatic restart waits longer, up to 5
  minutes from the seventh on, and systemd never gives up: a crash loop does not end in the
  `failed` state, and Home Assistant shows the gateway unavailable meanwhile
  ([home-assistant.md](home-assistant.md)). The count begins again only when the service is
  started by hand (`systemctl restart`, an update); `systemctl show -p NRestarts
  meshcore-home-gateway` shows it. The service may use 192 MB of memory (`MemoryMax=`): a leak
  ends with the service restarted, not with another process of the container killed. A stop
  waits up to a minute for the acknowledgement of a
  message in flight (`TimeoutStopSec=90`) and keeps the queued messages for the next start. `systemd-analyze security meshcore-home-gateway` rates the
  unit's exposure at 1.6 (lower is better).
* **Updates of the container:** the container is a Debian system and needs Debian's updates;
  `--update` updates only the gateway. The installer installs `unattended-upgrades`, which
  installs the updates of Debian's security archive and point releases every day, as its
  default configuration in `/etc/apt/apt.conf.d/50unattended-upgrades` says, and never reboots
  (the kernel is the host's). A running service keeps the Python and paho-mqtt it started with:
  restart it after an update of them (`grep -E 'python3|paho'
  /var/log/unattended-upgrades/unattended-upgrades.log`). `systemctl list-timers
  apt-daily-upgrade.timer` shows the next run, and `apt-config dump APT::Periodic` must show
  `APT::Periodic::Update-Package-Lists` and `APT::Periodic::Unattended-Upgrade` with `"1"` (the
  package switches itself on when it is installed without dialogs); `doctor` warns when either
  is `"0"` or missing. An installation from before this version gets the package with its
  next `--update`.
* **Disk:** the container has 2 GB. The installer limits the journal to 100 MB
  (`/etc/systemd/journald.conf.d/meshcore-home-gateway.conf`; Debian's default is a tenth of the
  file system); the history is bounded by its own limits ([configuration](configuration.md#history)),
  and the `health` section of `state` reports the free space (`disk_free_mb`).
* **Hardening further:** where the broker has a fixed address, a drop-in can limit the service
  to it (`systemctl edit meshcore-home-gateway`: `[Service]`, `IPAddressDeny=any`,
  `IPAddressAllow=<broker address>` and, for a radio over TCP, the radio's address). It needs
  systemd to load a BPF program, which an unprivileged container cannot where the host forbids
  unprivileged BPF; systemd then goes on without the filter and says so only at debug level
  ([platform notes](platform-notes.md#debian-systemd-and-linux)). Check that it holds:
  `systemd-run --wait -p IPAddressDeny=any python3 -c "import socket;
  socket.create_connection(('deb.debian.org', 80), 5)"` must fail. It suits a virtual machine
  or a host of its own better than the container.
* **State and backup:** `meshcore-home-gateway backup FILE` writes all of it into one archive
  (tar.gz, mode 0600) while the service runs: the configuration, the three files of
  `/var/lib/meshcore-home-gateway/` (the history copied with SQLite's online backup) and
  `MANIFEST.json`, which names the version and where each file came from. The configuration goes
  without the broker password, and the archive without the command senders' code secrets, unless
  `--with-secrets` (which also takes the file `password_file` names and the one `codes_file`
  names, as `password` and `command-codes`); without it, set the password again after a restore
  and enrol the senders anew. To restore, in the container as root: stop the service, unpack
  the archive into a private directory, and put each file back with its owner, group and mode.
  The archive holds every file as `root` with mode 0600; a file copied so cannot be read by the
  service, which then stops with a configuration error. The configuration and the secrets are
  `root:meshcore-home-gateway` 0640, so that the service's group reads them, and the state is the
  service's own. The secrets go where `MANIFEST.json` says they came from (`password_file`,
  `codes_file`), here the usual paths; leave out the line of a file the archive does not hold
  (`files` in `MANIFEST.json`). The history uses SQLite's write-ahead log: a stop that was cut
  short (the stop timeout, the watchdog, a crash) leaves `history.sqlite3-wal` and
  `history.sqlite3-shm` beside it. Remove both before the restore, or SQLite applies the old log
  to the restored history at the next start, and the history is the old one:

  ```sh
  systemctl stop meshcore-home-gateway
  rm -f /var/lib/meshcore-home-gateway/history.sqlite3-wal /var/lib/meshcore-home-gateway/history.sqlite3-shm
  mkdir -m 0700 /root/restore && tar -xzf FILE -C /root/restore && cd /root/restore
  install -m 0640 -o root -g meshcore-home-gateway config.toml /etc/meshcore-home-gateway/config.toml
  install -m 0640 -o root -g meshcore-home-gateway password /etc/meshcore-home-gateway/mqtt-password
  install -m 0640 -o root -g meshcore-home-gateway command-codes /etc/meshcore-home-gateway/command-codes
  install -m 0600 -o meshcore-home-gateway -g meshcore-home-gateway state/* /var/lib/meshcore-home-gateway/
  systemctl start meshcore-home-gateway
  ```

  By hand: back up
  `/etc/meshcore-home-gateway/` (`config.toml`, and `mqtt-password` where the installer wrote it,
  and the file `codes_file` names) and the three files of
  `/var/lib/meshcore-home-gateway/`. Stop the service for the copy (`systemctl stop
  meshcore-home-gateway`), or use a Proxmox backup of the whole container (`vzdump`): a backup of
  the running container holds the files as a power cut would leave them, which both survive
  (`state.json` is replaced atomically, the history uses SQLite's write-ahead log). Keep backups
  private and encrypted: `mqtt-password` (or, on an installation from before 2.0, the
  configuration) holds the broker password, `state.json` and `history.sqlite3` hold positions of
  the household's companions and message texts, and the history also other people's names, keys
  and channel texts
  ([what the gateway keeps](privacy.md)). Restore the three state files together and with the
  service stopped, and remove the old history's `-wal` and `-shm` first, as above; a Proxmox
  backup brings back the whole container, the log with its history. The service holds
  `state.json` in memory and writes it whole while it runs,
  over a copy restored meanwhile; an older `state.json` beside
  a newer `telemetry-excluded.json` brings back documents of companions excluded since, and an
  older `telemetry-excluded.json` asks companions excluded since.
  * `state.json` holds the newest accepted command time per sender (without it a restored system
    accepts older recorded commands again until a sender sends a new one, within `max_age_s`),
    and for a sender with a code secret the time step of its last accepted code (without it, a
    code seen in the last 90 seconds would serve once more) and the times of its wrong codes
    within the last hour (`command_code_failures`; without them a restored system gives a
    guesser up to 5 new guesses);
    the Home Assistant components last announced; the contacts whose documents are published;
    the last document of each companion whose answers are current, with its position and
    readings, so that they still expire after a restart without the radio (kept in its latest
    state, also once expired, until the radio's table is read again); the last checked advert of
    each companion (its time and whether it carried a position, no position); the radio
    profile's memory; the telemetry schedule: per companion when it is next due, its unanswered
    requests, when it last answered, was asked and flooded, how long its answers count and when
    one last carried a position, and whether it was last moving, no positions; and what is left
    of the first-start hour; the airtime budget's level; and from the start of a stop, the
    messages on their way, the one in flight included, until the next start takes them.
  * `telemetry-excluded.json` holds the companions the household excluded from telemetry, and
    with `[telemetry] ask = "household"` those it included: without it a restored system asks the
    excluded ones again, or no longer the included ones. An exclusions file that cannot be parsed
    is left as it is and pauses telemetry: fix it (`{"excluded": [keys], "included": [keys]}`,
    `included` optional) or delete it, then
    restart the service.
  * `history.sqlite3` holds the history: messages, positions, the companions' readings and the
    nodes heard ([configuration](configuration.md#history)); copy it only with the service
    stopped, or with `sqlite3 history.sqlite3 ".backup copy.sqlite3"`.
* **Broker password:** the installer keeps it in `/etc/meshcore-home-gateway/mqtt-password` (0640
  `root:meshcore-home-gateway`), which `[mqtt] password_file` names; `--reconfigure` moves a
  password that is still in the configuration there. To keep it from the service user too, hand it
  to the service as a systemd credential: a drop-in (`systemctl edit meshcore-home-gateway`) with
  `[Service]` and `LoadCredential=mqtt-password:/etc/meshcore-home-gateway/mqtt-password`, then
  `chmod 0600` the file. The service reads systemd's copy of a credential named like the file,
  the command line (as root) the file itself. `--reconfigure` writes the file with mode 0640
  again; set 0600 after it. The file `codes_file` names works the same way
  (`LoadCredential=command-codes:/etc/meshcore-home-gateway/command-codes`); `commands enrol`
  keeps its mode. The installer sets up neither credential.
* **Times:** the gateway's documents and events give times in UTC; the journal shows the
  container's local time, which the installer takes from the host (`--timezone host`).
* **Retained documents** stay on the broker after the gateway is removed, and its devices in Home
  Assistant. `meshcore-home-gateway retire --yes`, with the service stopped and Home Assistant
  running, removes the devices (each bound first, so that Home Assistant also drops what earlier
  versions announced), clears every retained document under the gateway's topics and every
  retained message of its Homie device, also with `[homie]` off now while the broker still lets
  the gateway's user read and write `homie/meshcore-<node_id>/#`, and leaves its availability
  `offline`. Then it looks again: a retained message left is an error. Without `--yes` it counts
  what it would do. By hand: for example,
  `mosquitto_sub -t 'meshcore/radio1/#' -t 'homie/meshcore-radio1/#' --retained-only
  --remove-retained -W 2` (Mosquitto 2.0), or one by one with `mosquitto_pub -r -n -t <topic>`.
* **Logs contain no message texts** at the default level; set `log_level = "debug"` temporarily to
  see them.
* **Metrics for Prometheus** come from an exporter beside the gateway, which has no listener of
  its own: [`contrib/mqtt2prometheus`](../contrib/mqtt2prometheus/README.md) has configurations
  for mqtt2prometheus that read `state`, `stats` and the contacts' documents.

## 5 Troubleshooting

| Symptom | Cause and remedy |
| --- | --- |
| `pct start` fails: `Device /dev/serial/by-id/... does not exist` | The radio is unplugged or has a different name (another radio, other firmware): plug it in, or pass the new path with `pct set <id> --dev0 path=<path>,gid=20,mode=0660` and set the same path as `device` in `[radio]` of `/etc/meshcore-home-gateway/config.toml` |
| Installer: `… is already passed through to another container (ID …)` | That container can open the radio: often one made by a setup script that gets every `/dev/ttyACM*` and `/dev/ttyUSB*` by name. If it needs none, take those lines out of its configuration ([Install](#install)) |
| `radio: cannot connect (... No such file or directory)` | Device not passed through: check `pct config <id>` for `dev0` |
| `radio: cannot connect (no answer from the radio)` | Another program or the phone app holds the radio, or wrong firmware: a BLE-only build, or a USB build made without its USB interface (four of the release builds, among them the T-Echo Lite's with display: [platform-notes.md](platform-notes.md#meshcore-companion-firmware)); close the app, check the firmware type |
| State `disconnected` after re-plugging the radio | The device number changed: `pct reboot <id>` |
| State `disconnected`, log shows `connection lost` every few minutes | USB power or cable; check `dmesg` on the host |
| `delivery: failed`, reason `unknown_contact` | The contact is not in the radio's contact table or has a different key; `meshcore-home-gateway probe` shows the table |
| `delivery: failed`, reason `channel_not_configured` | The channel index has no name on the radio |
| `delivery: expired`, reason `airtime_budget` | More messages than the budget allows; see the airtime entity, raise `budget_s_per_hour` only within the legal limit |
| `delivery: failed`, reason `exceeds_airtime_burst` | One long message needs more airtime than `burst_s` at the radio's settings (slow presets); shorten messages or raise `burst_s` |
| Commands rejected with `too_old` / `from_the_future` | The sender's clock or the gateway host's clock is wrong: the check compares the time the sender's device stamped on the message (the phone app sets a companion's clock) with the host's clock; the clock of the gateway's own radio does not enter it. Set the wrong clock right, or set `max_age_s = 0` |
| Log: `radio clock left ... behind the host's` | The host's time was not written to the radio: it is before this release, more than ten years after it, or more than `set_clock_max_days` (30) ahead of the radio's clock. A time written cannot be taken back without a restart of the radio, so check the host's clock first (`timedatectl`; in a container the Proxmox host's, `chronyc tracking` there). If it is right (the radio lay unused for a long time), raise `set_clock_max_days` or set it to 0 for one start |
| Log: `radio clock is ... ahead of the host's: restarting the radio to set it back` | Expected once: the firmware sets its clock back only at a start, to its newest contact time. The gateway connects again a few seconds later and moves the clock forward (`radio clock moved forward`). Over USB the radio is gone for a moment and comes back under the same device |
| Log: `radio clock is ... ahead of the host's and not set back (...)`, or *Radio clock offset* far above 0 | The reason in brackets: `more than an hour` (a wrong time was written to the radio earlier, or the host's clock went back; nodes and repeaters would ignore the radio as long after a restart: let real time catch up), `a restart starts it at its newest contact time` (a contact changed just now: checked again every minute), `a restart would leave it at its newest contact time, ..., and the next connect would not move it forward` (no contact changed for longer than `set_clock_max_days`, or the host's clock is wrong: a restart would leave the radio that far behind; a contact's advert makes the restart possible, and if the host's clock is right, `set_clock_max_days` raised for one start does too), `once no message is being delivered, at most once a day` (it waits), `without contacts` or `set_clock = false`. The radio keeps working: its adverts and its contacts' last-modified times carry the later time, the gateway's messages the host's. Fix the host's time first. At boot the service waits for a synchronised clock only where `systemd-time-wait-sync` or `chrony-wait` is enabled, outside a container; in a container the clock is the Proxmox host's |
| Commands rejected with `replay` although they are new | The sender's clock went backwards (reset, battery change): its timestamps are older than its last accepted command. Set the clock right; or, with the service stopped, remove the sender's key from `command_timestamps` in `/var/lib/meshcore-home-gateway/state.json` |
| Commands rejected with `sender_ambiguous` | Another key with the sender's first 6 key bytes is on the radio or was seen there in the last 24 hours (`meshcore-home-gateway probe` lists the table): remove the stray contact and switch off automatic adding (configuration.md, `[commands]`); a service restart forgets keys that are no longer in the table |
| Commands rejected with `code_locked` | The sender sent 5 wrong codes within the last hour (`code_missing`, `code_invalid`, with the exceptions in [mqtt-api.md](mqtt-api.md#commands-command)); the log says `sent 5 wrong command codes`. Someone may be guessing its codes: if that is likely, remove the sender ([configuration.md](configuration.md#commands)). Otherwise check its secret and the phone's clock, then wait until the first of them is an hour old, or stop the service, remove the sender's key from `command_code_failures` in `/var/lib/meshcore-home-gateway/state.json` and start it again |
| Commands rejected with `code_missing` from a sender without a code secret | The codes file holds the secret of a key that is not in `[commands] senders` (the start's log, `check-config` and `doctor` name it): while it is there, senders without a secret cannot command. Give the sender a secret with `meshcore-home-gateway commands enrol <key>`, which a key corrected by hand needs; or remove the ignored secret with `meshcore-home-gateway commands revoke <key>`, after which every sender without a secret commands without a code (`revoke` names them). Then restart the service ([configuration.md](configuration.md#commands)) |
| Commands rejected with `sender_unverified` | The gateway fetched the command while connecting: it waited in the radio (restart, update, unplugged radio) or arrived during the connection setup. Or the contact table could not be read (see the log). Send it again |
| `MQTT: connection refused: Not authorized (user '…')` | The broker refuses the login. Mosquitto gives this one answer for a wrong password, an unknown user and missing credentials alike, so check each: `meshcore-home-gateway check-config --broker` tries the login once. A standalone Mosquitto reads its `password_file` only when it starts or reloads: after `mosquitto_passwd`, run `systemctl reload mosquitto` (and add further users without `-c`, which replaces the file). `password_file` must be set (with `per_listener_settings true`: after the `listener` line it belongs to). The Home Assistant Mosquitto add-on takes a Home Assistant user by its login name, not the display name (the Supervisor log names the user it refused: `Unauthorized login for '…'`), or a `logins` entry (read when the add-on starts); it remembers a refused login for about 5 minutes, so restart the add-on after a fix. The broker's own log names the client (`meshcore-home-gateway-radio1-` and a random part), not the user |
| `MQTT: connection refused: Unsupported protocol version: the broker does not speak MQTT 5` | `[mqtt] protocol = "5"` and a broker that speaks MQTT 3.1.1 only. Set `protocol = "3.1.1"` (the default) or remove the line, or update the broker; the login has nothing to do with it. `meshcore-home-gateway check-config --broker` names the setting too ([configuration.md](configuration.md#mqtt)) |
| `MQTT: cannot reach …` | No broker answered at that name or address and port: check `mqtt.host` (a host name or IP address, no `mqtt://`) and `mqtt.port`, the network and the firewall |
| `MQTT: … closed the connection without answering` | Something answered but closed the connection before accepting it: TLS expected (port 8883 with `tls = false`), or the broker refuses a feature such as retained messages |
| `MQTT: another client wrote …; restoring the gateway's payload` | Another client wrote one of the gateway's retained topics, and the gateway wrote its own again ([mqtt-api.md](mqtt-api.md#securing-the-broker)). A second gateway with the same `node_id` and `base_topic` (an old container, a test install), a tool that cleared retained messages, or a client whose ACL lets it write below the gateway's topics: only the gateway's user may. Once for `availability` as the broker stops is harmless: the broker sends the gateway its own last will ([platform-notes.md](platform-notes.md#mosquitto)) |
| `MQTT: the broker refused the subscription to …` | The broker's ACL does not let the gateway's user read these topics: requests there do not arrive, and what other clients write on its documents stays. Give the gateway's user `readwrite` below its topics ([mqtt-api.md](mqtt-api.md#securing-the-broker)) |
| ``MQTT: the gateway's `online` on … did not come back within 10 s`` | The same with Mosquitto's `acl_file`, which grants every subscription and leaves out what the user may not read: give the gateway's user `readwrite` below its topics, not only `write` |
| `radio.tcp_unauthenticated: the radio's TCP interface has no authentication` | A radio over TCP needs `tcp_unauthenticated = true` in `[radio]` since 2.0: read what the interface exposes ([configuration.md](configuration.md#radio)), then add the setting, or connect the radio over USB. After an update from 1.x, add it by hand ([Update](#update-reconfigure-remove)) |
| `mqtt.insecure: commands are configured and the broker connection has no credentials` (or `no TLS`) | Since 2.0, commands (`[commands] senders`) need a broker connection with a user name, and TLS to a broker beyond the gateway's host: any client that reaches the broker could publish command events. Add credentials and TLS, or read [Securing the broker](mqtt-api.md#securing-the-broker) and set `insecure = true` in `[mqtt]`. After an update from 1.x, add it by hand ([Update](#update-reconfigure-remove)) |
| `radio: the radio closed the TCP connection and kept running` | Another client connected to the radio's TCP port, and the gateway took the link back (`radio_link_contested` in `state`, *Radio link taken over* in Home Assistant, for an hour). If it was you (the phone app over WiFi, `meshcore-cli`), stop the gateway first next time, or use USB. If not, treat the radio's private key as read: put the radio in a network of its own, give it a new key (a factory reset formats its storage, and at the next start it makes a new key, without its contacts, channels and settings), re-add the gateway on every companion, and set the radio up again. Nothing protects the direct messages recorded before (no forward secrecy) |
| `radio: the radio closed the TCP connection, then restarted` | The same as the line above, and the radio restarted before the gateway took the link back. A restart does not close the link: another client did, and any client can restart the radio, also to hide that it read the key (`radio_link_contested` is on for an hour). If you restarted it yourself after connecting another client, nothing more is wrong; if not, do what the line above says |
| Commands rejected with `broker_unavailable` | The gateway could not reach the broker when the command arrived; check the broker connection (`availability`, the log), then send the command again |
| Commands rejected with `state_unwritable` | `/var/lib/meshcore-home-gateway/state.json` cannot be written (disk full, permissions); fix it, then send the command again |
| *Telemetry status* `paused`, reason `exclusions_unreadable` | `/var/lib/meshcore-home-gateway/telemetry-excluded.json` cannot be parsed (the log names it): fix it or delete it, then restart the service; without the file every companion not excluded again is asked |
| *Telemetry status* `paused`, reason `first_start` | Telemetry runs for the first time on this state directory: it asks nobody for an hour after the radio connects. Exclude the companions that are not the household's meanwhile |
| *Gateway degraded* on, or `degraded` in `systemctl status` | Its attributes (the `health` section of `state`, [mqtt-api.md](mqtt-api.md#state-state)) name the part: `history` `broken` (the journal says why; it is off until the next start: check the disk, `history.sqlite3`), `state_file` `unwritable` (a full or read-only disk: `df -h /var/lib/meshcore-home-gateway`), `exclusions` (as `exclusions_unreadable` above, or `unsaved`: the disk again), `errors_1h` above 0 (a bug: the journal has its traceback, `journalctl -u meshcore-home-gateway -p err`; please report it), `loop_lag_max_s` from 1 s (the host gives the container no CPU or its disk is slow: a backup, swapping; give it more CPU or move the backup), `disk_free_mb` below 100 (free space). It clears an hour after the last error or stall |
| Blank container console; units fail with `243/CREDENTIALS` or `226/NAMESPACE` | Container without nesting: `pct set <id> --features nesting=1` and restart it |
| The dashboard has no chat page for a target or a contact | It was made before the target was added, before the contact got a device that takes texts, or before the service wrote its state with that contact: make it again ([home-assistant.md](home-assistant.md#dashboard)). Besides the contact targets, at most 30 contacts get a page; `meshcore-home-gateway dashboard` says on standard error how many contacts have none and which pages it skipped because a name does not fit a page's address, and *Chats* lists the contacts without a page |
| A chat's message field is empty after a restart | The gateway keeps the drafts in memory only: a stop or restart of the gateway empties every field, as does a draft left unchanged for 15 minutes. Nothing was sent; the thread shows what was. After a restart of the broker or of Home Assistant, the field shows the draft the gateway kept, not what was typed meanwhile ([Writing and sending](home-assistant.md#writing-and-sending)) |
| Home Assistant's controls to add and remove contacts unavailable after a firmware update of the radio; log: `radio: firmware … is not tested with this version of the gateway` | The gateway writes nothing to firmware it is not tested with ([ADR 0020](decisions/0020-managed-radio-profile-for-the-observer.md)): the radio profile is only observed and add and remove requests are refused. The *Contacts* sensor's attribute `manage_reason` says `untested_firmware`, and so does `meshcore-home-gateway contacts list`. Update the gateway once a version tested with that firmware is out ([testing.md](testing.md#testing-another-firmware-release) says how soon the project aims to test one), or set `manage = "always"` in `[radio.profile]` to write to it anyway, at your own risk |
| Installer: `… is already passed through to another container (ID …)` | Another container of the host has the same radio: take it out there (`pct set <ID> --delete dev0`, or remove its `lxc.mount.entry`), or choose another radio |
| `systemctl status` shows `activating (auto-restart)`, with longer pauses each time | A crash loop: the service fails at every start, and systemd waits longer each time, up to 5 minutes. `journalctl -u meshcore-home-gateway -p err` shows why; after the fix, `systemctl restart meshcore-home-gateway` starts it at once |
