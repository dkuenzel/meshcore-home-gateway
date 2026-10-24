#!/usr/bin/env bash
# meshcore-home-gateway installer for Proxmox VE 8 (current updates) and 9.
#
# Creates an unprivileged Debian 13 container, passes the radio's USB serial device through,
# installs python3 and python3-paho-mqtt from Debian and meshcore-home-gateway inside the
# container, reads the radio's channels and contacts, writes the configuration and starts the
# service. Run as root on the Proxmox host, straight from a release; nothing is stored on the
# host, the program is downloaded and checked inside the container:
#
#   bash -c "$(wget -qO- https://github.com/dkuenzel/meshcore-home-gateway/releases/latest/download/meshcore-home-gateway-lxc.sh)"
#
# Run from a checkout, it installs that checkout. Options: --help. Documentation:
# docs/deployment.md. Design decision: docs/decisions/0006-*.md.
set -Eeuo pipefail

REPOSITORY=dkuenzel/meshcore-home-gateway
RELEASES=https://github.com/$REPOSITORY/releases
RELEASE=""     # the copy published with a release names its version here (make release)
SCRIPT=${BASH_SOURCE[0]-}   # unset when the script comes from a URL: bash -c "$(wget ...)"
SOURCE=""      # where the program comes from: checkout, release or ref (detect_source)
REPO_DIR=""    # SOURCE=checkout: the checkout
REF=main       # SOURCE=ref: the branch, tag or commit whose source is built in the container
PICKED=""      # SOURCE=release: the release to install, this copy's or the one --release names
VERSION=""     # known in advance for a checkout and a release, else after the installation
TITLE="meshcore-home-gateway"
BROKER_LOGIN=""   # the result of check_broker, for the final message
SERIAL_DIR=${MESHCORE_HOME_GATEWAY_SERIAL_DIR:-/dev/serial/by-id}   # tests point this elsewhere
DIALOUT_GID=20            # static in Debian's base-passwd; checked inside the container
MIN_PVE_CONTAINER=5.3.1   # first pve-container that accepts every Debian 13 point release
LIB=/usr/local/lib/meshcore-home-gateway
CONFIG=/etc/meshcore-home-gateway/config.toml
STATE_DIR=/var/lib/meshcore-home-gateway   # the service's state (StateDirectory=)
STAGE=/root/meshcore-home-gateway-src      # the program before its installation, in the container
ANSWERS_FILE=/root/meshcore-home-gateway-answers
# pct exec passes the host's environment into the container; commands start from a clean one
CT_ENV=(env -i PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8)

# answers that steer the installer; every other key in --answers FILE is a configuration setting
INSTALLER_KEYS=" setup ct.id ct.hostname ct.storage ct.disk_gb ct.memory_mb ct.bridge ct.ip \
ct.gateway ct.template_storage radio.choice targets.channels targets.contacts "
declare -A ANSWERS=()   # from --answers FILE
declare -A CURRENT=()   # settings of the installation being reconfigured
declare -A OUT=()       # answers for `meshcore-home-gateway config render`
declare -A CT=()        # container settings
declare -a PROBE=()     # the radio connection as arguments of `probe` (probe_args)
INTERACTIVE=1
ADVANCED=0
CREATED=""              # container created by this run, named in error messages
STOPPED=""              # container whose service this run stopped, started again on errors
WORK=""                 # an empty directory on the host: marks errors already reported

# --- output and errors ----------------------------------------------------------------------

# after a hangup the terminal refuses writes: die must still restart the service
log() { printf '%s\n' "meshcore-home-gateway: $*" >&2 || true; }

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
  if (( INTERACTIVE )) && [[ $* != cancelled* ]] && type -P whiptail >/dev/null; then
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
# Ctrl-C, kill or a closed SSH session: die restarts a stopped service and names a new container
# PIPE too: after `... | tee log` and Ctrl-C the reader is gone, and the first message of die
# would end the script before it starts the service again (caught, not ignored, so that the
# commands it runs get the default SIGPIPE back)
trap 'trap "" INT TERM HUP PIPE; die "cancelled (interrupted)"' INT TERM HUP PIPE

# --- dialogs --------------------------------------------------------------------------------

# In the caller's locale, one setting the host lacks (LANG=de_DE.UTF-8 passed on by ssh, or
# LC_CTYPE=UTF-8 from macOS) is enough for the C locale, and whiptail then drops every non-ASCII
# character typed, passwords included, without a word. C.UTF-8 is built into Debian's C library.
whiptail() { LC_ALL=C.UTF-8 command whiptail "$@"; }

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
  show_dialog "$kind" "$text" "$default" "$@"
}

show_dialog() {  # show_dialog KIND TEXT DEFAULT [CHOICE ...]: a dialog, whatever is stored
  local kind=$1 text=$2 default=$3 result
  shift 3
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
  check_target_answers
}

check_target_answers() {  # they are used only after the container is created: check them now
  local channel='(0|[1-9][0-9]{0,2})' key='[0-9a-f]{64}' item
  if [[ -v "ANSWERS[targets.channels]" ]]; then
    ANSWERS[targets.channels]=${ANSWERS[targets.channels]// /}
    [[ ${ANSWERS[targets.channels]} =~ ^($channel(,$channel)*)?$ ]] \
      || die "answers file: targets.channels: expected channel indices, comma-separated"
    for item in ${ANSWERS[targets.channels]//,/ }; do
      (( item <= 255 )) || die "answers file: targets.channels: no channel $item (0-255)"
    done
  fi
  if [[ -v "ANSWERS[targets.contacts]" ]]; then
    ANSWERS[targets.contacts]=${ANSWERS[targets.contacts]// /}
    ANSWERS[targets.contacts]=${ANSWERS[targets.contacts],,}   # as the radio lists them
    [[ ${ANSWERS[targets.contacts]} =~ ^($key(,$key)*)?$ ]] \
      || die "answers file: targets.contacts: expected public keys (64 hex digits), comma-separated"
  fi
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

usage() {
  cat <<EOF
Usage: meshcore-home-gateway-lxc.sh [OPTION...]   (as root on the Proxmox VE host)

  (no option)          install, asking in dialogs
  --answers FILE       install without dialogs (docs/deployment.md, unattended installation)
  --update CTID        install the current version into container CTID and restart the service
  --reconfigure CTID   ask again, keeping the other settings
  --release VERSION    install this release (its files are checked against its SHA256SUMS)
  --ref REF            install the source of a branch, tag or commit, built in the container
  -h, --help           show this text

Run from a checkout, the script installs that checkout. Run from a URL
  bash -c "\$(wget -qO- URL)" -- OPTION...
it downloads the program inside the container: the release it was published with, or the
source of main before the first release. Nothing is stored on the host.
EOF
}

detect_source() {  # where meshcore-home-gateway comes from, unless --release or --ref chose
  local dir
  if [[ -z $SOURCE && -n $SCRIPT && -f $SCRIPT ]]; then
    dir=$(cd -- "$(dirname -- "$SCRIPT")/../.." && pwd)
    if [[ -f $dir/src/meshcore_home_gateway/__init__.py && -f $dir/tools/build_pyz.py ]]; then
      SOURCE=checkout REPO_DIR=$dir
      VERSION=$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' \
        "$dir/src/meshcore_home_gateway/__init__.py")
    fi
  fi
  if [[ -z $SOURCE && -n $RELEASE ]]; then SOURCE=release PICKED=$RELEASE VERSION=$RELEASE; fi
  SOURCE=${SOURCE:-ref}
  TITLE="meshcore-home-gateway ${VERSION:-($REF)}"
}

prerelease() { [[ $PICKED =~ (a|b|rc|dev)[0-9]+$ ]]; }   # as the release workflow marks them

invocation() {  # how to run this installer again, for the final message
  local address
  case $SOURCE in
    checkout) printf '%s' "$SCRIPT"; return ;;
    release)  # releases/latest skips pre-releases: for one of them, its own installer
      if prerelease; then address=$RELEASES/download/v$PICKED/meshcore-home-gateway-lxc.sh
      else address=$RELEASES/latest/download/meshcore-home-gateway-lxc.sh; fi ;;
    ref)  # main's installer: the one at an older ref may not run from an address
      address=https://raw.githubusercontent.com/$REPOSITORY/main/deploy/proxmox
      address+=/meshcore-home-gateway-lxc.sh ;;
  esac
  # shellcheck disable=SC2016 # the $(...) is for the user to run, not to expand here
  printf 'bash -c "$(wget -qO- %s)" --' "$address"
  if [[ $SOURCE == ref && $REF != main ]]; then printf ' --ref %s' "$REF"; fi
}

preflight() {  # preflight MODE
  [[ $(id -u) -eq 0 ]] || die "run as root on the Proxmox host"
  local tool
  local -a tools=(pct pveam pvesm pvesh pveversion dpkg-query)
  if [[ $SOURCE == checkout ]]; then tools+=(python3 tar); fi   # to check and pack the checkout
  for tool in "${tools[@]}"; do
    command -v "$tool" >/dev/null 2>&1 || die "'$tool' not found: this must run on a Proxmox VE host"
  done
  if (( INTERACTIVE )) && [[ $1 != update ]]; then   # --update shows no dialogs
    type -P whiptail >/dev/null \
      || die "whiptail not found: install it (apt install whiptail) or use --answers FILE"
    # `wget -qO- URL | bash`: the input is the rest of the script, the dialogs would read it
    if [[ -p /dev/stdin ]]; then
      INTERACTIVE=0   # no message box either
      # shellcheck disable=SC2016 # the $(...) is for the user to run
      die "the dialogs need the terminal as input: run" \
        'bash -c "$(wget -qO- URL)" instead of piping into bash, or use --answers FILE'
    fi
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
  if [[ $SOURCE == checkout ]]; then
    [[ -n $VERSION ]] || die "cannot read the version of the checkout $REPO_DIR"
  fi
}

CHECK_SETTINGS_PY='import sys
sys.path.insert(0, sys.argv[1])
from meshcore_home_gateway.config import ConfigError, render_config
try:
    render_config(sys.stdin.buffer.read().decode("utf-8"))
except ConfigError as exc:
    sys.exit(str(exc))'

check_settings() {  # from a checkout: with its configuration code, before anything is created
  [[ $SOURCE == checkout ]] || return 0   # otherwise in the new container (check_settings_in)
  local reason
  # -B: no __pycache__ in the checkout, which may belong to a user who could not remove it; the
  # answers go through a pipe, not a file
  reason=$(write_answers | python3 -I -B -c "$CHECK_SETTINGS_PY" "$REPO_DIR/src" 2>&1) \
    || die "invalid settings: $reason"
}

check_settings_in() {  # check_settings_in CTID: with the program installed in the container
  write_answers | in_ct_input "$1" bash -c "set -e
    umask 077
    dir=\$(mktemp -d)
    trap 'rm -rf \$dir' EXIT
    cat >\$dir/answers
    meshcore-home-gateway config render --answers \$dir/answers --output \$dir/config.toml \
      >/dev/null" >&2 \
    || die "invalid settings (see above)"
}

check_broker() {  # check_broker CTID: log in as the service will, before the radio is read
  local ctid=$1 reason status
  while :; do
    # with the program in the container, another client id than the service's; its messages go
    # to stdout, into $reason. Exit 3: that version has no login check; 4: settings refused
    status=0
    reason=$(write_answers | in_ct_input "$ctid" bash -c "set -e
        umask 077
        dir=\$(mktemp -d)
        trap 'rm -rf \$dir' EXIT
        cat >\$dir/answers
        meshcore-home-gateway check-config --help | grep -e --broker >/dev/null || exit 3
        meshcore-home-gateway config render --answers \$dir/answers \
          --output \$dir/login.toml 2>&1 >/dev/null || exit 4
        meshcore-home-gateway check-config --config \$dir/login.toml --broker 2>&1 >/dev/null") \
      || status=$?
    reason=${reason//meshcore-home-gateway: /}
    case $status in
      0) BROKER_LOGIN="login accepted"; return 0 ;;
      3)  # --reconfigure keeps the installed version
        log "meshcore-home-gateway $VERSION in container $ctid cannot check the broker login"
        BROKER_LOGIN="not checked (version $VERSION cannot; check after --update)"
        return 0 ;;
      4)  # a value typed again: no login was tried
        (( INTERACTIVE )) || die "the broker settings were not accepted: $reason"
        whiptail --title "$TITLE" --msgbox -- "The broker settings were not accepted:

$reason" 16 78 >&2 || true
        ask_broker_again
        continue ;;
    esac
    if (( ! INTERACTIVE )) || ! whiptail --title "$TITLE" --yesno -- "The broker login failed:

$reason

Enter the broker settings again? (No: continue; the service keeps trying, --reconfigure fixes \
the settings later.)" 20 78 >&2; then
      log "warning: the broker login failed: $reason"
      log "the service keeps trying; change the broker settings with --reconfigure $ctid"
      BROKER_LOGIN="login failed (see above), the service keeps trying"
      return 0
    fi
    ask_broker_again
  done
}

# --- container ------------------------------------------------------------------------------

ct_running() { [[ $(pct status "$1" 2>/dev/null) == "status: running" ]]; }

in_ct() {  # in_ct CTID COMMAND...: run a command in the container, clean environment, no input
  local ctid=$1
  shift
  pct exec "$ctid" -- "${CT_ENV[@]}" "$@" </dev/null
}

in_ct_input() {  # like in_ct, the command reads this function's input (files for the container)
  local ctid=$1
  shift
  pct exec "$ctid" -- "${CT_ENV[@]}" "$@"
}

first_storage() {  # first active storage for a content type (skips the header line)
  pvesm status --content "$1" | awk 'NR > 1 && $3 == "active" && !found {print $1; found = 1}'
}

ask_container() {
  # queried in assignments of their own: a failure inside a dialog's argument would not stop
  local next_id storage template_storage
  next_id=$(pvesh get /cluster/nextid) || die "cannot get the next free container ID (pvesh)"
  storage=$(first_storage rootdir) || die "cannot list the storages (pvesm status)"
  template_storage=$(first_storage vztmpl) || die "cannot list the storages (pvesm status)"
  CT[id]=$(ask_advanced ct.id input "Container ID" "$next_id")
  [[ ${CT[id]} =~ ^[1-9][0-9]{2,8}$ ]] || die "container ID must be a number of at least 100"
  pvesh get /cluster/nextid --vmid "${CT[id]}" >/dev/null 2>&1 \
    || die "ID ${CT[id]} is already used by a container or virtual machine"
  CT[hostname]=$(ask_advanced ct.hostname input "Container host name" "meshcore-home-gateway")
  CT[storage]=$(ask_advanced ct.storage input "Storage for the container disk" "$storage")
  CT[disk]=$(ask_advanced ct.disk_gb input "Disk size in GB" "2")
  CT[memory]=$(ask_advanced ct.memory_mb input "Memory in MB" "256")
  CT[bridge]=$(ask_advanced ct.bridge input "Network bridge" "vmbr0")
  CT[ip]=$(ask_advanced ct.ip input "IPv4 address with prefix (e.g. 192.0.2.20/24), or dhcp" "dhcp")
  CT[gateway]=""
  if [[ ${CT[ip]} != dhcp ]]; then
    CT[gateway]=$(ask_required ct.gateway "IPv4 gateway" "")
  fi
  CT[template_storage]=$(ask_advanced ct.template_storage input "Storage for templates" \
    "$template_storage")
  [[ ${CT[disk]} =~ ^[1-9][0-9]*$ && ${CT[memory]} =~ ^[1-9][0-9]*$ ]] \
    || die "disk size and memory must be whole numbers"
  [[ -n ${CT[storage]} ]] || die "no active storage for container disks"
  [[ -n ${CT[template_storage]} ]] || die "no active storage for templates"
}

login_text() {  # the broker login, for messages; never the password
  if [[ -z ${OUT[mqtt.username]-} ]]; then printf 'none (no user name)'; return; fi
  printf "user '%s' %s" "${OUT[mqtt.username]}" \
    "$(if [[ -n ${OUT[mqtt.password]-} ]]; then echo with; else echo without; fi) a password"
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
Login:    $(login_text)

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
    --description "meshcore-home-gateway ${VERSION:-$REF}: MeshCore radio to MQTT and Home Assistant" \
    >&2 \
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

# Runs in the container: downloads the program into a directory, given last on the command line.
#   release URL VERSION DIR: the zipapp and the unit of a release, checked against its SHA256SUMS
#   source URL DIR: the source archive of a branch, tag or commit (HTTPS only, nothing to check)
FETCH_PY='import hashlib, io, os, shutil, sys, tarfile, urllib.request

UNIT = "deploy/systemd/meshcore-home-gateway.service"
SOURCE = ("src/meshcore_home_gateway/", "tools/build_pyz.py", UNIT)


def get(url):
    request = urllib.request.Request(url, headers={"User-Agent": "meshcore-home-gateway-lxc"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.read()
    except OSError as exc:  # HTTP errors too
        sys.exit(f"cannot download {url}: {exc}")


def put(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as file:
        file.write(data)


mode, url, target = sys.argv[1], sys.argv[2], sys.argv[-1]
shutil.rmtree(target, ignore_errors=True)
if mode == "release":
    version, sums = sys.argv[3], {}
    for line in get(url + "SHA256SUMS").decode("utf-8").splitlines():
        digest, _, name = line.partition(" ")
        sums[name.strip().lstrip("*")] = digest
    files = {
        f"meshcore-home-gateway-{version}.pyz": "meshcore-home-gateway.pyz",
        os.path.basename(UNIT): UNIT,
    }
    for name, path in files.items():
        data = get(url + name)
        if sums.get(name) != hashlib.sha256(data).hexdigest():
            sys.exit(f"{name} does not match the SHA256SUMS of the release: not installed")
        put(os.path.join(target, path), data)
else:
    try:
        with tarfile.open(fileobj=io.BytesIO(get(url)), mode="r:gz") as archive:
            members = []
            for member in archive.getmembers():
                inner = member.name.partition("/")[2]  # below the top directory of the archive
                if inner.startswith(SOURCE[0]) or inner in SOURCE[1:]:
                    member.name = inner
                    members.append(member)
            if not any(member.name == SOURCE[1] for member in members):
                sys.exit(f"{url} holds no meshcore-home-gateway source")
            archive.extractall(target, members=members, filter="data")  # no links out, no devices
    except tarfile.TarError as exc:
        sys.exit(f"{url}: not a usable source archive ({exc})")'

stage_program() {  # stage_program CTID: the program to install, in $STAGE in the container
  local ctid=$1 base
  case $SOURCE in
    checkout)  # packed on the fly: no file on the host
      tar -C "$REPO_DIR" -cz --exclude=__pycache__ src/meshcore_home_gateway tools/build_pyz.py \
        deploy/systemd/meshcore-home-gateway.service \
        | in_ct_input "$ctid" bash -c "umask 077 && cat >$STAGE.tar.gz" \
        || die "copying meshcore-home-gateway into container $ctid failed (see above)" ;;
    release)
      base=$RELEASES/download/v$PICKED/
      in_ct "$ctid" python3 -I -c "$FETCH_PY" release "$base" "$PICKED" "$STAGE" >&2 \
        || die "cannot download release $PICKED into container $ctid (see above)" ;;
    ref)
      in_ct "$ctid" python3 -I -c "$FETCH_PY" source \
        "https://github.com/$REPOSITORY/archive/$REF.tar.gz" "$STAGE" >&2 \
        || die "cannot download the source of $REF into container $ctid (see above)" ;;
  esac
}

install_service() {  # packages, user, zipapp and unit; the version switches only if it is usable
  local ctid=$1 version
  log "installing python3, python3-paho-mqtt and meshcore-home-gateway ${VERSION:-from $REF}" \
    "in container $ctid"
  in_ct "$ctid" bash -c 'apt-get update -qq && DEBIAN_FRONTEND=noninteractive \
    apt-get install -y -qq --no-install-recommends python3 python3-paho-mqtt ca-certificates' \
    >&2 || die "installing packages in container $ctid failed (see above)"
  in_ct "$ctid" bash -c 'id meshcore-home-gateway >/dev/null 2>&1 || useradd --system \
    --user-group --groups dialout --no-create-home --home-dir /nonexistent \
    --shell /usr/sbin/nologin meshcore-home-gateway' >&2 || die "creating the service user failed"
  stage_program "$ctid"
  # prints the installed version on stdout, everything else goes to stderr
  version=$(in_ct "$ctid" bash -c "set -e
    exec 3>&1 1>&2
    umask 022   # not the host's: the service user must read the program
    src=$STAGE new=$LIB/.meshcore-home-gateway.pyz.new
    trap 'rm -rf \$src \$src.tar.gz \$new' EXIT
    mkdir -p $LIB
    source=$SOURCE   # not by which files are there: a leftover archive must not replace a release
    if [ \$source = checkout ]; then
      rm -rf \$src && mkdir -p \$src && tar -C \$src -xzf \$src.tar.gz
    fi
    if [ \$source = release ]; then   # the release file, checked against SHA256SUMS
      install -m 0755 \$src/meshcore-home-gateway.pyz \$new
    else
      python3 \$src/tools/build_pyz.py --source \$src/src --output \$new \
        --interpreter '/usr/bin/python3 -I' >/dev/null
    fi
    version=\$(python3 -I \$new --version) && version=\${version##* }
    case \$version in ''|*[!0-9A-Za-z.+-]*) echo \"unexpected version '\$version'\"; exit 1 ;; esac
    if [ -f $CONFIG ]; then /usr/bin/python3 -I \$new check-config --config $CONFIG; fi
    mv -f \$new $LIB/meshcore-home-gateway-\$version.pyz   # only now: it may be the active one
    ln -sfn meshcore-home-gateway-\$version.pyz $LIB/current.pyz
    ln -sfn $LIB/current.pyz /usr/local/bin/meshcore-home-gateway
    install -m 0644 \$src/deploy/systemd/meshcore-home-gateway.service /etc/systemd/system/
    systemctl daemon-reload
    echo \$version >&3") \
    || die "meshcore-home-gateway ${VERSION:-from $REF} was not installed in container $ctid" \
      "(see above)"
  VERSION=$version TITLE="meshcore-home-gateway $version"
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

ask_broker() {  # address and login
  local tls password user
  OUT[mqtt.host]=$(ask_required mqtt.host "MQTT broker: host name or IP address (no mqtt://)" "")
  tls=$(ask_advanced mqtt.tls yesno "Use TLS for the broker connection?" "false")
  OUT[mqtt.tls]=$tls
  OUT[mqtt.port]=$(ask_advanced mqtt.port input "MQTT broker port" \
    "$(if [[ $tls == true ]]; then echo 8883; else echo 1883; fi)")
  # no suggested name in the field: whiptail adds what is typed to it instead of replacing it
  user=$(ask mqtt.username input \
    "MQTT user name as set up on the broker, e.g. meshcore-home-gateway (empty: none)" "")
  OUT[mqtt.username]=$user
  if [[ -z $user ]]; then
    # a password needs a user name: no dialog, and the current one goes (the settings check
    # refuses one given in the answers file)
    [[ -v "ANSWERS[mqtt.password]" ]] || unset 'OUT[mqtt.password]'
  elif [[ -v "CURRENT[mqtt.password]" && $user == "${CURRENT[mqtt.username]-}" ]]; then
    password=$(ask mqtt.password password \
      "Password of MQTT user '$user' (empty: keep the current one)" "")
    if [[ -n $password ]]; then OUT[mqtt.password]=$password; fi
  else  # a new user name: a current password belongs to the old one
    password=""
    if (( INTERACTIVE )) || [[ -v "ANSWERS[mqtt.password]" ]]; then
      password=$(ask mqtt.password password "Password of MQTT user '$user' (empty: none)" "")
    fi
    if [[ -n $password ]]; then OUT[mqtt.password]=$password; else unset 'OUT[mqtt.password]'; fi
  fi
}

ask_broker_again() {  # after a refused login, with dialogs: every broker setting
  local host=${OUT[mqtt.host]-} previous=${OUT[mqtt.tls]-false} was=${OUT[mqtt.username]-}
  local tls port user password before=""
  if [[ -n $was ]]; then before=" (it was '$was')"; fi
  OUT[mqtt.host]=""
  while [[ -z ${OUT[mqtt.host]} ]]; do
    OUT[mqtt.host]=$(show_dialog input "MQTT broker: host name or IP address (no mqtt://)" "$host")
  done
  tls=$(show_dialog yesno "Use TLS for the broker connection?" "$previous")
  OUT[mqtt.tls]=$tls
  port=${OUT[mqtt.port]-}
  if [[ $tls != "$previous" && ( -z $port || $port == 1883 || $port == 8883 ) ]]; then
    port=$(if [[ $tls == true ]]; then echo 8883; else echo 1883; fi)   # the other default
  fi
  OUT[mqtt.port]=$(show_dialog input "MQTT broker port" "$port")
  # empty, not the refused name: whiptail adds what is typed to a field's content
  user=$(show_dialog input "MQTT user name as set up on the broker$before; empty: none" "")
  OUT[mqtt.username]=$user
  if [[ -z $user ]]; then
    unset 'OUT[mqtt.password]'
  elif [[ -v "CURRENT[mqtt.password]" && $user == "${CURRENT[mqtt.username]-}" ]]; then
    password=$(show_dialog password \
      "Password of MQTT user '$user' (empty: keep the installed one)" "")
    OUT[mqtt.password]=${password:-${CURRENT[mqtt.password]}}
  else
    password=$(show_dialog password "Password of MQTT user '$user' (empty: none)" "")
    if [[ -n $password ]]; then OUT[mqtt.password]=$password; else unset 'OUT[mqtt.password]'; fi
  fi
}

ask_bridge() {  # broker, topics, Home Assistant and message settings
  ask_broker
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

probe_args() {  # the radio connection as arguments of `probe`, in PROBE
  local host
  if [[ ${OUT[radio.transport]} == serial ]]; then
    PROBE=(--serial "${OUT[radio.device]}")
  else
    host=${OUT[radio.host]}
    if [[ $host == *:* ]]; then host="[$host]"; fi   # IPv6 address
    PROBE=(--tcp "$host:${OUT[radio.port]:-5000}")
  fi
}

choose_targets() {  # read channels and contacts from the radio and ask which to use
  local ctid=$1 channels contacts index name key type item selected alias
  local current_channels="" current_contacts="" current_senders=${OUT[commands.senders]-}
  local -a channel_items=() contact_items=() sender_items=()
  local -A channel_name=() contact_name=() alias_of=()
  probe_args
  channels=$(in_ct "$ctid" meshcore-home-gateway probe "${PROBE[@]}" --list channels) \
    || die "cannot read the radio (is it connected and not used by another program?)"
  contacts=$(in_ct "$ctid" meshcore-home-gateway probe "${PROBE[@]}" --list contacts) \
    || die "cannot read the radio's contacts"

  for key in "${!OUT[@]}"; do  # current targets keep their aliases when selected again
    [[ $key == target.* ]] || continue
    alias_of[${OUT[$key]}]+="${alias_of[${OUT[$key]}]:+ }${key#target.}"
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

  # answered targets count also when the radio offers none (a new radio, only repeaters)
  if (( ${#channel_items[@]} )) || [[ -v "ANSWERS[targets.channels]" ]]; then
    selected=$(ask targets.channels checklist "Channels to send to" "${current_channels#,}" \
      "${channel_items[@]}")
    drop_offered channel channel_name
    for item in ${selected//,/ }; do
      if [[ -v "alias_of[channel:$item]" ]]; then
        for alias in ${alias_of[channel:$item]}; do OUT[target.$alias]=channel:$item; done
        continue
      fi
      alias=$(unique_alias "$(slug "${channel_name[$item]:-channel-$item}")" "${alias_of[@]}")
      OUT[target.$alias]=channel:$item
      [[ -v "channel_name[$item]" ]] \
        || log "warning: channel $item is not offered by the radio (target $alias)"
    done
  fi
  if (( ${#contact_items[@]} )) || [[ -v "ANSWERS[targets.contacts]" ]]; then
    selected=$(ask targets.contacts checklist "Contacts to send to" "${current_contacts#,}" \
      "${contact_items[@]}")
    drop_offered contact contact_name
    for item in ${selected//,/ }; do
      if [[ -v "alias_of[contact:$item]" ]]; then
        for alias in ${alias_of[contact:$item]}; do OUT[target.$alias]=contact:$item; done
        continue
      fi
      alias=$(unique_alias "$(slug "${contact_name[$item]:-contact}")" "${alias_of[@]}")
      OUT[target.$alias]=contact:$item
      [[ -v "contact_name[$item]" ]] \
        || log "warning: contact $item is not a chat contact on the radio (target $alias)"
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

choose_profile() {  # the radio's name, and what the managed profile changes (ADR 0020)
  local ctid=$1 lines kind setting live wanted action current="" name contact status=0
  local sets="" reported="" left="" plan=""
  local prompt="Name of the radio in the mesh (1-31 bytes; empty: keep the radio's)"
  probe_args
  lines=$(in_ct "$ctid" meshcore-home-gateway probe "${PROBE[@]}" --list profile) || status=$?
  case $status in
    0) ;;
    2)  # a usage error: a version before the managed profile, without this list (--reconfigure)
      log "meshcore-home-gateway in container $ctid does not manage the radio's settings; skipped"
      return ;;
    *) die "cannot read the radio's settings (is it connected and not used by another program?)" ;;
  esac
  while IFS=$'\t' read -r kind setting _; do
    if [[ $kind == name ]]; then current=$setting; fi
  done <<<"$lines"
  name=$(ask_advanced radio.profile.name input "$prompt" "${OUT[radio.profile.name]:-$current}")
  if [[ $name == "$current" && ! -v "OUT[radio.profile.name]" ]]; then
    name=""   # the radio's name as found: not managed, and not checked
  fi
  while [[ -n $name ]] && (( $(printf '%s' "$name" | wc -c) > 31 )); do
    (( INTERACTIVE )) && [[ ! -v "ANSWERS[radio.profile.name]" ]] \
      || die "radio.profile.name: 1 to 31 bytes, got '$name'"
    name=$(show_dialog input "$prompt" "$name")
  done
  if [[ -n $name ]]; then
    OUT[radio.profile.name]=$name
  else
    unset 'OUT[radio.profile.name]'
  fi
  # the service's own plan: the answers as its configuration, with what it stored for the radio
  lines=$(profile_plan "$ctid") || die "cannot read the radio's settings"
  while IFS=$'\t' read -r kind setting live wanted action; do
    case $kind/$action in
      change/set) sets+="  $setting: $live -> $wanted"$'\n' ;;
      change/reported) reported+="  $setting: $live -> $wanted"$'\n' ;;
      change/kept) left+="  $setting: $live (changed on the radio; kept)"$'\n' ;;
      change/given_up)
        left+="  $setting: $live, not $wanted (did not take effect; no longer set)"$'\n' ;;
      change/unsupported)
        left+="  $setting: $live, not $wanted (the firmware does not support it)"$'\n' ;;
      contact/*)  # contact KEY ACTION NAME: a contact target or command sender
        contact="  contact ${setting:0:12}${wanted:+ $wanted}"
        case $live in
          add) sets+="$contact: added as a favourite"$'\n' ;;
          favourite) sets+="$contact: made a favourite"$'\n' ;;
          missing) reported+="$contact: not on the radio"$'\n' ;;
          not_favourite) reported+="$contact: no favourite"$'\n' ;;
        esac ;;
    esac
  done <<<"$lines"
  if [[ -n $sets ]]; then plan+="At its next connect the service sets the radio:"$'\n'$sets; fi
  if [[ -n $reported && ${OUT[radio.profile.manage]:-auto} == observe ]]; then
    plan+="With radio.profile.manage = observe the service only reports what differs:"$'\n'$reported
  elif [[ -n $reported ]]; then
    plan+="The radio's firmware is not tested: the service only reports what differs"$'\n'"(radio.profile.manage = always sets it anyway):"$'\n'$reported
  fi
  if [[ -n $left ]]; then plan+="The service leaves alone:"$'\n'$left; fi
  if [[ -z $plan ]]; then
    log "the radio already has the managed profile"
    return
  fi
  log "${plan//$'\n'/$'\n'  }"
  if (( INTERACTIVE )); then
    whiptail --title "$TITLE" --msgbox -- "$plan" 20 78 >&2 || true
  fi
}

profile_plan() {  # profile_plan CTID: `probe --list profile` with the answers as configuration
  local ctid=$1 args
  args=$(printf ' %q' "${PROBE[@]}")
  # through a pipe and a private directory: the answers hold the broker password
  write_answers | in_ct_input "$ctid" bash -c "set -e
    umask 077
    dir=\$(mktemp -d)
    trap 'rm -rf \$dir' EXIT
    cat >\$dir/answers
    meshcore-home-gateway config render --answers \$dir/answers --output \$dir/profile.toml \
      >/dev/null
    meshcore-home-gateway probe$args --config \$dir/profile.toml --state-dir $STATE_DIR \
      --list profile"
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
  local ctid=$1
  # through a pipe and a private file in the container: the answers hold the broker password
  write_answers | in_ct_input "$ctid" bash -c "set -e
    umask 022   # not the host's: the service user must reach the configuration
    trap 'rm -f $ANSWERS_FILE' EXIT
    (umask 077 && cat >$ANSWERS_FILE)
    meshcore-home-gateway config render --answers $ANSWERS_FILE \
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

summary() {  # commands for the user: pct exec takes the host's PATH, which may lack /usr/local/bin
  local ctid=$1 text update
  update="$(invocation) --update $ctid"
  if [[ $SOURCE == release ]] && prerelease; then   # its own installer would install it again
    update="the installer of a newer release ($RELEASES) with -- --update $ctid"
  fi
  text="meshcore-home-gateway $VERSION runs in container $ctid.

Broker:       ${BROKER_LOGIN:-login not checked}
Logs:         pct exec $ctid -- journalctl -u meshcore-home-gateway -f
Check:        pct exec $ctid -- /usr/local/bin/meshcore-home-gateway check-config
Reconfigure:  $(invocation) --reconfigure $ctid
Update:       $update"
  if [[ ${OUT[radio.transport]} == serial ]]; then
    text+="

The container starts only while the radio is plugged in.
After re-plugging the radio: pct reboot $ctid"
  fi
  log "$text"
  if (( INTERACTIVE )); then  # 22 rows hold it with the wrapped commands; with --scrolltext, Enter
    whiptail --title "$TITLE" --msgbox -- "$text" 22 78 >&2 || true   # would not close the box
  fi
}

# --- modes ----------------------------------------------------------------------------------

install() {
  local template
  pass_settings
  choose_setup
  ask_container
  ask_radio
  ask_bridge
  check_settings   # from a checkout; otherwise right after the installation in the container
  confirm_install
  template=$(choose_template "${CT[template_storage]}")
  create_container "$template"
  start_container "${CT[id]}"
  if [[ ${OUT[radio.transport]} == serial ]]; then check_device_group "${CT[id]}"; fi
  install_service "${CT[id]}"
  if [[ $SOURCE != checkout ]]; then check_settings_in "${CT[id]}"; fi
  check_broker "${CT[id]}"
  choose_targets "${CT[id]}"
  choose_profile "${CT[id]}"
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
  local ctid=$1 key version
  for key in "${!ANSWERS[@]}"; do  # the container and the radio connection stay as installed
    case $key in
      radio.choice|ct.*) die "--reconfigure cannot apply the answer $key (see docs/deployment.md)" ;;
    esac
  done
  ct_running "$ctid" || die "container $ctid is not running"
  load_current "$ctid"
  version=$(in_ct "$ctid" meshcore-home-gateway --version) && version=${version##* }
  [[ $version =~ ^[0-9][0-9A-Za-z.+-]*$ ]] \
    || die "cannot read the version of meshcore-home-gateway in container $ctid"
  VERSION=$version TITLE="meshcore-home-gateway $version"   # what runs there, not this installer
  pass_settings
  choose_setup
  ask_bridge
  check_settings_in "$ctid"   # with the version installed there, which renders the configuration
  check_broker "$ctid"   # while the service still runs
  STOPPED=$ctid   # first: an interruption during the stop also starts the service again
  in_ct "$ctid" systemctl stop meshcore-home-gateway >&2   # the radio accepts a single client
  choose_targets "$ctid"
  choose_profile "$ctid"
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
      --release)
        [[ $# -ge 2 && $2 =~ ^[0-9][0-9A-Za-z.+-]*$ ]] || die "--release needs a version, e.g. 0.1.0"
        [[ $SOURCE != ref ]] || die "--release and --ref exclude each other"
        SOURCE=release PICKED=$2 VERSION=$2; shift 2 ;;
      --ref)
        [[ $# -ge 2 && $2 =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] \
          || die "--ref needs a branch, tag or commit, e.g. main"
        [[ $SOURCE != release ]] || die "--release and --ref exclude each other"
        SOURCE=ref REF=$2; shift 2 ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option $1 (see --help)" ;;
    esac
  done
  detect_source
  preflight "$mode"
  WORK=$(mktemp -d)
  case $mode in
    install) install ;;
    update) update "$ctid" ;;
    reconfigure) reconfigure "$ctid" ;;
  esac
}

main "$@"
