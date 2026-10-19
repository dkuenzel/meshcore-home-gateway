# Deployment and operations

The recommended setup is an unprivileged Proxmox VE container created by the installer script
([ADR 0006](decisions/0006-deployment-in-an-unprivileged-proxmox-container.md)). Any other Linux with
systemd, Python 3.11+ and paho-mqtt 2.x works as well (section 3).

## 1 Before you start

* **Radio:** a MeshCore companion radio with USB firmware (or WiFi firmware for TCP), set up with
  the official app: name, radio preset, channels, contacts. Then disconnect the app; the bridge
  must be the radio's only client (protocol.md, section 1).
* **Radio settings worth checking** in the app:
  * *add contacts manually*, so unknown nodes cannot enter the contact table (ADR 0009);
  * the *airtime factor* for the legal duty cycle: the firmware allows 50 % by default; for the EU
    band 869.40–869.65 MHz (10 % per hour) the factor is 9 (protocol.md, section 7).
* **Broker:** an MQTT user for the bridge, ideally with an ACL (mqtt-api.md, last section).
* **Home Assistant:** MQTT integration with discovery enabled (the default).

## 2 Proxmox VE

### Requirements

* Proxmox VE 9, or Proxmox VE 8 with current updates: Debian 13 containers need `pve-container`
  5.3.1 or newer, which the script checks. Device passthrough into containers exists since 8.1.
* An amd64 host with internet access for the Debian template and packages.
* `whiptail` for the dialogs. It belongs to a standard Debian installation, but no Proxmox package
  depends on it; if it is missing, install it (`apt install whiptail`) or use `--answers`.

### Install

On the Proxmox VE host, as root, in a copy of this repository:

```sh
git clone <repository URL> meshcore-bus && cd meshcore-bus
./deploy/proxmox/meshcore-bus-lxc.sh
```

The first dialog chooses the setup. **Default** asks only what has no sensible default: the
radio, the broker address, user and password, and at the end the targets. **Advanced** asks for
every setting in the table.

| Question | Default | Setup |
| --- | --- | --- |
| Container ID | next free ID | Advanced |
| Host name, disk storage, disk size, memory, network bridge | `meshcore-bus`, first active storage for containers, 2 GB, 256 MB, `vmbr0` | Advanced |
| IPv4 address with prefix, gateway | `dhcp` | Advanced |
| Storage for templates | first active storage for templates | Advanced |
| Radio connection | the devices in `/dev/serial/by-id/`, or TCP with address and port (5000) | both |
| Broker address, user, password | –, `meshcore-bus`, – | both |
| Broker TLS, port | no, 1883 (8883 with TLS) | Advanced |
| Base topic, node id | `meshcore`, `radio1` | Advanced |
| Home Assistant discovery, device name | yes, `MeshCore radio` | Advanced |
| ASCII only | no | Advanced |
| Channels and contacts to send to | read from the radio | both |
| Contacts allowed to send commands, command words | read from the radio; `status` | both |

Before it creates anything, the script checks the settings with the bridge's own configuration
code and asks for confirmation. Then it downloads the newest Debian 13 template if needed, creates
and starts the container, installs the service, reads the radio's channels and contacts for the
last questions, writes the configuration and starts the service. The final message shows how to
read the logs. If a step fails after the container was created, the error message says how to
remove it.

### What the script does

The equivalent manual steps, for review or for a setup the script does not cover. On the host:

```sh
pveam update          # prints "update successful"; the exit status is 0 even when it fails
pveam available --section system | grep debian-13-standard
pveam download local debian-13-standard_<version>_amd64.tar.zst
pct create 105 local:vztmpl/debian-13-standard_<version>_amd64.tar.zst \
  --hostname meshcore-bus --cores 1 --memory 256 --swap 256 --rootfs local-lvm:2 \
  --net0 name=eth0,bridge=vmbr0,ip=dhcp --timezone host --unprivileged 1 --features nesting=1 \
  --onboot 1 --ostype debian --tags meshcore-bus
pct set 105 --dev0 path=/dev/serial/by-id/usb-<radio>-if00,gid=20,mode=0660
pct start 105
pct exec 105 -- getent group dialout    # 20 on Debian; otherwise use that number in dev0
tar -czf /tmp/meshcore-bus-src.tar.gz src/meshcore_bus tools/build_pyz.py \
  deploy/systemd/meshcore-bus.service
pct push 105 /tmp/meshcore-bus-src.tar.gz /root/meshcore-bus-src.tar.gz
pct enter 105
```

Inside the container:

```sh
apt-get update && apt-get install -y --no-install-recommends python3 python3-paho-mqtt
useradd --system --user-group --groups dialout --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin meshcore-bus
mkdir -p /root/src /usr/local/lib/meshcore-bus && tar -C /root/src -xzf /root/meshcore-bus-src.tar.gz
python3 /root/src/tools/build_pyz.py --source /root/src/src --interpreter '/usr/bin/python3 -I' \
  --output /usr/local/lib/meshcore-bus/meshcore-bus-<version>.pyz
ln -sfn meshcore-bus-<version>.pyz /usr/local/lib/meshcore-bus/current.pyz
ln -sfn /usr/local/lib/meshcore-bus/current.pyz /usr/local/bin/meshcore-bus
install -m 0644 /root/src/deploy/systemd/meshcore-bus.service /etc/systemd/system/
systemctl daemon-reload
meshcore-bus probe --serial /dev/serial/by-id/usb-<radio>-if00
meshcore-bus config render --answers answers.txt --output /etc/meshcore-bus/config.toml \
  --group meshcore-bus
systemctl enable --now meshcore-bus
```

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
  container; the script runs every command with `env -i` and a fixed `PATH`.

### Unattended installation

`--answers FILE` skips all dialogs. The file has one `key=value` per line; keys are the dialog
keys in the script (`ct.id`, `ct.hostname`, `ct.storage`, `ct.disk_gb`, `ct.memory_mb`,
`ct.bridge`, `ct.ip`, `ct.gateway`, `ct.template_storage`, `radio.choice` (a device path or
`tcp`), `radio.host`, `radio.port`) and configuration settings (`mqtt.host`, `mqtt.password`, …,
see configuration.md). Targets: `targets.channels` and `targets.contacts` (comma-separated
indices and keys as offered by `meshcore-bus probe --list`), `commands.senders`,
`commands.words`. Missing keys take the defaults above; without a broker address (or a radio
address for TCP) the script stops before it creates anything. Keep the file private; it may
contain the broker password.

### Update, reconfigure, remove

```sh
git pull && ./deploy/proxmox/meshcore-bus-lxc.sh --update 105       # new version, same configuration
./deploy/proxmox/meshcore-bus-lxc.sh --reconfigure 105               # dialogs again
pct exec 105 -- ln -sfn meshcore-bus-<old version>.pyz /usr/local/lib/meshcore-bus/current.pyz \
  && pct exec 105 -- systemctl restart meshcore-bus                  # roll back
pct stop 105 && pct destroy 105                                       # remove
```

* **Update** builds the new zipapp next to the old one and checks the current configuration with
  it; only then it switches the `current.pyz` link and restarts the service. If the configuration
  does not pass, the old version stays active.
* **Reconfigure** reads the current configuration (`meshcore-bus config answers`) and offers its
  values as defaults: an empty password keeps the current one, and settings the dialogs do not ask
  for are kept. The service is stopped while the script reads the radio and started again
  afterwards, also when a step fails.

## 3 Other Linux systems

Requirements: systemd, Python 3.11 or newer, paho-mqtt 2.x. On Debian 13 both come from the
distribution; on older systems install paho-mqtt from PyPI in a virtual environment and adjust the
interpreter line.

```sh
apt install python3 python3-paho-mqtt
install -d /usr/local/lib/meshcore-bus
python3 tools/build_pyz.py --interpreter '/usr/bin/python3 -I' \
  --output /usr/local/lib/meshcore-bus/meshcore-bus-0.1.0.pyz
ln -sfn meshcore-bus-0.1.0.pyz /usr/local/lib/meshcore-bus/current.pyz
ln -sfn /usr/local/lib/meshcore-bus/current.pyz /usr/local/bin/meshcore-bus
useradd --system --user-group --groups dialout --no-create-home --home-dir /nonexistent \
  --shell /usr/sbin/nologin meshcore-bus
install -D -m 0640 -g meshcore-bus deploy/config.example.toml /etc/meshcore-bus/config.toml
install -m 0644 deploy/systemd/meshcore-bus.service /etc/systemd/system/
editor /etc/meshcore-bus/config.toml && meshcore-bus check-config
systemctl daemon-reload && systemctl enable --now meshcore-bus
```

## 4 Operations

| Task | Command (inside the container) |
| --- | --- |
| Logs | `journalctl -u meshcore-bus -f` |
| Status | `systemctl status meshcore-bus` (shows radio state and queue) |
| Check the configuration | `meshcore-bus check-config` |
| Watch the bus | `meshcore-bus listen` |
| Send a test message | `meshcore-bus send --wait 60 <alias> "test"` |
| Inspect the radio | `systemctl stop meshcore-bus && meshcore-bus probe --serial <device>` |

* **Service:** `Type=notify` with a 60 s watchdog; restart after 5 s on failure, not on
  configuration errors (exit code 78). `systemd-analyze security meshcore-bus` rates the unit's
  exposure at 1.6 (lower is better).
* **State and backup:** back up `/etc/meshcore-bus/config.toml` and
  `/var/lib/meshcore-bus/state.json` (the newest accepted command time per sender; without it a
  restored system accepts older recorded commands again until a sender sends a new one, within
  `max_age_s`).
* **Logs contain no message texts** at the default level; set `log_level = "debug"` temporarily to
  see them.

## 5 Troubleshooting

| Symptom | Cause and remedy |
| --- | --- |
| `pct start` fails: `Device /dev/serial/by-id/... does not exist` | The radio is unplugged or has a different name: plug it in, or pass the new path with `pct set <id> --dev0 path=<path>,gid=20,mode=0660` |
| `radio: cannot connect (... No such file or directory)` | Device not passed through: check `pct config <id>` for `dev0` |
| `radio: cannot connect (no answer from the radio)` | Another program or the phone app holds the radio, or wrong firmware (BLE-only); close the app, check the firmware type |
| State `disconnected` after re-plugging the radio | The device number changed: `pct reboot <id>` |
| State `disconnected`, log shows `connection lost` every few minutes | USB power or cable; check `dmesg` on the host |
| `delivery: failed`, reason `unknown_contact` | The contact is not in the radio's contact table or has a different key; `meshcore-bus probe` shows the table |
| `delivery: failed`, reason `channel_not_configured` | The channel index has no name on the radio |
| `delivery: expired`, reason `airtime_budget` | More messages than the budget allows; see the airtime entity, raise `budget_s_per_hour` only within the legal limit |
| `delivery: failed`, reason `exceeds_airtime_burst` | One long message needs more airtime than `burst_s` at the radio's settings (slow presets); shorten messages or raise `burst_s` |
| Commands rejected with `too_old` / `from_the_future` | Sender or bridge clock wrong; the phone apps set the radio clock, or set `max_age_s = 0` |
| Commands rejected with `sender_ambiguous` | Two contacts share the first 6 key bytes; remove the stray one, add contacts manually |
| Blank container console; units fail with `243/CREDENTIALS` or `226/NAMESPACE` | Container without nesting: `pct set <id> --features nesting=1` and restart it |
