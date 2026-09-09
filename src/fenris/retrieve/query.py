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

# how many fused candidates rerank scores (the build plan: "scores the fused top-30")
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


def _apply_scope(conn, candidates, scope: list[str] | None):
    """Restrict candidates to files whose id is in scope (or whose source's
 title/path contains any scope term - the practical folder semantics).

 Empty scope = no restriction. An all-filtered corpus yields [] and the
 caller's empty-evidence path takes over - a scoped miss is an honest
 INSUFFICIENT, not a scope-ignoring fallback.
 """
    if not scope:
        return candidates
    terms = [s.strip().lower() for s in scope if s and s.strip()]
    if not terms:
        return candidates
    ids = [cid for cid, _s in candidates]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"""SELECT c.chunk_id, s.title, s.path, s.file_id
                FROM chunks c JOIN sources s ON s.id = c.source_id
                WHERE c.chunk_id IN ({placeholders})""", ids).fetchall()
    except Exception:  # noqa: BLE001 - a scope filter must never break search
        return candidates
    allowed = set()
    for chunk_id, title, path, file_id in rows:
        hay = " ".join([(title or ""), (path or ""), file_id]).lower()
        if any(t in hay for t in terms):
            allowed.add(chunk_id)
    return [(cid, s) for cid, s in candidates if cid in allowed]


def _apply_vaults(conn, candidates, vaults: list[str] | None):
    """Restrict candidates to the given vault namespaces (P2.5).

 Empty list/None = all vaults. Mirrors _apply_scope: post-fusion, so
 BM25 and KNN obey identically; an all-filtered corpus yields [] and
 the honest INSUFFICIENT path takes over - asking in one vault can
 never leak another vault's evidence.
 """
    if not vaults:
        return candidates
    ids = [cid for cid, _s in candidates]
    if not ids:
        return []
    placeholders = ",".join("?" * len(ids))
    try:
        rows = conn.execute(
            f"""SELECT c.chunk_id, s.drive_id
                FROM chunks c JOIN sources s ON s.id = c.source_id
                WHERE c.chunk_id IN ({placeholders})""", ids).fetchall()
    except Exception:  # noqa: BLE001 - a vault filter must never break search
        return candidates
    allowed = {chunk_id for chunk_id, drive_id in rows if drive_id in vaults}
    return [(cid, s) for cid, s in candidates if cid in allowed]


def search(conn: sqlite3.Connection, query: str, qvec: bytes | None,
           reranker=None, auth_epoch: int = 0,
           scope: list[str] | None = None,
           vaults: list[str] | None = None) -> EvidenceSet:
    """Run the full query path; never raises on empty/missing index state.

 - ``qvec``: the pre-embedded query as a 1536*4 fp32 blob (or None to run
 BM25-only - the dense side degrades to no hits, it does not raise).
 - ``reranker``: a fenris.retrieve.rerank.Reranker; defaults to
 NoopReranker() per the slice contract (unit tests must not download
 models).
 - ``auth_epoch``: the current auth epoch; KNN is filtered to it.
 - ``scope``: optional list of terms; a candidate serves only if its
 file's title/path/id contains one. Applied post-fusion so both
 retrieval sides obey it identically.
 - ``vaults``: optional vault-id list (P2.5); a candidate serves only
 if its source's drive_id is in the list. Same post-fusion position,
 same honesty rules.

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
    fused = _apply_scope(conn, fused, scope)
    fused = _apply_vaults(conn, fused, vaults)

    # byte-exact body dedup (R2, default-on, kill switch FENRIS_DEDUP_BODIES=0):
    # collapse byte-identical bodies AFTER scope/vault filters (a scoped
    # query must keep its in-scope twin even when an out-of-scope original
    # ranked higher) and BEFORE the pool (both route paths get lean lists,
    # and the reranker scores distinct bodies). Tables are exempt.
    from fenris.config import dedup_identical_bodies

    if dedup_identical_bodies():
        from fenris.retrieve.dedup import collapse_identical_bodies

        fused, dedup_dropped = collapse_identical_bodies(conn, fused)
    else:
        dedup_dropped = 0

    # per-reranker floor (route.py owns the map): the floor the ladder
    # will compare against, plus whether it was actually CALIBRATED for
    # this reranker - an uncalibrated floor is the FlashRank value held
    # conservatively, and the trace says so.
    floor_used, floor_calibrated = _route.floor_for(getattr(reranker, "name", None))
    if not floor_calibrated:
        floor_used = _route.RERANK_FLOOR  # conservative until the sweep runs

    trace: dict = {
        "query": query,
        "route_id": decision.route_id,
        "policy_version": decision.policy_version,
        "signals": decision.signals,
        "scope": scope or None,
        "vaults": vaults or None,
        "bm25": [(cid, s) for cid, s in lexical],
        "knn": [(cid, s) for cid, s in dense],
        "rrf": [(cid, s) for cid, s in fused],
        "rerank": [],
        "reranker": getattr(reranker, "name", "unknown"),
        "rerank_floor": floor_used,
        "floor_calibrated": floor_calibrated,
        "dedup_dropped": dedup_dropped,
        "k": 0,
        "agentic_trigger": None,
    }

    if not fused:
        # AGENTIC: evidence empty (the build plan). No rerank on nothing.
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
        if decision.route_id == "NORMAL" and top_score < floor_used:
            # AGENTIC: top rerank score below the APPLIED per-reranker
            # floor. Selection still proceeds (the caller decides whether
            # to run the retry loop), but the decision and trace record
            # the trigger.
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
