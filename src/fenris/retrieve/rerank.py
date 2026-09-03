"""Rerankers: the protocol, the unit-test double, and the FlashRank wrapper.

Protocol (``Reranker``): ``rerank(query, candidates) -> [(chunk_id, score)]``
where ``candidates`` is a best-first list of ``(chunk_id, ...anything...)``
tuples - only ``candidate[0]`` (the chunk id) is required, so the fused RRF
list feeds straight in. The output is the full candidate set, best-first,
each with a rerank score in [0, 1] (higher = more relevant).

Score conventions:
- FlashRank cross-encoder logits pass through a sigmoid (its own code path
  for single-logit models) and so already live in [0, 1]; the wrapper
  additionally min-max normalizes the batch to span [0, 1] so a floor like
  RERANK_FLOOR has a stable meaning across batch shapes. An empty batch
  returns [].
- NoopReranker preserves the input order and reports the normalized rank
  score 1/rank (1.0 for rank 1) - a deterministic stand-in that keeps
  adaptive-k testable offline without any model.

FlashRankReranker is lazy: the ONNX session is only built on first use, and
unit tests must use NoopReranker so no model is ever downloaded
(flashrank.Ranker fetches ~34 MB on first init when not cached).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

# the build plan: FlashRank MiniLM-L-12-v2 (~34 MB) is the default reranker.
FLASHRANK_MODEL = "ms-marco-MiniLM-L-12-v2"


class Reranker(Protocol):
    """Anything that can reorder (query, candidates) best-first with [0,1] scores."""

    def rerank(self, query: str, candidates: Sequence[tuple]) -> list[tuple[int, float]]:
        ...


class NoopReranker:
    """Identity reranker: preserves input order, score = 1/rank.

    The unit-test double - deterministic, offline, no model. Adaptive-k and
    the orchestrator are exercised against it everywhere in tests.
    """

    name = "noop"

    def rerank(self, query: str, candidates: Sequence[tuple]) -> list[tuple[int, float]]:
        return [(int(c[0]), 1.0 / rank) for rank, c in enumerate(candidates, start=1)]


class FlashRankReranker:
    """Wraps flashrank.Ranker (MiniLM cross-encoder, CPU ONNX).

    Lazy model init: constructing the Ranker downloads the model on first
    use when it is not already in cache_dir, so nothing here runs at import
    time. Scores are min-max normalized to [0, 1] per batch (raw sigmoid
    scores from a single-logit CE are already in [0, 1]; normalization makes
    the RERANK_FLOOR comparable across batches - a batch of all-terrible
    candidates must not present a fake 1.0).

    ``cache_dir`` defaults to flashrank's own default (/tmp) - pass the
    Fenris data dir in production wiring.
    """

    name = "flashrank"

    def __init__(self, model_name: str = FLASHRANK_MODEL, cache_dir: str | None = None,
                 max_length: int = 512):
        self.model_name = model_name
        self._cache_dir = cache_dir
        self._max_length = max_length
        self._ranker = None  # lazy: built on first rerank call
        self._texts: dict[int, str] = {}

    def _ensure_ranker(self):
        if self._ranker is None:
            from flashrank import Ranker

            kwargs = {"model_name": self.model_name, "max_length": self._max_length}
            if self._cache_dir is not None:
                kwargs["cache_dir"] = self._cache_dir
            self._ranker = Ranker(**kwargs)
        return self._ranker

    def register_texts(self, texts: dict[int, str]) -> None:
        """Supply ``{chunk_id: "header\nbody"}`` for the candidates about to be
        reranked. FlashRank reranks *text*; the pipeline carries ids, so the
        orchestrator registers texts before calling rerank. Ids without a
        registered text are skipped (never rerank on empty strings)."""
        self._texts = dict(texts)

    def rerank(self, query: str, candidates: Sequence[tuple]) -> list[tuple[int, float]]:
        if not candidates:
            return []
        passages = []
        ids = []
        for cand in candidates:
            chunk_id = int(cand[0])
            text = self._texts.get(chunk_id)
            if text and text.strip():
                ids.append(chunk_id)
                passages.append({"id": chunk_id, "text": text})
        if not passages:
            return []
        ranker = self._ensure_ranker()
        from flashrank import RerankRequest

        request = RerankRequest(query=query, passages=passages)
        results = ranker.rerank(request)  # flashrank sorts best-first
        scored = [(int(r["id"]), float(r["score"])) for r in results]
        if not scored:
            return []
        return _normalize_batch(scored)


def _normalize_batch(scored: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Min-max normalize [0,1] so floors stay comparable across batch
    shapes; an all-equal batch scores 1.0 for every candidate (the
    batch said they are all the same - not that all are terrible)."""
    raw = [s for _cid, s in scored]
    lo, hi = min(raw), max(raw)
    if hi - lo < 1e-12:
        return [(cid, 1.0) for cid, _s in scored]
    return [(cid, (s - lo) / (hi - lo)) for cid, s in scored]


class ModernBertReranker:
    """gte-reranker-modernbert-base int8 ONNX (2026-09 retrieval round).

    The encoder-class CPU upgrade over FlashRank's 2021 MiniLM (BEIR
    56.19 vs the v1-era large at 49.32; 149M params, 8k pair context,
    Apache-2.0). Weights arrive ONLY via `fenris models fetch
    modernbert-rerank` (pinned sha256 + smoke test) - this class never
    downloads. Score convention matches FlashRank exactly (sigmoid to
    [0,1], then the shared min-max batch normalization), so per-
    reranker floors stay the only calibration surface.

    Lazy session: nothing loads at import time. ``_weights_dir`` is the
    test/models seam (fetch's smoke test passes the fetched dir).
    """

    name = "modernbert"

    def __init__(self, *, _weights_dir=None):
        self._session = None
        self._tokenizer = None
        self._weights_dir = _weights_dir

    def _ensure_session(self):
        if self._session is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            from fenris import models as models_mod

            base = self._weights_dir or models_mod.locate("modernbert-rerank")
            if base is None:
                raise FileNotFoundError(
                    "modernbert reranker weights not fetched - run "
                    "`fenris models fetch modernbert-rerank` (explicit "
                    "download, pinned sha256 + smoke test)"
                )
            self._session = ort.InferenceSession(
                str(base / "model.onnx"),
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))

    def _score_pairs(self, query: str, texts: list[str]) -> list[float]:
        import numpy as np

        scores: list[float] = []
        for text in texts:
            # cross-encoder input: the (query, passage) pair
            enc = self._tokenizer.encode(query, text)
            ids = np.array([enc.ids], dtype=np.int64)
            mask = np.array([enc.attention_mask], dtype=np.int64)
            logits = self._session.run(None, {
                "input_ids": ids, "attention_mask": mask,
            })[0]
            # single-logit relevance head -> sigmoid to [0, 1]
            logit = float(np.asarray(logits).reshape(-1)[0])
            scores.append(1.0 / (1.0 + np.exp(-logit)))
        return scores

    def register_texts(self, texts: dict[int, str]) -> None:
        self._texts = dict(texts)

    def rerank(self, query: str, candidates: Sequence[tuple]) -> list[tuple[int, float]]:
        if not candidates:
            return []
        ids, passages = [], []
        for cand in candidates:
            chunk_id = int(cand[0])
            text = self._texts.get(chunk_id)
            if text and text.strip():
                ids.append(chunk_id)
                passages.append(text)
        if not passages:
            return []
        self._ensure_session()
        scored = list(zip(ids, self._score_pairs(query, passages)))
        if not scored:
            return []
        return _normalize_batch(scored)


def resolve_reranker(use_flashrank: bool = True):
    """The configured reranker, with the local fallback ladder.

    FENRIS_RERANKER=modernbert opts into the encoder upgrade when its
    weights are fetched; anything unfetchable degrades to FlashRank, then
    Noop (the CLI's existing ladder). The reason is returned for traces
    and doctor output - a silent no-op flag is the failure this prevents.
    """
    from fenris import models as models_mod
    from fenris.config import reranker_choice

    choice = reranker_choice().strip().lower()
    if choice == "modernbert":
        if models_mod.available("modernbert-rerank"):
            return ModernBertReranker(), "modernbert"
        return FlashRankReranker(), "modernbert-unavailable-flashrank"
    if choice not in ("flashrank", ""):
        return FlashRankReranker(), f"unknown-{choice}-flashrank"
    if use_flashrank:
        return FlashRankReranker(), "flashrank"
    return NoopReranker(), "noop"
