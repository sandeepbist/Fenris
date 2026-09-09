# Fenris build targets. GNU make instead of just so the
# repo needs no extra tool.

PY := .venv/bin/python
PIP := .venv/bin/pip

.DEFAULT_GOAL := help

.PHONY: help setup install install-full test eval lint clean clean-caches check-junk api ui ui-dev ui-build dist release

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
	@echo "  make clean        - remove venv, builds, frontend dist + all caches/junk"
	@echo "  make clean-caches - just the junk sweep (pycache, tool caches, reports)"
	@echo "  make check-junk   - list junk-shaped files; deletes nothing"

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
	rm -rf .venv build dist *.egg-info src/*.egg-info frontend/dist frontend/node_modules
	$(MAKE) clean-caches

# The junk sweep: caches, bytecode, build outputs, test-report leftovers.
# Run this after any work session; nothing it removes is source or config.
clean-caches:
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov playwright-report test-results
	find src tests eval -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null
	rm -rf desktop/target desktop/binaries/fenris-engine desktop/binaries/fenris-engine-*
	rm -rf .mimosa/history .mimosa/reports .mimosa/hook-status
	find . -path ./.git -prune -o -name '*.tsbuildinfo' -print -delete 2>/dev/null || true
	find . -path ./.git -prune -o -name '*.log' -print -delete 2>/dev/null || true

# Report anything junk-shaped still tracked or lying around, without deleting.
check-junk:
	@echo "-- untracked junk-shaped files:"
	@find . -path ./.git -prune -o -path ./.private-scrub -prune -o \
	  \( -name '__pycache__' -o -name '*.pyc' -o -name '.pytest_cache' -o -name '.ruff_cache' \
	     -o -name 'node_modules' -o -name 'target' -o -name '*.egg-info' -o -name '.venv' \
	     -o -name '*.tsbuildinfo' -o -name 'playwright-report' -o -name 'test-results' \
	     -o -name 'coverage' -o -name '*.log' -o -name '.DS_Store' -o -name '*.har' \) -print 2>/dev/null | grep -v '^./.git' || echo "  none"
	@echo "-- junk patterns accidentally tracked by git:"
	@git ls-files | grep -E '(__pycache__|\.pyc|\.pytest_cache|\.ruff_cache|node_modules/|/target/|\.egg-info|\.venv|tsbuildinfo|playwright-report|test-results/|coverage\.|\.log$$|\.DS_Store|\.har$$)' || echo "  none"
