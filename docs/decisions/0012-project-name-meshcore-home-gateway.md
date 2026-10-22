---
status: accepted
date: 2026-10-22
decision-makers: maintainers
---

# The project is named meshcore-home-gateway

## Context and Problem Statement

The project started under the working name `meshcore-bus`. The name says how the parts talk to
each other (a message bus), but not what the software is for: a gateway that lets home automation
send alerts over a MeshCore mesh and take commands back from it. Several community projects with
similar names already exist and do other things. The name must be fixed before the first release,
because it appears in public interfaces: the command, the Python package, the systemd unit, the
system user, file paths, the MQTT client identifier and the Home Assistant entity IDs.

## Decision Drivers

* The name tells a newcomer what the software does.
* No confusion with existing MeshCore projects.
* One name for the repository, the program, the package and the paths.
* Changing it later would break installations and Home Assistant entity IDs.

## Considered Options

* `meshcore-home-gateway`
* `meshcore-mqtt-gateway`
* `meshcore-alert-gateway`
* Keeping `meshcore-bus`

## Decision Outcome

Chosen option: "`meshcore-home-gateway`", because it names both ends, home automation and the
MeshCore mesh, and the role in between, and no other project uses it (checked on GitHub and PyPI
on 2026-10-22). It is used everywhere: the repository, the command `meshcore-home-gateway`, the
Python package `meshcore_home_gateway`, the unit `meshcore-home-gateway.service`, the system user
and group, `/etc/meshcore-home-gateway`, `/var/lib/meshcore-home-gateway`,
`/usr/local/lib/meshcore-home-gateway`, the installer `deploy/proxmox/meshcore-home-gateway-lxc.sh`,
the MQTT client identifier `meshcore-home-gateway-<node_id>`, and the Home Assistant device and
entity IDs `meshcore_home_gateway_<node_id>_...`. The default MQTT base topic stays `meshcore`: it
names the network, not the program.

### Consequences

* Good, because the name explains the purpose and is unique.
* Good, because the change happens before the first release: there are no installations to
  migrate.
* Neutral, because the earlier decision records were updated for the name only (paths and
  commands); their decisions are unchanged.
* Bad, because the name is longer to type than the working name.

### Confirmation

`git grep -i meshcore-bus` lists only this record and the changelog entry. The tests check the
command, the package, the unit, the installer and the discovery IDs under the new name.

## Pros and Cons of the Options

### `meshcore-mqtt-gateway`

* Good, because it names the protocol.
* Bad, because "MQTT gateway" in the MeshCore community usually means repeater firmware that
  uploads received packets to a broker, a different kind of software.

### `meshcore-alert-gateway`

* Good, because alerts are the main use.
* Bad, because the commands in the other direction are left out.

### Keeping `meshcore-bus`

* Good, because nothing changes.
* Bad, because it describes the mechanism, not the purpose, and resembles `meshcore-mqtt` and
  `meshcore-mqtt-bridge`, two existing bridges.

## More Information

* Existing projects with similar names (2026-10-22): `ipnet-mesh/meshcore-mqtt` (MQTT bridge),
  `Cisien/meshcoretomqtt` (packet capture to MQTT), the Unraid app `meshcore-mqtt-bridge`, and
  `meshcore-dev/meshcore-ha` (Home Assistant integration).
