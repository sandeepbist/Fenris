"""Fenris retrieval path (the build plan).

Public surface - the integrator wires these into the CLI and the generation
layer:

-:func:`fenris.retrieve.search` - the orchestrator entry point
-:class:`fenris.retrieve.EvidenceSet`
-:class:`fenris.retrieve.Evidence`
-:class:`fenris.retrieve.RouteDecision`
-:class:`fenris.retrieve.NoopReranker` /:class:`fenris.retrieve.FlashRankReranker`
 / the:class:`fenris.retrieve.Reranker` protocol
- stage functions: ``route.route``, ``bm25.bm25_search``, ``knn.knn_search``,
 ``knn.pack_vector``, ``rrf.rrf_fuse``, ``adaptive_k.select_k``,
 ``diversity.apply_diversity``, ``evidence.build_evidence``
"""

from fenris.retrieve.evidence import Evidence, build_evidence, build_evidence_set, evidence_id_for
from fenris.retrieve.query import EvidenceSet, embed_query, search
from fenris.retrieve.rerank import (
    FLASHRANK_MODEL,
    FlashRankReranker,
    NoopReranker,
    Reranker,
)
from fenris.retrieve.route import POLICY_VERSION, RERANK_FLOOR, RouteDecision, route

__all__ = [
    "FLASHRANK_MODEL",
    "POLICY_VERSION",
    "RERANK_FLOOR",
    "Evidence",
    "EvidenceSet",
    "FlashRankReranker",
    "NoopReranker",
    "Reranker",
    "RouteDecision",
    "build_evidence",
    "build_evidence_set",
    "embed_query",
    "evidence_id_for",
    "route",
    "search",
]
