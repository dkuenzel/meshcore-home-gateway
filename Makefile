# Single entry point for local work and CI: the workflow runs exactly these targets.
# `make check` before every push (the pre-push hook does it for you: `make hooks`).

SHELL := /bin/bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

PYTHON ?= python3
PYTHONS ?= python3.11 python3.12 python3.13 python3.14
VENV ?= .venv
BIN := $(VENV)/bin
COOLDOWN_DAYS ?= 14
# Home Assistant contract tests: their own environment, pinned in home-assistant/requirements.txt
HA_PYTHON ?= python3.14
HA_VENV ?= .venv-homeassistant
PYZ := meshcore-home-gateway.pyz
UNIT := meshcore-home-gateway.service

.PHONY: help venv lock ha-lock lint shellcheck workflows unit-check test matrix ha-test dist pyz \
  release check hooks clean

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

venv: $(VENV)/.installed ## Development environment from the hash-pinned lock file

$(VENV)/.installed: requirements/dev.txt pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --require-hashes --no-deps -r requirements/dev.txt
	$(BIN)/python -m pip install --quiet --no-deps --no-build-isolation --editable .
	touch $@

lock: ## Re-resolve requirements/dev.txt with uv: the newest releases older than COOLDOWN_DAYS
	uv pip compile requirements/dev.in --universal --python-version 3.11 --generate-hashes --upgrade \
	  --exclude-newer "$$(date -u -d '$(COOLDOWN_DAYS) days ago' +%Y-%m-%d)" \
	  --output-file requirements/dev.txt

ha-lock: ## Re-resolve home-assistant/requirements.txt, only releases older than COOLDOWN_DAYS
	uv pip compile home-assistant/requirements.in --universal --python-version 3.14.2 \
	  --generate-hashes --exclude-newer "$$(date -u -d '$(COOLDOWN_DAYS) days ago' +%Y-%m-%d)" \
	  --output-file home-assistant/requirements.txt

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

matrix: ## `make test` with every Python in PYTHONS that is installed (as the CI matrix)
	@failed=""; for python in $(PYTHONS); do \
	  if ! command -v $$python >/dev/null; then echo "matrix: $$python not installed, skipped"; continue; fi; \
	  echo "matrix: $$python"; \
	  $(MAKE) --no-print-directory test PYTHON=$$python VENV=.venv-$$python || failed="$$failed $$python"; \
	done; \
	if [ -n "$$failed" ]; then echo "matrix: failed with$$failed"; exit 1; fi; echo "matrix: all passed"

$(HA_VENV)/.installed: home-assistant/requirements.txt
	$(HA_PYTHON) -m venv $(HA_VENV)
	$(HA_VENV)/bin/python -m pip install --quiet --require-hashes --no-deps \
	  -r home-assistant/requirements.txt
	touch $@

ha-test: $(HA_VENV)/.installed ## Contract tests against the pinned Home Assistant (Python 3.14.2+)
	$(HA_VENV)/bin/python -m pytest -c home-assistant/pytest.ini -p no:cacheprovider \
	  home-assistant/tests

unit-check: pyz ## systemd-analyze: verify the unit, exposure score at most 2.0
	tmp=$$(mktemp -d) && trap 'rm -rf "$$tmp"' EXIT && \
	  sed 's|^ExecStart=/usr/local/bin/meshcore-home-gateway |ExecStart=$(CURDIR)/dist/$(PYZ) |' \
	    deploy/systemd/$(UNIT) >"$$tmp/$(UNIT)" && \
	  systemd-analyze verify "$$tmp/$(UNIT)" && \
	  systemd-analyze security --offline=true --threshold=20 "$$tmp/$(UNIT)" | tail -n 1

dist: venv ## Wheel, sdist and the single-file zipapp in dist/
	rm -rf dist
	$(BIN)/python -m build --no-isolation --outdir dist .
	$(MAKE) --no-print-directory pyz

pyz: venv ## Reproducible zipapp dist/meshcore-home-gateway.pyz, smoke-tested
	$(BIN)/python tools/build_pyz.py --interpreter '/usr/bin/python3 -I' --output dist/$(PYZ)
	$(BIN)/python dist/$(PYZ) --version

# The release workflow runs this on the tagged commit after `make check`; locally it is a dry run.
# Wheel and sdist get the commit time as their timestamps, so a rebuild gives the same bytes.
release: venv ## Release files in dist/release: zipapp, wheel, sdist, SHA256SUMS (RELEASE_TAG=vX.Y.Z checks it)
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
	  if ! $(BIN)/python tools/release_notes.py "$$version" >dist/release-notes.md; then \
	    if [ -n "$(RELEASE_TAG)" ]; then exit 1; fi; \
	    echo "release: dry run; with RELEASE_TAG=v$$version this stops here (see above)" >&2; fi; \
	  cd dist/release && sha256sum -- * >SHA256SUMS && cat SHA256SUMS

check: lint shellcheck workflows test dist unit-check ## Everything CI runs, except ha-test

hooks: ## Run `make check` before every push
	git config core.hooksPath .githooks
	@echo "pre-push hook active: make check runs before every push"

clean: ## Remove build and tool output (keeps the virtual environments)
	rm -rf dist build .coverage .coverage.* .mypy_cache .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -not -path './.venv*' -exec rm -rf {} +
