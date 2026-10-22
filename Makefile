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
PYZ := meshcore-home-gateway.pyz
UNIT := meshcore-home-gateway.service

.PHONY: help venv lock lint shellcheck workflows unit-check test matrix dist pyz check hooks clean

help: ## List the targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-11s %s\n", $$1, $$2}'

venv: $(VENV)/.installed ## Development environment from the hash-pinned lock file

$(VENV)/.installed: requirements/dev.txt pyproject.toml
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --quiet --require-hashes --no-deps -r requirements/dev.txt
	$(BIN)/python -m pip install --quiet --no-deps --no-build-isolation --editable .
	touch $@

lock: ## Re-resolve requirements/dev.txt with uv, only releases older than COOLDOWN_DAYS
	uv pip compile requirements/dev.in --universal --python-version 3.11 --generate-hashes \
	  --exclude-newer "$$(date -u -d '$(COOLDOWN_DAYS) days ago' +%Y-%m-%d)" \
	  --output-file requirements/dev.txt

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
	$(BIN)/python tools/build_pyz.py --output dist/$(PYZ)
	$(BIN)/python dist/$(PYZ) --version

check: lint shellcheck workflows test dist unit-check ## Everything CI runs

hooks: ## Run `make check` before every push
	git config core.hooksPath .githooks
	@echo "pre-push hook active: make check runs before every push"

clean: ## Remove build and tool output (keeps the virtual environments)
	rm -rf dist build .coverage .coverage.* .mypy_cache .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -not -path './.venv*' -exec rm -rf {} +
