"""Embedding protocol + two implementations (the build plan, "Embed").

Protocol: ``embed_documents(texts) -> list[bytes]`` - 1536-dim float32
vectors, L2-normalized in code (below-3072-dim Gemini embeddings need
manual normalization - the build plan), serialized as little-endian
f32 for the sqlite-vec ``float[1536]`` column.

- ``GeminiEmbedder``: google-genai, model ``gemini-embedding-001``,
  ``RETRIEVAL_DOCUMENT`` task, ``output_dimensionality=1536``, batches of
  <=32 texts, a local token pre-count per request, 429 backoff honoring
  ``Retry-After`` capped at 5 attempts.
- ``FakeEmbedder``: deterministic hash-seeded vectors, same shape -
  tests run the whole pipeline offline with zero network calls.

Every batch is pre-counted locally so a request that would blow the
per-request token budget is split before the API sees it.
"""

from __future__ import annotations

import hashlib
import math
import random
import struct
import time
from typing import Protocol, runtime_checkable

# --- embedding identity -----------------------------------------------------
EMBEDDING_MODEL = "gemini-embedding-001"
EMBED_DIMS = 1536
EMBED_TASK = "RETRIEVAL_DOCUMENT"
EMBED_BATCH = 32
EMBED_REQUEST_TOKEN_LIMIT = 2048
EMBED_MODEL_ID = f"{EMBEDDING_MODEL}:{EMBED_DIMS}"

# --- backoff -----------------------------------------------------------------
BACKOFF_MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
BACKOFF_MAX_SECONDS = 60.0


class EmbedError(RuntimeError):
    """Raised when embedding fails after the retry cap."""


def serialize_vector(values: list[float]) -> bytes:
    """Pack a float vector as little-endian f32 for sqlite-vec."""
    return struct.pack(f"<{len(values)}f", *values)


def l2_normalize(values: list[float]) -> list[float]:
    """Normalize in code (contract for <3072-dim Gemini embeddings)."""
    norm = math.sqrt(sum(v * v for v in values))
    if norm == 0.0:
        return list(values)
    return [v / norm for v in values]


def _heuristic_tokens(text: str) -> int:
    """Local token estimate for the per-request pre-count (chars/4, floored)."""
    if not text:
        return 0
    return max(1, int(len(text) / 4))


@runtime_checkable
class Embedder(Protocol):
    """The embedding contract the pipeline depends on."""

    def embed_documents(self, texts: list[str]) -> list[bytes]:
        """Embed texts; return one serialized vector per input, in order."""
        ...

    @property
    def model_id(self) -> str:
        """Identity recorded on sources (model:dims)."""
        ...


class GeminiEmbedder:
    """google-genai implementation: batched, pre-counted, rate-limit aware.

    Args:
        client: a ``google.genai.Client`` (injected; never constructed
            here, so tests never trigger auth/network paths).
        model: embedding model id.
        dims: output dimensionality.
        batch_size: max texts per API request.
        sleep: injectable sleep (tests pass a no-op recorder).
        max_retries: 429/5xx retry cap.
    """

    def __init__(
        self,
        client,
        model: str = EMBEDDING_MODEL,
        dims: int = EMBED_DIMS,
        batch_size: int = EMBED_BATCH,
        sleep=time.sleep,
        max_retries: int = BACKOFF_MAX_RETRIES,
    ) -> None:
        self._client = client
        self._model = model
        self._dims = dims
        self._batch_size = max(1, batch_size)
        self._sleep = sleep
        self._max_retries = max_retries

    @property
    def model_id(self) -> str:
        return f"{self._model}:{self._dims}"

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        """One API request with backoff. Raises EmbedError after the cap."""
        from google.genai import errors as genai_errors

        last_error: Exception | None = None
        for attempt in range(self._max_retries):
            try:
                response = self._client.models.embed_content(
                    model=self._model,
                    contents=texts,
                    config={
                        "task_type": EMBED_TASK,
                        "output_dimensionality": self._dims,
                    },
                )
                vectors = []
                for emb in response.embeddings:
                    values = list(emb.values)
                    if len(values) != self._dims:
                        raise EmbedError(
                            f"embedding dim mismatch: {len(values)} != {self._dims}"
                        )
                    vectors.append(l2_normalize(values))
                return vectors
            except Exception as exc:
                last_error = exc
                retryable = self._is_retryable(exc, genai_errors)
                if not retryable or attempt == self._max_retries - 1:
                    raise EmbedError(f"embed failed: {exc}") from exc
                delay = self._retry_after(exc) or self._backoff_delay(attempt)
                self._sleep(delay)
        raise EmbedError(f"embed failed after retries: {last_error}")

    def _is_retryable(self, exc: Exception, genai_errors) -> bool:
        """429 and 5xx are retryable; 4xx permission/argument errors are not."""
        code = getattr(exc, "code", None)
        if isinstance(code, int):
            return code == 429 or 500 <= code < 600
        if isinstance(exc, getattr(genai_errors, "ServerException", ())):
            return True
        text = str(exc).lower()
        return "429" in text or "resource_exhausted" in text or "overloaded" in text

    def _retry_after(self, exc: Exception) -> float | None:
        """Honor Retry-After when the API sent one (seconds or HTTP-date)."""
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        getter = getattr(headers, "get", None)
        if getter is None:
            return None
        raw = getter("retry-after") or getter("Retry-After")
        if raw is None:
            return None
        try:
            return min(float(raw), BACKOFF_MAX_SECONDS)
        except (TypeError, ValueError):
            return None

    def _backoff_delay(self, attempt: int) -> float:
        """Exponential backoff with jitter, capped."""
        delay = min(BACKOFF_BASE_SECONDS * (2**attempt), BACKOFF_MAX_SECONDS)
        return delay * (0.5 + random.random() / 2)

    def embed_documents(self, texts: list[str]) -> list[bytes]:
        """Embed texts in pre-counted batches <= batch_size texts each.

        The per-request token estimate stays under EMBED_REQUEST_TOKEN_LIMIT:
        with header+body ~600 tokens and 32 texts per request the estimate
        is checked before the call, and oversized batches are split.
        """
        if not texts:
            return []
        out: list[bytes] = []
        batch: list[str] = []
        batch_tokens = 0
        for text in texts:
            tokens = _heuristic_tokens(text)
            oversized_request = (
                batch
                and batch_tokens + tokens > EMBED_REQUEST_TOKEN_LIMIT
            ) or (tokens > EMBED_REQUEST_TOKEN_LIMIT and len(batch) >= self._batch_size)
            if oversized_request:
                out.extend(serialize_vector(v) for v in self._embed_batch(batch))
                batch = []
                batch_tokens = 0
            if len(batch) >= self._batch_size:
                out.extend(serialize_vector(v) for v in self._embed_batch(batch))
                batch = []
                batch_tokens = 0
            batch.append(text)
            batch_tokens += tokens
        if batch:
            out.extend(serialize_vector(v) for v in self._embed_batch(batch))
        return out


class FakeEmbedder:
    """Deterministic hash-seeded vectors - identical input, identical vector.

    Not semantic: it exists so the pipeline, dedup and vec0 storage are
    fully testable offline. Same shape as GeminiEmbedder's output
    (1536-dim f32, L2-normalized, serialized bytes). Counts every call so
    tests can assert dedup skipped the API.
    """

    def __init__(self, dims: int = EMBED_DIMS, seed_prefix: str = "fenris") -> None:
        self._dims = dims
        self._seed_prefix = seed_prefix
        self.calls: list[list[str]] = []
        self._cache: dict[str, bytes] = {}

    @property
    def model_id(self) -> str:
        return f"fake-{self._seed_prefix}:{self._dims}"

    def _vector_for(self, text: str) -> bytes:
        cached = self._cache.get(text)
        if cached is not None:
            return cached
        digest = hashlib.sha256(f"{self._seed_prefix}:{text}".encode()).digest()
        values: list[float] = []
        counter = 0
        while len(values) < self._dims:
            block = hashlib.sha256(
                digest + counter.to_bytes(4, "big")
            ).digest()
            for idx in range(0, len(block), 4):
                if len(values) >= self._dims:
                    break
                chunk = block[idx : idx + 4]
                values.append(struct.unpack("<I", chunk)[0] / 4294967295.0)
            counter += 1
        values = l2_normalize([v - 0.5 for v in values])
        vec = serialize_vector(values)
        self._cache[text] = vec
        return vec

    def embed_documents(self, texts: list[str]) -> list[bytes]:
        """Same contract as GeminiEmbedder; no network, deterministic."""
        self.calls.append(list(texts))
        return [self._vector_for(t) for t in texts]
