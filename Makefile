SHELL := /bin/bash

# Config
PYTHON ?= python3
VENV ?= .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
PIP_COMPILE := $(VENV)/bin/pip-compile

.PHONY: help setup venv install dev-install lock pull update auth modules status wipe export analyze clean test

help:
	@echo "Targets:"
	@echo "  setup          Create venv and install requirements"
	@echo "  lock           Compile requirements.in -> requirements.txt (pip-tools)"
	@echo "  pull           Run sync (ARGS='-e -f -o 江林' etc)"
	@echo "  update         Run update-only (ARGS='-e -N -l 100' etc)"
	@echo "  auth/modules/status/wipe/export/analyze  Short utilities (use ARGS=...)"
	@echo "  clean          Remove venv"

setup: venv install ## bootstrap environment

venv:
	@[ -d $(VENV) ] || $(PYTHON) -m venv $(VENV)
	@$(PY) -m pip install -U pip setuptools wheel >/dev/null

install:
	@$(PIP) install -r requirements.txt

dev-install: install ## install dev tools (pip-tools/pytest)
	@$(PIP) install pip-tools pytest pytest-cov

lock: venv ## lock deps from requirements.in
	@$(PIP) install -q pip-tools
	@$(PIP_COMPILE) --generate-hashes -o requirements.txt requirements.in

pull:
	@$(PY) scripts/pull $(ARGS)

update:
	@$(PY) scripts/update $(ARGS)

auth:
	@$(PY) scripts/auth $(ARGS)

modules:
	@$(PY) scripts/modules $(ARGS)

status:
	@$(PY) scripts/status $(ARGS)

wipe:
	@$(PY) scripts/wipe $(ARGS)

export:
	@$(PY) scripts/export $(ARGS)

analyze:
	@$(PY) scripts/analyze $(ARGS)

test:
	@$(PY) -m pytest -q $(ARGS)

clean:
	rm -rf $(VENV)
