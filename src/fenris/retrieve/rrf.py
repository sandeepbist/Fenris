"""RRF fusion of the BM25 and KNN candidate lists (the build plan).

``score(d) = sum over lists of 1 / (RRF_K + rank_d)`` with ``RRF_K = 60``
(fenris.config.RRF_K), rank starting at 1. A document found at rank 1 in both
lists scores 2/61 ~ 0.0328; the fusion scale is small by construction - which
is exactly why adaptive-k normalizes before applying the 0.15 elbow.

Ties break deterministically: higher fused score first, then the better
(best) single-list rank, then the smaller chunk_id.
"""

from __future__ import annotations

from fenris.config import RRF_K


def rrf_fuse(*ranked_lists: list[tuple[int, float]],
             k: int = RRF_K) -> list[tuple[int, float]]:
    """Fuse any number of ``[(chunk_id, score)]`` best-first lists.

 Only the *order* of each input list is used (RRF is rank-based; the raw
 BM25/KNN scores are not comparable across modalities). Returns
 ``[(chunk_id, fused_score)]`` best-first. Empty input lists -> fusion of
 what remains; all empty -> [].
 """
    best_rank: dict[int, int] = {}
    fused: dict[int, float] = {}
    for lst in ranked_lists:
        for rank, (chunk_id, _score) in enumerate(lst, start=1):
            fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (k + rank)
            if chunk_id not in best_rank or rank < best_rank[chunk_id]:
                best_rank[chunk_id] = rank
    ordered = sorted(
        fused.items(),
        key=lambda item: (-item[1], best_rank[item[0]], item[0]),
    )
    return [(chunk_id, score) for chunk_id, score in ordered]
