"""BM25 candidate generation over FTS5 (porter + unicode61).

Convention: SQLite's ``bm25()`` is *negative, better = more negative*
(https://www.sqlite.org/fts5.html, "The bm25() function returns a negative
value ... better matches are assigned numerically lower scores"). This module
returns *positive* relevance scores (``score = -bm25()``) so every stage of
the pipeline is "higher is better":

    bm25.py        score = -bm25(chunks_fts)          (>= 0, higher better)
    knn.py         similarity = 1 - distance/2        (in [-1, 1], higher better)
    rrf.py         score = sum(1 / (60 + rank))       (small positive)
    rerank.py      sigmoid of CE logits, in [0, 1]    (higher better)

Candidates are restricted to live representations: ``sources.active = 1 AND
sources.trashed = 0`` (the vec0 mirror columns enforce the same during KNN).
"""

from __future__ import annotations

import sqlite3

from fenris.config import EMBED_DIMS  # noqa: F401  (re-export convenience)

# candidate overfetch width shared by BM25 and KNN before fusion (the build plan s.6)
OVERFETCH = 30

# every literal FTS5 phrase from the query; an empty/whitespace-only query
# matches nothing (an FTS5 MATCH of '' would be a syntax error).
_SQL = """
SELECT c.chunk_id, -bm25(chunks_fts) AS score
FROM chunks_fts
JOIN chunks c ON c.chunk_id = chunks_fts.rowid
JOIN sources s ON s.id = c.source_id
WHERE chunks_fts MATCH ? AND s.active = 1 AND s.trashed = 0
ORDER BY score DESC
LIMIT ?
"""

# TABLE-route variant: hybrid restricted to table chunks (is_table = 1).
_SQL_TABLE = """
SELECT c.chunk_id, -bm25(chunks_fts) AS score
FROM chunks_fts
JOIN chunks c ON c.chunk_id = chunks_fts.rowid
JOIN sources s ON s.id = c.source_id
WHERE chunks_fts MATCH ? AND s.active = 1 AND s.trashed = 0 AND c.is_table = 1
ORDER BY score DESC
LIMIT ?
"""


def fts_query_string(query: str) -> str:
    """Build a safe FTS5 MATCH string: every token becomes a quoted phrase.

    Quoting every token makes odd input (``C++``, ``foo:bar``, ``2025-06-01``)
    a literal phrase lookup instead of an FTS5 syntax error; multiple tokens
    become an implicit-AND of phrases. An original double-quoted phrase
    (``"exact words"``) is kept as one multi-token phrase so the EXACT route
    can hit on a full-phrase match.
    """
    import re

    quoted = re.findall(r'"([^"]*)"', query)
    rest = re.sub(r'"[^"]*"', " ", query)
    parts: list[str] = []
    for phrase in quoted:
        parts.append('"' + phrase.replace('"', " ") + '"')
    for tok in re.findall(r"[A-Za-z0-9_]+", rest):
        parts.append(f'"{tok}"')
    if not parts:
        return ""
    return " ".join(parts)


def bm25_search(conn: sqlite3.Connection, query: str, table_only: bool = False,
                limit: int = OVERFETCH) -> list[tuple[int, float]]:
    """FTS5 BM25 search joined to live sources.

    Returns ``[(chunk_id, score)]`` ordered best-first, ``score = -bm25()``
    (positive, higher better). Empty/whitespace query, or no FTS hits -> ``[]``;
    never raises for those cases. LIMIT overfetches 30 candidates.
    """
    match = fts_query_string(query)
    if not match:
        return []
    sql = _SQL_TABLE if table_only else _SQL
    try:
        rows = conn.execute(sql, (match, limit)).fetchall()
    except sqlite3.OperationalError:
        # FTS5 syntax the quoting cannot save (shouldn't happen) or the
        # chunks_fts table missing: lexical retrieval degrades to no hits.
        return []
    return [(int(cid), float(score)) for cid, score in rows]
