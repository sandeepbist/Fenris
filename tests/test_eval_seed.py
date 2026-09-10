"""Golden-corpus seeding + offline eval seams.

The eval command must run 100% offline on a fresh machine (CI): the
corpus seeds through the real ingest pipeline (FakeEmbedder doc-side by
default), queries use FENRIS_EMBEDDER=fake, reranking is skippable via
FENRIS_RERANKER=noop. These tests pin all three seams.
"""

from __future__ import annotations

import pytest

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.eval.seed import seed_golden_corpus
from fenris.retrieve.rerank import NoopReranker, resolve_reranker


@pytest.fixture()
def conn(tmp_path):
    c = connect(FenrisConfig(data_dir=tmp_path / "home").db_path)
    yield c
    c.close()


class TestSeedGoldenCorpus:
    def test_seeds_all_docs_through_the_real_pipeline(self, conn):
        """99 sources, one active representation each, chunks + FTS5 rows
        + vec0 rows all written by ingest_file's own transaction."""
        n_docs, families = seed_golden_corpus(conn)
        assert n_docs == 99
        assert len(families) == 6
        assert conn.execute(
            "SELECT COUNT(DISTINCT file_id) FROM sources WHERE active=1"
        ).fetchone()[0] == 99
        assert conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 99
        # FTS5 trigger fired: a verbatim phrase finds its invoice
        hits = conn.execute(
            "SELECT COUNT(*) FROM chunks_fts WHERE chunks_fts MATCH ?",
            ('"inv 202301 323"',)).fetchone()[0]
        assert hits >= 1
        # vec0 rows exist (sqlite-vec loaded, FakeEmbedder 1536-dim blobs)
        assert conn.execute("SELECT COUNT(*) FROM chunks_vec").fetchone()[0] > 0
        # deterministic embedder identity recorded on every source
        models = conn.execute(
            "SELECT DISTINCT embedding_model FROM sources").fetchall()
        assert models == [("fake-fenris:1536",)]

    def test_seeding_is_idempotent(self, conn):
        """Re-running on a seeded DB must not duplicate representations:
        same source_revision -> unchanged path, still exactly 99 active
        sources, zero retired ones."""
        seed_golden_corpus(conn)
        seed_golden_corpus(conn)
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE active=1").fetchone()[0] == 99
        assert conn.execute(
            "SELECT COUNT(*) FROM sources WHERE active=0").fetchone()[0] == 0

    def test_seeded_corpus_answers_exact_questions(self, conn):
        """The point of seeding: a verbatim identifier question retrieves
        its document through the real retrieval stack, offline."""
        seed_golden_corpus(conn)
        from fenris.ingest.embed import FakeEmbedder
        from fenris.retrieve.query import search

        emb = FakeEmbedder()
        q = "Find invoice INV-202301-323"
        qv = emb.embed_documents([f"task: search result | query: {q}"])[0]
        ev = search(conn, q, qv, reranker=NoopReranker())
        assert ev.evidence, "expected offline evidence"
        assert ev.evidence[0].file_id == "inv-000"


class TestOfflineRerankerSeam:
    def test_noop_is_a_first_class_choice(self, monkeypatch):
        """FENRIS_RERANKER=noop must actually noop. The earlier ladder
        silently fell through to FlashRank on ANY unknown value - which
        downloads ~34MB of weights on first rerank, breaking hermetic CI."""
        monkeypatch.setenv("FENRIS_RERANKER", "noop")
        reranker, reason = resolve_reranker()
        assert isinstance(reranker, NoopReranker)
        assert reason == "noop-explicit"


class TestOfflineEmbedderSeam:
    def test_fake_embedder_env(self, monkeypatch):
        """FENRIS_EMBEDDER=fake returns the deterministic FakeEmbedder
        (the CI eval path); anything else constructs the Gemini client."""
        from fenris.ingest.embed import FakeEmbedder

        monkeypatch.setenv("FENRIS_EMBEDDER", "fake")
        import fenris.cli as cli_mod

        emb = cli_mod._make_embedder()
        assert isinstance(emb, FakeEmbedder)
        assert emb.model_id == "fake-fenris:1536"
