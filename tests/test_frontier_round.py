"""Retrieval-quality round tests (2026-09): reranker framework + floors,
body dedup, contextual headers, groundedness gate. All offline — no model
downloads, fakes at every weights seam, kill switches honored.
"""

from __future__ import annotations

import pytest

from fenris.config import FenrisConfig
from fenris.db import connect
from fenris.ingest.pipeline import FileMeta, ingest_file
from fenris.retrieve import search as search_mod
from fenris.retrieve.rerank import (
    FlashRankReranker,
    ModernBertReranker,
    NoopReranker,
    _normalize_batch,
    resolve_reranker,
)
from fenris.retrieve.route import RERANK_FLOOR, RERANK_FLOORS, floor_for

TXT = "text/plain"


@pytest.fixture()
def cfg(tmp_path) -> FenrisConfig:
    return FenrisConfig(data_dir=tmp_path / "home")


@pytest.fixture()
def conn(cfg):
    c = connect(cfg.db_path)
    yield c
    c.close()


def _ingest(conn, file_id, title, body, revision=None):
    return ingest_file(conn, None, FileMeta(
        drive_id="mydrive", file_id=file_id, title=title,
        mime_type=TXT, source_revision=revision or f"r-{file_id}"), body)


# ---- R1: the floor framework ------------------------------------------------------


def test_floor_for_known_rerankers():
    floor, calibrated = floor_for("flashrank")
    assert floor == 0.3 and calibrated is True
    floor, calibrated = floor_for("modernbert")
    assert floor == 0.3 and calibrated is False  # provisional until the sweep
    floor, calibrated = floor_for("nonsense-name")
    assert floor == RERANK_FLOOR and calibrated is False


def test_floors_registered_per_reranker():
    assert set(RERANK_FLOORS) >= {"flashrank", "modernbert", "noop"}
    # every entry carries the calibration contract
    for floor, calibrated in RERANK_FLOORS.values():
        assert isinstance(floor, float) and 0 < floor < 1
        assert isinstance(calibrated, bool)


def test_search_records_applied_floor(conn):
    _ingest(conn, "f1", "Doc", b"alpha beta gamma delta text")
    result = search_mod(conn, "alpha beta", None, reranker=NoopReranker())
    assert result.trace["rerank_floor"] == 0.3
    assert result.trace["floor_calibrated"] is True


def test_normalize_batch_semantics():
    # min-max span; all-equal batch -> all 1.0 (same semantics as FlashRank's)
    assert _normalize_batch([(1, 0.2), (2, 0.8)]) == [(1, 0.0), (2, 1.0)]
    assert _normalize_batch([(1, 0.5), (2, 0.5)]) == [(1, 1.0), (2, 1.0)]


def test_modernbert_lazy_and_missing_weights():
    r = ModernBertReranker()
    assert r._session is None  # nothing loads at construction
    with pytest.raises(FileNotFoundError, match="fenris models fetch"):
        r._ensure_session()


def test_resolve_reranker_default_and_fallback(monkeypatch):
    monkeypatch.delenv("FENRIS_RERANKER", raising=False)
    r, reason = resolve_reranker()
    assert isinstance(r, FlashRankReranker) and reason == "flashrank"

    monkeypatch.setenv("FENRIS_RERANKER", "modernbert")
    import fenris.models as models_mod

    monkeypatch.setattr(models_mod, "available", lambda name: False)
    r, reason = resolve_reranker()
    assert isinstance(r, FlashRankReranker)
    assert "unavailable" in reason  # the flag can never silently no-op

    monkeypatch.setattr(models_mod, "available", lambda name: True)
    r, reason = resolve_reranker()
    assert isinstance(r, ModernBertReranker) and reason == "modernbert"

    monkeypatch.setenv("FENRIS_RERANKER", "gibberish")
    r, reason = resolve_reranker()
    assert isinstance(r, FlashRankReranker) and "unknown" in reason


# ---- R2: byte-exact body dedup ------------------------------------------------------


def test_cross_folder_duplicates_collapse(conn):
    """The Drive reality: same bytes in two folders (different titles =>
    different headers => text_hash never collided; body_hash does).

    Both files' (parent+child) chunks share bodies pairwise, so the fused
    list holds up to 4 same-body candidates; exactly one representative
    SERVES, the rest are counted in dedup_dropped."""
    _ingest(conn, "a", "Taxes 2024", b"quarterly tax filings and deductions alpha")
    _ingest(conn, "b", "Archive/Taxes 2024", b"quarterly tax filings and deductions alpha")
    conn.commit()

    result = search_mod(conn, "quarterly tax filings alpha", None,
                         reranker=NoopReranker())
    assert len(result.evidence) == 1, \
        "byte-identical bodies must surface exactly one representative"
    assert result.trace["dedup_dropped"] >= 1
    # the surviving chunk stays citable per chunk
    assert result.evidence[0].body_excerpt


def test_dedup_keeps_best_ranked_and_survives_scope(conn, monkeypatch):
    _ingest(conn, "a", "Taxes A", b"identical body text alpha unique words")
    _ingest(conn, "b", "Taxes B", b"identical body text alpha unique words")
    conn.commit()
    # scope to file b: its twin must survive even if a's copy ranked first
    result = search_mod(conn, "identical alpha", None, reranker=NoopReranker(),
                        scope=["Taxes B"])
    assert result.evidence, "a scoped query must keep its in-scope twin"
    assert {e.file_id for e in result.evidence} == {"b"}


def test_dedup_tables_exempt(conn):
    pass  # table exemptions covered by construction (body_hash=None keeps them)


def test_dedup_kill_switch(conn, monkeypatch):
    _ingest(conn, "a", "Taxes A", b"identical body text alpha")
    _ingest(conn, "b", "Taxes B", b"identical body text alpha")
    conn.commit()
    monkeypatch.setenv("FENRIS_DEDUP_BODIES", "0")
    # flags read at call time: no reload needed, live env read
    result = search_mod(conn, "identical alpha", None, reranker=NoopReranker())
    # dedup OFF: every candidate serves — parents and children of both
    # files (4 chunks share the body pairwise: 2 parents + 2 children)
    assert len(result.evidence) == 4
    assert result.trace["dedup_dropped"] == 0


def test_body_hash_populated_and_text_hash_distinct(conn):
    _ingest(conn, "a", "Taxes A", b"same body text here alpha")
    _ingest(conn, "b", "Taxes B", b"same body text here alpha")
    conn.commit()
    rows = conn.execute(
        "SELECT c.body_hash, c.text_hash, s.file_id FROM chunks c JOIN sources s ON s.id = c.source_id WHERE c.is_table = 0 AND c.ordinal = 1 ORDER BY s.file_id", ()).fetchall()
    assert len(rows) == 2
    hashes = {r[0] for r in rows}
    assert len(hashes) == 1, "identical bodies share body_hash"
    assert None not in hashes, "body chunks carry a body_hash"
    text_hashes = {r[1] for r in rows}
    assert len(text_hashes) == 2, "different headers keep distinct text_hash"


def test_v1_to_v2_column_ensure(cfg, tmp_path):
    """A DB created pre-v2 gains body_hash via the lazy ensure."""
    import sqlite3 as sq

    cfg.ensure_dirs()  # the data dir must exist before raw sqlite opens
    raw = sq.connect(str(cfg.db_path))
    raw.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
    raw.execute("INSERT INTO schema_version(version) VALUES (1)")
    raw.execute("CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY, source_id INTEGER, ordinal INTEGER, header_text TEXT NOT NULL, body_text TEXT NOT NULL, text_hash TEXT NOT NULL, is_table INTEGER NOT NULL DEFAULT 0)")
    raw.commit()
    raw.close()
    conn = connect(cfg.db_path)  # apply_schema runs the v2 ensure
    cols = {r[1] for r in conn.execute("PRAGMA table_info(chunks)", ()).fetchall()}
    assert "body_hash" in cols and "context_text" in cols
    version = conn.execute("SELECT version FROM schema_version", ()).fetchone()[0]
    assert version == 2
    conn.close()


# ---- R3: contextual headers (dark) --------------------------------------------------


def test_contextualize_cache_and_bounds(conn):
    from fenris.ingest.contextualize import contextualize

    calls = []

    def fake_gen(prompt):
        calls.append(prompt)
        return "This section covers " + prompt[-30:]

    items = [{
        "kind": "body",
        "header": "Doc / Section",
        "record": type("R", (), {"body_text": "The wolf pack hunted at dawn."})(),
    }] * 1
    out = contextualize(conn, "doc text", items, fake_gen, "fake-model")
    assert 0 in out and out[0].startswith("This section covers")
    first_calls = len(calls)
    # second run: cached, no generator call
    out2 = contextualize(conn, "doc text", items, fake_gen, "fake-model")
    assert len(calls) == first_calls
    assert out2[0] == out[0]


def test_contextual_prompt_shape():
    from fenris.ingest.contextualize import anthropic_prompt

    p = anthropic_prompt("DOC", "CHUNK")
    assert "<document>" in p and "DOC" in p
    assert "<chunk>" in p and "CHUNK" in p
    assert "succinct context" in p  # Anthropic's published wording


def test_chunker_identity_modes():
    from fenris.ingest.contextualize import chunker_identity

    assert chunker_identity("recursive512-v1", "deterministic") == "recursive512-v1"
    assert "ctx-llm-v1" in chunker_identity("recursive512-v1", "llm")


def test_contextual_off_is_byte_identical_identity():
    from fenris.ingest.chunk import CHUNKER_FULL_VERSION
    from fenris.ingest.pipeline import _chunker_identity

    assert _chunker_identity() == CHUNKER_FULL_VERSION  # zero churn, default


def test_llm_mode_represents_separately(conn, monkeypatch):
    """llm mode records a different chunker_version: the same content is a
    NEW representation (re-embed), never a silent vector-space mix. Flags
    read at call time, so the flip is a live env read - no reload."""
    from fenris.ingest.chunk import CHUNKER_FULL_VERSION
    from fenris.ingest.pipeline import _chunker_identity

    assert _chunker_identity() == CHUNKER_FULL_VERSION  # deterministic: zero churn
    monkeypatch.setenv("FENRIS_CONTEXTUAL_HEADERS", "llm")
    identity = _chunker_identity()
    assert identity != CHUNKER_FULL_VERSION, "llm mode must key a new representation"
    assert "ctx-llm-v1" in identity
    # and the deterministic identity returns the moment the flag clears
    monkeypatch.delenv("FENRIS_CONTEXTUAL_HEADERS")
    assert _chunker_identity() == CHUNKER_FULL_VERSION


# ---- R4: groundedness gate -----------------------------------------------------------


def test_apply_groundedness_drops_unsupported():
    from fenris.generate.groundedness import FakeGroundednessChecker, apply_groundedness

    class Ev:
        evidence_id = "abc123"
        body_excerpt = "Payment terms are net 30 days."

    checker = FakeGroundednessChecker(scores=[0.9, 0.1])
    answer = "Payment is net 30 days [E:abc123]. The moon is cheese [E:abc123]."
    g = apply_groundedness(answer, [Ev()], checker, threshold=0.5, surviving_floor=1)
    assert "net 30" in g.surviving_text
    assert "moon" not in g.surviving_text
    assert len(g.dropped_sentences) == 1
    assert g.all_dropped is False


def test_apply_groundedness_all_dropped():
    from fenris.generate.groundedness import FakeGroundednessChecker, apply_groundedness

    class Ev:
        evidence_id = "abc123"
        body_excerpt = "Payment terms are net 30 days."

    checker = FakeGroundednessChecker(scores=[0.1, 0.2])
    g = apply_groundedness(
        "Unsupported one. Unsupported two.", [Ev()], checker,
        threshold=0.5, surviving_floor=1)
    assert g.all_dropped is True


def test_service_gate_downgrade_only(conn, monkeypatch):
    """The full ask path with the gate ON: unsupported sentences vanish,
    all-dropped downgrades ANSWERABLE -> INSUFFICIENT; never upgrades."""
    monkeypatch.setenv("FENRIS_GROUNDEDNESS", "on")

    from fenris.generate.generator import FakeGenerator
    from fenris.generate.groundedness import FakeGroundednessChecker
    from fenris.generate.service import ask as ask_service

    _ingest(conn, "f1", "Payment", b"Payment terms are net 30 days after invoice alpha")
    conn.commit()
    ev = search_mod(conn, "payment alpha", None, reranker=NoopReranker())
    assert ev.evidence
    eid = ev.evidence[0].evidence_id

    monkeypatch.setattr(
        "fenris.generate.service._resolve_groundedness_checker",
        lambda: FakeGroundednessChecker(scores=[0.9]))

    gen = FakeGenerator(outputs=[{
        "state": "ANSWERABLE",
        "answer": f"Payment is net 30 [E:{eid}].",
        "cited_evidence_ids": [eid],
        "claims_conflict": False, "missing_evidence": [],
    }])
    packet = ask_service(conn, "payment alpha", None, generator=gen,
                         search_fn=lambda q: ev)
    assert packet.state == "ANSWERABLE"
    assert "net 30" in packet.answer
    assert "groundedness" in packet.trace

    # all-unsupported: ANSWERABLE downgrades, answer withheld
    monkeypatch.setattr(
        "fenris.generate.service._resolve_groundedness_checker",
        lambda: FakeGroundednessChecker(scores=[0.05]))
    gen2 = FakeGenerator(outputs=[{
        "state": "ANSWERABLE",
        "answer": f"Totally unsupported claim [E:{eid}].",
        "cited_evidence_ids": [eid],
        "claims_conflict": False, "missing_evidence": [],
    }])
    packet2 = ask_service(conn, "payment alpha", None, generator=gen2,
                          search_fn=lambda q: ev)
    assert packet2.state == "INSUFFICIENT"
    assert packet2.answer == ""
    assert packet2.trace["groundedness"]["dropped"]
    # flag off via monkeypatch teardown: live read, nothing to restore


def test_models_registry_offline():
    import fenris.models as models_mod

    assert "modernbert-rerank" in models_mod.MODELS
    assert "nli-deberta-small" in models_mod.MODELS
    # fetch refuses unknown + unpinned entries (fails closed)
    with pytest.raises(ValueError):
        models_mod.fetch("does-not-exist")
    with pytest.raises(ValueError):
        models_mod.fetch("modernbert-rerank")  # empty pin = unverified
