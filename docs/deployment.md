# Deployment and operations

The recommended setup is an unprivileged Proxmox VE container created by the installer script
([ADR 0006](decisions/0006-deployment-in-an-unprivileged-proxmox-container.md)). Any other Linux with
systemd, Python 3.11+ and paho-mqtt 2.x works as well (section 3).

## 1 Before you start

* **Radio:** a MeshCore companion radio with USB firmware (or WiFi firmware for TCP), set up with
  the official app: name, radio preset, channels, contacts. Then disconnect the app; the bridge
  must be the radio's only client (protocol.md, section 1).
* **Radio settings worth checking** in the app:
  * *add contacts manually*, with every contact type switched off in the auto-add settings: a
    node that the radio adds with the key prefix of a command sender blocks that sender's
    commands until 24 hours after it was last seen (configuration.md, `[commands]`; ADR 0009);
  * the *airtime factor* for the legal duty cycle: the firmware allows 50 % by default; for the EU
    band 869.40–869.65 MHz (10 % per hour) the factor is 9 (protocol.md, section 7).
* **Broker:** an MQTT user for the bridge, with an ACL (mqtt-api.md, last section); the ACL is
  required when commands are configured. A standalone Mosquitto reads a user added with
  `mosquitto_passwd` only after `systemctl reload mosquitto`.
* **Home Assistant:** MQTT integration with discovery enabled (the default).

## 2 Proxmox VE

### Requirements

* Proxmox VE 9, or Proxmox VE 8 with current updates: Debian 13 containers need `pve-container`
  5.3.1 or newer, which the script checks. Device passthrough into containers exists since 8.1.
* An amd64 host with internet access for the Debian template and packages.
* `whiptail` for the dialogs. It belongs to a standard Debian installation, but no Proxmox package
  depends on it; if it is missing, install it (`apt install whiptail`) or use `--answers`.
  `--update` shows no dialogs and does not need it.

### Install

On the Proxmox VE host, as root:

```sh
bash -c "$(wget -qO- https://github.com/dkuenzel/meshcore-home-gateway/releases/latest/download/meshcore-home-gateway-lxc.sh)"
```

Before the first release there is nothing at that address; install the source of `main`:

```sh
bash -c "$(wget -qO- https://raw.githubusercontent.com/dkuenzel/meshcore-home-gateway/main/deploy/proxmox/meshcore-home-gateway-lxc.sh)"
```

The script runs from memory and stores nothing on the host besides the Debian template, which
lands in the template storage as for any container. It downloads the program inside the new
container:

* run from a release's address: that release's zipapp and unit, checked against the release's
  `SHA256SUMS` before they are installed (`--release X.Y.Z` picks another release);
* run from `main`'s address: the source archive of `main` (or of the branch, tag or commit that
  `--ref` names), built inside the container; HTTPS is the only check;
* run from a clone of this repository: that clone, packed and sent into the container (for
  development); with `--release` or `--ref`, a download as above.

Options follow a `--`, for example `bash -c "$(wget -qO- URL)" -- --answers FILE`; `--help` lists
them. If the command does nothing at all, the address could not be downloaded (`bash -c` gets an
empty script): `wget -O- URL` shows why. To check the installer before running it, download it, verify it on a machine with the
GitHub CLI `gh` (the release attests it like its other files), then run it:

```sh
wget https://github.com/dkuenzel/meshcore-home-gateway/releases/latest/download/meshcore-home-gateway-lxc.sh
gh attestation verify meshcore-home-gateway-lxc.sh --repo dkuenzel/meshcore-home-gateway \
  --signer-workflow dkuenzel/meshcore-home-gateway/.github/workflows/release.yml \
  --source-ref refs/tags/vX.Y.Z --deny-self-hosted-runners
bash meshcore-home-gateway-lxc.sh && rm meshcore-home-gateway-lxc.sh
```

The first dialog chooses the setup. **Default** asks only what has no sensible default: the
radio, the broker address, user and password, and at the end the targets. **Advanced** asks for
every setting in the table.

| Question | Default | Setup |
| --- | --- | --- |
| Container ID | next free ID | Advanced |
| Host name, disk storage, disk size, memory, network bridge | `meshcore-home-gateway`, first active storage for containers, 2 GB, 256 MB, `vmbr0` | Advanced |
| IPv4 address with prefix, gateway | `dhcp` | Advanced |
| Storage for templates | first active storage for templates | Advanced |
| Radio connection | the devices in `/dev/serial/by-id/`, or TCP with address and port (5000) | both |
| Broker address, user, password | –, none (suggested: `meshcore-home-gateway`), – (asked only with a user name) | both |
| Broker TLS, port | no, 1883 (8883 with TLS) | Advanced |
| Base topic, node id | `meshcore`, `radio1` | Advanced |
| Home Assistant discovery, device name | yes, `MeshCore radio` | Advanced |
| ASCII only | no | Advanced |
| Channels and contacts to send to | read from the radio | both |
| Contacts allowed to send commands, command words | read from the radio; `status` | both |

The script asks for confirmation before it creates anything. Then it downloads the newest Debian
13 template if needed, creates and starts the container, installs the program, checks the
settings with the program's own configuration code (when it installs a clone: already before the
container is created), logs in to the broker with the user name and password given, reads the
radio's channels and contacts for the last questions, writes the configuration and starts the
service. When the broker refuses the login, the script shows its answer and offers to enter the
broker settings again (without dialogs it warns and goes on: the service keeps trying). The final
message shows the result of the login and how to read the logs. If a step fails or the script is
interrupted after the container was created, the error message says how to remove it.

The dialogs run in the `C.UTF-8` locale: in a locale the host lacks (for example one passed on by
ssh), whiptail would drop every non-ASCII character typed, passwords included.

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
apt-get update && apt-get install -y --no-install-recommends python3 python3-paho-mqtt
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
  (credentials, namespaces) and for the bridge's sandboxing; without it, units fail with
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
    simple rule: after re-plugging the radio, `pct reboot 105`. Until then the bridge reports the
    radio as disconnected and keeps retrying.

  The script writes `path=` explicitly because device names may contain `=`.
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
  how to remove the container).

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
# new version, same configuration (before the first release: main's address, as for the install)
bash -c "$(wget -qO- $installer)" -- --update 105
# dialogs again
bash -c "$(wget -qO- $installer)" -- --reconfigure 105
# an earlier release again
bash -c "$(wget -qO- $installer)" -- --release 0.1.0 --update 105
# roll back to an earlier version that is still installed
pct exec 105 -- ls /usr/local/lib/meshcore-home-gateway/
pct exec 105 -- ln -sfn meshcore-home-gateway-<old version>.pyz \
  /usr/local/lib/meshcore-home-gateway/current.pyz
pct exec 105 -- systemctl restart meshcore-home-gateway
# remove
pct stop 105 && pct destroy 105
```

* **Update** downloads (or builds) the new zipapp under a temporary name next to the old one and
  checks the current configuration with it; only then it moves it to
  `meshcore-home-gateway-<version>.pyz`, switches the `current.pyz` link and restarts the service. If the configuration does not pass, the old
  version stays active, also when the version string did not change. A newer version can refuse
  a configuration that an older one accepted (see the changelog); fix the setting it names, for
  example with `--reconfigure`, and update again. The `latest` address serves the latest release,
  never a pre-release (`a`, `b`, `rc`, `dev`): it does not find one, or installs an older release
  over it. Install or update to a pre-release with its own installer
  (`releases/download/vX.Y.Zrc1/meshcore-home-gateway-lxc.sh`) or with `--release X.Y.Zrc1`; the
  final message of a pre-release names its own installer.
* **Reconfigure** reads the current configuration (`meshcore-home-gateway config answers`) and
  offers its values as defaults: an empty password keeps the current one while the user name stays
  the same (with a new user name it means no password, so an answers file that changes
  `mqtt.username` gives `mqtt.password` too; an empty user name removes both), and settings the
  dialogs do not ask for are kept. The broker login is checked before the service is stopped,
  with the version installed in the container (one from before the check skips it). Selected targets keep their
  aliases, all of them when several aliases name the same channel or contact. Targets and command
  senders that the radio does not offer (a contact that is not a chat contact, a channel slot
  without a name) are kept as well, and the log names them. The service is stopped while the script
  reads the radio and started again afterwards, also when a step fails or the script is interrupted
  (Ctrl-C, a closed SSH session). The container and the radio connection stay as installed:
  answers for them (`radio.choice`, `ct.*`) stop the script. A TCP radio's address changes with
  the settings `radio.host` and `radio.port` in an answers file; a serial radio under a new name is
  passed through again by hand (see Troubleshooting).
* **Roll back** works for versions that are still in `/usr/local/lib/meshcore-home-gateway/`.
  An update keeps the files of other versions, but replaces the file of its own version: after an
  update with an unchanged version string (a checkout of `main` between two releases) there is
  nothing to roll back to. The unit file is not rolled back: the one the update installed stays.
  Check the configuration with the old version first (`pct exec 105 -- python3 -I
  /usr/local/lib/meshcore-home-gateway/meshcore-home-gateway-<old version>.pyz check-config`):
  settings added later are refused by older versions.

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

## 4 Operations

| Task | Command (inside the container) |
| --- | --- |
| Logs | `journalctl -u meshcore-home-gateway -f` |
| Status | `systemctl status meshcore-home-gateway` (shows radio state and queue) |
| Check the configuration | `meshcore-home-gateway check-config` |
| Check the broker login | `meshcore-home-gateway check-config --broker` |
| Watch the bus | `meshcore-home-gateway listen` |
| Send a test message | `meshcore-home-gateway send --wait 60 <alias> "test"` |
| Inspect the radio | `systemctl stop meshcore-home-gateway && meshcore-home-gateway probe --serial <device>` |

* **Service:** `Type=notify` with a 60 s watchdog; restart after 5 s on failure, not on
  configuration errors (exit code 78). `systemd-analyze security meshcore-home-gateway` rates the
  unit's exposure at 1.6 (lower is better).
* **State and backup:** back up `/etc/meshcore-home-gateway/config.toml` and
  `/var/lib/meshcore-home-gateway/state.json` (the newest accepted command time per sender; without
  it a restored system accepts older recorded commands again until a sender sends a new one, within
  `max_age_s`; the Home Assistant components last announced; the contacts whose documents are
  published). `history.sqlite3` next to it holds the history ([configuration](configuration.md));
  copy it only with the service stopped, or with `sqlite3 history.sqlite3 ".backup copy.sqlite3"`.
* **Retained documents** stay on the broker after the gateway is removed. Clear them with, for
  example, `mosquitto_sub -t 'meshcore/radio1/#' --retained-only --remove-retained -W 2` (Mosquitto
  2.0), or one by one with `mosquitto_pub -r -n -t <topic>`.
* **Logs contain no message texts** at the default level; set `log_level = "debug"` temporarily to
  see them.

## 5 Troubleshooting

| Symptom | Cause and remedy |
| --- | --- |
| `pct start` fails: `Device /dev/serial/by-id/... does not exist` | The radio is unplugged or has a different name (another radio, other firmware): plug it in, or pass the new path with `pct set <id> --dev0 path=<path>,gid=20,mode=0660` and set the same path as `device` in `[radio]` of `/etc/meshcore-home-gateway/config.toml` |
| `radio: cannot connect (... No such file or directory)` | Device not passed through: check `pct config <id>` for `dev0` |
| `radio: cannot connect (no answer from the radio)` | Another program or the phone app holds the radio, or wrong firmware (BLE-only); close the app, check the firmware type |
| State `disconnected` after re-plugging the radio | The device number changed: `pct reboot <id>` |
| State `disconnected`, log shows `connection lost` every few minutes | USB power or cable; check `dmesg` on the host |
| `delivery: failed`, reason `unknown_contact` | The contact is not in the radio's contact table or has a different key; `meshcore-home-gateway probe` shows the table |
| `delivery: failed`, reason `channel_not_configured` | The channel index has no name on the radio |
| `delivery: expired`, reason `airtime_budget` | More messages than the budget allows; see the airtime entity, raise `budget_s_per_hour` only within the legal limit |
| `delivery: failed`, reason `exceeds_airtime_burst` | One long message needs more airtime than `burst_s` at the radio's settings (slow presets); shorten messages or raise `burst_s` |
| Commands rejected with `too_old` / `from_the_future` | Sender or bridge clock wrong; the phone apps set the radio clock, or set `max_age_s = 0` |
| Commands rejected with `replay` although they are new | The sender's clock went backwards (reset, battery change): its timestamps are older than its last accepted command. Set the clock right; or, with the service stopped, remove the sender's key from `command_timestamps` in `/var/lib/meshcore-home-gateway/state.json` |
| Commands rejected with `sender_ambiguous` | Another key with the sender's first 6 key bytes is on the radio or was seen there in the last 24 hours (`meshcore-home-gateway probe` lists the table): remove the stray contact and switch off automatic adding (configuration.md, `[commands]`); a service restart forgets keys that are no longer in the table |
| Commands rejected with `sender_unverified` | The bridge fetched the command while connecting: it waited in the radio (restart, update, unplugged radio) or arrived during the connection setup. Or the contact table could not be read (see the log). Send it again |
| `MQTT: connection refused: Not authorized (user '…')` | The broker refuses the login. Mosquitto gives this one answer for a wrong password, an unknown user and missing credentials alike, so check each: `meshcore-home-gateway check-config --broker` tries the login once. A standalone Mosquitto reads its `password_file` only when it starts or reloads: after `mosquitto_passwd`, run `systemctl reload mosquitto` (and add further users without `-c`, which replaces the file). `password_file` must be set (with `per_listener_settings true`: after the `listener` line it belongs to). The Home Assistant Mosquitto add-on takes a Home Assistant user by its login name, not the display name (the Supervisor log names the user it refused: `Unauthorized login for '…'`), or a `logins` entry (read when the add-on starts); it remembers a refused login for about 5 minutes, so restart the add-on after a fix. The broker's own log names the client (`meshcore-home-gateway-radio1`), not the user |
| `MQTT: cannot reach …` | No broker answered at that name or address and port: check `mqtt.host` (a host name or IP address, no `mqtt://`) and `mqtt.port`, the network and the firewall |
| `MQTT: … closed the connection without answering` | Something answered but closed the connection before accepting it: TLS expected (port 8883 with `tls = false`), or the broker refuses a feature such as retained messages |
| Commands rejected with `broker_unavailable` | The gateway could not reach the broker when the command arrived; check the broker connection (`availability`, the log), then send the command again |
| Commands rejected with `state_unwritable` | `/var/lib/meshcore-home-gateway/state.json` cannot be written (disk full, permissions); fix it, then send the command again |
| Blank container console; units fail with `243/CREDENTIALS` or `226/NAMESPACE` | Container without nesting: `pct set <id> --features nesting=1` and restart it |
