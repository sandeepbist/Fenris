# Fenris build targets. GNU make instead of just so the
# repo needs no extra tool.

PY := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

.PHONY: help setup install install-full test eval lint clean api ui ui-dev ui-build dist release

help:
	@echo "fenris targets:"
	@echo "  make setup        - create venv + install deps (editable, with dev)"
	@echo "  make install-full - slim setup PLUS torch (PDFs + images parse)"
	@echo "  make test         - run the test suite"
	@echo "  make lint         - ruff check"
	@echo "  make ui           - build the frontend (bun) + serve EVERYTHING from the API"
	@echo "  make ui-dev       - frontend dev server on :5173 with hot reload"
	@echo "  make eval         - run the golden eval suite over the synthetic corpus"
	@echo "  make dist         - build wheel + sdist"
	@echo "  make release      - gate on full suite + lint, then build all artifacts"

setup: install

install:
	python3 -m venv .venv
	$(PIP) install -e ".[dev]"
	$(MAKE) slim

# Full parsing tier: torch-backed Docling layout models. ~1.2 GB beyond
# slim; images route direct (OCR) and PDFs parse with layout models.
# `fenris doctor` reports the active tier.
install-full:
	python3 -m venv .venv
	$(PIP) install -e ".[dev]"
	$(PIP) install torch --index-url https://download.pytorch.org/whl/cpu
	@echo "full parsing tier installed - run \`fenris doctor\` to confirm"

# The venv's site-packages, resolved from the interpreter (version-agnostic).
VENV_SITE := $(shell $(PY) -c "import sysconfig; print(sysconfig.get_paths()['purelib'])" 2>/dev/null)

# docling (full) drags in torch + CUDA wheels (~4.5 GB); we run CPU-only.
# See the NOTE in pyproject.toml.
slim:
	-$(PIP) uninstall -y docling-ibm-models torch torchvision triton accelerate \
	  nvidia-cublas nvidia-cuda-cupti nvidia-cuda-nvrtc nvidia-cuda-runtime \
	  nvidia-cudnn-cu13 nvidia-cufft nvidia-cufile nvidia-curand nvidia-cusolver \
	  nvidia-cusparse nvidia-cusparselt-cu13 nvidia-nccl-cu13 nvidia-nvjitlink \
	  nvidia-nvshmem-cu13 nvidia-nvtx >/dev/null 2>&1 || true
	-rm -rf $(VENV_SITE)/torch

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

# ---- packaging / release ----------------------------------------------------

eval:
	$(PY) -m fenris.cli eval --suite golden

dist:
	$(PY) -m pip install --quiet build
	$(PY) -m build

# Local pre-flight for a release: same gate CI enforces on the tag.
release: test lint dist
	@echo "gate green; artifacts in dist/ - tag vX.Y.Z to trigger the real release"

clean:
	rm -rf .venv build dist *.egg-info src/*.egg-info frontend/dist
