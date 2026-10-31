# Platform notes

What we verified about the software this project depends on but does not control: Proxmox VE and
its container tools, the shell and whiptail, the MQTT broker and client, Home Assistant, and the
MeshCore firmware. Each entry names the versions it was verified with, the evidence, what the
project does about it, and how to check it yourself. Behaviour changes between versions: when a
release changes one of these, the entry gets the new version or goes.

Sections: [Proxmox VE](#proxmox-ve), [Shell](#shell), [Debian, systemd and
Linux](#debian-systemd-and-linux), [Python](#python), [GNU gettext](#gnu-gettext),
[GitHub](#github), [whiptail](#whiptail),
[Mosquitto](#mosquitto), [Home Assistant Mosquitto add-on](#home-assistant-mosquitto-add-on),
[paho-mqtt](#paho-mqtt), [Home Assistant MQTT](#home-assistant-mqtt), [Home Assistant
dashboards](#home-assistant-dashboards), [MeshCore companion
firmware](#meshcore-companion-firmware).

## How we verify

1. **Reproduce with the real component, at a named version.** A private Mosquitto on a local port,
   the real whiptail in a pseudo terminal
   ([`tests/manual/installer_dialogs.py`](testing.md#installer-dialogs-with-the-real-whiptail)),
   the Home Assistant release pinned in
   [`home-assistant/requirements.in`](../home-assistant/requirements.in)
   ([contract tests](testing.md#home-assistant-contract-tests)), paho-mqtt as Debian ships it. The
   output of the experiment is the evidence, not the component's documentation.
2. **Read the source at a tag.** Where no experiment is possible here (Proxmox VE, the firmware,
   the Home Assistant add-on), we read the source at a release tag and cite file and line, the
   same rule as for the firmware ([CONTRIBUTING.md](../CONTRIBUTING.md)).
3. **Write the test that fails without the fix.** Stubs answer like the real program, in the
   formats of its source ([`tests/test_installer.py`](../tests/test_installer.py)); the contract
   tests use the real Home Assistant integration. A fix counts when its test fails with the fix
   reverted; we revert it once to see that.
4. **A field report becomes a reproduction.** A failure on a real host is first reproduced here
   (a umask, a `PATH`, a locale, a broker that did not reload), then fixed, tested and recorded on
   this page.
5. **Unverified is labelled.** What only an upstream document says is marked as such; guesses are
   not listed.

## Proxmox VE

* **`pct exec` passes the host's environment into the container, `PATH` included.** A bare
  program name is looked up in the caller's `PATH`. When that lacks `/usr/local/bin`,
  `pct exec 105 -- meshcore-home-gateway` fails with `Failed to exec "meshcore-home-gateway"` and
  exit status 127, while programs in `/usr/bin` work. On a real host this broke the check command
  that the installer's final message printed. The option's description says the environment will
  no longer be kept by default with Proxmox VE 9, but pve-container 6.1.15 still keeps it.
  *Verified:* real Proxmox VE host with a Debian 13 container (Proxmox VE version not recorded);
  pve-container `src/PVE/CLI/pct.pm:215-253` at `7d99e49` (6.1.15) and `:208-246` at `3799ada`
  (5.3.6): `lxc-attach --keep-env` unless `--keep-env 0` is given, and only `SSL_CERT_FILE` and
  `SSL_CERT_DIR` are removed (`clean_environment()`, `:162-169` at `7d99e49`); LXC `v7.0.0`
  `src/lxc/attach.c:803-874` sets a default `PATH` only for `--clear-env`, and `:1832-1845`
  returns 127 when `execvp()` finds nothing (the same code is in `v6.0.0`); experiment with
  `lxc-attach` 5.0.3, whose environment code is the same: `Failed to exec` and 127 for a program
  missing from the caller's `PATH`, LXC's default `PATH` with `--clear-env`. *Here:* every
  container command runs as `env -i PATH=... LANG=C.UTF-8 ...` (`CT_ENV`, used by `in_ct()` and
  `in_ct_input()` in `deploy/proxmox/meshcore-home-gateway-lxc.sh`), `summary()` prints the
  program with its full path, and the `pct` stub in `tests/test_installer.py` refuses commands
  without `env -i`; see [deployment.md](deployment.md#what-the-script-does), *Clean environment*.
  *Check:*
  `env PATH=/usr/sbin:/usr/bin:/sbin:/bin pct exec 105 -- meshcore-home-gateway --version; echo $?`
  prints `Failed to exec` and 127; `FOO=bar pct exec 105 -- sh -c 'echo $FOO'` prints `bar`.
* **`pct exec` and `pct enter` keep the umask of the process that runs `pct`.** Neither `pct` nor
  `lxc-attach` sets one, also not with `--keep-env 0`. Under umask 077, a directory that a command
  in the container creates is 0700 and owned by root, so the service user cannot enter it. GNU
  `mkdir -p` follows the umask; `install -d`, and the parent directories that `install -D`
  creates, are 0755 whatever the umask. On a real host the installer's own `umask 077` around the
  configuration step made `/etc/meshcore-home-gateway` 0700, and the service user could not read
  the configuration (`cannot read /etc/meshcore-home-gateway/config.toml: Permission denied`).
  *Verified:* that host (the installer's own umask); `pct.pm` at `7d99e49` and `3799ada` only
  reads the umask (for `pct push`), pve-common sets one only when a caller asks for it
  (`src/PVE/Cmd.pm:121-122` at `d3e8405`), and LXC's `src/lxc/attach.c` has no `umask()` call at
  `v6.0.0` and `v7.0.0`; experiment with `lxc-attach` 5.0.3: under umask 077 the command printed
  `0077` and created a `drwx------` directory, with and without `--clear-env`; GNU coreutils 9.4
  for `mkdir` and `install`. `pct enter` starts root's shell as a non-login shell
  (`lxc_attach_run_shell()`, `attach.c:1890` at `v7.0.0`, also with lxc-pve's patch
  `0002-PVE-Config-attach-always-use-getent.patch`), and Debian 13's `/root/.bashrc` (base-files
  13.8+deb13u7, `share/dot.bashrc`) leaves `umask 022` commented out, so that shell keeps the
  umask too (read in the sources, not run on a host). *Here:* `install_service()` and
  `configure()` set `umask 022` in the container, and `configure()` writes only the answers file
  under 077; `config render` creates missing directories with mode 0755 whatever the umask
  (`_make_directory()` in `src/meshcore_home_gateway/cli.py`); `tests/test_installer.py` and
  `tests/test_cli.py` run these steps under umask 077. The manual steps in
  [deployment.md](deployment.md#what-the-script-does) run `mkdir -p` inside `pct enter` without
  setting a umask. *Check:*
  `(umask 077; pct exec 105 -- sh -c 'umask; mkdir /tmp/x; stat -c %a /tmp/x')` prints `0077` and
  `700`.
* **`pct exec` hands its standard input to the command in the container.** Data piped into
  `pct exec` arrives there, and a container command started from a script without `</dev/null`
  reads that script's input. *Verified:* experiment with `lxc-attach` 5.0.3 (two lines piped in
  arrived as two lines; with `</dev/null` nothing arrived); on a real host the installer's answers
  reached `config render` through `pct exec`. *Here:* files reach the container through command
  input, never through temporary files on the host (`in_ct_input()`, used by `stage_program()`,
  `configure()` and `check_broker()`); `in_ct()` closes the input with `</dev/null`
  ([ADR 0006](decisions/0006-deployment-in-an-unprivileged-proxmox-container.md), step 6).
  *Check:* `echo hi | pct exec 105 -- cat` prints `hi`.
* **`pveam update` and `pveam download` exit with status 0 when they fail.** `pveam update` prints
  `update successful` on standard output or `update failed - see /var/log/pveam.log for details`
  on standard error, and returns `undef` in both cases. `pveam download` runs the download as a
  task, and a failed task does not reach the exit status; only the checks before the task (for
  example a template that is not in the list, an unknown template type, or a storage that is
  disabled or holds no templates) make it fail. *Verified:* pve-manager
  `PVE/CLI/pveam.pm:37-50` at `f643ce4` (master) and `5aee9c0` (stable-8), `PVE/APLInfo.pm:240-253`
  (`update()` returns 0 after a download error) and `PVE/API2/Nodes.pm:1823-1855`
  (`apl_download`) at `f643ce4`; pve-common `src/PVE/RESTEnvironment.pm:529` and `:741-746` at
  `d3e8405` (a command-line task runs synchronously, and its result is returned only in list
  context). Read in the source, not run on a host. *Here:* `choose_template()` checks the text of
  `pveam update` and warns that it uses the cached list; after `pveam download ... || true`,
  `has_template()` looks for the template in `pveam list`. *Check:* on a host without internet
  access, `pveam update; echo $?` prints `update failed ...` and `0`.
* **On Proxmox VE 8, `pct create` makes a privileged container unless `--unprivileged 1` is
  given; on 8 and 9, nesting stays off unless `--features nesting=1` is given.** Only the web
  wizard ticks *Nesting* (for unprivileged containers), and Debian 13's systemd needs it.
  Documented with the consequences in [deployment.md](deployment.md#what-the-script-does)
  (*Unprivileged and with nesting*) and [deployment.md](deployment.md#5-troubleshooting).
  *Verified:* pve-container `src/PVE/LXC/Config.pm:648-654` (`unprivileged`, default 0) and
  `:441-449` (`nesting`, default 0) at `3799ada` (5.3.6), `:462-470` (`nesting`, default 0) at
  `7d99e49` (6.1.15); `src/PVE/API2/LXC.pm:272` at `7d99e49` (unprivileged by default since
  6.0.6); pve-manager `www/manager6/lxc/CreateWizard.js:74-83` at `f643ce4`. *Here:*
  `create_container()` passes both options.
* **pve-container accepts every Debian 13 point release only from 5.3.1 on.** See
  [deployment.md](deployment.md#requirements). *Verified:* pve-container `debian/changelog`,
  entry 5.3.1 (2025-09-07), branch `stable-bookworm` at `3799ada`. *Here:* `preflight()` refuses
  an older pve-container (`MIN_PVE_CONTAINER`).
* **A passed-through device (`devN`) is resolved when the container starts.** A missing device
  stops the start, and a device that comes back under another number is allowed only after a
  restart. See [deployment.md](deployment.md#what-the-script-does) (*Device passthrough*) and
  [deployment.md](deployment.md#5-troubleshooting). *Verified:* pve-container
  `src/PVE/LXC/Tools.pm:150-157` (`Device <path> does not exist`), `src/PVE/LXC.pm:768-786`
  (`update_lxc_config()` allows exactly that device's major and minor number) and
  `src/PVE/LXC.pm:3193` (`vm_start()` calls `update_lxc_config()`) at `7d99e49`.
* **Container IDs are 100 to 999999999, and `pvesh get /cluster/nextid --vmid N` fails
  differently below 100.** It returns a free ID, fails with `VM N already exists` for a used one,
  and fails its parameter check for an ID below 100. An earlier installer reported IDs below 100
  as already used. *Verified:* pve-common `src/PVE/JSONSchema.pm:65-74` (`pve-vmid`, minimum 100)
  and `:312-315` (`^[1-9][0-9]{2,8}\z`) at `d3e8405`; pve-manager `PVE/API2/Cluster.pm:1037-1040`
  at `f643ce4`. Read in the source, not run on a host. *Here:* `ask_container()` checks
  `^[1-9][0-9]{2,8}$` ("container ID must be a number of at least 100") before it asks `pvesh`.
  *Check:* `pvesh get /cluster/nextid --vmid 50`, and the same with a used ID.

## Shell

* **`bash -c "$(wget -qO- URL)"` prints nothing and exits with status 0 when the address answers
  404.** `wget -q` writes no message and exits with 8, but the command substitution only yields
  an empty string, and `bash -c ''` runs nothing and succeeds. On a real host the documented
  install command did nothing at all before the first release existed. *Verified:* GNU bash
  5.2.21 and GNU Wget 1.21.4, against a local web server and against the project's
  `releases/latest/download/` address on 2026-10-23 (status 0, no output; `wget -O-` printed
  `ERROR 404: Not Found.`). *Here:* [deployment.md](deployment.md#install) gives `main`'s address
  for the time before the first release, and says that a command that does nothing could not
  download the script (`wget -O- URL` shows why). *Check:*
  `bash -c "$(wget -qO- https://example.org/missing.sh)"; echo $?` prints only `0`.
* **With `wget -qO- URL | bash`, the script is bash's standard input, and every command in it
  reads from there.** A `read` or a dialog takes the following lines of the script itself, and
  those lines never run. With `bash -c "$(wget -qO- URL)"` the script is an argument, and the
  standard input stays the caller's, normally the terminal. *Verified:* GNU bash 5.2.21, with a
  served script whose `read -r answer` swallowed the next line; run with `bash -c`, the same
  `read` got the caller's input and every line ran. *Here:* `preflight()` stops an interactive
  run when `/dev/stdin` is a pipe and names `bash -c "$(wget -qO- URL)"` or `--answers FILE`
  instead; `in_ct()` runs container commands with `</dev/null`. *Check:*
  `printf 'read -r x\necho never runs\necho after\n' | bash` prints only `after`.
* **A long here-string goes through a temporary file; process substitution never does.** bash
  writes a here-string or here-document longer than the pipe holds into a file `sh-thd.*` in
  `$TMPDIR` (else `/tmp`), so it fails where no temporary file can be made; a short one goes
  through a pipe. `< <(printf …)` is a pipe at any length. *Verified:* GNU bash 5.2.21 under
  `strace -e trace=openat`: `cat <<<"$x"` opened `/tmp/sh-thd.*` twice for 70,000 bytes and never
  for 1,000; `cat < <(printf '%s\n' "$x")` opened none. *Here:* the installer reads a language
  pack with `done < <(printf '%s\n' "$text")`, so a pack is never written to the host's disk.
  *Check:* `strace -f -e trace=openat bash -c 'x=$(head -c 70000 /dev/zero | tr "\0" a); cat
  <<<"$x" >/dev/null' 2>&1 | grep -c sh-thd` prints a number above 0, and 0 with 1000.
* **In `bash -c SCRIPT ARG...`, the first argument becomes `$0`, not `$1`.**
  `bash -c "$(wget -qO- URL)" --answers FILE` gives the script `$0=--answers` and `$1=FILE`.
  *Verified:* GNU bash 5.2.21. *Here:* options follow a `--`, which becomes `$0`;
  [deployment.md](deployment.md#install) shows the form, and `invocation()` prints the `--`.
  *Check:* `bash -c 'echo "0=$0 1=$1"' --answers FILE` prints `0=--answers 1=FILE`.
* **A script run with `bash -c` has no `BASH_SOURCE`.** `${BASH_SOURCE[0]}` is unset, and under
  `set -u` the script stops with `BASH_SOURCE[0]: unbound variable`. A script run from a file has
  its path there. *Verified:* GNU bash 5.2.21. *Here:* the installer reads
  `SCRIPT=${BASH_SOURCE[0]-}`, and `detect_source()` takes an empty value as "run from an
  address". *Check:* `bash -uc 'echo "${BASH_SOURCE[0]}"'`.
* **`set -e` does not stop at a failed command substitution inside another command's arguments or
  inside `local x=$(...)`.** `echo "$(false)"` goes on with an empty value, and `local` returns 0.
  Only a plain assignment, `x=$(false)`, passes the failure on. With `set -E`, the `ERR` trap
  runs inside the substitution's subshell while the script goes on. *Verified:* GNU bash 5.2.21:
  `bash -ec 'echo "[$(false)]"; echo still running'` exits with 0, and
  `bash -ec 'x=$(false); echo not reached'` with 1. *Here:* the installer queries values in
  assignments of their own, declares locals separately and checks with `|| die`, for example
  `next_id=$(pvesh get /cluster/nextid) || die ...` in `ask_container()`; shellcheck in
  `make check` flags `local x=$(...)` (SC2155). *Check:* the two `bash -ec` commands above.
* **A bash script whose standard error is a pipe without a reader ends with `SIGPIPE` (status
  141) at its next message.** After `... 2>&1 | tee log` and Ctrl-C, `tee` is gone too, so an
  interrupt handler that first prints a message stops there; only the `EXIT` trap still runs.
  With `PIPE` trapped, the write fails and the handler goes on. An ignored `SIGPIPE`
  (`trap '' PIPE`) is inherited by every program the script starts; a caught one is reset to the
  default for them. *Verified:* GNU bash 5.2.21: an `INT` handler writing to a closed pipe ended
  with 141 before its next command, and with `PIPE` caught it ran to the end;
  `bash -c 'trap "" PIPE; yes | head -1'` printed `yes: standard output: Broken pipe`, while
  with `trap : PIPE` `yes` ended silently with 141. *Here:* the installer traps
  `INT TERM HUP PIPE` with a handler that calls `die`, so an interrupted `--reconfigure` starts
  the service again, and `log()` ignores failed writes. *Check:*
  `bash -c 'trap "" PIPE; yes | head -1'`, and the same with `trap : PIPE`.

## Debian, systemd and Linux

* **Debian 13 ships `python3-paho-mqtt` 2.1.0 and Python 3.13; Debian 12 ships paho-mqtt 1.6.1,
  without the version-2 callback API.** See
  [ADR 0005](decisions/0005-runtime-dependencies-from-the-distribution.md). *Verified:* Debian QA
  madison on 2026-10-23 (1.6.1-1 in bookworm, 2.1.0-1 in trixie; `python3-defaults` 3.11.2-1 and
  3.13.5-1); paho-mqtt `v2.0.0` defines `CallbackAPIVersion` (`src/paho/mqtt/enums.py:31`), and
  no module in `src/paho/mqtt/` at `v1.6.1` has that name; on a real host the installer installed
  `python3-paho-mqtt` 2.1.0-1 in a container from `debian-13-standard_13.6-1`. *Check:*
  `curl -s 'https://qa.debian.org/madison.php?package=python3-paho-mqtt&text=on'`.
* **The unit's full sandbox runs in an unprivileged Debian 13 container with nesting.** This
  includes `ProtectSystem=strict`, `PrivateTmp`, `ProtectProc=invisible`, `RestrictNamespaces`,
  `SystemCallFilter=@system-service`, an empty `CapabilityBoundingSet` and `Type=notify` with a
  watchdog; the radio is reached through `SupplementaryGroups=dialout`. *Verified:* real Proxmox
  VE host, container from `debian-13-standard_13.6-1` (systemd 257): the service started and read
  the radio over the passed-through USB serial device; `systemd-analyze security --offline=true`
  (systemd 255) rates the unit 1.6. *Here:* `deploy/systemd/meshcore-home-gateway.service`;
  `make unit-check` fails above 2.0; the installer enables nesting. *Check:* in the container,
  `systemctl is-active meshcore-home-gateway` and `systemd-analyze security meshcore-home-gateway`.
* **`PrivateDevices=` would hide the radio, and `DeviceAllow=` is not enforced in an
  unprivileged container.** `PrivateDevices=yes` gives the service a `/dev` with pseudo devices
  only, no physical ones. systemd enforces `DeviceAllow=` with a cgroup device BPF program; root
  in a user namespace may not load one, and systemd then logs only at debug level that BPF device
  control is not supported. *Verified:* `PrivateDevices=` in systemd.exec(5) for v257
  (<https://www.freedesktop.org/software/systemd/man/257/systemd.exec.html>); systemd `v257`
  `src/core/bpf-devices.c:254-306`, `src/core/cgroup.c:4122-4127` and `:2586-2588`
  (`unit_get_target_mask()` drops the unsupported controller without a warning); Linux `v6.14`
  `kernel/bpf/syscall.c:2782` and `:2805-2808` (`CAP_BPF` needed) and `kernel/bpf/token.c:21`
  (without a BPF token, in the initial user namespace). Read in the source, not run in a
  container; whether Proxmox gives containers a BPF token was not checked. *Here:* the unit uses
  neither option (comment in `deploy/systemd/meshcore-home-gateway.service`); the container's own
  device list (`dev0`) and the `dialout` group limit access
  ([design.md](design.md#9-deployment-and-operations), *Service*). *Check:* in the container,
  `systemd-run --wait -p DevicePolicy=strict -p DeviceAllow=/dev/null head -c1 /dev/zero` should
  succeed, although the rule forbids `/dev/zero`.
* **Debian's `dialout` group has the fixed GID 20.** *Verified:* `group.master` in base-passwd
  3.6.7 (Debian 13) and 3.6.3 (Ubuntu 24.04's build): `dialout:*:20:`. *Here:* `dev0` uses
  `DIALOUT_GID=20`, and `check_device_group()` reads the GID in the container with `getent` and
  passes the device through again if it differs
  ([deployment.md](deployment.md#what-the-script-does)). *Check:*
  `pct exec 105 -- getent group dialout`.
* **Root on the host can open a serial port that another process locked with `TIOCEXCL`; root in
  an unprivileged container cannot.** The kernel lets a process with `CAP_SYS_ADMIN` in the
  initial user namespace past the lock; everyone else gets `EBUSY`. *Verified:* Linux 6.18, on a
  pseudo terminal locked by the same process: root opened it again, while uid 65534 and root in a
  new user namespace got `Device or resource busy`; Linux `v6.14` `drivers/tty/tty_io.c:1348`.
  *Here:* the serial transport locks the port with `TIOCEXCL`
  ([design.md](design.md#61-radio-session), *Opening*); one transport test is skipped as root and
  runs as a normal user in CI ([testing.md](testing.md), below the table of test suites);
  [deployment.md](deployment.md#4-operations) stops the service before `probe`. *Check:* as root
  and as a normal user,
  `python3 -c 'import fcntl,os,pty,termios as t; m,s=pty.openpty(); fcntl.ioctl(s,t.TIOCEXCL); os.open(os.ttyname(s),2)'`
  succeeds for root on the host and fails with `EBUSY` otherwise.

## Python

* **PEP 440 calls some versions pre-releases that a tag pattern like `(a|b|rc|dev)[0-9]+$` calls
  final.** `1.0.0rc1.post1`, `1.0.0a1.post2` and `1.0.0rc1+build` are pre-releases, and the
  alternative spellings `1.0.0c1` and `1.0.0rc` mean `1.0.0rc1` and `1.0.0rc0`; `1.0.0.post1` is
  final. *Verified:* `packaging` 26.3 (`Version(v).is_prerelease`) and GNU bash 5.2.21 for the
  pattern. *Here:* `check_version()` in `tools/release_notes.py`, which `make release` runs,
  admits only the canonical forms `X.Y.Z` and `X.Y.Z(a|b|rc)N`, each with an optional `.devN`, and
  `X.Y.Z.postN`, so the pattern in `.github/workflows/release.yml` and the installer's
  `prerelease()` is right for every version that can be released. *Check:*
  `python3 -c "from packaging.version import Version as V; print(V('1.0.0c1'))"` prints
  `1.0.0rc1`, and `V('1.0.0rc1.post1').is_prerelease` is `True`.
* **On Python 3.11, `asyncio.wait_for()` can lose a cancellation.** See
  [design.md](design.md#52-concurrency). *Verified:* found by the test matrix; since 3.12.0a6
  `wait_for()` is built on `asyncio.timeout()` (gh-96764,
  <https://docs.python.org/3.12/whatsnew/changelog.html>). *Here:* waits with a time limit use
  `asyncio.timeout()`. *Check:* `grep -rn 'wait_for(' src/` finds nothing.
* **Python's `gettext` reads `LANGUAGE` also under the `C` locale; the C library ignores it
  there.** With `LANGUAGE=de` and `LC_ALL`, `LC_MESSAGES` or `LANG` set to `C` or `C.UTF-8`,
  `gettext.translation()` finds the German catalog, while glibc's `dgettext()` after
  `setlocale(LC_ALL, "")` returns the English text. *Verified:* Python 3.12.3 and glibc
  2.39-0ubuntu8.9, with a one-entry catalog compiled by GNU `msgfmt` 0.21, for
  `LANG=C.UTF-8`, `LC_ALL=C.UTF-8`, `LC_ALL=C` and `LANG=C`. *Here:* the command line follows
  Python's order, `LANGUAGE` first ([configuration.md](configuration.md#language-of-the-command-line)
  says so, and that `LC_ALL=C` alone does not give English then). The installer runs the
  container's commands with `LANG=C.UTF-8` and adds `LANGUAGE` for a loaded pack: the program
  speaks its language, `apt` and `systemctl` stay English. *Check:* compile a catalog
  `loc/de/LC_MESSAGES/x.mo`, then `LC_ALL=C.UTF-8 LANGUAGE=de python3 -c "import gettext;
  print(gettext.translation('x', 'loc').gettext('Hello'))"` prints the German text.
* **argparse %-formats help texts, and a description that contains `%(prog)`: a lone `%` there
  stops the program.** A `help=` text with `10 %` raises `TypeError` at `--help` on 3.11–3.13
  and `ValueError: badly formed help string` in `add_argument` on 3.14; a description with
  `%(prog)s` and a lone `%` raises `TypeError` in `format_help()` on all four; a description
  without `%(prog)` is not formatted. argparse looks its own words up through the module's `_`
  and `ngettext` when a parser is built and used, so `vars(argparse).update(_=…, ngettext=…)`
  translates them (titles, `usage: `, `-h`'s help, its errors). *Verified:* Python 3.11.17,
  3.12.3, 3.13.16 and 3.14.6, a script that builds a parser each way and formats its help.
  *Here:* `tr_help` marks our help and description texts; a translation of one, and of
  argparse's own words, is used only with the `%` directives of the English text
  ([design §6.12](design.md#612-translations)); `cli._speaking()` sets argparse's words for one
  command and restores them. *Check:* `python3 -c "import argparse; p =
  argparse.ArgumentParser(); p.add_argument('--x', help='10 % of it'); p.print_help()"` raises.

## GNU gettext

* **`msgfmt -c` warns about a header without translator fields but exits 0, does not compare
  `{x!r}` fields, and keeps a plural entry with an empty form.** A header with only `Language`,
  `MIME-Version`, `Content-Type`, `Content-Transfer-Encoding` and `Plural-Forms` gives four
  warnings (`Project-Id-Version`, `PO-Revision-Date`, `Last-Translator`, `Language-Team`
  missing) and status 0. In a `python-brace-format` entry, `{value}` translated as `{wert}` is a
  fatal error (status 1), but `{value!r}` as `{wert!r}` passes. `--no-hash` compiles a plural
  entry whose second form is empty, and Python's gettext then returns `""` for a count of 2.
  *Verified:* GNU gettext-tools 0.21 and Python 3.12.3, with one-entry catalogs. *Here:* the
  catalogs carry exactly those five header fields (no dates or names); `tools/i18n.py`'s own
  checks compare every field, `!r` included; our MO writer leaves a plural entry with an empty
  form out, so the text stays English, and equals GNU's bytes for every other catalog
  ([ADR 0027](decisions/0027-message-catalogs-compiled-when-loaded.md)); `make check` judges a
  catalog by the tests, not by `msgfmt`'s status. *Check:* `msgfmt -c -o /dev/null x.po; echo $?`
  on such files.

## GitHub

* **`releases/latest` never serves a pre-release or a draft; without a full release, its download
  address answers 404.** GitHub's REST reference calls the latest release the newest full release
  by `created_at`, the date of the release's commit rather than of publishing, and lets each
  publish set it (`make_latest`, default `true` for a newly published release);
  `gh release create` documents its default as automatic, by date and version, unless `--latest`
  is given. *Verified:* GitHub REST API, *Get the latest release* and `make_latest` under
  *Create a release* (<https://docs.github.com/en/rest/releases/releases>);
  <https://docs.github.com/en/repositories/releasing-projects-on-github/linking-to-releases>;
  `gh release create --help` (gh 2.89.0); on 2026-10-23, before the first release, the project's
  `releases/latest/download/meshcore-home-gateway-lxc.sh` answered 404. A repository with only
  pre-releases and a fix release for an older version line were not tested. *Here:*
  `invocation()` names a pre-release's own installer (`releases/download/vX.Y.Zrc1/...`) instead
  of the latest address; `release.yml` passes `--prerelease` for `a`, `b`, `rc` and `dev` tags
  and no `--latest`; see [deployment.md](deployment.md#update-reconfigure-remove), *Update*.
  *Check:*
  `curl -s -o /dev/null -w '%{http_code}\n' https://github.com/OWNER/REPO/releases/latest/download/FILE`
  prints 404 while there is no full release, 302 otherwise.
* **`gh attestation verify` with only the repository given accepts an attestation from any
  workflow and any ref of that repository.** `--repo OWNER/REPO` alone pins neither;
  `--signer-workflow`, `--source-ref` and `--deny-self-hosted-runners` do. See
  [ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md) and
  [deployment.md](deployment.md#3-other-linux-systems). *Verified:* `gh attestation verify --help`
  (gh 2.89.0); not run against a release, since none exists yet. *Here:* the documented command
  uses all three, and [development.md](development.md#release) asks for a repository ruleset that
  protects the `v*` tags. *Check:* `gh attestation verify --help`.
* **`actions/attest-build-provenance` v4 is only a wrapper around `actions/attest`, and
  `actions/attest` needs `artifact-metadata: write` only for registry storage records.** Its
  README lists that permission in the first usage step, but storage records are made only with
  `push-to-registry`, for container images; attesting files needs `id-token: write` and
  `attestations: write`. *Verified:* `action.yml` of `actions/attest-build-provenance` v4.2.2
  (`4d10147`: `uses: actions/attest@508db95`, v4.2.1) and its README; `README.md` of
  `actions/attest` v4.2.2 (`1e69f48`). The publish job has not run yet, since no release exists.
  *Here:* the publish job in `.github/workflows/release.yml` uses `actions/attest` with
  `contents: write`, `id-token: write` and `attestations: write`
  ([ADR 0013](decisions/0013-signed-releases-with-github-artifact-attestations.md)). *Check:*
  `curl -s https://raw.githubusercontent.com/actions/attest/v4.2.2/README.md | grep -n -i "storage record"`.
* **`github.com/OWNER/REPO/archive/REF.tar.gz` serves the source of a branch, tag or commit under
  one top-level directory; a missing release file or raw path answers 404.** *Verified:* on
  2026-10-23, `archive/main.tar.gz` of this repository held every file under
  `meshcore-home-gateway-main/`; `releases/download/v0.1.0/SHA256SUMS` (no such release) and a
  missing path on `raw.githubusercontent.com` answered 404. *Here:* for `--ref` and `main`'s
  address, `FETCH_PY` in the installer downloads the archive inside the container and extracts
  only `src/meshcore_home_gateway/`, `tools/build_pyz.py` and the unit from below the top
  directory, with `filter="data"`; a failed download stops with `cannot download URL: ...`.
  *Check:* `curl -sSfL https://github.com/OWNER/REPO/archive/main.tar.gz | tar tz | sed -n 1,3p`.

## whiptail

The installer asks its questions with whiptail, the dialog program of newt
([pagure.io/newt](https://pagure.io/newt)). Each entry was verified on 2026-10-23 with the
packages `whiptail` and `libnewt0.52` 0.52.23-1+b1 (Debian 12), 0.52.24-2ubuntu2 (Ubuntu 24.04)
and 0.52.25-1 (Debian 13), each with the `libslang2` and `libpopt0` of its distribution, all on
Ubuntu 24.04 (glibc 2.39). A script drove the real program in a pseudo terminal, called as the
installer calls it; the three versions behaved alike. Source lines are from newt's tag
`r0-52-24`; the cited lines are the same at `r0-52-23` and `r0-52-25`.

* **In a locale that cannot be set, whiptail drops every non-ASCII character typed into an input
  or password box.** whiptail calls `setlocale(LC_ALL, "")`; when any one locale variable names a
  locale the system lacks, the whole call fails and the C locale stays, in which newt accepts only
  ASCII. `pässwörd€` comes back as `psswrd` with exit status 0, and the only signs are a beep per
  dropped byte and fewer characters in the field (6 `*` for 9 typed in a password box). This
  happens without any locale variable, with `LANG=C`, with `LANG=de_DE.UTF-8` or `LC_CTYPE=UTF-8`
  on a host that lacks them (ssh may pass them on), and even with
  `LANG=C.UTF-8 LC_TIME=de_DE.UTF-8`. `LC_ALL=C.UTF-8` keeps every character, emoji included,
  whatever the other variables say; so does `LANG` or `LC_CTYPE` set to `C.UTF-8` when no other
  variable names a missing locale. The broker then refuses the login although the right password
  was typed. *Verified:* experiment, all three versions, and again with the Debian 12 and 13
  packages on their own C library (glibc 2.36-9+deb12u14 and 2.41-12+deb13u4, with the `C.utf8`
  files of their `libc-bin`); newt `whiptail.c:402` and `entry.c:441-465` (a byte from 0x80 up
  is inserted only when `mbrtowc()` accepts it). *Here:* the installer runs every dialog through
  `whiptail() { LC_ALL=C.UTF-8 command whiptail "$@"; }`; Debian 12 and 13 ship `C.UTF-8` in the
  essential package `libc-bin` (`/usr/lib/locale/C.utf8/`).
  `test_the_dialogs_run_in_a_utf8_locale` in `tests/test_installer.py` and
  `tests/manual/installer_dialogs.py` check it, and [deployment.md](deployment.md#install) says it
  in one sentence. *Check:*
  `LC_ALL=xx_XX.UTF-8 whiptail --passwordbox Password 10 50 2>pw; od -c pw`, type `pässwörd` and
  Enter: no `ä`, no `ö`. Again with `LC_ALL=C.UTF-8`: all bytes are there.
* **whiptail draws the dialog on standard output and writes the answer to standard error.** In
  `answer=$(whiptail ...)` the dialog goes into the variable (about 3 KB for a small box) instead
  of onto the screen, and the answer onto the terminal. `3>&1 1>&2 2>&3` swaps the two; whiptail's
  own error messages then land in the variable as well, so only the exit status tells an answer
  from an error. A yes/no box answers with its exit status alone: 0 for Yes, 1 for No, 255 for
  Esc. *Verified:* experiment, all three versions; newt `whiptail.c:354` (the answer goes to
  descriptor 2 unless `--output-fd` names another). *Here:* `show_dialog()` swaps the descriptors
  for every dialog that returns text and takes any exit status but 0 as cancel; yes/no and
  message boxes send their screen to standard error (`>&2`), so that a box shown inside `$(...)`
  still reaches the screen. The whiptail stub in `tests/test_installer.py` draws on standard
  output like the real one, so a missing swap corrupts the answers in the tests. *Check:*
  `x=$(whiptail --inputbox Name 10 40); echo "${#x}"`: no dialog appears; type `bob` and Enter:
  `bob` shows on the terminal, then a length in the thousands. With `3>&1 1>&2 2>&3` added,
  `echo "$x"` prints `bob`.
* **An input box starts with its default text in the field and the cursor after it, so typing
  adds to the default instead of replacing it.** Default `meshcore-home-gateway` and typed `bob`
  give `meshcore-home-gatewaybob`; Ctrl-U, or Backspace once per character, clears the field
  first. *Verified:* experiment, all three versions; newt `entry.c:107-110`. *Here:* on a new
  installation the user name field starts empty, so that a name typed onto a suggestion cannot
  become a user the broker does not know: `ask_broker()` gives the example in the dialog's text.
  After a refused login, `ask_broker_again()` names the refused user in the text and starts the
  field empty as well (`test_the_user_name_field_starts_empty_and_the_password_names_the_user`,
  `test_a_refused_login_offers_the_broker_settings_again`). Most other input boxes hold a default
  (port, topics, container settings), and with `--reconfigure` the input boxes hold the current
  settings, the user name included, which have to be cleared before typing new ones. *Check:*
  `whiptail --inputbox User 10 50 example 2>u; cat u`, type `bob` and Enter: `examplebob`.
* **`--yes-button`, `--no-button`, `--ok-button` and `--cancel-button` set the labels of every box
  kind and keep the exit status.** *Verified:* experiment, 0.52.24-2ubuntu2, in a pseudo
  terminal under `LC_ALL=C.UTF-8`: `Ja`/`Nein` on a yes/no box (Enter 0, Tab and Enter 1),
  `Weiter`/`Abbrechen` on an input box (0 with the text, 1 on the second button), and a
  13-character label on a message box, each shown. The slice's research also ran 0.52.23 and
  0.52.25 with the labels of five languages, every box kind with all four options at once, and
  saw a 31-character label run over the right border of a 40-column box. *Here:* the installer
  passes the four labels only while a language pack is loaded (English keeps whiptail's own), and
  the translator notes ask for at most 12 characters. *Check:*
  `whiptail --yes-button Ja --no-button Nein --yesno -- Text 10 40; echo $?`.
* **In a password or input box, Tab moves the focus to the buttons instead of being typed, and
  what is typed after it is lost.** Typing or pasting `ab`, Tab, `cd` and pressing Enter returns
  `ab` with exit status 0, so a password that contains a Tab is cut without a warning. A second
  Tab reaches Cancel (exit status 1), Esc gives 255; spaces, trailing ones included, are kept.
  *Verified:* experiment, all three versions; newt `form.c:664-667` (the form takes Tab before
  the field sees it). *Here:* nothing in the dialogs. A password with a Tab can be given in an
  answers file (`mqtt.password=` with `--answers FILE`,
  [deployment.md](deployment.md#unattended-installation)), which keeps it: `config render` writes
  it into the configuration unchanged. *Check:*
  `whiptail --passwordbox Password 10 50 2>pw; echo $?; cat pw`, type `ab`, Tab, `cd` and Enter:
  `0`, then `ab`.
* **whiptail takes every argument that starts with `-` for an option, also a text, a menu tag or
  a list item, unless it follows `--`.** `whiptail --msgbox '-x' 10 40` prints
  `-x: unknown option` and exits with status 1; `whiptail --msgbox -- '-x' 10 40` shows the box.
  *Verified:* experiment, all three versions, with a text, a menu tag, a menu item, a checklist
  tag and an input box's default; newt `whiptail.c:477-481` (popt's error, exit status 1).
  *Here:* every whiptail call in the installer puts `--` before its text, since channel and
  contact names read from the radio may start with `-`;
  `test_names_that_look_like_options_are_offered_safely` in `tests/test_installer.py`, whose stub
  refuses such arguments as popt does. *Check:* `whiptail --msgbox '-x' 10 40; echo $?`, then the
  same with `--` before `'-x'`.
* **A message box shows as many lines as fit its height and cuts the rest without a mark; with
  `--scrolltext` the text scrolls, but Enter no longer closes the box.** A box of height 12 shows
  6 lines, one of height 22 shows 16. With `--scrolltext` the text has the focus, also when it
  fits: Enter and space do nothing, Tab moves to Ok, and Enter then closes the box. *Verified:*
  experiment, all three versions; the text of the final message for each source, with
  0.52.24-2ubuntu2; newt `textbox.c:121-125` (a scrolling text box takes the focus). *Here:*
  `summary()` shows the final message in a box of height 22 without `--scrolltext`. The message
  fits for an installation from a release, a pre-release, `main` or a clone at a path of up to 130
  characters; with `--ref`, its commands wrap onto more lines and the last line can be cut. The
  same text is logged to standard error before the box; with an xterm-like `TERM`, whiptail draws
  on the terminal's alternate screen, so the text shows again when the box closes (with
  `TERM=linux` the box stays on the screen instead). *Check:*
  `whiptail --msgbox "$(seq 1 30)" 12 40` shows 1 to 6 and closes with Enter;
  `whiptail --scrolltext --msgbox "$(seq 1 30)" 12 40` stays open on Enter; Tab, then Enter
  closes it.

## Mosquitto

Verified with Mosquitto 2.0.18 (Ubuntu package `2.0.18-1build3`) and read in its source at tag
`v2.0.18`; where 2.1 differs, at `v2.1.2`. Paths are relative to the source root. The *Check*
steps use a private broker on port 18830, started by a normal user in an empty directory
(`mosquitto_sub` and `mosquitto_pub` are in Debian's `mosquitto-clients`). For a variant, add the
option to `m.conf` and restart: `kill $broker; mosquitto -c m.conf & broker=$!`.

```sh
mosquitto_passwd -c -b pw mhg s3cret && chmod 600 pw
printf 'allow_anonymous false\npassword_file %s/pw\nlistener 18830 127.0.0.1\n' "$PWD" > m.conf
printf 'log_dest file %s/log\nlog_type all\n' "$PWD" >> m.conf
mosquitto -c m.conf & broker=$!
```

* **Every failed login gets the same answer: CONNACK 5, "not authorised" (MQTT 5: 135).** A wrong
  password, an unknown user, no user name, a user name in other letter case or with a leading
  blank, and `allow_anonymous false` without any `password_file` (the broker starts without a
  warning) look alike to the client. Code 4, "bad user name or password", is not used for
  password-file logins; in 2.1 an MQTT 3.1.1 client gets 5 also when a plugin reports a bad user
  name or password. *Verified:* 2.0.18, with paho-mqtt 2.1.0 and `mosquitto_sub`;
  `src/handle_connect.c:931-941`; at `v2.1.2`, `src/handle_connect.c:1271-1285`. *Here:*
  `MqttBus._paho_connected()` in [`mqtt/client.py`](../src/meshcore_home_gateway/mqtt/client.py)
  logs the refusal with the user name it sent and points to `check-config --broker`, which lists
  the possible causes; the configuration refuses a user name with a blank at either end (`_mqtt()`
  in [`config.py`](../src/meshcore_home_gateway/config.py));
  [deployment.md](deployment.md#5-troubleshooting), row `MQTT: connection refused`. *Check:*
  `mosquitto_sub -p 18830 -u mhg -P wrong -t x -E` prints `Connection error: Connection Refused:
  not authorised.` and exits with 5; so do `-u nobody -P s3cret`, `-u MHG -P s3cret` and no `-u`.
* **Mosquitto reads `password_file` only when it starts and when it reloads (SIGHUP, which
  `systemctl reload mosquitto` sends).** Until then a new user is refused, and after a password
  change the old password still works and the new one does not. The man page says that a reload
  does not affect connected clients, but it disconnects those whose user was removed or whose
  password changed; the others stay connected. *Verified:* 2.0.18, with three connected clients;
  `src/loop.c:232-238` (the reload), `src/security_default.c:1252-1256` (the re-check of connected
  clients); mosquitto.conf(5), `password_file`: "Clients that are already connected will not be
  affected."; `ExecReload=/bin/kill -HUP $MAINPID` in the package's `mosquitto.service`. *Here:*
  [deployment.md](deployment.md#1-before-you-start) and its troubleshooting row say to run
  `systemctl reload mosquitto` after `mosquitto_passwd`; `check-config --broker` repeats it when
  the login is refused. *Check:* `mosquitto_passwd -b pw bob pw1`; `mosquitto_sub -p 18830 -u bob
  -P pw1 -t x -E` exits with 5; `kill -HUP $broker`, and the same command exits with 0.
* **`mosquitto_passwd -c` creates the file anew, without the users already in it**, as
  [deployment.md](deployment.md#5-troubleshooting) says. *Verified:* 2.0.18. *Check:*
  `mosquitto_passwd -c -b f a x; mosquitto_passwd -b f b y; mosquitto_passwd -c -b f c z; cut -d:
  -f1 f` prints only `c`.
* **With `per_listener_settings true`, a `password_file` above the first `listener` line belongs
  to an extra default listener.** Mosquitto then also opens port 1883 on all addresses, and the
  listener the clients use has no password file, so it refuses every login, valid ones included.
  `per_listener_settings` after any security option stops the broker: "per_listener_settings must
  be set before any other security settings.". Mosquitto 2.1 deprecates the option; 3.0 is to
  remove it. *Verified:* 2.0.18; `src/conf.c:424-454` (the default listener, also for a
  `password_file`) and `:1779-1784`; `ChangeLog.txt` at `v2.1.2`, section 2.1.0. *Here:*
  [deployment.md](deployment.md#5-troubleshooting): `password_file` goes after the `listener` line
  it belongs to. *Check:* with `per_listener_settings true`, `password_file <dir>/pw`, `listener
  18830 127.0.0.1` and `allow_anonymous false`, in this order, the log shows `Opening ipv4 listen
  socket on port 1883.`, and `-u mhg -P s3cret` exits with 5. Port 1883 must be free; otherwise
  the broker stops with `Error: Address already in use`.
* **The broker's log names the client id of a refused login, not the user, and its format changed
  in 2.1.1.** 2.0: `Client <id> disconnected, not authorised.`; 2.1.1 and later, as in the Home
  Assistant add-on 7.x: `Client <id> [<address>:<port>] disconnected: not authorised.`. The user
  name appears only for an accepted login (`New client connected from … as <id> (p2, c1, k60,
  u'<user>')`), the `Sending CONNACK` lines only with `log_type debug` or `all`. In the add-on,
  `debug: true` sets `log_type all` and also logs passwords (see below); since add-on 7.1.0 its
  `log_type` option sets `debug` without that. *Verified:* 2.0.18; `src/loop.c:330` at `v2.1.2`;
  `ChangeLog.txt`, section 2.1.1: "Fix inconsistent disconnect log message format, and add
  address:port."; the add-on's `rootfs/usr/share/tempio/mosquitto.gtpl:9-19` and `:43`,
  `CHANGELOG.md`, section 7.1.0. *Here:* the gateway's own warning names the user it sent
  (`_paho_connected()`); [deployment.md](deployment.md#5-troubleshooting) says that the broker
  names the client (`meshcore-home-gateway-radio1`). *Check:* after the refused logins above and
  one accepted (`-u mhg -P s3cret`), `grep -E "not authorised|u'" log`: the refusals name only
  client ids, and only the accepted login's line shows `u'mhg'`.
* **`clientid_prefixes` refuses a client id without the prefix with the same CONNACK 5 as a wrong
  password.** It is checked before the login, so only the log tells the two apart: the client
  appears as `<unknown>`. At start, 2.0.18 reports the option as deprecated. *Verified:* 2.0.18;
  `src/handle_connect.c:647-657`; `src/conf.c:1235`. *Here:* the service connects as
  `meshcore-home-gateway-<node_id>` unless `client_id` is set (`make_paho_client()`), the commands
  `check-config --broker`, `send` and `listen` as `meshcore-home-gateway-check-<pid>`,
  `-send-<pid>` and `-listen-<pid>` (`_mqtt_client()` in
  [`cli.py`](../src/meshcore_home_gateway/cli.py)). The prefix `meshcore-home-gateway-` admits
  both; the service's full id as the prefix locks the commands out. *Check:* add
  `clientid_prefixes meshcore-gw-`; `mosquitto_sub -p 18830 -i meshcore-home-gateway-radio1 -u mhg
  -P s3cret -t x -E` exits with 5, and the log reads `Client <unknown> disconnected, not
  authorised.`.
* **With `retain_available false`, a CONNECT whose will is retained is refused, over MQTT 3.1.1
  without any CONNACK.** The broker closes the connection; an MQTT 5 client gets CONNACK 154,
  "Retain not supported". The log shows only `Client <unknown> disconnected due to using not
  allowed feature (QoS too high, retain not supported, or bad AUTH method).`. *Verified:* 2.0.18,
  with paho-mqtt 2.1.0, which calls only `on_disconnect`; `src/handle_connect.c:572-578`.
  *Here:* the gateway's will is retained (`MqttBus.start()`); `MqttBus` notes whether the broker
  answered the attempt (`_answered`) and logs `closed the connection without answering` instead of
  a refusal ([deployment.md](deployment.md#5-troubleshooting)). *Check:* add `retain_available
  false`; `mosquitto_sub -p 18830 -u mhg -P s3cret -t x -E --will-topic t --will-payload offline
  --will-retain` prints nothing and keeps trying; with `-V mqttv5` it prints `Connection error:
  Retain not supported`.
* **With an `acl_file`, a user has only the access listed for it.** *Verified:* mosquitto.conf(5),
  `acl_file`: "only the topics listed will have access"; the three experiments below. *Here:* the
  ACL in [mqtt-api.md](mqtt-api.md#securing-the-broker) and the
  [requirements](home-assistant.md#requirements) give Home Assistant's user `readwrite` on
  `homeassistant/#`. *Check:* for the next three entries, `printf 'user mhg\ntopic read
  meshcore/#\nuser obs\ntopic read #\n' > acl`, `mosquitto_passwd -b pw obs o`, add `acl_file
  <dir>/acl` and restart.
* **Over MQTT 3.1.1, a QoS 1 PUBLISH that the ACL denies is acknowledged like any other.** Only
  MQTT 5 carries the reason (135, "Not authorized"); the broker logs `Denied PUBLISH` only with
  `log_type debug` or `all`. *Verified:* 2.0.18, with paho-mqtt 2.1.0 (`on_publish` reports
  `Success`, `is_published()` is true) and `mosquitto_pub`. *Here:* the gateway speaks MQTT 3.1.1
  (`make_paho_client()`), so `MqttBus.publish()` and `send` (`_publish()` in `cli.py`) take a denied
  message as delivered; only the broker's log shows the denial. *Check:* `mosquitto_pub -p 18830 -u
  mhg -P s3cret -q 1 -t meshcore/radio1/availability -m online` exits with 0; with `-V mqttv5` it
  prints `Warning: Publish 1 failed: Not authorized.`.
* **The `acl_file` check never refuses a SUBSCRIBE.** A subscription to a topic the user may not
  read is granted at the requested QoS; its messages are then not delivered, and nothing is
  logged. *Verified:* 2.0.18, MQTT 3.1.1 and 5; `src/security_default.c:383` ("FIXME - implement
  ACL subscription strings."). *Here:* `send --wait` checks only the SUBACK (`_subscribe()` in
  `cli.py`), so a missing read grant on `delivery` ends in `no delivery result in time` rather
  than an error. The gateway needs `read` on `homeassistant/status` to see Home Assistant's birth
  message, as in the ACL in [mqtt-api.md](mqtt-api.md#securing-the-broker). *Check:*
  `mosquitto_sub -d -p 18830 -u mhg -P s3cret -q 1 -t homeassistant/status -E` prints
  `Subscribed (mid: 1): 1`, that is, granted with QoS 1.
* **Mosquitto checks a will against the ACL only when it publishes it, not at CONNECT.** A client
  without write access to its will topic connects normally; after an abnormal disconnect its will
  is dropped without a log line, also with `log_type all`. *Verified:* 2.0.18, with a `readwrite`
  grant as the control; `will__read()` in `src/handle_connect.c:333` checks only the topic's form,
  `context__send_will()` in `src/context.c:193` checks the ACL. *Here:* the gateway's will is its
  `availability` topic, which the ACL in [mqtt-api.md](mqtt-api.md#securing-the-broker) covers
  with `readwrite meshcore/radio1/#`. Without that grant the gateway logs `MQTT: connected`, but
  Home Assistant never sees its availability change. *Check:* with `mosquitto_sub -p 18830 -u obs
  -P o -t 'meshcore/#' -v` running, start `mosquitto_sub -p 18830 -u mhg -P s3cret -t x
  --will-topic meshcore/radio1/availability --will-payload offline &` and `kill -9` it: the reader
  gets nothing, and the log shows `Will message specified` and `closed its connection`, no denial.
* **Under MQTT 3.1.1, a retained PUBLISH reaches clients that are already subscribed with the
  retain flag cleared; only the stored copy, sent on every new subscription, has it set.** A
  clean-session client subscribes anew after every reconnect and gets the stored copy each time.
  *Verified:* 2.0.18, with paho-mqtt 2.1.0, in
  `test_with_broker_a_retained_request_is_flagged_only_when_replayed`
  ([`tests/test_mqtt_client.py`](../tests/test_mqtt_client.py)); MQTT 3.1.1, section 3.3.1.3.
  *Here:* [mqtt-api.md](mqtt-api.md#sending-sendalias): a retained send request is sent once,
  and its replays are rejected with `retained`. *Check:* `.venv/bin/pytest tests/test_mqtt_client.py
  -k retained_request` (needs `mosquitto`; `make test` runs it too).
* **A password file written by `mosquitto_passwd` 2.1 loses users on a 2.0 broker.** 2.0 writes
  `$7$` entries (PBKDF2-HMAC-SHA512) with 101 iterations and a 12-byte salt, 2.1 with 1000
  iterations and a 64-byte salt. 2.0 drops any entry whose salt is not 12 bytes ("Error: Unable to
  decode password salt for user <user>, removing entry.") and starts normally, and that user then
  gets CONNACK 5. 2.1 reads both. *Verified:* 2.0.18, with an entry in the 2.1 layout;
  `src/security_default.c:899-919`; at `v2.1.2`, `libcommon/password_common.c:47`, `:207` and
  `:323`. *Here:* nothing in the code; create users with the `mosquitto_passwd` of the broker that
  reads the file. *Check:* write an entry `gw:$7$1000$<64-byte salt>$<hash>` (base64; the hash is
  PBKDF2-HMAC-SHA512 of the password, 64 bytes) into the password file of a 2.0 broker and start
  it: the log shows `removing entry`.
* **Mosquitto accepts passwords with unusual characters.** Quotes, `\`, `#`, `$`, `=`, `:`, a
  Tab, blanks at either end, non-ASCII characters and 200 characters all worked with
  `mosquitto_passwd -b` and a login, as did user names with blanks, non-ASCII characters and `@`.
  `mosquitto_passwd` refuses `:` in a user name: "Error: Username must not contain the ':'
  character.". *Verified:* 2.0.18, with paho-mqtt 2.1.0; the same password with one character
  added was refused. *Here:* nothing needed: the configuration passes the password on as written;
  the characters that went missing were lost in the dialogs ([deployment.md](deployment.md#install),
  the `C.UTF-8` locale). *Check:* `mosquitto_passwd -b pw mhg '  pä:ss#$ '`, `kill -HUP $broker`,
  then `mosquitto_sub -p 18830 -u mhg -P '  pä:ss#$ ' -t x -E` exits with 0.

## Home Assistant Mosquitto add-on

Read in the add-on's source: `home-assistant/addons` at commit `2c0a216` (2026-10-23), add-on
7.1.2, which builds Mosquitto 2.1.2 and mosquitto-go-auth 3.0.0 (`mosquitto/build.yaml:7-8`).
Paths are relative to `mosquitto/`; go-auth paths refer to `iegomez/mosquitto-go-auth` at tag
`3.0.0`. Where an entry says "image", the add-on 7.1.2 image's own `mosquitto` and go-auth were
run with a stub in place of the Supervisor; nothing was tested against a running add-on.

The add-on checks a login first against its own password file (its two system users and the
`logins` option), then asks the Supervisor, which takes Home Assistant users
(`rootfs/usr/share/tempio/mosquitto.gtpl:36`, `:46` and `:52`;
`rootfs/usr/share/tempio/nginx.gtpl:36-38`). What that means for the user name is in
[deployment.md](deployment.md#5-troubleshooting).

* **The add-on remembers the result of each login for 300 s ± 30 s, refusals included, per user
  name and password.** A client that retries with the same credentials stays refused until the
  entry expires, up to 330 s after the remembered refusal, even if the user was fixed meanwhile;
  other credentials are checked at once, and a restart of the add-on clears the memory.
  *Verified:* `rootfs/usr/share/tempio/mosquitto.gtpl:38-40` (`auth_opt_cache true`,
  `auth_opt_auth_cache_seconds 300`, `auth_opt_auth_jitter_seconds 30`, no `cache_type`, so
  go-auth keeps it in memory); go-auth `go-auth.go:318-339` stores refusals as well; image, with
  debug logging: the second login with the same refused credentials showed `found in cache` and
  CONNACK 5 without asking the stub. *Here:*
  [deployment.md](deployment.md#5-troubleshooting): restart the add-on after a fix, since the
  gateway retries with the same credentials at every reconnect. *Check:* with `debug: true`, log
  in as a user that does not exist yet, create it in Home Assistant and log in again at once: the
  add-on's log shows `found in cache`, and the login is refused.
* **`logins` are read only when the add-on starts, and a pre-hashed password must be in go-auth's
  format.** `rootfs/etc/cont-init.d/mosquitto.sh:46-63` writes them into the add-on's password
  file when the container starts, hashed by the add-on's `pw -p` as `PBKDF2$sha512$100000$…`
  (`DOCS.md`, `password_pre_hashed`). go-auth accepts only its own layouts, not a `$7$` hash from
  `mosquitto_passwd`. *Verified:* the files named; go-auth `hashing/pbkdf2.go:86`, `:113` and
  `:149`. *Here:* [deployment.md](deployment.md#5-troubleshooting): a `logins` entry is read when
  the add-on starts; the documentation does not suggest pre-hashed passwords. *Check:* read
  `rootfs/etc/cont-init.d/mosquitto.sh`.
* **With `debug: true`, the add-on's log contains MQTT passwords in recoverable form.** go-auth logs
  each cache key as `to auth record: [...]`, a list of byte values; the key is the text
  `auth-<user>-<password>` followed by a constant 20 bytes (the SHA-1 of nothing), not a hash of
  that text. `DOCS.md` warns only that "sensitive information will be logged". *Verified:*
  `rootfs/usr/share/tempio/mosquitto.gtpl:43` (`debug` sets `auth_opt_log_level debug`); go-auth
  `cache/cache.go:113-117`; image: such a line decoded to the user name and password. *Here:*
  nothing in the code; the bug report form
  ([`bug_report.yml`](../.github/ISSUE_TEMPLATE/bug_report.yml)) asks to remove passwords, which
  these lines do not resemble: ask for an add-on debug log only without its `to auth record`
  lines. *Check:* `bytes([...]).decode(errors="replace")` on the list from one such line.
* **The user names `homeassistant` and `addons` belong to the add-on.** At every start it writes
  both into its password file, with 64-character random passwords kept in
  `/data/system_user.json`, and hands `homeassistant` to Home Assistant's MQTT integration
  (`rootfs/etc/cont-init.d/mosquitto.sh:20-44`, `rootfs/etc/services.d/mosquitto/discovery:26`).
  `DOCS.md` calls the names reserved, but nothing refuses them in `logins`: such an entry is added
  as a second line for the name (lines 46-63), and go-auth keeps the last line for a name
  (`backends/files/files.go:172-174`), so it replaces the password the add-on gave that user.
  *Verified:* the source named. *Here:* the installer's dialog
  ([`meshcore-home-gateway-lxc.sh`](../deploy/proxmox/meshcore-home-gateway-lxc.sh)) and the ACL in
  [mqtt-api.md](mqtt-api.md#securing-the-broker) give `meshcore-home-gateway` as the example user
  name. *Check:* read `rootfs/etc/cont-init.d/mosquitto.sh`.
* **When the Supervisor answers the login check with an error status (500 or higher), the add-on
  closes the connection without a CONNACK and does not remember the result.** Its log shows
  `disconnected: bad socket read/write: Unknown error` instead of `not authorised`. *Verified:*
  go-auth `backends/http.go:267-272` (a status of 500 or more is an error), `go-auth.go:298-301`
  and `:329` (an error is not cached), `auth-plugin.c:103-104`; at Mosquitto `v2.1.2`,
  `src/handle_connect.c:1287-1290` (no CONNACK for that error); image, with the stub answering
  500. *Here:* the gateway logs `closed the connection without answering` rather than a refusal
  (`_answered` in `MqttBus`); [deployment.md](deployment.md#5-troubleshooting) does not list this
  cause for that message. *Check:* read `backends/http.go` at the lines named.
* **A `password_file` added through the add-on's `customize` folder shuts out Home Assistant users
  on add-on 6.5.2, but not on 7.1.2.** The add-on includes the folder above its listeners
  (`rootfs/usr/share/tempio/mosquitto.gtpl:56-60`), so the file applies to all of them. On 6.5.2
  (Mosquitto 2.0.22, `CHANGELOG.md`), Mosquitto's own password check answers before go-auth and
  refuses every user not in the file: Home Assistant users, `logins` and the add-on's own users.
  On 7.1.2 (Mosquitto 2.1.2) both kinds of user still log in beside the file's users. *Verified:*
  Mosquitto `v2.0.22`, `src/security.c:775-785` and `src/security_default.c:1044` (the same code
  as in `v2.0.18`); image, with a `customize` file: a Home Assistant user and a `logins` user were
  accepted. *Here:* [deployment.md](deployment.md#5-troubleshooting) points add-on users to a Home
  Assistant user or a `logins` entry, not to `mosquitto_passwd`. *Check:* read the two Mosquitto
  files named.

## paho-mqtt

paho-mqtt 2.1.0, the version of Debian 13's `python3-paho-mqtt` (`2.1.0-1`, packages.debian.org)
and of the pin in [`requirements/dev.in`](../requirements/dev.in), with Python 3.13 and Mosquitto
2.0.18. Line numbers refer to `paho/mqtt/client.py` 2.1.0. The gateway uses
`CallbackAPIVersion.VERSION2` and MQTT 3.1.1 (`make_paho_client()` in
[`mqtt/client.py`](../src/meshcore_home_gateway/mqtt/client.py)).

* **A refused login reaches `on_connect` as an MQTT 5 reason code: CONNACK 5 arrives as "Not
  authorized", value 135.** paho converts every MQTT 3.1.1 return code
  (`convert_connack_rc_to_reason_code()`, line 352; its docstring says to compare by name). It
  then calls `on_disconnect` with "Unspecified error" (128) and, under `loop_start()`, tries again
  after the reconnect delay: a refusal does not stop it. *Verified:* MQTT 3.1.1 and 5. *Here:*
  `MqttBus._paho_connected()` checks `reason.is_failure` and logs the reason by name, as
  `_Connection.on_connect()` in [`cli.py`](../src/meshcore_home_gateway/cli.py) does;
  `_paho_disconnected()` uses the `_answered` flag to tell a refusal from a connection that was
  closed without an answer. *Check:* connect with a wrong password and print the reason and its
  `.value` in both callbacks: `Not authorized 135`, then `Unspecified error 128`, once per
  attempt.
* **When no broker answers, paho calls only `on_connect_fail`, at every attempt and without the
  error; the error is in `sys.exception()`, because the callback runs inside paho's `except
  OSError:` block.** This covers a name that does not resolve and a closed port; `on_connect` and
  `on_disconnect` are not called. *Verified:* `gaierror(-2, 'Name or service not known')` and
  `ConnectionRefusedError(111, 'Connection refused')`; `loop_forever()`, lines 2283-2285 and
  2324-2326. *Here:* `MqttBus._paho_connect_failed()` logs `MQTT: cannot reach <host>:<port>:
  <reason>` from `sys.exception()`, or `no connection` when called outside an except block;
  [`tests/test_mqtt_client.py`](../tests/test_mqtt_client.py) covers both. This relies on paho's
  internals: check it again after a paho upgrade. *Check:* set `c.on_connect_fail = lambda c, u:
  print(repr(sys.exception()))`, then `c.connect_async("127.0.0.1", 1)` and `c.loop_start()`: it
  prints `ConnectionRefusedError(111, 'Connection refused')`.
* **A host name with an empty label or a label longer than 63 characters ends paho's network
  thread without any callback.** `socket.getaddrinfo()` raises `UnicodeEncodeError` ("label
  empty", "label too long"), which is not an `OSError`; paho does not catch it and never tries
  again. *Verified:* with `broker..example.org` and a 64-character label. *Here:* the
  configuration refuses such a host, since `_host_problem()` in
  [`config.py`](../src/meshcore_home_gateway/config.py) requires `host.encode("idna")` to succeed;
  `_Connection.open()` in `cli.py` also catches `UnicodeError`. *Check:* `python3 -c 'import
  socket; socket.getaddrinfo("broker..example.org", 1883)'` raises `UnicodeEncodeError`.
* **An exception in a callback such as `on_message` ends paho's network thread; the client then
  receives nothing more while the program goes on.** `suppress_exceptions` is false by default
  (line 877). *Verified:* the second message after a raising `on_message` was never handled, and
  the thread had ended. *Here:* the commands' callbacks catch what a payload can raise
  (`_DeliveryWatch.on_message()` and the `on_message` of `listen` in `cli.py`); `MqttBus` does not
  parse payloads in paho's thread, it hands them to the event loop (`_call()`). *Check:* raise in
  `on_message` under `loop_start()`: paho prints `Exception in thread paho-mqtt-client-…` and
  handles no further message.
* **`publish()` of a QoS 1 message without a connection returns `MQTT_ERR_NO_CONN` (4), but keeps
  the message and sends it after the connection is made.** *Verified:* a message published before
  `connect()` reached a subscriber; lines 1804-1810. *Here:* `MqttBus.publish()` warns only for
  other codes. *Check:* `python3 -c 'import paho.mqtt.client as m; c =
  m.Client(m.CallbackAPIVersion.VERSION2); print(c.publish("t", b"x", qos=1).rc)'` prints `4`.
* **`max_queued_messages_set()` counts the unacknowledged backlog, and at a reconnect `on_connect`
  runs before the backlog is sent again.** With a full queue, what `on_connect` publishes
  (`online`, discovery, state) fails with `MQTT_ERR_QUEUE_SIZE` (15) and is lost. *Verified:* with
  a limit of 3 and three messages queued, a publish from `on_connect` got 15; lines 1788-1790, and
  `_handle_connack()` calls `on_connect` (line 3934) before it sends the backlog (from line 3949).
  *Here:* `MqttBus.start()` sets `max_queued_messages_set(0)`; `MqttBus.publish()` bounds the
  events itself (`MAX_QUEUED_EVENTS`, 1000) and skips retained documents while disconnected,
  since the reconnect publishes them again. *Check:* the same steps with
  `max_queued_messages_set(3)`.
* **paho notices a broker that stops answering only through keepalive, after up to two keepalive
  intervals; then it sends unacknowledged QoS 1 messages again with the DUP flag, also with a clean
  session.** QoS 0 messages written to the silent connection are lost. *Verified:* Mosquitto
  stopped with `SIGSTOP`, keepalive 2 s: `on_disconnect` with "Keep alive timeout" after 4 s;
  after `SIGCONT` the broker logged `Received PUBLISH from … (d1, q1, …)`, and the QoS 0 message
  never arrived; `_check_keepalive()` (line 3261) and `_messages_reconnect_reset_out()` (lines
  3712-3724). *Here:* command events go out with QoS 0 (`Bridge._on_message()` in
  [`bridge.py`](../src/meshcore_home_gateway/bridge.py)), so a command is never delivered late;
  [mqtt-api.md](mqtt-api.md#commands-command), reason `broker_unavailable`. *Check:* `kill -STOP`
  the broker while a client with keepalive 2 publishes QoS 0 and QoS 1 messages, wait 5 s, then
  `kill -CONT`.
* **paho reads a whole packet into memory before any callback sees it**, so one large PUBLISH
  costs its full size in memory; the advice is in [mqtt-api.md](mqtt-api.md#securing-the-broker)
  (`max_packet_size`). *Verified:* `_packet_read()`, lines 3120-3134.

## Home Assistant MQTT

The contract tests ([testing.md](testing.md#home-assistant-contract-tests)) run the release pinned
in [`home-assistant/requirements.in`](../home-assistant/requirements.in): Home Assistant
2026.10.0, through `pytest-homeassistant-custom-component` 0.13.371. A *probe* is a throwaway test
in the same environment, built like the contract tests. File and line refer to `homeassistant/` in
the `home-assistant/core` repository at the named tag (for 2026.10.0 also in
`.venv-homeassistant/lib/python3.14/site-packages/`); `src` fetches such a file:

```sh
src() { curl -s "https://raw.githubusercontent.com/home-assistant/core/$1/homeassistant/$2"; }
```

* **Home Assistant validates a device discovery document as a whole: without `o` (origin), with a
  device that has neither `identifiers` nor `connections`, with a component that has options but
  no `unique_id`, or with an unknown top-level key, `~` included, it creates none of the
  document's entities.** It logs `Invalid MQTT device discovery payload for <id>, <error>:
  '<payload>'` and keeps what an earlier document created. An invalid value in one component, such
  as a unit that does not fit the device class, drops only that component; an unknown key inside a
  component is ignored without a message. *Verified:* 2026.10.0, probe: each of the five faults
  left 0 of 13 entities; a wrong unit on one sensor left 12; an unknown key in a component left 13
  and no warning; an invalid document after a valid one left the 13 in place. Source:
  `components/mqtt/schemas.py` lines 199-227 (`check_unique_id`, `DEVICE_DISCOVERY_SCHEMA`),
  `components/mqtt/discovery.py` lines 304-312. *Here:* `discovery_payload()` in
  `src/meshcore_home_gateway/mqtt/discovery.py` always sends `o`, and a `unique_id` and
  `default_entity_id` for every component it configures; a removal is a bare `{"p": ...}`, which
  needs neither ([ADR 0010](decisions/0010-home-assistant-through-mqtt-device-discovery.md)).
  `test_discovery_creates_the_documented_entities` runs the generated document through the
  integration and fails on any warning. *Check:* delete `o` from the document in that test and run
  `make ha-test`.
* **A JSON `null` in any device field rejects the whole document; a field left out is accepted.**
  Device fields must be strings: `string value is None at 'device.model'`. *Verified:* 2026.10.0,
  `test_a_null_device_field_would_reject_the_whole_document` (`mdl`); probe: `null` in `mf`,
  `name`, `mdl`, `sw`, `hw`, `sn`, `sa` or `mdl_id` left 0 of 13 entities, each with that warning
  for its field. *Here:* `discovery_payload()` adds model, firmware and key only once they are
  known ([design.md, 6.7](design.md#67-home-assistant)). *Check:* `make ha-test`.
* **Deleting one entity of a device-discovered device in Home Assistant removes all the device's
  entities, until the gateway announces again.** Home Assistant publishes an empty retained
  payload on the entity's discovery topic, which for device discovery is the whole device. The
  broker sends it back, because Home Assistant subscribes without MQTT 5 *No Local*, and Home
  Assistant applies it. Deleting an entity that Home Assistant has not discovered since its start
  publishes nothing. *Verified:* 2026.10.0, probe: deleting the battery sensor published an empty
  payload, retained, QoS 0, on `homeassistant/device/meshcore_home_gateway_radio1/config`; fed
  back, it left 0 of 13 entities, and the next announcement restored all 13; deleting a stale
  registry entry published nothing. Source: `components/mqtt/entity.py` lines 758-778;
  `components/mqtt/` sets no subscribe options. Home Assistant's development branch (read on
  2026-10-23) clears a device topic only when the device goes as well
  (`async_remove_entity_discovery_payload`); 2026.10.3, the latest release, does not have it.
  *Here:* the gateway removes old components itself, and
  [home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves) says to stop
  the gateway before deleting the device. Its hint to delete a leftover entity is safe for an
  entity Home Assistant has not discovered since its start; after deleting any other, restart the
  gateway to get the device's entities back. *Check:* in a contract test,
  `er.async_get(hass).async_remove()` one entity and print `mqtt.async_publish.call_args_list`.
* **A component left out of the next document stays; Home Assistant removes it only when the
  document lists it as `{"p": <platform>}`. An empty payload on the device topic removes every
  component of the device.** *Verified:* 2026.10.0, probe: a notify component left out was still
  registered and was gone after `{"p": "notify"}`; an empty payload then removed the remaining 12
  entities; `test_a_component_of_an_earlier_configuration_is_removed`. *Here:*
  `discovery_payload()` sends the components of an earlier configuration (`removed`) as
  `{"p": ...}`; the gateway never publishes an empty payload. *Check:* announce a document,
  announce it again without one component, and list the entities.
* **A bare `{"p": <platform>}` removes only a component that Home Assistant has discovered since
  its own start.** Stated, with the gateway's placeholder for this case, in
  [design.md, 6.7](design.md#67-home-assistant);
  [home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves) describes
  the effect. Verified with 2026.10.0 by
  `test_a_component_is_removed_also_after_a_restart_of_home_assistant`.
* **The documented setup needs Home Assistant 2026.8, for the five reasons listed in
  [home-assistant.md, Requirements](home-assistant.md#requirements).** One of them fails silently:
  up to 2026.5.1 a device-level `qos` is accepted but not passed to the components, which use
  QoS 0. *Verified:* source at the tags; the contract tests run only the pinned release.
  `components/mqtt/schemas.py` has `DEVICE_DISCOVERY_SCHEMA` from 2024.11.0 (not at 2024.10.0),
  `CONF_DEFAULT_ENTITY_ID` from 2025.10.0 (not at 2025.9.4), and `CONF_QOS` in `SHARED_OPTIONS`
  from 2026.5.2 (2026.5.1 accepts it only at device level, line 206). `components/event/trigger.py`
  exists from 2026.4.0; at 2026.6.0 and 2026.6.4 `components/automation/__init__.py` puts `event`
  among the triggers behind the Labs flag `new_triggers_conditions` (line 171); at 2026.7.0 the flag
  is gone. `components/event/__init__.py` moves each event at least 1 ms past the previous one from
  2026.8.0 (line 173); 2026.7.4 takes plain `utcnow()` (line 164). *Here:* the floor in
  home-assistant.md and [ADR 0010](decisions/0010-home-assistant-through-mqtt-device-discovery.md);
  `discovery_payload()` sets `"qos": 1` once at device level, and
  `test_the_actions_of_the_first_steps_send_a_message` sees QoS 1 on the notify publish. *Check:*
  `src 2026.5.1 components/mqtt/schemas.py | sed -n '/^SHARED_OPTIONS/,/^]/p'`, then the same with
  `2026.5.2`.
* **Home Assistant subscribes to device discovery at QoS 0 by default, so a discovery document can
  be lost on the way.** The topics are `<prefix>/device/+/config` and `<prefix>/device/+/+/config`;
  the QoS is an option of the MQTT integration from 2026.6.0 and fixed at 0 before. IDs in the
  topic must match `[a-zA-Z0-9_-]`. *Verified:* 2026.10.0, source `components/mqtt/discovery.py`
  lines 60-63 and 605-630; at 2026.5.4, the last 2026.5 release, line 608 is a literal `0`.
  *Here:* the document is not retained and is sent again on every broker connection and after
  each birth message
  ([home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves)); node IDs
  follow `ID_PATTERN` in `src/meshcore_home_gateway/config.py`, a subset. *Check:*
  `src 2026.10.0 components/mqtt/discovery.py | sed -n 605p`.
* **Home Assistant's birth message is `online` on `homeassistant/status`, QoS 0, not retained, and
  its topic does not follow a changed discovery prefix.** Home Assistant's documentation asks a
  publisher to send discovery again when it sees the birth message, after a random delay; after a
  restart of Home Assistant the entities stay unavailable until a document arrives. *Verified:*
  2026.10.0, source `components/mqtt/const.py` lines 281-282 and 362-367 (`DEFAULT_BIRTH`); prefix
  and birth message are separate options (`components/mqtt/config_flow.py` lines 4500-4515);
  <https://www.home-assistant.io/integrations/mqtt/#birth-and-last-will-messages> (read on
  2026-10-23). *Here:* `homeassistant.status_topic` is a setting of its own, default
  `homeassistant/status` ([home-assistant.md, Requirements](home-assistant.md#requirements)); the
  gateway announces a random 0.5–2 s after `online` (`DISCOVERY_DELAY_S` in
  `src/meshcore_home_gateway/bridge.py`). *Check:*
  `src 2026.10.0 components/mqtt/const.py | sed -n 281,282p`.
* **A later document with changed device fields updates the device page in place and changes no
  entity state; device fields it leaves out keep their stored values.** *Verified:* 2026.10.0,
  `test_the_device_page_shows_the_radio_once_it_is_known`. *Here:* the gateway announces again when
  the radio's model, firmware or key first becomes known or changes (`Bridge` in
  `src/meshcore_home_gateway/bridge.py`;
  [home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves)). *Check:*
  `make ha-test`.
* **A known entity that comes with another name keeps its entity ID and the name a user gave it:
  Home Assistant updates only its original name.** *Verified:* 2026.10.0,
  `helpers/entity_registry.py` lines 1520-1577 (`async_get_or_create` of a known unique ID calls
  `_async_update_entity` with `original_name`, never the entity ID or `name`);
  `test_a_language_change_renames_only_display_names` in `home-assistant/tests/test_languages.py`
  announces a gateway again in another language, after a user renamed an entity and the device.
  *Here:* a change of `[homeassistant] language` renames the
  display names at the next start ([home-assistant.md](home-assistant.md#language-of-the-names)).
  *Check:* `make ha-test`.
* **`default_entity_id` sets the entity ID only when Home Assistant registers the unique ID for the
  first time, or again after the entity was deleted; a changed value does not rename an existing
  entity, and an entity ID set by the user stays.** Home Assistant keeps the part after the first
  `.` and takes the domain from the platform. `object_id`, its predecessor, is ignored from
  2026.4.0. *Verified:* 2026.10.0, probe: a changed `default_entity_id` kept
  `sensor.meshcore_home_gateway_radio1_battery`, a user's new entity ID survived the next
  announcement, and after the entity was deleted the next announcement brought the default back;
  an `object_id` in place of `default_entity_id` was ignored without a message (the entity ID came
  from the device and entity names); source `components/mqtt/entity.py` lines 1469-1487;
  `CONF_OBJECT_ID` is in `components/mqtt/schemas.py` at 2026.3.4 and gone at 2026.4.0. *Here:*
  `discovery_payload()` derives it from the unique ID; a new pattern there would reach new
  installations only. *Check:* change an entity's ID in Home Assistant, restart the gateway, and
  see that the new ID stays.
* **Home Assistant builds entity IDs with `slugify`: `-` becomes `_`, runs of `-` and `_` collapse,
  a trailing one goes, and an ID already taken gets `_2`.** Stated in
  [home-assistant.md, Entities](home-assistant.md#entities); verified with 2026.10.0 by
  `test_a_hyphen_in_the_node_id_becomes_an_underscore_only_in_the_entity_ids`;
  `_MERGED_BY_HOME_ASSISTANT` in `src/meshcore_home_gateway/config.py` refuses node IDs and aliases
  that would collapse. *Check:* `slugify('a--b_')` from `homeassistant.util` returns `a_b`.
* **The MQTT event entity ignores every message that arrives with the retain flag.** Stated in
  [home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves); verified
  with 2026.10.0 by `test_retained_events_are_ignored`; source `components/mqtt/event.py` lines
  118-125.
* **An event payload is a JSON object whose `event_type` is one of the component's `event_types`;
  every other key, nested objects included, becomes an attribute.** Stated in
  [home-assistant.md, What reaches Home Assistant](home-assistant.md#what-reaches-home-assistant);
  verified with 2026.10.0 by `test_events_carry_every_payload_key_as_attributes` (flat keys) and a
  probe (the nested `command` object of a received message became a dictionary attribute); source
  `components/mqtt/event.py` lines 144-146. An undeclared `event_type` is dropped with a warning
  (probe, 2026.10.0; lines 175-185); `discovery_payload()` takes the event types from
  `DeliveryState` and `commands.words`.
* **Automations on an event entity need the `event.received` trigger: a state trigger that skips
  `unknown` misses the entity's first event, and a trigger on the attribute `event_type` misses the
  second of two equal events.** Stated in [home-assistant.md, Examples](home-assistant.md#examples);
  verified with 2026.10.0 by `test_the_documented_automations_work` (the cases that must fire) and
  a probe: after two equal events `event.received` had fired twice, a state trigger with
  `not_from: [unavailable, unknown]` once, an attribute trigger once.
* **The new entity triggers, such as `event.received`, reach through a device, area or floor target
  only the entities without an `entity_category`.** *Verified:* 2026.10.0, source
  `helpers/trigger/entity_trigger.py` lines 143-145 (`_primary_entities_only = True`); probe: an
  `event.received` trigger with the gateway's device as target fired for both of two command
  events. *Here:* in `discovery_payload()` only the sensors and the radio link are `diagnostic`;
  the event and notify entities have no category, so an automation that targets the gateway's
  device reaches them. *Check:* `src 2026.10.0 helpers/trigger/entity_trigger.py | sed -n 143,145p`.
* **While an entity is unavailable, its state drops the state and extra attributes: an event
  entity's `event_type` and payload attributes are hidden until the gateway is online again.**
  Capability attributes such as `event_types`, and the name, stay. *Verified:* 2026.10.0, probe:
  the message entity showed only `event_types` and `friendly_name` while offline and all
  attributes again after `online`; source `helpers/entity.py` lines 1124-1130. *Here:* nothing
  needed; the Markdown card of [First steps](home-assistant.md#first-steps) shows "none yet"
  meanwhile. *Check:* receive a message, publish `offline` on the availability topic, and look at
  the entity in *Developer tools → States*.
* **Event attributes, message texts and sender keys included, are recorded, and the last event of
  each event entity is kept as restore data in `.storage/core.restore_state`.** The event platform
  keeps only `event_types` out of the recorder. *Verified:* 2026.10.0, source
  `components/event/__init__.py` lines 115-120 and 239-244; probe: the restore data of the message
  entity held the text, sender key and sender name of the last message. *Here:*
  [home-assistant.md, Privacy](home-assistant.md#privacy) shows the recorder exclude; it does not
  cover `core.restore_state`. *Check:* receive a message, stop Home Assistant, and search
  `.storage/core.restore_state` for its text.
* **The MQTT notify entity publishes only the `message` of `notify.send_message`, with the entity's
  QoS and without retain; `title` is dropped.** Stated in
  [home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves); verified
  with 2026.10.0 by `test_the_actions_of_the_first_steps_send_a_message` (topic, text, QoS 1),
  `test_the_documented_automations_work` (not retained) and a probe (a call with `title` published
  only the message); source `components/mqtt/notify.py` lines 85-88. `command_topic` is required
  (line 30), although <https://www.home-assistant.io/integrations/notify.mqtt/> lists it as
  optional (read on 2026-10-23); `_REQUIRED` in `src/meshcore_home_gateway/mqtt/discovery.py` gives
  the placeholders one.
* **From 2026.10.0, `mqtt.publish` and `mqtt.dump` need an administrator when a user starts them;
  `notify.send_message` does not.** Stated for `mqtt.publish` in
  [home-assistant.md, How discovery behaves](home-assistant.md#how-discovery-behaves); source
  `components/mqtt/__init__.py` lines 373 and 398 (`async_register_admin_service` at 2026.10.0,
  `hass.services.async_register` at 2026.9.4) and `components/notify/services.py` line 36 (a plain
  entity service); no contract test.
* **A device discovery topic may have a node ID level, `<prefix>/device/<node_id>/<object_id>/config`,
  and works like one without; one subscription with `+` for the object ID sees all devices below
  that node ID, and does not match the one-level topic.** *Verified:* 2026.10.0,
  `test_every_contact_gets_a_device_below_the_gateway` and the removal tests in
  [`home-assistant/tests/test_contact_devices.py`](../home-assistant/tests/test_contact_devices.py);
  source `components/mqtt/discovery.py` lines 60-63 (`TOPIC_MATCHER`). Home Assistant's own guide
  calls leaving out the node ID best practice; the gateway deviates on purpose. *Here:*
  `contact_discovery_topic()` in `src/meshcore_home_gateway/mqtt/contact_devices.py`, one device
  per contact ([home-assistant.md, Contacts](home-assistant.md#contacts)). *Check:* `make ha-test`.
* **An empty document on a device topic removes only what Home Assistant discovered since its own
  start; for anything else it warns `No device components to cleanup` and does nothing. Each
  component sent first as a placeholder under its `unique_id` binds what it kept, and the empty
  document then removes the device. Sent again for a device Home Assistant has already removed,
  the placeholders bring it back for a moment, and it logs `Unable to remove unknown dispatcher`
  per entity.** *Verified:* 2026.10.0, `test_the_removal_works_after_a_restart_of_home_assistant`
  (a reload of the MQTT entry forgets what was discovered, as a restart does) and
  `test_a_removal_sent_again_after_home_assistant_started_leaves_nothing`; source
  `components/mqtt/discovery.py` lines 244-295. *Here:* `ContactDevices._send_removal()` always
  sends the placeholders first, and sends a removal once more only after a birth message when
  Home Assistant may not have been running. *Check:* drop the placeholder `publish` in
  `_send_removal()` and run `make ha-test`.
* **When Home Assistant removes a disabled entity of a device-discovered device, it publishes an
  empty retained message on the device's discovery topic itself, once per disabled entity; the
  broker hands it back, and Home Assistant removes every entity of the device (the further copies
  warn `No device components to cleanup`).** This also happens when the gateway removes a single
  disabled component: switching the rx log off removed the gateway's whole device. A disabled
  placeholder that binds nothing creates a disabled entity and has the same effect. *Verified:*
  2026.10.0, `test_a_disabled_component_goes_without_losing_the_device_for_long` and
  `test_an_entity_the_user_disabled_never_hides_the_contact`; a probe with two contact entities
  disabled by default gave two such publishes; without disabled entities, none. Source:
  `components/mqtt/entity.py` lines 758-777 (`async_clear_discovery_topic_if_entity_removed`).
  *Here:* placeholders are enabled; contact devices have no entity disabled by default and never
  lose a single component; the gateway watches its own device's topic and announces again when it
  is cleared (`Bridge._ha_device_message()`); an empty contact document it did not send within
  `ECHO_S` of a removal of its own is a deletion by the user
  ([home-assistant.md, Contacts](home-assistant.md#contacts)). *Check:* put
  `"enabled_by_default": False` back into `placeholder()` and run `make ha-test`.
* **Deleting a device in Home Assistant publishes one empty retained message, QoS 0, on its
  discovery topic; a re-announced device gets its old device ID, name, area and entity settings
  back, except entity IDs the user renamed, which return to `default_entity_id`.** A live
  subscriber gets the message without the retain flag, and the broker stores nothing, so only a
  client connected at that moment notices. *Verified:* 2026.10.0,
  `test_a_device_the_user_deletes_stays_hidden_until_shown` (WebSocket command
  `config/device_registry/remove`; the older `config/device_registry/remove_config_entry` logs that
  it goes in 2027.9); source `components/mqtt/entity.py` lines 758-767 and 1461-1487. *Here:*
  `ContactDevices.discovery_message()` takes an empty document it did not send as the user hiding
  the contact. *Check:* `make ha-test`.
* **`default_entity_id` gives the entity ID only if it is free; otherwise Home Assistant appends
  `_2`.** *Verified:* 2026.10.0, `test_equal_names_on_two_gateways_get_numbered_entity_ids`; source
  `components/mqtt/entity.py` lines 1468-1475 (`async_generate_entity_id`). *Here:* the gateway
  makes equal contact names distinct itself (four digits of the key), which keeps the IDs
  independent of the order of discovery. *Check:* `make ha-test`.
* **The map shows every entity whose attributes have numeric `latitude` and `longitude`, of any
  domain; a tracker's `gps_accuracy` widens the zone test, so a large one puts it `home`.** A
  tracker whose attributes are `{}` keeps its last position; `null` coordinates reset it with a
  warning. A component's own `availability` list replaces the device's. *Verified:* 2026.10.0,
  `test_the_contact_document_fills_its_entities` (only the tracker and `zone.home` have
  coordinates) and `test_a_tracker_without_a_position_leaves_the_map`; source
  `components/mqtt/device_tracker.py` lines 160-235, `components/device_tracker/entity.py` lines
  386-387, frontend `src/panels/lovelace/cards/hui-map-card.ts` (`show_all`). *Here:* only the
  contact trackers carry coordinates; their availability adds the condition that a position is
  known. A position from telemetry carries `gps_accuracy` 15, so a companion up to 15 m outside a
  zone's radius is in it (`test_a_position_from_telemetry_carries_its_accuracy`: 110 m from a
  100 m `zone.home` is `home`); an advert's carries none (0). A `gps_accuracy` that is not a number
  counts as 0 with a warning (`components/mqtt/device_tracker.py` lines 194-214), so the template
  sets it only where the document has a number. *Check:* `make ha-test`.
* **An MQTT tracker writes its state only when its attributes as rendered change.** The gateway's
  `position_at` is when the position last changed, not when it was last received, so a companion
  at rest writes no state (and no recorder row) per telemetry answer. *Verified:* 2026.10.0,
  `test_a_position_from_telemetry_carries_its_accuracy` (an answer at rest leaves `last_updated`);
  source `components/mqtt/entity.py` lines 532-542 and 1708-1735 (the attributes the message
  callback compares before it writes).
  *Check:* `make ha-test`.
* **For each new subscription Home Assistant subscribes to the topic again, so the broker sends
  its retained message again; each subscription takes only the first retained message.**
  *Verified:* source `components/mqtt/client.py` lines 949-962 (queued subscriptions) and
  1350-1356. *Here:* the entities of a contact device announced later get the retained
  availability; the tests deliver it again after a new device (`online()` in
  `home-assistant/tests/test_contact_devices.py`). *Check:*
  `src 2026.10.0 components/mqtt/client.py | sed -n 949,962p`.
* **A sensor's unit must be one of its device class's units, or that component is dropped; a
  template result `None`, as from a JSON `null`, makes the sensor `unknown`.** *Verified:*
  2026.10.0, probe: unit `volt` on the battery sensor left 12 of 13 entities and logged that the
  unit is not valid together with device class `voltage`; source `components/mqtt/sensor.py`
  lines 148-154 and 317; `test_the_state_document_fills_sensors_and_attributes` (battery `unknown`
  for `null`). *Here:* `discovery_payload()` uses V, s, dBm and dB; the state document sends `null`
  for values not known yet ([mqtt-api.md](mqtt-api.md)). *Check:* change a unit in
  `src/meshcore_home_gateway/mqtt/discovery.py` and run `make ha-test`.
* **Home Assistant hands each subscription only the first retained message of a topic and skips
  later ones that still carry the retain flag.** A broker forwards live messages without the flag
  (Home Assistant does not ask for *Retain As Published*), so this matters for tests. *Verified:*
  2026.10.0, probe: a retained `offline` after a retained `online` left the radio link `on`, a live
  `offline` made it `unavailable`; source `components/mqtt/client.py` lines 1350-1356. *Here:*
  `fire()` in [`home-assistant/tests/test_contract.py`](../home-assistant/tests/test_contract.py)
  delivers a message as the broker would, and the tests set `retain` only for the first message
  after the subscription; after a device is announced again, its document is sent live first, as
  the observer does. *Check:* `src 2026.10.0 components/mqtt/client.py | sed -n 1350,1356p`.
* **Home Assistant accepts a discovery prefix of several topic levels; the gateway accepts one.**
  *Verified:* 2026.10.0, source `components/mqtt/config_flow.py` lines 4500-4505 (the prefix is
  checked as a publish topic). *Here:* a known limitation: `_homeassistant()` in
  `src/meshcore_home_gateway/config.py` refuses a `homeassistant.prefix` with `/` (`a single topic
  segment`). *Check:* set `prefix = "a/b"` under `[homeassistant]` and run
  `meshcore-home-gateway check-config --config <file>`.
* **Home Assistant moves its own broker connection to MQTT 5 when the broker accepts it; otherwise
  it raises the repair issue `protocol_5_migration`, which names 2027.1.0 as the release that
  breaks.** *Verified:* source, `components/mqtt/__init__.py` lines 505-545 at 2026.10.0; absent at
  2026.5.4, the last 2026.5 release, present at 2026.6.0. *Here:* nothing needed: the gateway
  connects with MQTT 3.1.1 (`MQTTProtocolVersion.MQTTv311` in
  `src/meshcore_home_gateway/mqtt/client.py`), and Mosquitto 2.0.18 accepted a 3.1.1 and an MQTT 5
  client on the same listener. *Check:*
  `src 2026.10.0 components/mqtt/__init__.py | grep -n breaks_in_ha_version`.
* **From 2026.10.0 the MQTT schemas use `probatio` instead of `voluptuous`.** Validation errors in
  the log come from it (`probatio.Invalid`). *Verified:* `components/mqtt/schemas.py` uses it at
  2026.10.0 and not at 2026.9.0 or 2026.9.4; `home-assistant/requirements.txt` pins
  `probatio==0.13.0`. *Here:* nothing needed; the contract tests compare entities and states, not
  error texts. *Check:* `src 2026.9.4 components/mqtt/schemas.py | grep -c probatio` prints 0.
* **Home Assistant 2026.10.0 requires Python 3.14.2 or newer, and
  `pytest-homeassistant-custom-component` pins one Home Assistant release.** Hence the separate
  environment and CI job ([testing.md](testing.md#home-assistant-contract-tests));
  `Requires-Python: >=3.14.2` in the installed package's metadata, and the metadata of 0.13.371
  requires `homeassistant==2026.10.0`.
* **Home Assistant installs missing integration requirements from PyPI at runtime, with
  `uv pip install`.** One of the reasons for a standalone bridge
  ([ADR 0002](decisions/0002-standalone-bridge-instead-of-home-assistant-integration.md));
  source `util/package.py` lines 156-161 at 2026.10.0.

* **A select's options exist only in its discovery configuration; announced again with other
  options, the select keeps its entity and shows the new ones. A state that renders `None` sets it
  to *unknown*.** *Verified:* 2026.10.0, `test_new_options_keep_the_entity` and
  `test_a_choice_publishes_the_label_and_the_select_stays_unchosen` in
  [`home-assistant/tests/test_manage_contacts.py`](../home-assistant/tests/test_manage_contacts.py)
  (the registry entry keeps its ID; a choice publishes the option, QoS 1, not retained); source
  `components/mqtt/select.py` line 126 (`payload.lower() == "none"`). *Here:* the controls that
  add contacts have a discovery document of their own (`src/meshcore_home_gateway/mqtt/manage_entities.py`),
  so changing options does not send the gateway's main document again; `value_template: "None"`
  keeps the select unchosen, so the same node can be picked again. *Check:* `make ha-test`.
* **Home Assistant warns when an entity changes its capabilities (a select's options) more than
  100 times within an hour, and it updates the entity registry, which it saves to disk, for every
  change.** The warning asks for a bug report against Home Assistant's MQTT integration.
  *Verified:* 2026.10.0, source `helpers/entity.py` line 84 (`CAPABILITIES_UPDATE_LIMIT = 100`)
  and lines 1227-1265 (a rolling 3600 s window, the warning once per entity, then
  `async_update_entity`); `test_more_than_100_option_changes_in_an_hour_make_home_assistant_warn`
  in [`home-assistant/tests/test_manage_contacts.py`](../home-assistant/tests/test_manage_contacts.py).
  Home Assistant keeps the count on the entity, which outlives the gateway's announcements and
  restarts. *Here:* the select's options change at most 10 times at once, then once a minute,
  70 times in any hour, also when a node in range renames itself with every advert; an
  announcement without a token for a change repeats the options sent before, and a start of the
  gateway begins with the 2 changes it needs, so each start adds at most 2 (Home Assistant warns
  only if the gateway starts more than about 15 times in an hour while a node renames itself)
  (`OPTIONS_PER_S`, `OPTIONS_BURST`,
  `OPTIONS_AT_START` in `src/meshcore_home_gateway/mqtt/manage_entities.py`,
  `test_a_node_that_renames_itself_cannot_flood_home_assistant`). *Check:* `make ha-test`.
* **A text entity's state must fit its own length limits and pattern, or Home Assistant raises
  `ValueError` writing it; `text.set_value` refuses a value that does not fit with `ValueError`
  before anything is published. Both use `re.match`, so a `$` also matches before a final line
  break.** *Verified:* 2026.10.0, source `components/text/__init__.py` lines 130-152 (state) and
  `components/text/services.py` lines 16-34 (the action);
  `test_other_text_is_refused_before_it_is_sent` (a contact card, a short key, 300 characters).
  *Here:* the pattern of *Add contact (key or link)* allows the empty state with `^$`, keys and
  contact links only, with spaces around them as a copied key often has, and at most 255
  characters; the gateway strips the text it gets, so spaces and a final line break do no harm.
  The frontend uses the same pattern as an HTML `pattern`, which compiles it with the `v` flag;
  it is valid there. *Check:* `make ha-test`.
* **A template that prints, tests or iterates a key that `value_json` lacks logs a warning; `is
  defined`, `is true`, `is false`, `is not false`, `is number`, `is string` and `.get()` log
  nothing.** *Verified:* 2026.10.0, source `helpers/template/__init__.py` lines 732-762
  (`LoggingUndefined`: `__str__`, `__iter__`, `__bool__`), which these tests never call (probe
  with Jinja 3.1.6: none of them on a missing key logged); the contract tests feed documents
  without the keys added with contact management and with telemetry and assert no warning
  (`test_the_controls_need_the_radio_and_management`,
  `test_the_remove_button_is_available_while_a_request_would_be_taken`,
  `test_a_look_alike_node_is_a_problem`, `test_the_qr_card_shows_the_gateways_contact_link`,
  `test_older_documents_and_the_clearing_message_log_nothing`). The empty message that clears a
  contact's document reaches its entities while they still exist.
  *Here:* templates that read a key newer than the first documents guard it (`is defined`, `is
  true`, `is false`, `is number`, `is string`, `.get()`), so a document that an earlier version
  left retained reads as `offline` or `off`; every contact template asks `value_json is defined`,
  which also covers a payload that is no JSON, such as the empty message that clears a contact's
  document. *Check:* drop `is defined` from the `look_alike` value
  template in `src/meshcore_home_gateway/mqtt/discovery.py` and run `make ha-test`
  (`test_a_look_alike_node_is_a_problem` fails).
* **Deleting a device in Home Assistant clears every discovery topic of it: the gateway's main
  document and the one of its controls.** *Verified:* 2026.10.0,
  `test_deleting_the_gateway_device_clears_the_controls_topic_too` (an empty retained payload, QoS
  0, on both topics). *Here:* the gateway watches both topics and announces again; with
  `contact_devices = 0` it subscribes the controls' topic by itself (`_serve()` in
  `src/meshcore_home_gateway/cli.py`). *Check:* `make ha-test`.

* **An MQTT switch with a state topic is not optimistic: it follows only the value its template
  renders from that topic, `on` for `state_on` (else `payload_on`), `off` for `state_off` (else
  `payload_off`), unknown for `None`, and ignores every other value.** *Verified:* 2026.10.0,
  source `components/mqtt/switch.py` lines 108-128;
  `test_the_switch_sends_the_request_and_follows_the_document` (`switch.turn_off` publishes `off`,
  QoS 1, not retained; the state changes only with the document; a refusal leaves it `on`).
  *Here:* the switch *Telemetry* sends `on` or `off` to `contacts/<key>/telemetry` and renders its
  state from `telemetry_excluded` in the contact's document, so it shows what the gateway took, not
  the click. *Check:* `make ha-test`.
* **An enum sensor ignores a value outside its options, with a warning, and keeps its state.**
  *Verified:* 2026.10.0, source `components/mqtt/sensor.py` lines 328-334;
  `test_every_state_is_an_option`. *Here:* the options of *Telemetry state* and *Telemetry status*
  are built from the `Literal` types in `core/telemetry.py`, which mypy checks every producer
  against, and the snapshot checks that they are; a document of a later version, retained during a
  downgrade, can log this warning until the next document. *Check:* `make ha-test`.
* **MQTT entities carry no `translation_key`, so an enum sensor shows its raw values.**
  *Verified:* 2026.10.0, the discovery schemas in `components/mqtt/` take none (the key appears
  there only for repair issues and errors). *Here:* the values of *Telemetry state* and *Telemetry
  status* are chosen to read plainly (`answering`, `no_position`); translations wait for
  [ADR 0019](decisions/0019-internationalisation-with-gettext.md). *Check:* `grep -n
  translation_key .venv-homeassistant/lib/python3.14/site-packages/homeassistant/components/mqtt/*.py`.
* **A `person` skips a device tracker that is `unknown` or `unavailable` and follows its other
  trackers; with none it turns `unknown`, and a zone, whose state counts the persons in it, no
  longer counts it.** *Verified:* 2026.10.0, source `components/person/__init__.py` lines 84
  (`IGNORE_STATES`) and 640-691, `components/zone/__init__.py` lines 472-475;
  `test_a_person_ignores_an_expired_mesh_tracker` (the person `unknown`, `zone.home` 0; with a
  second GPS tracker the person follows it). *Here:* a tracker of a position from telemetry turns
  `unavailable` while the position is not current
  ([ADR 0024](decisions/0024-telemetry-values-in-home-assistant-expire.md)). *Check:*
  `make ha-test`.
* **The legacy `zone` trigger fires `leave` when a person turns `unknown`, because its state keeps
  the `in_zones` attribute (empty); the entity triggers `zone.left` and `zone.entered` drop
  `unknown` and `unavailable` on either side, so they never fire for a person whose only GPS
  tracker expired, also not for a real departure.** *Verified:* 2026.10.0, source
  `components/zone/trigger.py` lines 57-69 and 150-171, `helpers/trigger/entity_trigger.py` lines
  133-141; `test_a_person_ignores_an_expired_mesh_tracker` (a `leave` automation fires, a
  `zone.left` one does not). *Here:* the guide lists which presence triggers fire and recommends a
  state trigger with `for:` ([home-assistant.md](home-assistant.md#positions-and-presence)).
  *Check:* `make ha-test`.
* **Device triggers skip a sensor that has neither a unit nor a state class.** *Verified:*
  2026.10.0, source `components/sensor/device_trigger.py` line 285. *Here:* *Telemetry state* (an
  enum) has no device trigger; the guide's automations use the event *Telemetry* and state
  triggers. *Check:* `src 2026.10.0 components/sensor/device_trigger.py | sed -n 280,286p`.
* **`recorder.purge_entities` removes an entity's states and events, not its long-term
  statistics; those go only with `recorder/clear_statistics` (*Developer tools → Statistics*).**
  *Verified:* 2026.10.0, source `components/recorder/purge.py` lines 719-725
  (`purge_entity_data`), `components/recorder/websocket_api.py` line 395. *Here:* the guide's
  privacy section says how to delete the statistics of a companion's `measurement` sensors
  ([home-assistant.md](home-assistant.md#privacy)). *Check:* `src 2026.10.0
  components/recorder/purge.py | sed -n 719,725p`.
* **The device classes and units of the reading sensors are accepted: voltage V, current A, power
  W, temperature °C, humidity %, atmospheric pressure hPa, distance m, frequency Hz.** *Verified:*
  2026.10.0, source `components/sensor/const.py` lines 646-760 (`DEVICE_CLASS_UNITS`);
  `test_every_published_reading_type_gets_its_class_unit_and_precision` (a reading of each
  published type, no warning). *Here:* `READING_ENTITIES` in
  `src/meshcore_home_gateway/mqtt/contact_devices.py`; luminosity, generic and analog input have
  neither class nor unit, percentage a unit without a class. *Check:* `make ha-test`.
* **A discovery update that keeps an entity's `state_topic`, QoS and encoding keeps its
  subscription, so the broker does not send the retained message again: the entity keeps its
  state, read with the old template, until the next message on the topic.** *Verified:*
  2026.10.0, source `components/mqtt/subscription.py` lines 29-37 and 66-81
  (`resubscribe_if_necessary` reuses the old subscription unless topic, QoS or encoding differ);
  an experiment with the gateway's discovery of 1.1.0a3 over that of 1.1.0a2 kept the state of
  *Companion position in adverts* and logged nothing. *Here:* after the update to 1.1.0a3 the
  gateway sensor keeps its 1.1.0a2 state until the gateway publishes the `contacts` index again
  (when the radio connects, and at each table read). *Check:* `src 2026.10.0
  components/mqtt/subscription.py | sed -n 66,81p`.

## Home Assistant dashboards

* **A Markdown card parses what its template puts out as Markdown, after the template's own
  escaping: an escaped mesh text still becomes an image that the browser loads, a link or an
  autolink; in an HTML block on one line it stays text.** The card renders its `content` with the
  template engine; the frontend then parses the result with marked (GFM, line breaks on) and filters
  each token with js-xss against a whitelist that admits `img` and `a`. `| e` escapes `<`, `>` and
  `&` only, which leaves `![](https://example.org/p.png)`, `[x](https://example.org)` and
  `www.example.org` intact; in a paragraph they become an `<img>` (a tracking pixel), an `<a>` and
  an autolink. A line that starts with `<div>` opens a CommonMark HTML block, which marked passes on
  as HTML, so the escaped text stays literal; a blank line ends the block, so line breaks in the
  text must go: only LF and CR end it, while VT, FF, NEL, NUL, U+2028 and U+2029 inside a `<div>`
  line keep it one HTML token. Anyone in radio range chooses a message's text, a channel sender's
  claimed name and a contact's own name (an advert renames the contact), line breaks included.
  *Verified:* frontend `20260930.2`, the release Home Assistant 2026.10.0 pins:
  `src/resources/markdown-worker.ts:19-36` (whitelist) and `:71-74` (each token filtered),
  `src/components/ha-markdown-element.ts:92-93` (`gfm: true`), with marked 18.0.14 and xss 1.0.15
  (`package.json:119` and `:140`); experiment: the card of [First
  steps](home-assistant.md#first-steps), rendered by Home Assistant 2026.10.0's template engine with
  a hostile text and sender name and then by a replica of that pipeline with the same library
  versions (not in a browser): the earlier card gave two `<img>` and three `<a>`, the current one a
  single HTML token with everything literal; a `<div>` line with two of each line ending in it
  before the Markdown of an image: LF, CR and CRLF gave an `<img>`, the six others one HTML token. A
  contact named `x`, a blank line and `![](https://example.org/p.png)` put the image after a blank
  line in the card of 1.1.0a3, which escaped the sender's name but kept its line breaks. *Here:*
  the card escapes the text and the sender name, replaces their line breaks with spaces, and keeps
  them in a `<div>` on one line; each card of the generated dashboard prints every value it reads
  from Home Assistant through one macro that does the same, on a line that is one `<div>`,
  `<table>` or `<ha-qr-code>` element. `test_the_markdown_card_keeps_a_hostile_text_inert` and
  `home-assistant/tests/test_dashboard.py` (hostile names, three with line breaks, in every place
  a name reaches) check the form of their output. *Check:* `printf
  '![](https://example.org/p.png)' | npx marked@18.0.14` prints an `<img>`; `printf
  '<div>![](https://example.org/p.png)</div>' | npx marked@18.0.14` prints the input unchanged.

* **A Markdown card can show a QR code: `ha-qr-code` with `data` and `scale` is on the
  sanitiser's allow list. A button entity's own row in an entities card is disabled while the
  button is unavailable and asks first when the row has `confirmation`; a row of `type: button`
  stays pressable then, asks, and its `button.press` reaches no entity (Home Assistant only logs
  a warning). The more-info dialog presses at once.** *Verified:* read in the frontend bundle
  `20260930.4` (Home Assistant 2026.10.0 pins `20260930.2`), not run in a browser: the markdown
  worker's whitelist
  `"ha-qr-code":["data","scale","width","margin","error-correction-level","center-image"]` and the
  element loaded for `ha-qr-code` in a card; `hui-button-entity-row` with `.disabled` for
  `unavailable` and `this._config?.confirmation` before `button.press`; `hui-button-row` without
  `.disabled`, its action run after the action's `confirmation`; the more-info control without a
  confirmation. In 2026.10.0 `button.press` on an unavailable entity publishes nothing: source
  `helpers/service.py` line 726 keeps only available entities. *Here:* the QR card and the
  dashboard row of [home-assistant.md](home-assistant.md#adding-and-removing-contacts) use the
  button's own row; the contract tests check the card's markup
  (`test_the_qr_card_shows_the_gateways_contact_link`) and the row's form and press
  (`test_the_dashboard_button_removes_the_contact`); the browser part is step 28 of the
  [hardware checklist](testing.md#hardware-checklist). *Check:*
  `grep -o '"ha-qr-code":\[[^]]*\]'` over `hass_frontend/frontend_latest/*.js` of the
  `home-assistant-frontend` wheel.

* **A map card draws a trail only with `hours_to_show` above 0, which defaults to 0; the Map panel
  sets none. A trail is the entity's recorder rows that have `latitude` and `longitude`: a row
  without them (`unavailable`) is skipped, and the line joins its neighbours.** The card asks for
  its configured entities with the `history/stream` subscription, `minimal_response`,
  `significant_changes_only` and `no_attributes` all false, only while the `history` integration
  is loaded. *Verified:* frontend `20260930.2` (Home Assistant 2026.10.0),
  `src/panels/lovelace/cards/hui-map-card.ts` lines 79 (`DEFAULT_HOURS_TO_SHOW = 0`), 609-633 (the
  request) and 944-953 (the points); `20260729.5` (2026.8.0) line 48; the drawing was not run in a
  browser. *Here:* the guide's card lists the trackers and sets `hours_to_show: 24`
  ([Traces on the map](home-assistant.md#traces-on-the-map)); `home-assistant/tests/test_traces.py`
  sends the card's exact request against the real recorder: one row per move, none per resting
  answer, an expiry as a row without coordinates. *Check:* `make ha-test`.
* **A device tracker is `force_update`: every write is a new state, so a write after an MQTT
  reconnect or a restart adds a second point on one spot. A card open while its entity is kept out
  of the recorder still draws the points that come while it stays open, and a `person` keeps its
  own copy of the position.** *Verified:* 2026.10.0, source `components/device_tracker/entity.py`
  lines 297-301; `test_without_the_recorder_there_is_no_trace_but_a_person_keeps_one` (no rows
  from the database, the open subscription gets the next point, the person records its rows).
  *Here:* the guide says how expiry, restarts and the recorder exclusion look on a trace.
  *Check:* `make ha-test`.
* **A map card's `conditions` filter only the markers: with `show_all`, every entity with a
  location draws its trail, a phone's and a person's too, also when its marker is filtered out.
  `show_all` takes the entities that have a location when the card gets its configuration; one
  without a location then (an `unavailable` tracker) gets neither marker nor trail until the page
  loads again.** *Verified:* read only, frontend `20260930.2`, `hui-map-card.ts` lines 160-178
  (`show_all`: the entities with a location, persons' sources left out), 205-211 and 425-434 (taken
  once), 451-458 (the markers filtered), 609-633 (the history of every configured entity) and
  934-960 (a trail for each entity of that history); not run in a browser. *Here:* the guide's card
  and the generated dashboard list one tracker per contact instead of `show_all`. *Check:* `sed -n
  451,458p src/panels/lovelace/cards/hui-map-card.ts` in the frontend's source at `20260930.2`.
* **A Markdown card's `entity_id` option does nothing: Home Assistant renders the template again
  when what it read changes, and a template that calls `now()` also each minute.** The frontend
  sends the option as `entity_ids` with the `render_template` subscription; Home Assistant accepts
  the key and does not read it. *Verified:* 2026.10.0, source
  `components/websocket_api/commands.py` lines 759 (the schema) and 767-850 (the handler); frontend
  `20260930.2`, `hui-markdown-card.ts` lines 220-242. *Here:* the generated dashboard's contacts
  table calls `now()` once, so a new contact shows within a minute also when no row has a time,
  and reads its contacts from the registry, which sets up no listener; `test_dashboard.py` checks
  `has_time` and that no card listens to a whole domain. *Check:* `make ha-test`.
* **A card's visibility condition `state` reads a missing entity, and a missing attribute, as
  `unknown`, and compares the values as they are: an unquoted `on` in YAML is the boolean true,
  which matches no state.** *Verified:* read only, frontend `20260930.2`,
  `src/panels/lovelace/common/validate-condition.ts` lines 161-207; Home Assistant 2026.10.0 loads
  a YAML-mode dashboard with PyYAML, which reads YAML 1.1, where `on` is a boolean. *Here:* the
  dashboard's hint for the script shows while `script.mesh_message` is missing
  (`state_not: ["on", "off"]`), the version card hides while the gateway is unavailable (its
  attributes go then), and every `on`, `off` and version is quoted;
  `test_cards_show_only_while_their_state_says_so` evaluates the conditions as that code does.
  *Check:* `sed -n 161,207p src/panels/lovelace/common/validate-condition.ts` at `20260930.2`.
* **A script saved in the script editor takes its ID from its alias (`_2` if that is taken), and
  its more-info dialog shows a form for its `fields`.** *Verified:* read only, frontend
  `20260930.2`, `src/panels/config/script/ha-script-editor.ts` lines 714-722 and 959-980,
  `src/dialogs/more-info/controls/more-info-script.ts` lines 63-131 (the fields from the services'
  descriptions); Home Assistant 2026.10.0 registers them as the script's description
  (`components/script/__init__.py` lines 759-765), and its service takes any data, so `fields`
  only make the form. *Here:* the guide's script *Mesh message* is `script.mesh_message` when its
  name is kept, and the dashboard's tile opens its form;
  `test_the_guides_script_is_a_form_that_sends_for_a_household_user` checks its description and
  that a household user sends with it. *Check:* `make ha-test`.
* **A script's `data` written as a mapping of templates reads each value as Python would:
  `message: "{{ message }}"` sends `+493023125000` as `493023125000`, `1.10` as `1.1`, a text
  without the spaces around it, and refuses `None`. One template for the whole mapping, `data:
  "{{ {'message': message} }}"`, keeps the text as it was.** *Verified:* 2026.10.0, a probe of the
  guide's script and `test_the_guides_script_is_a_form_that_sends_for_a_household_user`; source
  `helpers/template/__init__.py` lines 244-302 (`_parse_result`: `literal_eval`, a text that looks
  like a number becomes one) and 474 (the render stripped). *Here:* the guide's script hands on its
  whole `data` as one template. *Check:* `make ha-test`.
* **Every user can open any entity from the entity search (`e`) and use its controls: a button's
  dialog has *Press*, a switch's dialog its toggle; only the device and command searches, and the
  *Settings* panel with the device pages, are for administrators.** The actions behind them
  (`button.press`, `switch.turn_off`) check no role. *Verified:* read only, frontend `20260930.2`,
  `src/state/quick-bar-mixin.ts` lines 104-138 (`e` for every user, `c` and `d` for
  administrators), `src/dialogs/quick-bar/ha-quick-bar.ts` lines 562-597 (entities without a role
  check, devices with one) and 796 (more-info), `src/state-summary/state-card-button.ts` lines
  33-50 (*Press*); Home Assistant 2026.10.0, `components/config/__init__.py` lines 49-51
  (`require_admin=True`); `test_a_user_who_is_no_administrator_can_press_the_remove_button`.
  *Here:* the guide says that the device page keeps no user from removing a contact or from its
  *Telemetry* switch. *Check:* `make ha-test`, and `sed -n 104,138p src/state/quick-bar-mixin.ts`
  at `20260930.2`.

## MeshCore companion firmware

[protocol.md](protocol.md) documents the companion protocol from the firmware source. Citations
follow its convention: `path:line` at `companion-v1.18.0` (commit `81dbae5`), where `MyMesh.cpp`
is `examples/companion_radio/MyMesh.cpp`. A tree to check them:
`git clone --depth 1 --branch companion-v1.18.0 https://github.com/meshcore-dev/MeshCore.git`.
The entries below are what that page does not cover.

* **`SYNC_NEXT_MESSAGE` takes the message out of the radio's queue when it answers.** A client
  that closes the connection before the answer has arrived loses the message: the next sync does
  not return it. *Verified:* `companion-v1.18.0`; `MyMesh.cpp:1415-1423` sends the frame that
  `getFromOfflineQueue` has just removed from the queue (`MyMesh.cpp:248-260`). The test under
  *Check*, with a radio emulator that dequeues the same way, fails when the session does not wait
  for the answer (`assert [] == ['hello']`). *Here:* `CompanionSession.close`
  (`radio/session.py`) waits for the answer to a sync in flight, for at most the 5 s command
  timeout, and on stop the bridge still handles the messages already handed over, for up to
  `STOP_DRAIN_S` (2 s, `bridge.py`; [design.md](design.md#52-concurrency)). *Check:*
  `.venv/bin/pytest tests/test_bridge.py -k answer_is_on_its_way`; in the firmware tree,
  `sed -n '248,260p;1415,1423p' examples/companion_radio/MyMesh.cpp`.
* **Radios on ESP32 boards with an auto-reset circuit reset when RTS alone is asserted.** The
  USB-UART bridge's RTS line drives the chip's enable pin and DTR its boot pin (GPIO0); both
  asserted together do not reset the chip. *Verified:* upstream documentation only, Espressif's
  [esptool "Boot Mode Selection", Automatic Bootloader](https://docs.espressif.com/projects/esptool/en/latest/esp32/advanced-topics/boot-mode-selection.html#automatic-bootloader)
  and the "Auto program" truth table in the ESP32-DevKitC V4 schematic it links (both read
  2026-10-23); not tested on a radio here. *Here:* `SerialTransport` (`radio/transport.py`) turns
  hardware flow control off, which would drive RTS, then asserts DTR and clears RTS, as
  meshcore_py 2.3.17 and meshcore-cli 1.6.6 do by default
  ([design.md](design.md#61-radio-session), "Opening"). *Check:* the truth table in
  `https://dl.espressif.com/dl/schematics/esp32_devkitc_v4-sch-20180607a.pdf`: the lines are
  active low, so the row `DTR 1, RTS 0 → EN 0` is RTS alone asserted; for a given radio, the
  auto-reset part of its board's schematic.
* **The WiFi interface source moved at `companion-v1.18.0`.** Up to `companion-v1.17.1`
  `SerialWifiInterface.cpp` and `.h` are in `src/helpers/esp32/`; at 1.18.0 both are in
  `src/helpers/wifi/`, and the old path is gone. Both files are the same at every companion tag
  from 1.12.0 to 1.18.0, so the TCP notes in protocol.md hold for these versions; 1.10.0 and
  1.11.0 have no header parser and take whatever has arrived as one frame, minus its first 3 bytes
  (`src/helpers/esp32/SerialWifiInterface.cpp:89-94` at `companion-v1.11.0`). *Verified:*
  `git diff -M` from `companion-v1.17.1` and from `companion-v1.12.0` to `companion-v1.18.0` shows
  a rename without changes (`R100`) for both files; the files at the tags in between were fetched
  from raw.githubusercontent.com and compared by checksum. *Here:* protocol.md and
  `tests/radio_emulator.py` cite the new path with the tag, as [CONTRIBUTING.md](../CONTRIBUTING.md)
  asks for every firmware citation. *Check:* in the tree above,
  `git fetch --depth 1 origin tag companion-v1.17.1 tag companion-v1.12.0 tag companion-v1.11.0`,
  then `git diff -M --name-status <tag> companion-v1.18.0 -- '*SerialWifiInterface*'` for each:
  `R100` for both files from 1.17.1 and 1.12.0, not from 1.11.0.

* **A factory reset gives the radio a new identity.** `CMD_FACTORY_RESET` formats the file
  system (on ESP32 also the NVS); at the next start `loadMainIdentity` fails and the radio makes
  a new random key. A reset radio is therefore a new radio to anyone who knew it, the gateway
  included. *Verified:* `companion-v1.18.0`; `MyMesh.cpp:1965-1974` calls `formatFileSystem`
  (`DataStore.cpp:166-182`), `MyMesh.cpp:948-955` creates the identity when none loads. *Here:*
  the managed profile keys what it applied by the radio's key, so a reset radio gets every
  difference written as `initial` ([ADR 0020](decisions/0020-managed-radio-profile-for-the-observer.md));
  its contacts are gone, and the profile puts the protected ones back. *Check:* in the firmware
  tree, `sed -n '948,955p;1965,1974p' examples/companion_radio/MyMesh.cpp`.
* **`ADD_UPDATE_CONTACT` overwrites every field of an existing key, and only contacts get the
  advert replay check.** The command copies type, flags, path, name, advert time and position
  from the frame into the existing record, or adds a new one: into a free slot, or, with
  overwrite-oldest on, into that of the oldest non-favourite (`BaseChatMesh.cpp:88-103`); there is
  no push afterwards. Adverts of contacts with a timestamp not newer than the stored one are
  dropped as replays; adverts of other nodes are not checked. *Verified:* `companion-v1.18.0`;
  `MyMesh.cpp:194-217` (`updateContactFromFrame`), `1328-1348`; `BaseChatMesh.cpp:129-139`.
  *Here:* the gateway refuses to add a key that is already a contact (`already_contact`) and adds
  a heard node with the time of its signed advert, so the radio drops older replays from then on
  ([mqtt-api.md](mqtt-api.md#managing-contacts-add_contact-and-contactskeyremove)); its own memory
  of heard nodes keeps the newest advert's record (`core/mesh.py`). The gateway writes position
  0, 0, and the radio then refuses the heard advert as not newer, so the record of a companion it
  adds holds no position until its next advert; the gateway checks the advert it heard before the
  add instead (`NEW_ADVERT`, [ADR 0025](decisions/0025-positions-in-adverts-checked-from-the-packet-log.md)).
  Another client that writes a contact with a newer advert time and another position looks like
  an advert that carried one. *Check:* in the firmware tree,
  `sed -n '194,217p;1328,1358p' examples/companion_radio/MyMesh.cpp` and
  `sed -n '129,139p' src/helpers/BaseChatMesh.cpp`.
* **The radio's record keeps the position of a contact's last advert that carried one.** An
  accepted advert sets latitude and longitude only when it has flag 0x10, then the advert time,
  then the push; an advert without the flag leaves the old position, and the flag with 0, 0 writes
  0, 0. A node the radio did not add gets `NEW_ADVERT` with a record made from that advert alone,
  in the same call. The positions are saved with the contacts and survive a reboot. *Verified:*
  `companion-v1.18.0`; `BaseChatMesh.cpp:108-120` (the record of a new node), `:122-200` (the
  name check, the replay check, the stored copy, then `:193-197` the position and the time, `:200`
  the push); `MyMesh.cpp:358-366` (`ADVERT` is the key only);
  `examples/companion_radio/DataStore.cpp:278-279, 312-313` (loaded and saved). *Here:*
  `advert_position` says what the record holds; `advert_carries_position` what the newest checked
  advert carries, and a position from adverts is shown only while that check, if any, carried one
  ([mqtt-api.md](mqtt-api.md#contacts-contacts-and-contactskey)). *Check:* in the firmware tree,
  `sed -n '108,120p;186,201p' src/helpers/BaseChatMesh.cpp`.
* **A companion that shares its position in adverts sends the last valid one when it has no fix,
  after a reboot the one it saved; 0, 0 only when it never had a position.** The advert builder
  sets flag 0x10 whatever the values. The values are the sensor manager's, which keep the live fix
  while it is valid and are loaded from the saved preferences at boot; every setting saves them,
  and the app can set them by hand. No age of the fix is sent. *Verified:* `companion-v1.18.0`,
  read only; `MyMesh.cpp:980-981, 1268-1278, 1297-1303, 1488-1502, 2436-2441`, `MyMesh.h:197-200`,
  `src/helpers/sensors/EnvironmentSensorManager.cpp:937-951`, `src/helpers/SensorManager.h:17`,
  `src/helpers/AdvertDataHelpers.h:29-30`, `src/helpers/AdvertDataHelpers.cpp:7-8`. Whether a
  resting fix jitters, so that a position repeated bit for bit marks a saved one, is step 42 of
  the [hardware checklist](testing.md#hardware-checklist). *Here:* the flag counts as sharing,
  also with 0, 0; the guide says a position from adverts can be one the companion saved long ago.
  *Check:* `sed -n '2436,2448p' examples/companion_radio/MyMesh.cpp`.
* **The device button sends a zero-hop advert, which no repeater forwards; the app sends zero hop
  or, with the parameter 1, a flood.** *Verified:* `companion-v1.18.0`;
  `examples/companion_radio/ui-new/UITask.cpp:586-592` calls `advert()`, which sends
  `sendZeroHop` (`MyMesh.cpp:2436-2448`); `CMD_SEND_SELF_ADVERT` at `MyMesh.cpp:1297-1316`;
  `src/Mesh.cpp:344-357, 717-726`. *Here:* the guide asks for *Advert* within range of the
  gateway's radio, or a flood advert from the app, after switching sharing off
  ([companions.md](companions.md#positions-in-adverts)). *Check:* `sed -n '586,592p'
  examples/companion_radio/ui-new/UITask.cpp`.
* **The radio logs every received packet (`LOG_RX_DATA`) before it checks or handles it, if the
  frame fits 176 bytes: an advert is not logged when its path is longer than 71 bytes minus its
  app data (67 with transport codes).** An advert's raw packet is 2 bytes of header and path
  length, the path, 100 bytes of key, time and signature, and up to 32 bytes of app data.
  Repeaters at 1.17.1 and 1.18.0 forward no advert with 8 hops or more, so with default repeaters
  every advert the radio takes fits *(inferred)*. *Verified:* `companion-v1.18.0`;
  `src/Dispatcher.cpp:199`, `MyMesh.cpp:291-302`, `src/helpers/BaseSerialInterface.h:5`,
  `src/MeshCore.h:8, 11, 12`; repeaters `examples/simple_repeater/MyMesh.cpp:908`
  (`:907` at 1.17.1), `src/helpers/RoutingPolicy.h:15-21`. *Here:* a pushed advert without its
  log is not checked (a debug line "not checked (not logged)"); a new position in the record still
  counts. *Check:* `sed -n '289,302p' examples/companion_radio/MyMesh.cpp`.
* **With the default receive delay 0 the radio logs a packet and handles it in the same pass, so
  an advert's push is the frame right after its log; with a delay above 0 a flood waits up to
  32 s and is handled at the start of a later pass.** Only `SET_TUNING_PARAMS` raises the delay;
  the line that would set 10 is commented out. *Verified:* `companion-v1.18.0`;
  `src/Dispatcher.cpp:11, 138-145, 191-256`, `MyMesh.cpp:273-276, 934, 985, 1470-1477`,
  `examples/companion_radio/NodePrefs.h:29`. *Here:* the gateway reads the receive delay at
  connect; only while it is 0 does the log right before the push decide, and give the advert's
  signal, which no other log gives then ([design.md](design.md#66-observer-documents-and-history)).
  *Check:* `sed -n '138,145p;236,256p' src/Dispatcher.cpp`.
* **`EXPORT_CONTACT` (17) with a key reads the radio's stored copy of the contact's advert into
  its 177-byte frame buffer; an ESP32 or RP2040 radio reads up to 255 bytes there, and a frame
  over 176 bytes is never sent.** On nRF52 and STM32 the store keeps a limited number of copies
  by a 7-byte prefix and refuses one that is too long, keeping the older. *Verified:*
  `companion-v1.18.0`, at the code level; `MyMesh.cpp:1379-1405`, `MyMesh.h:268-269` (the buffer,
  followed by another object), `examples/companion_radio/DataStore.cpp:4-8, 370, 501-557, 573`,
  `BaseChatMesh.cpp:584-586`, `src/helpers/wifi/SerialWifiInterface.cpp:22-25`; what the overrun
  does on a radio was not tested. *Here:* the gateway never sends command 17; it reads the advert
  in the packet log ([ADR 0025](decisions/0025-positions-in-adverts-checked-from-the-packet-log.md)).
  *Check:* `sed -n '1379,1405p' examples/companion_radio/MyMesh.cpp`.

* **A companion denies telemetry to everyone until its owner allows it.** The three telemetry
  modes (base, location, environment) start at 0, *deny*; only the app's command changes them.
  *Verified:* `companion-v1.18.0`; `examples/companion_radio/NodePrefs.h:7-9, 26-28`,
  `MyMesh.cpp:1491`. *Here:* telemetry is on by default
  ([configuration.md](configuration.md#telemetry)): a companion answers only once it allows the
  gateway, or allows everyone, which the household excludes. *Check:* in the firmware tree,
  `sed -n '7,9p;26,28p' examples/companion_radio/NodePrefs.h`.
* **A telemetry answer starts with the battery as the channel 1 voltage and ends with the
  processor's temperature as a channel 1 temperature, only on a board that reports one.** Between
  them come the board's sensors. *Verified:* `companion-v1.18.0`; `MyMesh.cpp:666-675` (the MCU
  temperature only when it is not NaN; an ESP32-C3 may return NaN). *Here:* `lpp.named()` takes
  the first entry as `battery_v` if it is the channel 1 voltage, and the last as
  `mcu_temperature_c` if it is a channel 1 temperature and nothing after it was left undecoded; on
  a board without one, a channel 1 temperature sensor that comes last is named so
  ([mqtt-api.md](mqtt-api.md#reading-keys)). *Check:* `sed -n '666,675p'
  examples/companion_radio/MyMesh.cpp`.
* **The T1000-E sends its light level as 0–100 % in the luminosity type**, the only 1.18
  companion that sends that type. *Verified:* `companion-v1.18.0`;
  `variants/t1000-e/target.cpp:115-116`. *Here:* luminosity has no unit (`ch1_luminosity`), since
  other boards would send lux and the gateway cannot tell them apart. *Check:* `sed -n '110,118p'
  variants/t1000-e/target.cpp`.

Already in protocol.md, with the firmware lines:

* Zeros sent to resynchronise misalign the TCP parser, so the bridge sends them only over USB:
  [section 1](protocol.md#1-framing-usb-serial-and-tcp), "The TCP parser is different".
* Over USB the zeros complete a partly received frame, which then runs with zero-padded arguments:
  [section 1](protocol.md#1-framing-usb-serial-and-tcp), "Resynchronisation".
* The serial parser has no timeout; nRF52 radios send only while DTR is asserted, and opening the
  port at 1200 baud and dropping DTR reboots them into the bootloader:
  [section 1](protocol.md#1-framing-usb-serial-and-tcp).
* TCP on port 5000 has no authentication, serves one client and drops frames while its 4-frame
  queue is full; an advert's log and its push are queued in one pass, so a push never comes
  without its log: [section 1](protocol.md#1-framing-usb-serial-and-tcp) and
  [section 5](protocol.md#5-semantics), "Over TCP pushes can be lost".
* `SET_DEVICE_TIME` only moves the clock forward: [section 2](protocol.md#2-commands).
* The offline queue holds 16 messages by default, in RAM; `MSG_WAITING` comes once per message,
  and a message's frame layout is fixed when it is queued:
  [section 3](protocol.md#3-responses), "Received messages", and
  [section 5](protocol.md#5-semantics), "Offline queue".
* Contacts can leave the table without a push, every advert is pushed once to the client connected
  at that moment, `SEND_TXT_MSG` takes the first contact with the key prefix, and temporary
  contacts are never listed: [section 2](protocol.md#2-commands), "Details that matter", and
  [section 5](protocol.md#5-semantics).
* The radio does not retry direct messages, and a retry with the same attempt number gets no
  acknowledgement: [section 5](protocol.md#5-semantics), "ACK code" and "Retries".
* The path packet returned after a flooded acknowledgement grows with the path, beyond the
  bridge's estimate for more than 4 path bytes: [section 6](protocol.md#6-on-air-sizes).
* The firmware's own transmit budget, `airtime_factor`, allows 50 % by default:
  [section 7](protocol.md#7-radio-parameters-and-time-on-air); setting it is in
  [deployment.md](deployment.md#1-before-you-start).
* The firmware's `docs/companion_protocol.md` differs from the code in five points:
  [section 8](protocol.md#8-where-the-firmwares-own-documentation-differs-from-the-code).
