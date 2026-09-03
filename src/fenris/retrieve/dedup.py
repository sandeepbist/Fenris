"""Byte-exact body dedup at the retrieval boundary (2026-09 round, R2).

The Drive reality: the same attachment lands in several folders. The
same bytes in different folders carry different titles/paths, so the
existing `text_hash = sha256(header+body)` — which correctly includes
the header — never collides for them; its cross-ingest embedding reuse
only catches same-header repeats. A body-only hash is the dedup key.

Design constraint (the citation contract): the chunk row must stay
queryable per chunk — `evidence_id` resolution walks `chunks` by
`chunk_id`, so dedup collapses CANDIDATE LISTS, never rows. Every
chunk keeps its chunks row, FTS row, and vec0 row. The wins that
survive: the rerank pool (30) and the evidence/prompt budget (K_MAX
25) stop being crowded by byte-identical bodies, and the (heavier)
reranker scores fewer duplicate passages.

Table row-blocks are excluded: a table hit cites `table_id` +
`tables.range` — collapsing file B's block would cite file A's table
region, a cross-file citation lie. Exclusion is the citation-safe
choice.

Every SQL statement is one single-line fixed string with ? placeholders.
"""

from __future__ import annotations

import hashlib
import sqlite3


def ensure_body_hash(conn: sqlite3.Connection) -> None:
    """Idempotent lazy column ensure (the ensure_hints pattern): adds
    chunks.body_hash to pre-v2 DBs. Silent no-op when present or the
    DB is read-only."""
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)", ()).fetchall()}
        if "body_hash" not in cols:
            conn.execute("ALTER TABLE chunks ADD COLUMN body_hash TEXT")
            conn.commit()
    except sqlite3.Error:
        pass  # read-only or concurrently migrated: callers degrade


def body_hash_of(body_text: str) -> str:
    """sha256(body) — the dedup key. Deliberately NOT text_hash (which
    correctly includes the header); cross-folder duplicates share bodies
    but not headers."""
    return hashlib.sha256((body_text or "").encode("utf-8")).hexdigest()


def collapse_identical_bodies(
    conn: sqlite3.Connection,
    candidates: list[tuple[int, float]],
) -> tuple[list[tuple[int, float]], int]:
    """Keep the best-ranked candidate per byte-identical body.

    Per-row lookups (not an IN-list): scanner-clean by construction and
    trivial at the fused pool's N (~<= 60). is_table=1 rows pass
    through untouched. Any sqlite3.Error (e.g. pre-migration DB without
    the column) degrades to a no-op — dedup must never break search.
    """
    if not candidates:
        return candidates, 0
    seen: set[str] = set()
    out: list[tuple[int, float]] = []
    dropped = 0
    for cid, score in candidates:
        try:
            row = conn.execute(
                "SELECT body_hash, is_table FROM chunks WHERE chunk_id = ?",
                (cid,),
            ).fetchone()
        except sqlite3.Error:
            return candidates, 0  # degrade: no dedup rather than a broken search
        if row is None or row[0] is None or row[1]:
            out.append((cid, score))  # no hash (pre-migration rows) or table: keep
            continue
        if row[0] in seen:
            dropped += 1
            continue
        seen.add(row[0])
        out.append((cid, score))
    return out, dropped
