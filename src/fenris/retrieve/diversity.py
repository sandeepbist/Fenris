"""Per-file diversity cap on the final evidence set (the build plan: at most 2 chunks
per file in the final evidence).

Drop-only: the surviving items keep their ranking order exactly; items beyond
the cap per ``file_id`` are simply removed. The mapping chunk -> file comes
from chunks JOIN sources at call time (the file identity is Drive's file_id,
not source id - one file has at most one active representation).
"""

from __future__ import annotations

import sqlite3

# max chunks per file_id in the final evidence set
DIVERSITY_MAX_PER_FILE = 2


def apply_diversity(conn: sqlite3.Connection, ranked: list[tuple[int, float]],
                    max_per_file: int = DIVERSITY_MAX_PER_FILE) -> list[tuple[int, float]]:
    """Keep at most ``max_per_file`` chunks per file_id, in the given order.

 ``ranked`` is a best-first ``[(chunk_id, score)]`` list. Returns the
 filtered list (order preserved, no re-scoring). Ids that no longer resolve
 to a live chunk row are dropped (defense in depth - evidence assembly
 re-guards anyway).
 """
    if not ranked:
        return []
    ids = [cid for cid, _s in ranked]
    try:
        rows = conn.execute(
            f"SELECT chunk_id, file_id FROM chunks c"
            f" JOIN sources s ON s.id = c.source_id"
            f" WHERE c.chunk_id IN ({','.join('?' * len(ids))})", ids).fetchall()
    except sqlite3.Error:
        return []
    file_of = {int(cid): fid for cid, fid in rows}
    out: list[tuple[int, float]] = []
    per_file: dict[str, int] = {}
    for cid, score in ranked:
        fid = file_of.get(cid)
        if fid is None:
            continue  # chunk vanished between stages; drop, never surface
        per_file[fid] = per_file.get(fid, 0) + 1
        if per_file[fid] <= max_per_file:
            out.append((cid, score))
    return out
