#!/usr/bin/env bash
# meshcore-home-gateway installer for Proxmox VE 8 (current updates) and 9.
#
# Creates an unprivileged Debian 13 container, passes the radio's USB serial device through,
# installs python3 and python3-paho-mqtt from Debian, builds the meshcore-home-gateway zipapp from
# this checkout inside the container, reads the radio's channels and contacts, writes the
# configuration and starts the service. Run as root on the Proxmox host, in the checkout:
#
#   deploy/proxmox/meshcore-home-gateway-lxc.sh                     install, asking in dialogs
#   deploy/proxmox/meshcore-home-gateway-lxc.sh --answers FILE      install without dialogs
#   deploy/proxmox/meshcore-home-gateway-lxc.sh --update CTID       install this checkout into CTID
#   deploy/proxmox/meshcore-home-gateway-lxc.sh --reconfigure CTID  ask again, keep other settings
#
# Documentation: docs/deployment.md. Design decision: docs/decisions/0006-*.md.
set -Eeuo pipefail

REPO_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)
VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' \
  "$REPO_DIR/src/meshcore_home_gateway/__init__.py")
TITLE="meshcore-home-gateway $VERSION"
SERIAL_DIR=${MESHCORE_HOME_GATEWAY_SERIAL_DIR:-/dev/serial/by-id}   # tests point this elsewhere
DIALOUT_GID=20            # static in Debian's base-passwd; checked inside the container
MIN_PVE_CONTAINER=5.3.1   # first pve-container that accepts every Debian 13 point release
LIB=/usr/local/lib/meshcore-home-gateway
CONFIG=/etc/meshcore-home-gateway/config.toml
# pct exec passes the host's environment into the container; commands start from a clean one
CT_ENV=(env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8)

# answers that steer the installer; every other key in --answers FILE is a configuration setting
INSTALLER_KEYS=" setup ct.id ct.hostname ct.storage ct.disk_gb ct.memory_mb ct.bridge ct.ip \
ct.gateway ct.template_storage radio.choice targets.channels targets.contacts "
declare -A ANSWERS=()   # from --answers FILE
declare -A CURRENT=()   # settings of the installation being reconfigured
declare -A OUT=()       # answers for `meshcore-home-gateway config render`
declare -A CT=()        # container settings
INTERACTIVE=1
ADVANCED=0
CREATED=""              # container created by this run, named in error messages
STOPPED=""              # container whose service this run stopped, started again on errors
WORK=""

# --- output and errors ----------------------------------------------------------------------

log() { printf '%s\n' "meshcore-home-gateway: $*" >&2; }

die() {
  if [[ -n $WORK ]]; then : >"$WORK/failed"; fi   # a parent shell's ERR trap stays quiet
  log "error: $*"
  if [[ -n $STOPPED ]]; then
    in_ct "$STOPPED" systemctl start meshcore-home-gateway >&2 \
      && log "the service in container $STOPPED runs again with its previous configuration"
  fi
  if [[ -n $CREATED ]]; then
    log "container $CREATED was created but is not finished; remove it with" \
      "'pct stop $CREATED; pct destroy $CREATED' and run the installer again"
  fi
  if (( INTERACTIVE )) && [[ $* != cancelled* ]] && command -v whiptail >/dev/null 2>&1; then
    whiptail --title "$TITLE" --msgbox -- "Error: $*" 12 72 >&2 || true
  fi
  exit 1
}

on_error() {  # ERR trap: a command failed; errors die() reported in a subshell are not repeated
  if [[ -n $WORK && -e $WORK/failed ]]; then exit 1; fi
  die "command failed at line $1"
}

cleanup() {
  if [[ -n $WORK ]]; then rm -rf -- "$WORK"; fi
}

trap cleanup EXIT
trap 'on_error $LINENO' ERR

# --- dialogs --------------------------------------------------------------------------------

# ask KEY KIND TEXT DEFAULT [CHOICE DESCRIPTION [on|off] ...]
#   KIND: input | password | yesno | menu | checklist. The answer comes from --answers, else from
#   a dialog; without dialogs it is the default. When reconfiguring, the current setting is the
#   default. whiptail draws on stdout and writes the answer to stderr, so `3>&1 1>&2 2>&3`
#   swaps them: the screen goes to the terminal, the answer into $(...). `--` ends whiptail's
#   options, so names from the radio that start with '-' are not taken for options.
ask() {
  local key=$1 kind=$2 text=$3 default=${4-}
  shift 4 || shift $#
  if [[ -v "CURRENT[$key]" ]]; then default=${CURRENT[$key]}; fi
  if [[ -v "ANSWERS[$key]" ]]; then
    printf '%s' "${ANSWERS[$key]}"
    return
  fi
  if (( ! INTERACTIVE )); then
    printf '%s' "$default"
    return
  fi
  local result
  case $kind in
    input)
      result=$(whiptail --title "$TITLE" --inputbox -- "$text" 12 72 "$default" 3>&1 1>&2 2>&3) ;;
    password)
      result=$(whiptail --title "$TITLE" --passwordbox -- "$text" 12 72 3>&1 1>&2 2>&3) ;;
    yesno)  # the answer is the exit status; the screen goes to stderr, not into $(...)
      local -a options=()
      if [[ $default == false ]]; then options+=(--defaultno); fi
      if whiptail --title "$TITLE" "${options[@]}" --yesno -- "$text" 12 72 >&2; then
        result=true
      else
        result=false
      fi ;;
    menu)
      result=$(whiptail --title "$TITLE" --default-item "$default" --menu -- "$text" 20 78 10 \
        "$@" 3>&1 1>&2 2>&3) ;;
    checklist)
      result=$(whiptail --title "$TITLE" --separate-output --checklist -- "$text" 20 78 10 \
        "$@" 3>&1 1>&2 2>&3 | paste -sd, -) ;;
    *) die "unknown dialog kind $kind" ;;
  esac || die "cancelled"
  printf '%s' "$result"
}

ask_advanced() {  # like ask, but the default setup takes the default without a dialog
  if (( INTERACTIVE && ! ADVANCED )); then
    local default=${4-}
    if [[ -v "CURRENT[$1]" ]]; then default=${CURRENT[$1]}; fi
    printf '%s' "$default"
    return
  fi
  ask "$@"
}

ask_required() {  # ask_required KEY TEXT DEFAULT: an input that must not stay empty
  local value
  while :; do
    value=$(ask "$1" input "$2" "${3-}")
    if [[ -n $value ]]; then break; fi
    (( INTERACTIVE )) || die "no answer for $1 ($2)"
  done
  printf '%s' "$value"
}

load_answers() {
  local file=$1 line key number=0 invalid
  [[ -r $file ]] || die "cannot read answers file $file"
  # in a UTF-8 locale '.' matches no invalid byte: such a line does not match as a whole
  invalid=$(LC_ALL=C.UTF-8 grep -naxv '.*' -- "$file" | head -n 1 | cut -d: -f1) || true
  [[ -z $invalid ]] || die "answers file line $invalid: not valid UTF-8 (save the file as UTF-8)"
  while IFS= read -r line || [[ -n $line ]]; do
    number=$(( number + 1 ))
    line=${line%$'\r'}   # files edited on Windows
    [[ -z $line || $line == \#* ]] && continue
    key=${line%%=*}
    # the line number only: the line may hold the broker password (also after a ':' or ' '
    # typed instead of '=', when the password itself contains a '=')
    [[ $line == *=* && $key =~ ^[A-Za-z0-9_.-]+$ ]] \
      || die "answers file line $number: expected key=value"
    case $key in
      setup|ct.*|radio.choice|targets.*)
        [[ $INSTALLER_KEYS == *" $key "* ]] || die "answers file: unknown answer $key" ;;
    esac
    ANSWERS[$key]=${line#*=}
  done <"$file"
}

pass_settings() {  # configuration settings from --answers go to the configuration as given
  local key
  for key in "${!ANSWERS[@]}"; do
    if [[ $INSTALLER_KEYS != *" $key "* ]]; then OUT[$key]=${ANSWERS[$key]}; fi
  done
}

choose_setup() {
  local setup
  setup=$(ask setup menu "Setup" "default" \
    default "recommended: asks only for the radio, the broker and the targets" \
    advanced "all settings: container, network, topics, Home Assistant")
  if [[ $setup == advanced ]]; then ADVANCED=1; fi
}

# --- checks ---------------------------------------------------------------------------------

preflight() {
  [[ $(id -u) -eq 0 ]] || die "run as root on the Proxmox host"
  local tool
  for tool in pct pveam pvesm pvesh pveversion dpkg-query python3 tar; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found: this must run on a Proxmox VE host"
  done
  if (( INTERACTIVE )); then
    command -v whiptail >/dev/null 2>&1 \
      || die "whiptail not found: install it (apt install whiptail) or use --answers FILE"
  fi
  local version major container
  version=$(pveversion | sed -n 's|^pve-manager/\([^/]*\)/.*|\1|p')   # 8.4.14, or 7.4-3
  major=${version%%.*}
  [[ $major =~ ^[0-9]+$ ]] || die "cannot read the Proxmox VE version"
  (( major >= 8 )) || die "Proxmox VE $version is too old: 8 with current updates, or 9, is needed"
  # shellcheck disable=SC2016 # ${Version} is a dpkg-query field, not a shell variable
  container=$(dpkg-query -W -f '${Version}' pve-container 2>/dev/null) \
    || die "package pve-container not found"
  if [[ $(printf '%s\n' "$MIN_PVE_CONTAINER" "$container" | sort -V | sed -n 1p) \
        != "$MIN_PVE_CONTAINER" ]]; then
    die "pve-container $container cannot create Debian 13 containers;" \
      "update the host first (apt update && apt full-upgrade)"
  fi
  [[ -n $VERSION && -f $REPO_DIR/tools/build_pyz.py ]] || die "run from a complete meshcore-home-gateway checkout"
}

CHECK_SETTINGS_PY='import sys
sys.path.insert(0, sys.argv[1])
from meshcore_home_gateway.config import ConfigError, render_config
try:
    with open(sys.argv[2], encoding="utf-8") as answers:
        render_config(answers.read())
except ConfigError as exc:
    sys.exit(str(exc))'

check_settings() {  # validate the answers with this checkout before anything is created
  local answers="$WORK/check" reason
  ( umask 077 && write_answers >"$answers" )
  reason=$(python3 -I -c "$CHECK_SETTINGS_PY" "$REPO_DIR/src" "$answers" 2>&1) \
    || die "invalid settings: $reason"
  rm -f -- "$answers"
}

# --- container ------------------------------------------------------------------------------

ct_running() { [[ $(pct status "$1" 2>/dev/null) == "status: running" ]]; }

in_ct() {  # in_ct CTID COMMAND...: run a command in the container, clean environment, no input
  local ctid=$1
  shift
  pct exec "$ctid" -- "${CT_ENV[@]}" "$@" </dev/null
}

first_storage() {  # first active storage for a content type (skips the header line)
  pvesm status --content "$1" | awk 'NR > 1 && $3 == "active" && !found {print $1; found = 1}'
}

ask_container() {
  CT[id]=$(ask_advanced ct.id input "Container ID" "$(pvesh get /cluster/nextid)")
  [[ ${CT[id]} =~ ^[1-9][0-9]{2,8}$ ]] || die "container ID must be a number of at least 100"
  pvesh get /cluster/nextid --vmid "${CT[id]}" >/dev/null 2>&1 \
    || die "ID ${CT[id]} is already used by a container or virtual machine"
  CT[hostname]=$(ask_advanced ct.hostname input "Container host name" "meshcore-home-gateway")
  CT[storage]=$(ask_advanced ct.storage input "Storage for the container disk" \
    "$(first_storage rootdir)")
  CT[disk]=$(ask_advanced ct.disk_gb input "Disk size in GB" "2")
  CT[memory]=$(ask_advanced ct.memory_mb input "Memory in MB" "256")
  CT[bridge]=$(ask_advanced ct.bridge input "Network bridge" "vmbr0")
  CT[ip]=$(ask_advanced ct.ip input "IPv4 address with prefix (e.g. 192.0.2.20/24), or dhcp" "dhcp")
  CT[gateway]=""
  if [[ ${CT[ip]} != dhcp ]]; then
    CT[gateway]=$(ask_required ct.gateway "IPv4 gateway" "")
  fi
  CT[template_storage]=$(ask_advanced ct.template_storage input "Storage for templates" \
    "$(first_storage vztmpl)")
  [[ ${CT[disk]} =~ ^[1-9][0-9]*$ && ${CT[memory]} =~ ^[1-9][0-9]*$ ]] \
    || die "disk size and memory must be whole numbers"
  [[ -n ${CT[storage]} ]] || die "no active storage for container disks"
  [[ -n ${CT[template_storage]} ]] || die "no active storage for templates"
}

confirm_install() {
  if (( ! INTERACTIVE )); then return 0; fi
  local radio=${OUT[radio.device]:-${OUT[radio.host]-}:${OUT[radio.port]-}}
  local network="${CT[bridge]}, ${CT[ip]}${CT[gateway]:+, gateway ${CT[gateway]}}"
  whiptail --title "$TITLE" --yesno -- "Create container ${CT[id]} (${CT[hostname]}) now?

Disk:     ${CT[storage]}, ${CT[disk]} GB
Memory:   ${CT[memory]} MB
Network:  $network
Radio:    $radio
Broker:   ${OUT[mqtt.host]}:${OUT[mqtt.port]}, TLS ${OUT[mqtt.tls]}

Afterwards the channels and contacts are read from the radio." 18 78 >&2 || die "cancelled"
}

choose_template() {  # newest Debian 13 template, downloaded if needed; prints its volume ID
  local storage=$1 output template
  output=$(pveam update 2>&1) || true   # exits 0 even when it fails: check the text
  if [[ $output != *"update successful"* ]]; then
    log "warning: template list update failed (${output:-no output}); using the cached list"
  fi
  template=$(pveam available --section system \
    | awk '$2 ~ /^debian-13-standard_.*_amd64\.tar\.(zst|xz|gz)$/ {print $2}' | sort -V | tail -n 1)
  [[ -n $template ]] || die "pveam offers no Debian 13 template (check the host's internet access)"
  if ! has_template "$storage" "$template"; then
    log "downloading $template to $storage"
    pveam download "$storage" "$template" >&2 || true   # the exit status is not reliable either
    has_template "$storage" "$template" || die "could not download $template to $storage"
  fi
  printf '%s:vztmpl/%s' "$storage" "$template"
}

has_template() {
  local listing
  listing=$(pveam list "$1" 2>/dev/null) || return 1
  [[ $listing == *"$1:vztmpl/$2"* ]]
}

create_container() {
  local template=$1 net
  net="name=eth0,bridge=${CT[bridge]},ip=${CT[ip]}"
  if [[ -n ${CT[gateway]} ]]; then net+=",gw=${CT[gateway]}"; fi
  log "creating container ${CT[id]}"
  pct create "${CT[id]}" "$template" \
    --hostname "${CT[hostname]}" --cores 1 --memory "${CT[memory]}" --swap "${CT[memory]}" \
    --rootfs "${CT[storage]}:${CT[disk]}" --net0 "$net" --timezone host \
    --unprivileged 1 --features nesting=1 --onboot 1 --ostype debian \
    --tags meshcore-home-gateway \
    --description "meshcore-home-gateway $VERSION: MeshCore radio to MQTT and Home Assistant" >&2 \
    || die "creating container ${CT[id]} failed (see above)"
  CREATED=${CT[id]}
  if [[ ${OUT[radio.transport]} == serial ]]; then
    pct set "${CT[id]}" --dev0 "path=${OUT[radio.device]},gid=$DIALOUT_GID,mode=0660" >&2 \
      || die "passing ${OUT[radio.device]} to container ${CT[id]} failed (see above)"
  fi
}

start_container() {
  pct start "$1" >&2 || die "container $1 did not start (see above)"
  wait_ready "$1"
}

wait_ready() {  # until systemd has booted and DNS works
  local ctid=$1 attempt
  in_ct "$ctid" timeout 120 systemctl is-system-running --wait >/dev/null 2>&1 || true
  for (( attempt = 0; attempt < 60; attempt++ )); do
    if in_ct "$ctid" getent hosts deb.debian.org >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  die "container $ctid has no network (DNS lookup of deb.debian.org failed)"
}

check_device_group() {  # dev0 used Debian's dialout GID; pass the device again if it differs
  local ctid=$1 gid
  gid=$(in_ct "$ctid" getent group dialout | cut -d: -f3) || gid=""
  [[ $gid =~ ^[0-9]+$ ]] || die "container $ctid has no dialout group"
  if [[ $gid != "$DIALOUT_GID" ]]; then
    log "dialout has GID $gid in container $ctid; passing the radio through again"
    pct set "$ctid" --dev0 "path=${OUT[radio.device]},gid=$gid,mode=0660" >&2
    pct reboot "$ctid" >&2
    wait_ready "$ctid"
  fi
}

install_service() {  # packages, user, zipapp and unit; the version switches only if it is usable
  local ctid=$1 archive="$WORK/meshcore-home-gateway-src.tar.gz"
  log "installing python3, python3-paho-mqtt and meshcore-home-gateway $VERSION in container $ctid"
  in_ct "$ctid" bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive \
    apt-get install -y -qq --no-install-recommends python3 python3-paho-mqtt' >&2 \
    || die "installing packages in container $ctid failed (see above)"
  in_ct "$ctid" bash -c 'id meshcore-home-gateway >/dev/null 2>&1 || useradd --system \
    --user-group --groups dialout --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin meshcore-home-gateway' >&2 || die "creating the service user failed"
  tar -C "$REPO_DIR" -czf "$archive" --exclude=__pycache__ \
    src/meshcore_home_gateway tools/build_pyz.py deploy/systemd/meshcore-home-gateway.service
  pct push "$ctid" "$archive" /root/meshcore-home-gateway-src.tar.gz \
    || die "copying meshcore-home-gateway into container $ctid failed (see above)"
  in_ct "$ctid" bash -c "set -e
    src=/root/meshcore-home-gateway-src new=$LIB/.meshcore-home-gateway.pyz.new
    pyz=$LIB/meshcore-home-gateway-$VERSION.pyz
    trap 'rm -rf \$src \$src.tar.gz \$new' EXIT
    rm -rf \$src && mkdir -p \$src $LIB
    tar -C \$src -xzf \$src.tar.gz
    python3 \$src/tools/build_pyz.py --source \$src/src --output \$new \
      --interpreter '/usr/bin/python3 -I' >/dev/null
    if [ -f $CONFIG ]; then /usr/bin/python3 -I \$new check-config --config $CONFIG; fi
    mv -f \$new \$pyz   # only now: the same version may be the active one
    ln -sfn meshcore-home-gateway-$VERSION.pyz $LIB/current.pyz
    ln -sfn $LIB/current.pyz /usr/local/bin/meshcore-home-gateway
    install -m 0644 \$src/deploy/systemd/meshcore-home-gateway.service /etc/systemd/system/
    systemctl daemon-reload" >&2 \
    || die "meshcore-home-gateway $VERSION was not installed in container $ctid (see above)"
}

# --- configuration --------------------------------------------------------------------------

ask_radio() {
  local -a choices=()
  local path radio
  shopt -s nullglob
  for path in "$SERIAL_DIR"/*; do
    choices+=("$path" "$(basename -- "$path")")
  done
  shopt -u nullglob
  choices+=("tcp" "radio with WiFi firmware (TCP, no authentication)")
  if (( ! INTERACTIVE )) && [[ ! -v "ANSWERS[radio.choice]" ]]; then
    die "no answer for radio.choice: name the radio's device path, or tcp"  # never guess one
  fi
  radio=$(ask radio.choice menu "Radio connection" "${choices[0]}" "${choices[@]}")
  if [[ $radio == tcp ]]; then
    OUT[radio.transport]=tcp
    OUT[radio.host]=$(ask_required radio.host "Radio address (host name or IP address)" "")
    OUT[radio.port]=$(ask radio.port input "Radio TCP port" "5000")
  else
    [[ -e $radio ]] || die "serial device $radio not found"
    OUT[radio.transport]=serial
    OUT[radio.device]=$radio
  fi
}

ask_bridge() {  # broker, topics, Home Assistant and message settings
  local tls password
  OUT[mqtt.host]=$(ask_required mqtt.host "MQTT broker address" "")
  tls=$(ask_advanced mqtt.tls yesno "Use TLS for the broker connection?" "false")
  OUT[mqtt.tls]=$tls
  OUT[mqtt.port]=$(ask_advanced mqtt.port input "MQTT broker port" \
    "$(if [[ $tls == true ]]; then echo 8883; else echo 1883; fi)")
  OUT[mqtt.username]=$(ask mqtt.username input "MQTT user name (empty: none)" \
    "meshcore-home-gateway")
  if [[ -v "CURRENT[mqtt.password]" ]]; then
    password=$(ask mqtt.password password "MQTT password (empty: keep the current one)" "")
    if [[ -n $password ]]; then OUT[mqtt.password]=$password; fi
  else
    OUT[mqtt.password]=$(ask mqtt.password password "MQTT password (empty: none)" "")
  fi
  OUT[mqtt.base_topic]=$(ask_advanced mqtt.base_topic input "Base topic" "meshcore")
  OUT[mqtt.node_id]=$(ask_advanced mqtt.node_id input "Node id (in topics and entity IDs)" \
    "radio1")
  OUT[homeassistant.discovery]=$(ask_advanced homeassistant.discovery yesno \
    "Announce the radio to Home Assistant (MQTT discovery)?" "true")
  if [[ ${OUT[homeassistant.discovery]} == true ]]; then
    OUT[homeassistant.device_name]=$(ask_advanced homeassistant.device_name input \
      "Device name in Home Assistant" "MeshCore radio")
  fi
  OUT[messages.ascii_only]=$(ask_advanced messages.ascii_only yesno \
    "Send ASCII only (for handhelds without Unicode fonts)?" "false")
}

slug() {  # alias from a name: lower case, a-z 0-9 - _, starts with a letter or digit, max 32
  local value
  value=$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -c 'a-z0-9_-' '-' \
    | sed -e 's/-\{2,\}/-/g' -e 's/^[-_]*//' | cut -c1-32 | sed -e 's/-*$//')
  printf '%s' "${value:-target}"
}

unique_alias() {  # unique_alias BASE [RESERVED...]: BASE, or BASE-2, BASE-3, ... if taken
  local base=$1 alias=$1 number=2
  shift
  while [[ -v "OUT[target.$alias]" ]] || [[ " $* " == *" $alias "* ]]; do
    alias="${base:0:29}-$number"
    number=$(( number + 1 ))
  done
  printf '%s' "$alias"
}

onoff() {  # onoff ITEM LIST: "on" if ITEM is in the comma-separated LIST
  if [[ ,$2, == *,"$1",* ]]; then echo on; else echo off; fi
}

choose_targets() {  # read channels and contacts from the radio and ask which to use
  local ctid=$1 channels contacts index name key type item selected alias host
  local current_channels="" current_contacts="" current_senders=${OUT[commands.senders]-}
  local -a probe=() channel_items=() contact_items=() sender_items=()
  local -A channel_name=() contact_name=() alias_of=()
  if [[ ${OUT[radio.transport]} == serial ]]; then
    probe=(--serial "${OUT[radio.device]}")
  else
    host=${OUT[radio.host]}
    if [[ $host == *:* ]]; then host="[$host]"; fi   # IPv6 address
    probe=(--tcp "$host:${OUT[radio.port]:-5000}")
  fi
  channels=$(in_ct "$ctid" meshcore-home-gateway probe "${probe[@]}" --list channels) \
    || die "cannot read the radio (is it connected and not used by another program?)"
  contacts=$(in_ct "$ctid" meshcore-home-gateway probe "${probe[@]}" --list contacts) \
    || die "cannot read the radio's contacts"

  for key in "${!OUT[@]}"; do  # current targets keep their alias when selected again
    [[ $key == target.* ]] || continue
    alias_of[${OUT[$key]}]=${key#target.}
    case ${OUT[$key]} in
      channel:*) current_channels+=",${OUT[$key]#channel:}" ;;
      contact:*) current_contacts+=",${OUT[$key]#contact:}" ;;
    esac
  done
  while IFS=$'\t' read -r index name; do
    [[ -n $index ]] || continue
    channel_name[$index]=$name
    channel_items+=("$index" "$name" "$(onoff "$index" "$current_channels")")
  done <<<"$channels"
  while IFS=$'\t' read -r key name type; do
    [[ -n $key && $type == chat ]] || continue
    contact_name[$key]=$name
    contact_items+=("$key" "$name" "$(onoff "$key" "$current_contacts")")
    sender_items+=("$key" "$name" "$(onoff "$key" "$current_senders")")
  done <<<"$contacts"

  if (( ${#channel_items[@]} )); then
    selected=$(ask targets.channels checklist "Channels to send to" "${current_channels#,}" \
      "${channel_items[@]}")
    drop_offered channel channel_name
    for item in ${selected//,/ }; do
      alias=${alias_of[channel:$item]:-$(unique_alias "$(slug "${channel_name[$item]:-channel-$item}")" \
        "${alias_of[@]}")}
      OUT[target.$alias]=channel:$item
    done
  fi
  if (( ${#contact_items[@]} )); then
    selected=$(ask targets.contacts checklist "Contacts to send to" "${current_contacts#,}" \
      "${contact_items[@]}")
    drop_offered contact contact_name
    for item in ${selected//,/ }; do
      alias=${alias_of[contact:$item]:-$(unique_alias "$(slug "${contact_name[$item]:-contact}")" \
        "${alias_of[@]}")}
      OUT[target.$alias]=contact:$item
    done
    selected=$(ask commands.senders checklist \
      "Contacts allowed to send commands (direct messages only)" "$current_senders" \
      "${sender_items[@]}")
    for key in ${current_senders//,/ }; do  # senders the radio does not offer stay
      if [[ ! -v "contact_name[$key]" ]]; then
        log "kept command sender $key (not offered by the radio as a chat contact)"
        [[ ,$selected, == *,"$key",* ]] || selected+=${selected:+,}$key
      fi
    done
    if [[ -n $selected ]]; then
      OUT[commands.senders]=$selected
      OUT[commands.words]=$(ask commands.words input \
        "Command words, comma-separated (e.g. status,arm,disarm)" "status")
    else
      unset 'OUT[commands.senders]' 'OUT[commands.words]'
    fi
  fi
}

drop_offered() {  # drop_offered KIND OFFERED: forget the targets the dialog asked about again
  local kind=$1 key ref
  local -n offered=$2
  for key in "${!OUT[@]}"; do
    [[ $key == target.* && ${OUT[$key]} == "$kind":* ]] || continue
    ref=${OUT[$key]#"$kind":}
    if [[ -v "offered[$ref]" ]]; then
      unset "OUT[$key]"
    else
      log "kept target ${key#target.} ($kind $ref is not offered by the radio)"
    fi
  done
}

write_answers() {  # the answers for `meshcore-home-gateway config render`, one key=value per line
  local key
  for key in "${!OUT[@]}"; do
    printf '%s=%s\n' "$key" "${OUT[$key]}"
  done | LC_ALL=C sort
}

configure() {  # write the configuration into the container
  local ctid=$1 answers="$WORK/answers"
  ( umask 077 && write_answers >"$answers" )
  pct push "$ctid" "$answers" /root/meshcore-home-gateway-answers --perms 0600
  rm -f -- "$answers"
  in_ct "$ctid" bash -c "set -e
    trap 'rm -f /root/meshcore-home-gateway-answers' EXIT
    meshcore-home-gateway config render --answers /root/meshcore-home-gateway-answers \
      --output $CONFIG --group meshcore-home-gateway" >&2 \
    || die "the configuration was not accepted (see above)"
}

load_current() {  # the settings of an existing installation, as answers
  local ctid=$1 text line
  text=$(in_ct "$ctid" meshcore-home-gateway config answers --config "$CONFIG") \
    || die "cannot read the configuration in container $ctid"
  while IFS= read -r line; do
    if [[ $line == *=* ]]; then CURRENT[${line%%=*}]=${line#*=}; fi
  done <<<"$text"
  [[ -v "CURRENT[radio.transport]" ]] || die "container $ctid has no radio configured"
  # settings missing from the file run with the program's defaults; without these two, ask_bridge
  # would offer the installer's defaults (a user name, port 8883 with TLS) and write them
  [[ -v "CURRENT[mqtt.username]" ]] || CURRENT[mqtt.username]=""
  [[ -v "CURRENT[mqtt.port]" ]] || CURRENT[mqtt.port]=1883
  for line in "${!CURRENT[@]}"; do OUT[$line]=${CURRENT[$line]}; done
}

start_service() {  # Type=notify: "active" means the service has reported readiness
  local ctid=$1 attempt
  in_ct "$ctid" systemctl enable --now meshcore-home-gateway >&2 \
    || die "the service did not start; see: pct exec $ctid -- journalctl -u meshcore-home-gateway"
  for (( attempt = 0; attempt < 15; attempt++ )); do
    if in_ct "$ctid" systemctl is-active --quiet meshcore-home-gateway; then return 0; fi
    sleep 1
  done
  die "the service did not start; see: pct exec $ctid -- journalctl -u meshcore-home-gateway"
}

summary() {
  local ctid=$1 text
  text="meshcore-home-gateway $VERSION runs in container $ctid.

Logs:         pct exec $ctid -- journalctl -u meshcore-home-gateway -f
Check:        pct exec $ctid -- meshcore-home-gateway check-config
Reconfigure:  $0 --reconfigure $ctid
Update:       $0 --update $ctid"
  if [[ ${OUT[radio.transport]} == serial ]]; then
    text+="

The container starts only while the radio is plugged in.
After re-plugging the radio: pct reboot $ctid"
  fi
  log "$text"
  if (( INTERACTIVE )); then whiptail --title "$TITLE" --msgbox -- "$text" 20 78 >&2 || true; fi
}

# --- modes ----------------------------------------------------------------------------------

install() {
  local template
  pass_settings
  choose_setup
  ask_container
  ask_radio
  ask_bridge
  check_settings
  confirm_install
  template=$(choose_template "${CT[template_storage]}")
  create_container "$template"
  start_container "${CT[id]}"
  if [[ ${OUT[radio.transport]} == serial ]]; then check_device_group "${CT[id]}"; fi
  install_service "${CT[id]}"
  choose_targets "${CT[id]}"
  configure "${CT[id]}"
  start_service "${CT[id]}"
  CREATED=""
  summary "${CT[id]}"
}

update() {
  local ctid=$1
  ct_running "$ctid" || die "container $ctid is not running"
  in_ct "$ctid" test -f "$CONFIG" \
    || die "container $ctid has no meshcore-home-gateway installation ($CONFIG is missing)"
  install_service "$ctid"
  in_ct "$ctid" systemctl restart meshcore-home-gateway >&2 \
    || die "the service did not restart; see: pct exec $ctid -- journalctl -u meshcore-home-gateway"
  log "container $ctid runs meshcore-home-gateway $VERSION"
}

reconfigure() {
  local ctid=$1 key
  for key in "${!ANSWERS[@]}"; do  # the container and the radio connection stay as installed
    case $key in
      radio.choice|ct.*) die "--reconfigure cannot apply the answer $key (see docs/deployment.md)" ;;
    esac
  done
  ct_running "$ctid" || die "container $ctid is not running"
  load_current "$ctid"
  pass_settings
  choose_setup
  ask_bridge
  check_settings
  in_ct "$ctid" systemctl stop meshcore-home-gateway >&2   # the radio accepts a single client
  STOPPED=$ctid
  choose_targets "$ctid"
  configure "$ctid"
  STOPPED=""
  start_service "$ctid"
  summary "$ctid"
}

main() {
  local mode=install ctid=""
  while (( $# )); do
    case $1 in
      --answers) [[ $# -ge 2 ]] || die "--answers needs a file"; INTERACTIVE=0; load_answers "$2"; shift 2 ;;
      --update) [[ $# -ge 2 ]] || die "--update needs a container ID"; mode=update; ctid=$2; shift 2 ;;
      --reconfigure) [[ $# -ge 2 ]] || die "--reconfigure needs a container ID"; mode=reconfigure; ctid=$2; shift 2 ;;
      -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
      *) die "unknown option $1 (see --help)" ;;
    esac
  done
  preflight
  WORK=$(mktemp -d)
  case $mode in
    install) install ;;
    update) update "$ctid" ;;
    reconfigure) reconfigure "$ctid" ;;
  esac
}

main "$@"
