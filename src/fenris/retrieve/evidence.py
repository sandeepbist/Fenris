"""Evidence records: the citation frame (the build plan).

``evidence_id = sha256(source_revision || chunk_id || page_nos || char_start
|| char_end)[0:16]`` hex - the concatenation is over the raw field values
joined with no separator (the fields are positionally unambiguous in practice;
the concatenation order is the schema order above and is now frozen: any
change would re-key every citation).

Every record carries what the prompt and the citation renderer need:
file_id, source_revision, page(s), char span over the *stored* body_text
(the citation frame - byte offsets into original Drive files are deliberately
not an MVP field, see the build plan "Span/citation frame"), score,
auth_epoch, header, and a body excerpt.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass

# excerpt length for the evidence record body preview
EXCERPT_CHARS = 300

# selected columns for evidence assembly, keyed by name
_COLUMNS = (
    "c.chunk_id, c.page_nos, c.char_start, c.char_end, c.header_text, c.body_text,"
    " c.is_table, c.table_id, c.row_range, c.ordinal, c.section_path,"
    " s.file_id, s.source_revision, s.auth_epoch, s.title, s.path,"
    " s.modified_time, s.drive_id"
)


@dataclass(frozen=True)
class Evidence:
    """One retrievable, citable unit (a chunk of an active representation)."""

    evidence_id: str
    chunk_id: int
    file_id: str
    source_revision: str
    page_nos: list[int]
    header: str
    body_excerpt: str
    char_start: int | None
    char_end: int | None
    score: float
    auth_epoch: int
    is_table: bool = False
    table_id: int | None = None
    row_range: str | None = None
    ordinal: int | None = None
    section_path: str | None = None
    title: str | None = None
    path: str | None = None
    drive_id: str | None = None
    modified_time: str | None = None


def evidence_id_for(source_revision: str, chunk_id: int, page_nos, char_start,
                    char_end) -> str:
    """Deterministic 16-hex-char evidence id: sha256 over the concatenated
    identity fields, first 16 hex chars. Same inputs -> same id, always.

    Field encodings (frozen - any change re-keys every citation):
    ``source_revision`` as-is; ``chunk_id`` as its decimal string;
    ``page_nos`` as its stored JSON string when it is one, else
    json.dumps(list) for an iterable, else the empty string for NULL;
    ``char_start``/``char_end`` via str() (so NULL -> "None").
    Each field is preceded by a NUL delimiter (``\\x1f``, the ASCII unit
    separator) so concatenation is injective - naive ``f"{a}{b}"`` would
    collide e.g. (rev, chunk 1, span 12-3) with (rev, chunk 11, span 1-23).
    The MVP formula sha256(source_revision | chunk_id | page_nos |
    char_start | char_end)[:16] is preserved; the separator only fixes which
    byte boundary each field starts at.
    """
    import json

    if page_nos is None:
        pages = ""
    elif isinstance(page_nos, str):
        pages = page_nos
    else:
        pages = json.dumps(list(page_nos))
    payload = "\x1f".join((
        str(source_revision), str(chunk_id), pages, str(char_start), str(char_end)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[0:16]


def _pages_list(page_nos_json: str | None) -> list[int]:
    import json

    if not page_nos_json:
        return []
    try:
        data = json.loads(page_nos_json)
    except (TypeError, ValueError):
        return []
    if isinstance(data, list):
        return [int(p) for p in data]
    return [int(data)]


def build_evidence(conn: sqlite3.Connection, chunk_id: int, score: float) -> Evidence | None:
    """Assemble the Evidence record for one chunk from the DB.

    Returns None when the chunk does not resolve (deleted between candidate
    generation and assembly - the caller drops it) or the score is not a
    float-able number. The excerpt is the head of body_text; when
    char_start/char_end are present and point inside body_text, the excerpt is
    the *span* text (the citation frame makes the quote verifiable).
    """
    try:
        row = conn.execute(
            f"SELECT {_COLUMNS} FROM chunks c JOIN sources s ON s.id = c.source_id"
            f" WHERE c.chunk_id = ? AND s.active = 1 AND s.trashed = 0",
            (chunk_id,),
        ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    (cid, page_nos, char_start, char_end, header, body, is_table, table_id,
     row_range, ordinal, section_path, file_id, source_revision, auth_epoch,
     title, path, modified_time, drive_id) = row

    body = body or ""
    if char_start is not None and char_end is not None and 0 <= char_start <= char_end:
        span = body[char_start:char_end]
        excerpt = span if span else body[:EXCERPT_CHARS]
    else:
        excerpt = body[:EXCERPT_CHARS]

    return Evidence(
        evidence_id=evidence_id_for(source_revision, cid, page_nos, char_start, char_end),
        chunk_id=int(cid),
        file_id=file_id,
        source_revision=source_revision,
        page_nos=_pages_list(page_nos),
        header=header or "",
        body_excerpt=excerpt,
        char_start=char_start,
        char_end=char_end,
        score=float(score),
        auth_epoch=int(auth_epoch or 0),
        is_table=bool(is_table),
        table_id=table_id,
        row_range=row_range,
        ordinal=ordinal,
        section_path=section_path,
        title=title,
        path=path,
        drive_id=drive_id,
        modified_time=modified_time,
    )


def build_evidence_set(conn: sqlite3.Connection,
                       ranked: list[tuple[int, float]]) -> list[Evidence]:
    """Build Evidence records for a best-first [(chunk_id, score)] list,
    preserving order and dropping unresolvable ids."""
    out: list[Evidence] = []
    for chunk_id, score in ranked:
        ev = build_evidence(conn, chunk_id, score)
        if ev is not None:
            out.append(ev)
    return out
