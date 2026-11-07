# Single entry point for local work and CI: the workflow runs exactly these targets.
# `make check` before every push (the pre-push hook does it for you: `make hooks`).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PYTHON ?= python3
PYTHONS ?= python3.11 python3.12 python3.13 python3.14
VENV ?= .venv
BIN := $(VENV)/bin
# The lock file VENV is made from. The release workflow builds with the build tools alone:
# `make release VENV=.venv-release REQUIREMENTS=requirements/release.txt` (another VENV for another
# lock: an environment is made again only when its lock file changes).
REQUIREMENTS ?= requirements/dev.txt
COOLDOWN_DAYS ?= 14
# `make lock UPGRADE=` writes the lock files again with the versions they pin
UPGRADE ?= --upgrade
AUDIT_VENV ?= .venv-audit
# Home Assistant contract tests: their own environment, pinned in home-assistant/requirements.txt.
# HA_SERIES=2026.11 takes the series' own lock, home-assistant/requirements-2026.11.txt, into
# .venv-homeassistant-2026.11 (docs/testing.md).
HA_PYTHON ?= python3.14
HA_SERIES ?=
HA_LOCK := requirements$(if $(HA_SERIES),-$(HA_SERIES))
HA_VENV ?= .venv-homeassistant$(if $(HA_SERIES),-$(HA_SERIES))
# the branch a pull request goes into: `make release-plan` checks the version against it
RELEASE_BASE ?= origin/main
PYZ := meshcore-home-gateway.pyz
UNIT := meshcore-home-gateway.service

# Firmware tests: MeshCore's firmware at the release tests/firmware/sources.toml pins, built natively
FIRMWARE_BUILD ?= build/firmware
# what `make firmware-test` runs, e.g. FIRMWARE_TESTS=tests/firmware/test_paths.py
FIRMWARE_TESTS ?= tests/firmware

.PHONY: help venv lock ha-lock lint shellcheck workflows unit-check unit-run test matrix ha-test \
  interface translations firmware-fetch firmware firmware-test firmware-play dist pyz release \
  release-plan check hooks \
  clean audit

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

venv: $(VENV)/.installed ## Development environment from the hash-pinned lock file

# A changed lock gives a fresh environment, as in CI: --clear drops the packages it no longer has.
# Only an existing environment is cleared, never another directory that VENV names by mistake.
$(VENV)/.locked: $(REQUIREMENTS)
	$(PYTHON) -m venv $(if $(wildcard $(VENV)/pyvenv.cfg),--clear) $(VENV)
	$(BIN)/python -m pip install --quiet --require-hashes --no-deps -r $(REQUIREMENTS)
	touch $@

# Another pyproject.toml (tool settings, metadata) only reinstalls the package itself.
$(VENV)/.installed: $(VENV)/.locked pyproject.toml
	$(BIN)/python -m pip install --quiet --no-deps --no-build-isolation --editable .
	touch $@

# For Linux alone (ADR 0005), and from inside the directory: the form Dependabot writes too, so that
# its pull requests change versions and hashes only (SUP-004).
lock: ## Re-resolve the lock files in requirements/: the newest releases older than COOLDOWN_DAYS
	cd requirements && for name in dev release audit; do \
	  uv pip compile "$$name.in" --python-version 3.11 --python-platform linux --no-strip-extras \
	    --generate-hashes $(UPGRADE) \
	    --exclude-newer "$$(date -u -d '$(COOLDOWN_DAYS) days ago' +%Y-%m-%d)" \
	    --output-file "$$name.txt"; \
	done

# home-assistant/overrides.txt raises versions the pinned Home Assistant asks for (SUP-005).
# `make ha-lock HA_SERIES=2026.11` resolves requirements-2026.11.in the same way.
ha-lock: ## Re-resolve home-assistant/requirements.txt (or HA_SERIES's), only releases older than COOLDOWN_DAYS
	cd home-assistant && uv pip compile $(HA_LOCK).in --python-version 3.14.2 \
	  --python-platform linux --no-strip-extras --generate-hashes --overrides overrides.txt \
	  --exclude-newer "$$(date -u -d '$(COOLDOWN_DAYS) days ago' +%Y-%m-%d)" \
	  --output-file $(HA_LOCK).txt

# Known vulnerabilities, from the Python Packaging Advisory Database (CI runs it; SUP-005). One in
# the tool chains fails; one in the test environment of Home Assistant, whose versions its pinned
# release decides, warns: home-assistant/overrides.txt raises a version past it.
audit: ## Known vulnerabilities in the lock files (pip-audit; needs the network)
	$(MAKE) --no-print-directory VENV=$(AUDIT_VENV) REQUIREMENTS=requirements/audit.txt \
	  $(AUDIT_VENV)/.locked
	$(AUDIT_VENV)/bin/pip-audit --progress-spinner off --disable-pip --require-hashes --strict \
	  -r requirements/dev.txt -r requirements/release.txt -r requirements/audit.txt
	$(foreach lock,$(sort $(wildcard home-assistant/requirements*.txt)),\
	  $(AUDIT_VENV)/bin/pip-audit --progress-spinner off --disable-pip --require-hashes --strict \
	  -r $(lock) || echo "audit: warning: $(lock) has the vulnerabilities above" >&2;)

lint: venv ## ruff lint and format check, mypy --strict
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .
	$(BIN)/mypy

shellcheck: venv ## Static analysis of all shell scripts
	$(BIN)/shellcheck --severity=style deploy/proxmox/*.sh .githooks/*

workflows: venv ## Security audit of the GitHub workflows (zizmor, offline)
	$(BIN)/zizmor --offline --collect=all .github

test: venv ## Tests with branch coverage, service subprocesses included (gate in pyproject.toml)
	rm -f .coverage .coverage.*
	$(BIN)/coverage run -m pytest
	$(BIN)/coverage combine --quiet
	$(BIN)/coverage report

matrix: ## `make test` with every Python in PYTHONS (the CI matrix); a missing one fails it
	@failed=""; missing=""; for python in $(PYTHONS); do \
	  if ! command -v $$python >/dev/null; then missing="$$missing $$python"; continue; fi; \
	  echo "matrix: $$python"; \
	  $(MAKE) --no-print-directory test PYTHON=$$python VENV=.venv-$$python || failed="$$failed $$python"; \
	done; \
	if [ -n "$$missing" ]; then echo "matrix: not installed:$$missing (or narrow PYTHONS)"; fi; \
	if [ -n "$$failed" ]; then echo "matrix: failed with$$failed"; fi; \
	if [ -n "$$missing$$failed" ]; then exit 1; fi; echo "matrix: all passed"

$(HA_VENV)/.installed: home-assistant/$(HA_LOCK).txt
	$(HA_PYTHON) -m venv $(if $(wildcard $(HA_VENV)/pyvenv.cfg),--clear) $(HA_VENV)
	$(HA_VENV)/bin/python -m pip install --quiet --require-hashes --no-deps \
	  -r home-assistant/$(HA_LOCK).txt
	touch $@

# A series' lock exists once `make ha-lock HA_SERIES=…` admitted its releases: the cooldown is
# not bypassed (CONTRIBUTING.md).
home-assistant/requirements-%.txt:
	@echo "ha-test: no lock $@ yet; make ha-lock HA_SERIES=$* writes it once its releases are $(COOLDOWN_DAYS) days old" >&2
	@exit 1

# Home Assistant's own test plugin (pytest-homeassistant, since 2026.11) keeps the test
# configuration in home-assistant/tests/testing_config unless told otherwise: a temporary folder
# here, so that no test writes into the checkout. The older plugin does not know the option.
ha-test: $(HA_VENV)/.installed ## Contract tests against the pinned Home Assistant, or HA_SERIES's (Python 3.14.2+)
	tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && options=() && \
	  if $(HA_VENV)/bin/python -c 'import pytest_homeassistant' 2>/dev/null; then \
	    options=(-o "ha_test_config_dir=$$tmp"); fi && \
	  $(HA_VENV)/bin/python -m pytest -c home-assistant/pytest.ini -p no:cacheprovider \
	    "$${options[@]}" home-assistant/tests

# The firmware tests are not part of `make check`: they need git access to the pinned sources (or a
# local clone in MESHCORE_SOURCE and CAYENNELPP_SOURCE), a C++ compiler and OpenSSL's headers.
firmware-fetch: ## Fetch the pinned MeshCore firmware and CayenneLPP (MESHCORE_SOURCE: a local clone)
	$(PYTHON) -I tools/fetch_firmware.py tests/firmware/sources.toml $(FIRMWARE_BUILD)/src

firmware: firmware-fetch ## Build MeshCore's companion and repeater natively (needs g++, libssl-dev)
	$(MAKE) --no-print-directory -C tests/firmware SOURCES=$(abspath $(FIRMWARE_BUILD))/src \
	  OUT=$(abspath $(FIRMWARE_BUILD))

firmware-test: venv firmware ## The gateway against the real firmware on a simulated air
	MESHCORE_HOME_GATEWAY_FIRMWARE=$(abspath $(FIRMWARE_BUILD)) $(BIN)/python -m pytest \
	  $(FIRMWARE_TESTS)

# EXT-019: radio A, a repeater and Bob's companion on the simulated air, a broker on 127.0.0.1 and
# the gateway on A, until Ctrl-C (PLAY: its options, such as PLAY="--port 0 --hops 2").
firmware-play: venv firmware ## A playground: the firmware's household, a broker and the gateway
	MESHCORE_HOME_GATEWAY_FIRMWARE=$(abspath $(FIRMWARE_BUILD)) $(BIN)/python -m tests.firmware.play \
	  $(PLAY)

# The snapshot test fails until this runs (docs/development.md#compatibility). Review the
# snapshot's diff like code: a removal or narrowing of what the last release had needs a
# **Breaking:** entry in CHANGELOG.md, an addition or change an Added or Changed entry.
interface: venv ## Write tests/public_interface.json from the code and docs/mqtt-api.asyncapi.yaml from it, after a deliberate interface change
	$(BIN)/python -m tests.public_interface --write
	$(BIN)/python tools/asyncapi.py --write

# The translation tests fail until this runs (tools/i18n.py): it writes the templates from the
# marked texts, argparse's own words (adding this Python's) and the installer's texts, merges the
# catalogs with them, writes the installer's language packs and pins them in the installer. Every
# new text then needs its translations in each catalog.
translations: venv ## Write the translation templates, merge the catalogs, write the installer's packs and pins
	$(BIN)/python tools/i18n.py update

unit-check: dist ## systemd-analyze: verify the unit, exposure score at most 2.0
	tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	  sed 's|^ExecStart=/usr/local/bin/meshcore-home-gateway |ExecStart=$(CURDIR)/dist/$(PYZ) |' \
	    deploy/systemd/$(UNIT) >"$$tmp/$(UNIT)" && \
	  systemd-analyze verify "$$tmp/$(UNIT)" && \
	  systemd-analyze security --offline=true --threshold=20 "$$tmp/$(UNIT)" | tail -n 1

# Installs into the system: only on a machine to throw away, as root, after `make dist`
unit-run: ## The zipapp and the unit under this host's systemd, as installed (CI; as root)
	PYTHONDONTWRITEBYTECODE=1 MESHCORE_HOME_GATEWAY_UNIT_RUN=1 \
	  $(BIN)/python -m pytest -p no:cacheprovider tests/test_unit_run.py
# flit packs every file of the package, untracked ones too: a catalog compiled by Poedit next to
# its PO file would reach the wheel and the sdist (the program compiles them itself, ADR 0027).
dist: venv ## Wheel, sdist and the single-file zipapp in dist/
	@compiled=$$(find src -name '*.mo'); if [ -n "$$compiled" ]; then \
	  printf 'dist: remove %s: Poedit wrote it, and flit would pack it into the wheel and sdist\n' \
	    $$compiled >&2; exit 1; fi
	rm -rf dist
	$(BIN)/python -m build --no-isolation --outdir dist .
	$(MAKE) --no-print-directory pyz

pyz: venv ## Reproducible zipapp dist/meshcore-home-gateway.pyz, smoke-tested
	$(BIN)/python tools/build_pyz.py --interpreter '/usr/bin/python3 -I' --output dist/$(PYZ)
	$(BIN)/python dist/$(PYZ) --version

# The release workflow runs this on the tagged commit after `make check`; locally it is a dry run.
# Wheel and sdist get the commit time as their timestamps, so a rebuild gives the same bytes.
# The installer is published with the release's version and the digests of its zipapp and unit
# written in: run from the release's URL, it installs that release's zipapp and unit, checked
# inside the container against SHA256SUMS and against those digests (so the installer's
# attestation covers them), and fetches the language pack it shows its dialogs in from beside it
# (each checked against its pin).
# The unit names the release's version in its comment line: after a rollback, which keeps the
# newer unit, `head -2` of the installed unit says which version it came with.
release: venv ## Release files in dist/release: zipapp, unit, installer and its language packs, wheel, sdist, AsyncAPI document, SHA256SUMS (RELEASE_TAG=vX.Y.Z checks it)
	version=$$($(BIN)/python -c 'import meshcore_home_gateway; print(meshcore_home_gateway.__version__)'); \
	  if [ -n "$(RELEASE_TAG)" ] && [ "$(RELEASE_TAG)" != "v$$version" ]; then \
	    echo "release: tag $(RELEASE_TAG) does not match version $$version" >&2; exit 1; fi; \
	  export SOURCE_DATE_EPOCH=$$(git log -1 --format=%ct); \
	  $(MAKE) --no-print-directory dist; \
	  rm -rf dist/release dist/release-notes.md && mkdir -p dist/release; \
	  cp dist/*.whl dist/*.tar.gz dist/release/; \
	  $(BIN)/python tools/build_pyz.py --interpreter '/usr/bin/python3 -I' \
	    --output "dist/release/meshcore-home-gateway-$$version.pyz" >/dev/null; \
	  "dist/release/meshcore-home-gateway-$$version.pyz" --version; \
	  sed "s/^# Version: .*/# Version: $$version/" deploy/systemd/meshcore-home-gateway.service \
	    >dist/release/meshcore-home-gateway.service; \
	  grep -qx "# Version: $$version" dist/release/meshcore-home-gateway.service \
	    || { echo "release: the unit names no version" >&2; exit 1; }; \
	  chmod 0644 dist/release/meshcore-home-gateway.service; \
	  pyz_sha256=$$(sha256sum "dist/release/meshcore-home-gateway-$$version.pyz" | cut -d' ' -f1); \
	  unit_sha256=$$(sha256sum dist/release/meshcore-home-gateway.service | cut -d' ' -f1); \
	  sed -e "s/^RELEASE=\"\"/RELEASE=\"$$version\"/" \
	    -e "s/^RELEASE_PYZ_SHA256=\"\"$$/RELEASE_PYZ_SHA256=\"$$pyz_sha256\"/" \
	    -e "s/^RELEASE_UNIT_SHA256=\"\"$$/RELEASE_UNIT_SHA256=\"$$unit_sha256\"/" \
	    deploy/proxmox/meshcore-home-gateway-lxc.sh >dist/release/meshcore-home-gateway-lxc.sh; \
	  grep -q "^RELEASE=\"$$version\" " dist/release/meshcore-home-gateway-lxc.sh \
	    || { echo "release: the installer names no release" >&2; exit 1; }; \
	  grep -qx "RELEASE_PYZ_SHA256=\"$$pyz_sha256\"" dist/release/meshcore-home-gateway-lxc.sh \
	    && grep -qx "RELEASE_UNIT_SHA256=\"$$unit_sha256\"" dist/release/meshcore-home-gateway-lxc.sh \
	    || { echo "release: the installer pins no digests" >&2; exit 1; }; \
	  chmod 0755 dist/release/meshcore-home-gateway-lxc.sh; \
	  $(BIN)/python tools/i18n.py release-packs dist/release >/dev/null; \
	  $(BIN)/python tools/asyncapi.py --release "$$version" || exit 1; \
	  cp docs/mqtt-api.asyncapi.yaml "dist/release/meshcore-home-gateway-$$version.asyncapi.yaml"; \
	  chmod 0644 "dist/release/meshcore-home-gateway-$$version.asyncapi.yaml"; \
	  if ! $(BIN)/python tools/release_notes.py "$$version" >dist/release-notes.md; then \
	    if [ -n "$(RELEASE_TAG)" ]; then exit 1; fi; \
	    echo "release: dry run; with RELEASE_TAG=v$$version this stops here (see above)" >&2; fi; \
	  cd dist/release && sha256sum -- * >SHA256SUMS && cat SHA256SUMS

# The version a branch writes, against the main it branched from and the tags, and the public
# interface against that of the last release (CI job release plan).
release-plan: ## Check that a changed __version__ is the next one and the public interface stays compatible
	python3 -I tools/release_plan.py check --base "$$(git merge-base HEAD $(RELEASE_BASE))"

check: lint shellcheck workflows test dist unit-check ## Everything CI runs, except ha-test, firmware-test, release-plan

hooks: ## Run `make check` before every push
	git config core.hooksPath .githooks
	@echo "pre-push hook active: make check runs before every push"

clean: ## Remove build and tool output (keeps the virtual environments)
	rm -rf dist build .coverage .coverage.* .mypy_cache .pytest_cache .ruff_cache .hypothesis
	find . -name __pycache__ -type d -prune -not -path './.venv*' -exec rm -rf {} +
