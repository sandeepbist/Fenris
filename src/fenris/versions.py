"""Versioned-document QA: what changed between revisions of a file.

The projection stores every representation of a file - retired rows
keep their chunks until purge. This module turns that history into
diffable evidence: chunk-level added/removed/unchanged sets between
two revisions, exposed via the API and answerable in chat.

The roadmap research found no shipped precedent for this feature;
Fenris's revision-aware identity is the raw material.

Every SQL statement below is a fixed string with ? placeholders;
values only ever travel through parameter tuples.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field


@dataclass
class RevisionInfo:
    source_id: int
    revision: str
    created_at: str | None
    active: bool
    chunk_count: int


@dataclass
class RevisionDiff:
    file_id: str
    title: str
    from_rev: RevisionInfo
    to_rev: RevisionInfo
    added: list[dict] = field(default_factory=list)
    removed: list[dict] = field(default_factory=list)
    unchanged_count: int = 0
    summary: str = ""


def file_revisions(conn: sqlite3.Connection, drive_id: str, file_id: str) -> list[RevisionInfo]:
    """Every indexed representation of a file, newest-first."""
    rows = conn.execute(
        "SELECT id, source_revision, created_at, active FROM sources"
        " WHERE drive_id = ? AND file_id = ? ORDER BY id DESC",
        (drive_id, file_id),
    ).fetchall()
    out: list[RevisionInfo] = []
    for source_id, revision, created_at, active in rows:
        count = conn.execute(
            "SELECT count(*) FROM chunks WHERE source_id = ?",
            (source_id,),
        ).fetchone()[0]
        out.append(RevisionInfo(source_id, revision, created_at, bool(active), count))
    return out


def revision_diff(conn: sqlite3.Connection, drive_id: str, file_id: str,
                  from_source_id: int, to_source_id: int) -> RevisionDiff | None:
    """Chunk-level diff between two indexed revisions of one file.

    Returns None when either id is not a representation of that file.
    """
    revs = {r.source_id: r for r in file_revisions(conn, drive_id, file_id)}
    if from_source_id not in revs or to_source_id not in revs:
        return None

    title_row = conn.execute(
        "SELECT title FROM sources WHERE id = ?",
        (to_source_id,),
    ).fetchone()
    from_chunks = _chunks_by_hash(conn, from_source_id)
    to_chunks = _chunks_by_hash(conn, to_source_id)

    added = sorted(
        (c for h, c in to_chunks.items() if h not in from_chunks),
        key=lambda c: c["ordinal"],
    )
    removed = sorted(
        (c for h, c in from_chunks.items() if h not in to_chunks),
        key=lambda c: c["ordinal"],
    )
    unchanged = len(set(from_chunks) & set(to_chunks))

    total = len(from_chunks) + len(to_chunks) or 1
    change_pct = int(100 * (len(added) + len(removed)) / total)
    if not added and not removed:
        summary = "No text changes between these revisions (identical content)."
    else:
        summary = (
            f"{len(added)} chunk(s) added, {len(removed)} removed, "
            f"{unchanged} unchanged (~{change_pct}% of text changed)."
        )

    return RevisionDiff(
        file_id=file_id,
        title=(title_row or [""])[0] or file_id,
        from_rev=revs[from_source_id],
        to_rev=revs[to_source_id],
        added=_trim(added),
        removed=_trim(removed),
        unchanged_count=unchanged,
        summary=summary,
    )


def _chunks_by_hash(conn: sqlite3.Connection, source_id: int) -> dict[str, dict]:
    """Stored chunks of one representation, keyed by content hash."""
    rows = conn.execute(
        "SELECT text_hash, header_text, body_text, ordinal, page_nos"
        " FROM chunks WHERE source_id = ?",
        (source_id,),
    ).fetchall()
    return {
        r[0]: {"hash": r[0], "header": r[1], "body": r[2],
               "ordinal": r[3], "pages": r[4]}
        for r in rows
    }


def _trim(chunks: list[dict]) -> list[dict]:
    return [
        {"header": c["header"], "body": c["body"][:400], "pages": c["pages"]}
        for c in chunks
    ]
