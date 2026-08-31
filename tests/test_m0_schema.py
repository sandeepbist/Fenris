"""M0 smoke tests: schema applies, FTS5 triggers fire, vec0 supports the
filtered-KNN lifecycle, the one-active-representation invariant holds.

These are the exit criteria of the build plan milestone M0 - if these fail, nothing
built on top can be trusted.
"""

from __future__ import annotations

import json
import struct

import pytest

from fenris.config import EMBED_DIMS, FenrisConfig
from fenris.db import connect


@pytest.fixture()
def cfg(tmp_path) -> FenrisConfig:
    return FenrisConfig(data_dir=tmp_path / "fenris-home")


def _insert_source(conn, file_id="f1", revision="abc123", active=1) -> int:
    cur = conn.execute(
        """INSERT INTO sources(drive_id, file_id, source_revision, repr_mime,
             parser_version, chunker_version, embedding_model, embed_dims, active)
           VALUES ('mydrive', ?, ?, 'application/pdf', 'docling-1',
                   'recursive512-v1', 'gemini-embedding-001', 1536, ?)""",
        (file_id, revision, active),
    )
    return cur.lastrowid


def _insert_chunk(conn, source_id, ordinal, header, body) -> int:
    cur = conn.execute(
        """INSERT INTO chunks(source_id, ordinal, header_text, body_text, text_hash)
           VALUES (?, ?, ?, ?, ?)""",
        (source_id, ordinal, header, body, f"hash-{source_id}-{ordinal}"),
    )
    return cur.lastrowid


def test_schema_applies_and_is_idempotent(cfg):
    conn = connect(cfg.db_path)
    try:
        # core tables exist
        for table in ("sources", "chunks", "tables", "term_df", "drive_files",
                      "sync_cursor", "source_artifacts", "eval_runs",
                      "eval_case_results", "chunks_fts"):
            assert conn.execute(
                "SELECT name FROM sqlite_master WHERE name=?", (table,)
            ).fetchone(), f"missing {table}"
        # re-run is safe
        from fenris.schema import apply_schema

        apply_schema(conn)
        assert conn.execute("SELECT version FROM schema_version").fetchone()[0] == 1
    finally:
        conn.close()


def test_fts5_triggers_keep_index_in_sync(cfg):
    """The M0 blocker from the red-team review: without triggers the FTS index
    stays empty and BM25 silently returns nothing."""
    conn = connect(cfg.db_path)
    try:
        src = _insert_source(conn)
        cid = _insert_chunk(conn, src, 1, "Invoice 2025 / Finance",
                            "total amount due was 412.50 dollars")
        # trigger fired on INSERT
        rows = conn.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'invoice'"
        ).fetchall()
        assert [r[0] for r in rows] == [cid]
        # trigger fires on DELETE (external-content 'delete' command)
        conn.execute("DELETE FROM chunks WHERE chunk_id=?", (cid,))
        assert conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'invoice'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_fts5_bm25_ranks(cfg):
    conn = connect(cfg.db_path)
    try:
        src = _insert_source(conn)
        _insert_chunk(conn, src, 1, "doc", "apple banana cherry")
        _insert_chunk(conn, src, 2, "doc", "banana banana banana banana")
        scores = conn.execute(
            "SELECT rowid, bm25(chunks_fts) FROM chunks_fts WHERE chunks_fts MATCH 'banana'"
            " ORDER BY bm25(chunks_fts)"
        ).fetchall()
        # bm25() is negative; the banana-dense chunk should rank first
        assert scores[0][0] == 2
    finally:
        conn.close()


def test_vec0_lifecycle_and_filtered_knn(cfg):
    """vec0 INSERT/DELETE/UPDATE(metadata) + filtered KNN - the tombstone path."""
    conn = connect(cfg.db_path)
    try:
        has_vec = conn.execute(
            "SELECT name FROM sqlite_master WHERE name='chunks_vec'"
        ).fetchone()
        if not has_vec:
            pytest.skip("sqlite-vec not available in this environment")

        def emb(*v):
            vec = list(v) + [0.0] * (EMBED_DIMS - len(v))
            return struct.pack(f"{len(vec)}f", *vec)

        conn.execute(
            "INSERT INTO chunks_vec(chunk_id, embedding, active, trashed, auth_epoch)"
            " VALUES (1, ?, 1, 0, 0)", (emb(0.1, 0.2),))
        conn.execute(
            "INSERT INTO chunks_vec(chunk_id, embedding, active, trashed, auth_epoch)"
            " VALUES (2, ?, 1, 0, 0)", (emb(0.9, 0.9),))
        # tombstone by metadata update: KNN must exclude retired chunk
        conn.execute("UPDATE chunks_vec SET active=0 WHERE chunk_id=2")
        rows = conn.execute(
            "SELECT chunk_id, distance FROM chunks_vec"
            " WHERE embedding MATCH ? AND k = 10 AND active = 1 AND trashed = 0"
            " AND auth_epoch = 0",
            (emb(0.1, 0.2),),
        ).fetchall()
        assert [r[0] for r in rows] == [1]
        # delete + reinsert works (revision swap path)
        conn.execute("DELETE FROM chunks_vec WHERE chunk_id=1")
        conn.execute(
            "INSERT INTO chunks_vec(chunk_id, embedding, active, trashed, auth_epoch)"
            " VALUES (1, ?, 1, 0, 0)", (emb(0.1, 0.2),))
        rows = conn.execute(
            "SELECT chunk_id FROM chunks_vec WHERE embedding MATCH ? AND k = 10"
            " AND active = 1", (emb(0.1, 0.2),)).fetchall()
        assert [r[0] for r in rows] == [1]
    finally:
        conn.close()


def test_one_active_representation_invariant(cfg):
    """The partial unique index must refuse a second active representation."""
    conn = connect(cfg.db_path)
    try:
        _insert_source(conn, file_id="f1", revision="r1")
        with pytest.raises(Exception):
            _insert_source(conn, file_id="f1", revision="r2")
        # retiring the first allows the second
        conn.execute("UPDATE sources SET active=0 WHERE source_revision='r1'")
        _insert_source(conn, file_id="f1", revision="r2")
    finally:
        conn.close()


def test_chunks_cascade_and_fts_cleanup(cfg):
    """Deleting a source (ON DELETE CASCADE) removes chunks; the FTS trigger
    fires on those deletes so the lexical index never orphans rows."""
    conn = connect(cfg.db_path)
    try:
        src = _insert_source(conn)
        cid = _insert_chunk(conn, src, 1, "h", "zebra quarantine text")
        assert conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'quarantine'"
        ).fetchone()[0] == 1
        conn.execute("DELETE FROM sources WHERE id=?", (src,))
        assert conn.execute("SELECT count(*) FROM chunks").fetchone()[0] == 0
        assert conn.execute(
            "SELECT count(*) FROM chunks_fts WHERE chunks_fts MATCH 'quarantine'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_eval_tables_keyed(cfg):
    conn = connect(cfg.db_path)
    try:
        cur = conn.execute(
            "INSERT INTO eval_runs(started_at, git_sha, flag_config, generation, verdict)"
            " VALUES ('t', 'sha', '{}', 'g1', 'pass')")
        run_id = cur.lastrowid
        conn.execute(
            "INSERT INTO eval_case_results(run_id, case_id, suite) VALUES (?,?,?)",
            (run_id, "g001", "golden"))
        with pytest.raises(Exception):
            conn.execute(
                "INSERT INTO eval_case_results(run_id, case_id, suite) VALUES (?,?,?)",
                (run_id, "g001", "golden"))
    finally:
        conn.close()
