# Fenris build targets (the build plan). GNU make instead of just so the
# repo needs no extra tool.

PY := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

.PHONY: help setup install test eval lint clean

help:
	@echo "fenris targets:"
	@echo "  make setup  - create venv + install deps (editable, with dev)"
	@echo "  make test   - run the test suite"
	@echo "  make lint   - ruff check"

setup: install

install:
	python3 -m venv .venv
	$(PIP) install -e ".[dev]"

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info
