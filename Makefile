# Fenris build targets (the build plan). GNU make instead of just so the
# repo needs no extra tool.

PY := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

.PHONY: help setup install test eval lint clean api ui ui-dev ui-build

help:
	@echo "fenris targets:"
	@echo "  make setup  - create venv + install deps (editable, with dev)"
	@echo "  make test   - run the test suite"
	@echo "  make lint   - ruff check"
	@echo "  make ui     - build the frontend (bun) + serve EVERYTHING from the API"
	@echo "  make ui-dev - frontend dev server on :5173 with hot reload"

setup: install

install:
	python3 -m venv .venv
	$(PIP) install -e ".[dev]"
	$(MAKE) slim

# docling (full) drags in torch + CUDA wheels (~4.5 GB); we run CPU-only.
# See the NOTE in pyproject.toml.
slim:
	-$(PIP) uninstall -y docling-ibm-models torch torchvision triton accelerate \
	  nvidia-cublas nvidia-cuda-cupti nvidia-cuda-nvrtc nvidia-cuda-runtime \
	  nvidia-cudnn-cu13 nvidia-cufft nvidia-cufile nvidia-curand nvidia-cusolver \
	  nvidia-cusparse nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvjitlink \
	  nvidia-nvshmem-cu13 nvidia-nvtx >/dev/null 2>&1 || true
	-rm -rf .venv/lib/python3.14/site-packages/torch

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check src tests

# ---- web UI ----------------------------------------------------------------

ui-build:
	cd frontend && bun run build

ui: ui-build
	$(PY) -m uvicorn fenris.api:app --host 127.0.0.1 --port 8000

ui-dev:
	cd frontend && bun run dev

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info frontend/dist
