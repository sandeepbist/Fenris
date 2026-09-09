"""Dense KNN candidate generation over the vec0 virtual table.

**Similarity convention (documented):** sqlite-vec's vec0 distance for float
vectors is *linear* Euclidean L2 (verified empirically: identical unit vectors
-> 0.0, orthogonal -> 1.4142, opposite -> 2.0). Our embeddings are
L2-normalized (manual normalize, the build plan), and for unit vectors
``L2^2 = 2 - 2*cos(theta)``, so::

 similarity = 1 - distance^2 / 2 = cos(theta)

- distance 0 -> +1 (the query itself), sqrt(2) -> 0 (orthogonal),
 2 -> -1 (fully opposed). This is "1 - normalized distance" in its exact
 cosine form; higher = more similar, always in [-1, 1] for normalized inputs.
- The transform is strictly monotone in distance on [0, 2] (the whole range
 for unit vectors), so KNN ordering is preserved exactly. Distances above 2
 are only possible with non-normalized inputs; there the formula still
 decreases monotonically (it just leaves [-1, 1]), so ordering remains right.

KNN runs with the same live-representation filter as BM25 - active=1 AND
trashed=0 AND auth_epoch matching - pushed into the vec0 metadata columns so
the filter applies *during* the scan, not after (the build plan: "both mirrored
into vec0 metadata columns so the KNN filter enforces them during the scan -
verified"). A tombstoned/retired chunk therefore never surfaces.

An empty vec0 table (no vectors yet) returns []; a missing vec0 table (raw
DB, extension absent) also returns [] - the dense side degrades to no hits,
never raises.
"""

from __future__ import annotations

import sqlite3
import struct

from fenris.config import EMBED_DIMS

OVERFETCH = 30  # candidate width, matches bm25.OVERFETCH (the build plan)

_SQL = (
    "SELECT chunk_id, distance FROM chunks_vec"
    " WHERE embedding MATCH ? AND k = ? AND active = 1 AND trashed = 0"
    " AND auth_epoch = ?"
)


def pack_vector(coords: list[float] | tuple[float, ...] | bytes) -> bytes:
    """Pack a sparse/partial coordinate list into the 1536-dim fp32 blob vec0
 expects; zero-fills the tail. A bytes payload of the right size passes
 through unchanged."""
    if isinstance(coords, (bytes, bytearray)):
        if len(coords) != EMBED_DIMS * 4:
            raise ValueError(
                f"expected {EMBED_DIMS * 4}-byte fp32 blob, got {len(coords)}")
        return bytes(coords)
    if len(coords) > EMBED_DIMS:
        raise ValueError(f"{len(coords)} coords exceed {EMBED_DIMS} dims")
    full = list(coords) + [0.0] * (EMBED_DIMS - len(coords))
    return struct.pack(f"<{EMBED_DIMS}f", *full)


def distance_to_similarity(distance: float) -> float:
    """vec0 Euclidean L2 distance -> cosine similarity for unit vectors.

 ``1 - distance**2 / 2``: 0 -> +1, sqrt(2) -> 0 (orthogonal), 2 -> -1.
 Strictly monotone in distance on [0, 2], so KNN ordering is preserved."""
    d = max(0.0, distance)
    return 1.0 - d * d / 2.0


def knn_search(conn: sqlite3.Connection, qvec: bytes | list[float], k: int = OVERFETCH,
               auth_epoch: int = 0) -> list[tuple[int, float]]:
    """vec0 filtered KNN.

 Returns ``[(chunk_id, similarity)]`` ordered most-similar-first using the
 convention above. ``qvec`` may be a 1536*4-byte fp32 blob (use
:func:`pack_vector` to build one from sparse coords) or a coordinate
 list/tuple, which is zero-filled and packed here.

 Never raises on an empty index (no vectors) or a missing vec0 table: both
 return []. SQLite errors mid-scan (corrupt vec0) also degrade to [].
 """
    blob = pack_vector(qvec) if not isinstance(qvec, bytes) else qvec
    if len(blob) != EMBED_DIMS * 4:
        # wrong dimensionality is a caller bug, not a degraded index
        raise ValueError(f"qvec must be {EMBED_DIMS} fp32 dims")
    try:
        rows = conn.execute(_SQL, (blob, k, auth_epoch)).fetchall()
    except sqlite3.OperationalError:
        # no chunks_vec table (fresh/raw DB, sqlite-vec absent) or vec0
        # misconfiguration: dense retrieval degrades to no hits.
        return []
    return [(int(cid), distance_to_similarity(float(dist))) for cid, dist in rows]
