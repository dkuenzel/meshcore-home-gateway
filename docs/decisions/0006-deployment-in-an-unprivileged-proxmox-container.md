---
status: accepted
date: 2026-10-18
decision-makers: maintainers
---

# Deployment in an unprivileged Proxmox container by a host script with dialogs

## Context and Problem Statement

A typical installation runs Proxmox VE with Home Assistant as a virtual machine and an MQTT broker
somewhere in the network. The radio hangs on a USB port of the Proxmox host. Where does the bridge
run, and how does it get there without manual steps?

## Decision Drivers

* Isolation from Home Assistant and from the host.
* Minimal effort for the operator: one command, questions in dialogs, no editing of files.
* The USB device must reach the bridge, and only the bridge.
* Small footprint, easy update and rollback.
* Works the same on plain Debian for users without Proxmox.

## Considered Options

* An unprivileged LXC container created by a script on the Proxmox host, with dialogs (whiptail)
* Inside the Home Assistant virtual machine (add-on or custom integration)
* A Docker container
* A privileged LXC container or a service directly on the Proxmox host

## Decision Outcome

Chosen option: "an unprivileged LXC container created by a host script with dialogs", because it
isolates the bridge with almost no overhead, passes exactly one USB device into it, and lets one
script do everything from container creation to a running, configured service.

The script `deploy/proxmox/meshcore-home-gateway-lxc.sh`:

1. checks the host: root, Proxmox VE 8 or 9 with a `pve-container` of 5.3.1 or newer (the first
   that accepts every Debian 13 point release; device passthrough itself exists since Proxmox VE
   8.1), the Proxmox tools, `python3` and `whiptail`;
2. asks in dialogs: a default setup only for the radio (devices under `/dev/serial/by-id/`, or a
   TCP address) and the broker, an advanced one also for the container, topics and Home Assistant
   settings; it validates the answers with the bridge's own configuration code and asks for
   confirmation before it creates anything;
3. picks the newest Debian 13 template that `pveam` offers, downloads it if needed and checks the
   result (`pveam` exits with 0 even when it fails);
4. creates an unprivileged container with nesting, both given explicitly: on Proxmox VE 8 the
   command line creates privileged containers by default, and only the web wizard enables nesting,
   which Debian 13's systemd needs for its own services and for the bridge's sandboxing;
5. passes the serial device in as `dev0` with an explicit `path=`, group `dialout` as seen inside
   the container (checked there; Debian's static ID 20) and mode 0660;
6. installs `python3` and `python3-paho-mqtt` from Debian, builds the zipapp from the checkout
   inside the container and installs the systemd unit; every command in the container runs with
   a clean environment, because `pct exec` passes the host's environment through;
7. runs `meshcore-home-gateway probe` inside the container and offers the radio's channels and chat
   contacts as targets and command senders;
8. writes the configuration through `meshcore-home-gateway config render` (validated, mode 0640)
   from an answers file that is deleted afterwards, and starts the service.

`--answers FILE` runs the same steps without dialogs. `--update CTID` builds a new version and
switches to it only if it accepts the current configuration. `--reconfigure CTID` offers the current
settings as defaults (`meshcore-home-gateway config answers`) and keeps those it does not ask for.

### Consequences

* Good, because the bridge has its own small container (256 MB memory, 2 GB disk by default),
  independent of the Home Assistant virtual machine and its updates.
* Good, because configuration happens in dialogs that show what the radio actually offers, and
  wrong answers are caught before a container exists.
* Good, because the same zipapp and unit work on any systemd-based Debian outside Proxmox.
* Bad, because Proxmox resolves a `dev0` device when the container starts: the container does
  not start while the radio is unplugged (also at host boot), and a radio that gets a different
  device number after re-plugging is only reachable after a container restart (documented in
  `docs/deployment.md`).
* Bad, because nesting exposes more of the host's `/proc` and `/sys` to the container; acceptable
  for an unprivileged container, and Debian 13 needs it.
* Bad, because the script depends on Proxmox command-line tools whose options and output can
  change between major versions; the installer tests run it against stubs of those tools.

### Confirmation

`tests/test_installer.py` runs the script with stub `pct`, `pveam`, `pvesm`, `pvesh`, `pveversion`,
`dpkg-query` and `whiptail` (output formats of Proxmox VE 9 from its sources; the whiptail stub
parses options like the real one) and checks the container options, the device passthrough, the
commands inside the container, refusals before anything is created, and that the answers render
to a valid configuration. `tests/manual/installer_dialogs.py` drives the real whiptail.
`shellcheck` runs in CI. The hardware checklist covers a real installation.

## Pros and Cons of the Options

### Inside the Home Assistant virtual machine

* Good, because no extra container.
* Bad, because the USB device must be passed to the virtual machine and the bridge shares its
  update cycle; see [ADR 0002](0002-standalone-bridge-instead-of-home-assistant-integration.md).

### Docker container

* Good, because widespread.
* Bad, because it needs a Docker host (on Proxmox usually an extra virtual machine or a nested
  setup) and an image registry, which adds a supply chain of its own.

### Privileged container or directly on the host

* Bad, because a compromise of the bridge would reach the host; with nesting, a privileged
  container is "essentially uncontained"
  ([Proxmox staff](https://forum.proxmox.com/posts/833838)).

## More Information

* Container options and device passthrough, `pct.conf(5)`:
  <https://pve.proxmox.com/pve-docs/pct.conf.5.html>
* Proxmox VE container chapter: <https://pve.proxmox.com/pve-docs/chapter-pct.html>
* Debian 13 containers without nesting (Proxmox staff in the forum):
  <https://forum.proxmox.com/threads/lxc-debian-13-with-nesting-disabled-no-console.173418/>
* A missing device stops the container start (`pve-container`, `src/PVE/LXC/Tools.pm`):
  <https://github.com/proxmox/pve-container/blob/7cc33f73778067a4b0deaf4443d8b4b2273afe36/src/PVE/LXC/Tools.pm>
* Revisit if Proxmox VE offers hot-plug USB passthrough for containers, or if the bridge needs to
  run on hosts without systemd.
