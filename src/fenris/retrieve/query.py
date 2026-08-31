"""The retrieval orchestrator (the build plan, the query path).

    route -> BM25 || KNN -> RRF (k=60) -> [rerank] -> adaptive-k
          -> per-file diversity cap -> evidence assembly

Route-dependent pipeline:

- EXACT: BM25-first (rerank skipped - "skip rerank if top-1 full-phrase match
  with >30% gap"; MVP gives no calibrated gap test yet, so EXACT skips rerank
  outright and runs adaptive-k on normalized RRF scores instead).
- TABLE: the hybrid is restricted to table chunks (is_table = 1) on the BM25
  side and table candidates on the KNN side; table_id flows through to the
  evidence records for a tables.range citation.
- NORMAL: full hybrid + rerank + adaptive-k on rerank scores.
- AGENTIC: not chosen by ``route()`` (it has no scores yet); the orchestrator
  flips NORMAL -> AGENTIC post-rerank when the top-1 rerank score is below
  RERANK_FLOOR, or when evidence comes back empty. The bounded retry loop
  itself is M4+ (out of this slice); the flip is recorded in the decision
  and the trace so the caller can act on it.

The trace dict records per-stage candidates and scores (bm25, knn, rrf,
rerank, k, final) - the eval harness and the CONFLICTING pre-check both read it.

Embedding the query text is NOT this module's job: ``search()`` accepts the
already-embedded ``qvec``. ``embed_query`` stays a delegation stub for wiring
the embedder in the CLI layer.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol

from fenris.retrieve import adaptive_k, bm25, diversity, evidence, knn, rrf
from fenris.retrieve import route as _route
from fenris.retrieve.rerank import NoopReranker

# how many fused candidates rerank scores (the build plan s.6: "scores the fused top-30")
RERANK_POOL = 30


class Embedder(Protocol):
    """Minimal embedder contract: text -> 1536-dim L2-normalized vector."""

    def embed_query(self, text: str) -> bytes:
        ...


def embed_query(text: str, embedder: Embedder | None = None) -> bytes:
    """Stub for later wiring: delegate to an injected embedder.

    The retrieval slice never embeds; the CLI/caller embeds and passes qvec.
    Raises RuntimeError when no embedder is wired (fail loudly, not silently
    with a zero vector)."""
    if embedder is None:
        raise RuntimeError("embed_query requires an injected embedder (qvec is caller-supplied)")
    return embedder.embed_query(text)


@dataclass
class EvidenceSet:
    """The result of one retrieval pass."""

    evidence: list[evidence.Evidence] = field(default_factory=list)
    route_decision: _route.RouteDecision | None = None
    trace: dict = field(default_factory=dict)


def search(conn: sqlite3.Connection, query: str, qvec: bytes | None,
           reranker=None, auth_epoch: int = 0) -> EvidenceSet:
    """Run the full query path; never raises on empty/missing index state.

    - ``qvec``: the pre-embedded query as a 1536*4 fp32 blob (or None to run
      BM25-only - the dense side degrades to no hits, it does not raise).
    - ``reranker``: a fenris.retrieve.rerank.Reranker; defaults to
      NoopReranker() per the slice contract (unit tests must not download
      models).
    - ``auth_epoch``: the current auth epoch; KNN is filtered to it.

    Returns EvidenceSet(evidence, route_decision, trace). Empty index ->
    empty evidence with the AGENTIC trigger recorded in the decision signals
    and the trace, no exception.
    """
    decision = _route.route(query, conn)
    reranker = reranker if reranker is not None else NoopReranker()

    table_mode = decision.route_id == "TABLE"
    lexical = bm25.bm25_search(conn, query, table_only=table_mode)

    dense: list[tuple[int, float]] = []
    if qvec is not None and qvec:
        dense = knn.knn_search(conn, qvec, auth_epoch=auth_epoch)

    if table_mode:
        # restrict the dense side to table chunks as well (is_table mirror
        # lives on chunks, not vec0 metadata - filter post-KNN)
        dense = _filter_tables(conn, dense)

    fused = rrf.rrf_fuse(lexical, dense)

    trace: dict = {
        "query": query,
        "route_id": decision.route_id,
        "policy_version": decision.policy_version,
        "signals": decision.signals,
        "bm25": [(cid, s) for cid, s in lexical],
        "knn": [(cid, s) for cid, s in dense],
        "rrf": [(cid, s) for cid, s in fused],
        "rerank": [],
        "reranker": getattr(reranker, "name", "unknown"),
        "k": 0,
        "agentic_trigger": None,
    }

    if not fused:
        # AGENTIC: evidence empty (the build plan s.6). No rerank on nothing.
        trace["agentic_trigger"] = "empty_evidence"
        signals = dict(decision.signals)
        signals["agentic_trigger"] = "empty_evidence"
        decision = _route.RouteDecision("AGENTIC", decision.policy_version, signals)
        trace["route_id"] = decision.route_id
        trace["final"] = []
        return EvidenceSet([], decision, trace)

    pool = fused[:RERANK_POOL]

    if decision.route_id == "EXACT":
        # EXACT: BM25-first, rerank skipped; adaptive-k runs on normalized RRF.
        ranked = list(pool)
        trace["rerank_skipped"] = True
    else:
        # NORMAL / TABLE: rerank the fused top-30.
        if hasattr(reranker, "register_texts"):
            reranker.register_texts(_texts_for(conn, [cid for cid, _s in pool]))
        ranked = reranker.rerank(query, pool)
        trace["rerank"] = [(cid, s) for cid, s in ranked]
        top_score = ranked[0][1] if ranked else 0.0
        if decision.route_id == "NORMAL" and top_score < _route.RERANK_FLOOR:
            # AGENTIC: top rerank score below the calibrated floor. Selection
            # still proceeds (the caller decides whether to run the retry
            # loop), but the decision and trace record the trigger.
            trace["agentic_trigger"] = "rerank_floor"
            signals = dict(decision.signals)
            signals["agentic_trigger"] = "rerank_floor"
            decision = _route.RouteDecision("AGENTIC", decision.policy_version, signals)
            trace["route_id"] = decision.route_id

    selected = adaptive_k.select_k(ranked)
    trace["k"] = len(selected)

    diversified = diversity.apply_diversity(conn, selected)
    trace["diversity_dropped"] = len(selected) - len(diversified)

    records = evidence.build_evidence_set(conn, diversified)
    trace["final"] = [(ev.chunk_id, ev.score, ev.evidence_id) for ev in records]

    return EvidenceSet(records, decision, trace)


def _filter_tables(conn: sqlite3.Connection,
                   dense: list[tuple[int, float]]) -> list[tuple[int, float]]:
    """Keep only is_table=1 chunks from a dense candidate list (TABLE route)."""
    if not dense:
        return []
    ids = [cid for cid, _s in dense]
    try:
        rows = conn.execute(
            f"SELECT chunk_id FROM chunks WHERE is_table = 1"
            f" AND chunk_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    except sqlite3.Error:
        return []
    table_ids = {int(r[0]) for r in rows}
    return [(cid, s) for cid, s in dense if cid in table_ids]


def _texts_for(conn: sqlite3.Connection, chunk_ids: Sequence[int]) -> dict[int, str]:
    """header + body text per chunk id, for rerankers that rerank text."""
    if not chunk_ids:
        return {}
    try:
        rows = conn.execute(
            f"SELECT chunk_id, header_text, body_text FROM chunks"
            f" WHERE chunk_id IN ({','.join('?' * len(chunk_ids))})",
            list(chunk_ids)).fetchall()
    except sqlite3.Error:
        return {}
    out: dict[int, str] = {}
    for cid, header, body in rows:
        header = header or ""
        body = body or ""
        out[int(cid)] = f"{header}\n{body}" if header else body
    return out
