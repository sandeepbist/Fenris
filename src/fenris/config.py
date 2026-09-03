"""Shared config: paths, model pins, and toolchain versions.

Everything that identifies a representation (parser, chunker, embedding
model + dims) is pinned here and flows into the sources manifest, so a
toolchain bump produces a new representation rather than corrupting the old.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# Toolchain identity - bump to force new representations of the same content
PARSER_VERSION = "docling-1"
CHUNKER_VERSION = "recursive512-v1"
EMBEDDING_MODEL = "gemini-embedding-001"
EMBED_DIMS = 1536
TASK_FORMAT_VERSION = "asym-v1"          # RETRIEVAL_QUERY / RETRIEVAL_DOCUMENT pairing

GENERATION_MODEL = "gemini-2.5-flash"
JUDGE_MODEL = "gemini-2.5-flash"

# RRF fusion constant (the design notes: k=60 default)
RRF_K = 60

# Adaptive-k
ELBOW_DELTA = 0.15                        # on scores normalized s/s1
CUMULATIVE_MASS = 0.90
K_MIN, K_MAX, K_DEFAULT = 1, 25, 8

# Ingestion
CHUNK_TOKENS = 512
PARENT_TOKENS = 2048
EMBED_BATCH = 32
MAX_FILE_BYTES = 100 * 1024 * 1024       # 100 MB parse ceiling
MAX_PAGES = 500
ARTIFACT_CAP_BYTES = 2 * 1024 * 1024 * 1024   # 2 GB Docling JSON store

# Sync
POLL_INTERVAL_MIN = 15
BACKOFF_MAX_RETRIES = 5
RESYNC_MISMATCH_THRESHOLD = 0.10

# Security / logging
TRACE_RETENTION_DAYS = 30
INJECTION_PATTERNS = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "system prompt:",
    "you are now",
    "exfiltrate",
    "send the contents to",
    "reveal your instructions",
)

# --- retrieval-quality round (2026-09); env overrides ---------------------------
# All feature flags are read AT CALL TIME (not import time) so tests can
# monkeypatch the env per test and no module-reload hazards exist: a flag
# flip is a live read, and every consumer imports the reader, never a
# snapshot. Defaults unchanged when the env vars are absent.


def reranker_choice() -> str:
    """flashrank (default, calibrated) | modernbert (encoder upgrade;
    requires `fenris models fetch modernbert-rerank`; floor provisional
    until the golden-suite sweep runs)."""
    return os.environ.get("FENRIS_RERANKER", "flashrank")


def dedup_identical_bodies() -> bool:
    """Byte-exact body dedup at the retrieval boundary (default ON)."""
    return os.environ.get("FENRIS_DEDUP_BODIES", "1") == "1"


def contextual_headers_mode() -> str:
    """deterministic (default) | llm — the Anthropic-style chunk context
    into the embed payload (dark until its eval gate)."""
    return os.environ.get("FENRIS_CONTEXTUAL_HEADERS", "deterministic")


def groundedness_mode() -> str:
    """off (default) | on — the NLI downgrade-only gate (dark until its
    eval gate; requires `fenris models fetch nli-deberta-small`)."""
    return os.environ.get("FENRIS_GROUNDEDNESS", "off")


GROUNDEDNESS_THRESHOLD = 0.5  # provisional; calibrated in the eval round
SURVIVING_SENTENCE_FLOOR = 1


def default_data_dir() -> Path:
    """XDG-conscious local data dir. FENRIS_HOME overrides for tests."""
    if override := os.environ.get("FENRIS_HOME"):
        return Path(override)
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / "fenris"
    return Path.home() / ".local" / "share" / "fenris"


@dataclass
class FenrisConfig:
    data_dir: Path = field(default_factory=default_data_dir)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "fenris.db"

    @property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> FenrisConfig:
    cfg = FenrisConfig()
    cfg.ensure_dirs()
    return cfg
