"""Related files: what else in the corpus relates to this document.

Two signals, fused - both computed from the local projection, offline:

1. CO-CITATION: files that retrieved together with this one in past
   answers (the turns table stores each answer's citations). Real
   usage beats static similarity - these files were jointly relevant
   to actual questions.
2. VECTOR AFFINITY: mean-vector cosine between active files. A
   file's centroid is the mean of its chunk vectors; nearest
   centroids are topically related.

Signals fuse by reciprocal rank (k=60, the engine's standard).
Every statement is a flat SELECT with ? placeholders - no joins, no
subqueries, no literals: file_id equality is the only predicate.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass

EMPTY_CITATIONS = "[]"


@dataclass
class RelatedFile:
    file_id: str
    title: str
    reason: str          # "co-cited" | "similar" | "co-cited+similar"
    score: float


def related_files(conn, file_id: str, limit: int = 8) -> list[RelatedFile]:
    """Neighbors of one file by co-citation rank-fused with vector affinity.

    Never raises on empty state: no history and no vectors -> [].
    """
    co_ranks = _co_cited_ranks(conn, file_id)
    vec_ranks = _vector_affinity_ranks(conn, file_id)

    scores: dict[str, float] = {}
    for ranks in (co_ranks, vec_ranks):
        for rank, fid in enumerate(ranks, start=1):
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (60 + rank)

    in_co = set(co_ranks)
    in_vec = set(vec_ranks)
    out: list[RelatedFile] = []
    for fid, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if fid == file_id:
            continue
        if fid in in_co and fid in in_vec:
            reason = "co-cited+similar"
        elif fid in in_co:
            reason = "co-cited"
        else:
            reason = "similar"
        out.append(RelatedFile(
            file_id=fid,
            title=_title_for(conn, fid),
            reason=reason,
            score=round(score, 4),
        ))
        if len(out) >= limit:
            break
    return out


def _active_source_id(conn, file_id: str):
    """The newest active source id for a file, or None."""
    row = conn.execute(
        "SELECT id FROM sources WHERE file_id = ? AND active = 1"
        " ORDER BY id DESC LIMIT 1",
        (file_id,),
    ).fetchone()
    return row[0] if row else None


def _co_cited_ranks(conn, file_id: str) -> list[str]:
    """Files co-cited with this one across stored answer turns."""
    source_id = _active_source_id(conn, file_id)
    if source_id is None:
        return []
    chunks = conn.execute(
        "SELECT count(*) FROM chunks WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    if not chunks or chunks[0] == 0:
        return []

    co: dict[str, int] = {}
    turns = conn.execute(
        "SELECT citations_json FROM turns ORDER BY id DESC LIMIT 200",
        (),
    ).fetchall()
    for (citations_json,) in turns:
        if not citations_json or citations_json == EMPTY_CITATIONS:
            continue
        try:
            citations = json.loads(citations_json)
        except (ValueError, TypeError):
            continue
        cited_files = {
            c.get("file_id") for c in citations
            if isinstance(c, dict) and c.get("file_id")
        }
        if file_id in cited_files:
            for other in cited_files:
                if other != file_id:
                    co[other] = co.get(other, 0) + 1
    return [fid for fid, _count in sorted(co.items(), key=lambda kv: -kv[1])]


def _vector_affinity_ranks(conn, file_id: str) -> list[str]:
    """Files with the nearest mean-vector centroids, from stored vec0 rows."""
    vec_by_file = _centroids(conn)
    target = vec_by_file.get(file_id)
    if target is None:
        return []

    sims = [
        (_dot_sparse(target, centroid), fid)
        for fid, centroid in vec_by_file.items()
        if fid != file_id
    ]
    sims.sort(reverse=True)
    return [fid for _s, fid in sims[:12]]


def _dot_sparse(a: dict[int, float], b: dict[int, float]) -> float:
    """Dot product over the smaller sparse dict."""
    if len(a) > len(b):
        a, b = b, a
    return sum(val * b.get(k, 0.0) for k, val in a.items())


def _unpack_fp32(blob: bytes) -> tuple:
    """Unpack a stored fp32 vector blob."""
    fmt = str(len(blob) // 4) + "f"
    return struct.unpack(fmt, blob)


def _centroids(conn) -> dict[str, dict[int, float]]:
    """Sparse mean vector per active file: {dim_index: mean_value}.

    Three flat passes (active sources, their chunk ids, vec rows by
    chunk) instead of one join - the projection is small and the
    statements stay trivially parameterized.
    """
    vec_by_file: dict[str, dict[int, float]] = {}
    counts: dict[str, int] = {}
    sources = conn.execute(
        "SELECT id, file_id FROM sources WHERE active = 1",
        (),
    ).fetchall()
    for source_id, file_id in sources:
        chunk_rows = conn.execute(
            "SELECT chunk_id FROM chunks WHERE source_id = ?",
            (source_id,),
        ).fetchall()
        for (chunk_id,) in chunk_rows:
            vec_row = conn.execute(
                "SELECT embedding FROM chunks_vec WHERE chunk_id = ?"
                " AND active = 1",
                (chunk_id,),
            ).fetchone()
            if not vec_row or not vec_row[0]:
                continue
            dims = _unpack_fp32(vec_row[0])
            acc = vec_by_file.setdefault(file_id, {})
            for idx, val in enumerate(dims):
                if val != 0.0:
                    acc[idx] = acc.get(idx, 0.0) + val
            counts[file_id] = counts.get(file_id, 0) + 1
    for fid, acc in vec_by_file.items():
        n = counts.get(fid, 1)
        for k in acc:
            acc[k] /= n
    return vec_by_file


def _title_for(conn, file_id: str) -> str:
    """Title lookup: sources first, drive_files fallback."""
    row = conn.execute(
        "SELECT title FROM sources WHERE file_id = ? AND active = 1 LIMIT 1",
        (file_id,),
    ).fetchone()
    if row and row[0]:
        return row[0]
    row = conn.execute(
        "SELECT title FROM drive_files WHERE file_id = ? LIMIT 1",
        (file_id,),
    ).fetchone()
    return (row[0] if row and row[0] else None) or file_id
