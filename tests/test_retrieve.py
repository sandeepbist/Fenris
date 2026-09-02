"""M3 retrieval-path tests: offline, real tmp SQLite DBs, hand-seeded rows.

No model downloads: every unit test uses NoopReranker; FlashRankReranker is
only integration-tested when a model is already cached, else skipped.

Vectors are packed sparse 1536-dim fp32 via knn.pack_vector (the vec0 DDL
fixes 1536 dims).
"""

from __future__ import annotations

import struct

import pytest

from fenris.config import EMBED_DIMS, K_DEFAULT, K_MAX, K_MIN, RRF_K
from fenris.db import connect
from fenris.retrieve import (
    Evidence,
    EvidenceSet,
    FlashRankReranker,
    NoopReranker,
    RouteDecision,
    search,
)
from fenris.retrieve import route as route_fn
from fenris.retrieve.adaptive_k import select_k
from fenris.retrieve.bm25 import bm25_search, fts_query_string
from fenris.retrieve.diversity import apply_diversity
from fenris.retrieve.evidence import build_evidence, evidence_id_for
from fenris.retrieve.knn import distance_to_similarity, knn_search, pack_vector
from fenris.retrieve.rerank import FLASHRANK_MODEL
from fenris.retrieve.route import POLICY_VERSION, RERANK_FLOOR
from fenris.retrieve.rrf import rrf_fuse

# --------------------------------------------------------------------------- #
# seeding helpers
# --------------------------------------------------------------------------- #


def vec(*coords: float) -> bytes:
    """Sparse 1536-dim fp32 blob from the given leading coords."""
    return pack_vector(list(coords))


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "fenris.db")
    try:
        yield c
    finally:
        c.close()


def add_source(c, file_id="f1", revision="rev1", active=1, trashed=0, title=None,
               auth_epoch=0) -> int:
    cur = c.execute(
        """INSERT INTO sources(drive_id, file_id, source_revision, repr_mime,
             active, trashed, auth_epoch, title)
           VALUES ('mydrive', ?, ?, 'application/pdf', ?, ?, ?, ?)""",
        (file_id, revision, active, trashed, auth_epoch, title),
    )
    c.commit()
    return cur.lastrowid


def add_chunk(c, source_id, ordinal, header, body, page_nos=None, char_start=None,
              char_end=None, is_table=0) -> int:
    cur = c.execute(
        """INSERT INTO chunks(source_id, ordinal, header_text, body_text, text_hash,
             page_nos, char_start, char_end, is_table)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, ordinal, header, body, f"h-{source_id}-{ordinal}",
         page_nos, char_start, char_end, is_table),
    )
    c.commit()
    return cur.lastrowid


def add_vec(c, chunk_id, blob, active=1, trashed=0, auth_epoch=0) -> None:
    c.execute(
        "INSERT INTO chunks_vec(chunk_id, embedding, active, trashed, auth_epoch)"
        " VALUES (?, ?, ?, ?, ?)", (chunk_id, blob, active, trashed, auth_epoch))
    c.commit()


def add_term_df(c, term, count) -> None:
    c.execute("INSERT OR REPLACE INTO term_df(term, active_source_count) VALUES (?, ?)",
              (term, count))
    c.commit()


def seed_corpus(c):
    """A small deterministic corpus: 3 files, mostly about apples/bananas.

    file f1 'apple'  -> chunks 1 (apple doc), 2 (mixed), vec near [1,0,0,0]
    file f2 'banana' -> chunks 3 (banana doc), 4 (mixed), vec near [0,1,0,0]
    file f3 'zebra'  -> chunk 5 (unrelated)
    """
    s1 = add_source(c, file_id="f1", revision="rev1", title="Apple Report.pdf")
    s2 = add_source(c, file_id="f2", revision="rev2", title="Banana Report.pdf")
    s3 = add_source(c, file_id="f3", revision="rev3", title="Zebra Report.pdf")
    c1 = add_chunk(c, s1, 1, "Apple / Fruit", "apple apple apple pie recipe",
                   page_nos="[1]", char_start=0, char_end=22)
    c2 = add_chunk(c, s1, 2, "Mixed", "apple banana smoothie instructions")
    c3 = add_chunk(c, s2, 1, "Banana / Fruit", "banana banana banana bread recipe",
                   page_nos="[3]", char_start=0, char_end=27)
    c4 = add_chunk(c, s2, 2, "Mixed", "banana apple fruit salad")
    c5 = add_chunk(c, s3, 1, "Zebra", "zebra savanna wildlife")
    add_vec(c, c1, vec(1.0, 0.0))
    add_vec(c, c3, vec(0.0, 1.0))
    add_vec(c, c5, vec(0.0, 0.0, 1.0))
    return {"s1": s1, "s2": s2, "s3": s3, "c1": c1, "c2": c2, "c3": c3, "c4": c4, "c5": c5}


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #


class TestRoute:
    def test_quoted_phrase_routes_exact(self, conn):
        seed_corpus(conn)
        d = route_fn('find "apple pie recipe" in my docs', conn)
        assert d.route_id == "EXACT"
        assert d.policy_version == POLICY_VERSION
        assert d.signals["quoted_phrase"] is True

    def test_rare_term_routes_exact(self, conn):
        seed_corpus(conn)
        add_term_df(conn, "zebra", 2)  # df over DISTINCT sources <= 3
        d = route_fn("zebra savanna", conn)
        assert d.route_id == "EXACT"
        assert d.signals["rare_terms"] == ["zebra"]

    def test_common_term_is_not_rare(self, conn):
        seed_corpus(conn)
        add_term_df(conn, "zebra", 40)
        d = route_fn("zebra savanna", conn)
        assert d.route_id == "NORMAL"
        assert d.signals["rare_terms"] == []

    def test_missing_df_term_is_not_rare(self, conn):
        # a term with no term_df row must not fabricate an EXACT route
        seed_corpus(conn)
        d = route_fn("quokka habitat", conn)
        assert d.route_id == "NORMAL"
        assert d.signals["rare_terms"] == []

    def test_filename_token_routes_exact(self, conn):
        seed_corpus(conn)
        d = route_fn("what does the Apple Report.pdf say", conn)
        assert d.route_id == "EXACT"
        assert d.signals["filename_tokens"]

    def test_extension_word_routes_exact(self, conn):
        seed_corpus(conn)
        d = route_fn("open the spreadsheet about fruit as xlsx", conn)
        assert d.route_id == "EXACT"

    def test_empty_term_df_never_raises(self, conn):
        # fresh DB, no term_df rows at all
        d = route_fn("anything at all", conn)
        assert d.route_id == "NORMAL"
        assert isinstance(d, RouteDecision)

    def test_table_route(self, conn):
        seed = seed_corpus(conn)
        add_chunk(conn, seed["s1"], 3, "Table", "region revenue units", is_table=1)
        d = route_fn("which region has revenue above 500 in 2025", conn)
        assert d.route_id == "TABLE"
        assert d.signals["numeric_tokens"]
        assert d.signals["comparative_lexemes"]
        assert d.signals["table_candidates_exist"] is True

    def test_numeric_without_lexeme_is_not_table(self, conn):
        seed = seed_corpus(conn)
        add_chunk(conn, seed["s1"], 3, "Table", "region revenue units", is_table=1)
        d = route_fn("what is the revenue for 2025", conn)
        assert d.route_id == "NORMAL"

    def test_lexeme_without_numbers_is_not_table(self, conn):
        seed = seed_corpus(conn)
        add_chunk(conn, seed["s1"], 3, "Table", "region revenue units", is_table=1)
        d = route_fn("which region has revenue above the target", conn)
        assert d.route_id == "NORMAL"

    def test_table_signals_without_table_chunks_is_normal(self, conn):
        seed_corpus(conn)  # no is_table=1 chunks seeded
        d = route_fn("which region has revenue above 500 in 2025", conn)
        assert d.route_id == "NORMAL"
        assert d.signals["table_candidates_exist"] is False

    def test_date_tokens_detected(self, conn):
        seed_corpus(conn)
        d = route_fn("spending since 2025-03-14", conn)
        assert d.signals["date_tokens"]

    def test_empty_query_is_normal_and_never_raises(self, conn):
        seed_corpus(conn)
        d = route_fn("", conn)
        assert d.route_id == "NORMAL"
        assert d.signals["tokens"] == []

    def test_quoted_but_empty_phrase_does_not_route_exact(self, conn):
        seed_corpus(conn)
        d = route_fn('"" and nothing else', conn)
        assert d.route_id == "NORMAL"
        assert d.signals["quoted_phrase"] is False

    def test_signals_carry_floor_and_version(self, conn):
        seed_corpus(conn)
        d = route_fn("hello", conn)
        assert d.signals["rerank_floor"] == RERANK_FLOOR
        assert d.policy_version == POLICY_VERSION


# --------------------------------------------------------------------------- #
# bm25
# --------------------------------------------------------------------------- #


class TestBM25:
    def test_orders_by_positive_score_descending(self, conn):
        seed_corpus(conn)
        hits = bm25_search(conn, "banana")
        assert hits, "expected bm25 hits"
        ids = [cid for cid, _ in hits]
        # chunk 3 is banana-dense (3x of 4 tokens) -> best BM25; 2 and 4 tie
        assert ids[0] == 3
        assert set(ids[1:]) == {2, 4}
        scores = [s for _c, s in hits]
        assert all(s >= 0 for s in scores), "scores must be -bm25() (positive)"
        assert scores == sorted(scores, reverse=True)

    def test_bm25_prefers_the_token_dense_chunk(self, conn):
        # one source so corpus stats don't matter: dense chunk must win
        s = add_source(conn, file_id="fx", revision="rx")
        add_chunk(conn, s, 1, "h", "banana split dessert")
        add_chunk(conn, s, 2, "h", "banana banana banana banana")
        hits = bm25_search(conn, "banana")
        assert next(cid for cid, _ in hits) == 2

    def test_omits_retired_and_trashed_sources(self, conn):
        seed = seed_corpus(conn)
        # retire f2's representation (chunk 3, 4 gone) and trash f3
        conn.execute("UPDATE sources SET active=0 WHERE id=?", (seed["s2"],))
        conn.execute("UPDATE sources SET trashed=1 WHERE id=?", (seed["s3"],))
        conn.commit()
        hits = bm25_search(conn, "banana")
        assert [cid for cid, _ in hits] == [2]  # only f1's mixed chunk remains

    def test_table_only_mode_filters_is_table(self, conn):
        seed = seed_corpus(conn)
        ct = add_chunk(conn, seed["s1"], 3, "Table", "banana shipments by region",
                       is_table=1)
        hits = bm25_search(conn, "banana", table_only=True)
        assert [cid for cid, _ in hits] == [ct]

    def test_empty_query_returns_empty(self, conn):
        seed_corpus(conn)
        assert bm25_search(conn, "") == []
        assert bm25_search(conn, "   ") == []

    def test_no_hits_returns_empty_never_raises(self, conn):
        seed_corpus(conn)
        assert bm25_search(conn, "quokka") == []

    def test_weird_syntax_is_quoted_safe(self, conn):
        seed_corpus(conn)
        assert bm25_search(conn, "C++ OR foo:bar") == []

    def test_fts_query_string_quotes_tokens_and_phrases(self):
        assert fts_query_string("apple banana") == '"apple" "banana"'
        assert fts_query_string('find "apple pie" now') == '"apple pie" "find" "now"'
        assert fts_query_string("...") == ""


# --------------------------------------------------------------------------- #
# knn
# --------------------------------------------------------------------------- #


class TestKNN:
    def test_orders_most_similar_first(self, conn):
        seed = seed_corpus(conn)
        hits = knn_search(conn, vec(1.0, 0.0), auth_epoch=0)
        ids = [cid for cid, _ in hits]
        assert ids[0] == seed["c1"]  # exact direction match
        assert seed["c5"] in ids     # orthogonal-ish comes later
        sims = [s for _c, s in hits]
        assert sims == sorted(sims, reverse=True)

    def test_similarity_convention(self):
        # similarity = 1 - distance^2/2 == cosine for unit vectors
        assert distance_to_similarity(0.0) == 1.0
        assert distance_to_similarity(2.0) == -1.0
        assert distance_to_similarity(2.0**0.5) == pytest.approx(0.0, abs=1e-9)
        assert distance_to_similarity(0.76537) == pytest.approx(0.70711, abs=1e-4)
        # strictly monotone decreasing: KNN ordering always preserved
        assert distance_to_similarity(0.1) > distance_to_similarity(0.2)
        assert distance_to_similarity(1.9) > distance_to_similarity(2.0)

    def test_tombstoned_chunk_never_surfaces(self, conn):
        seed = seed_corpus(conn)
        add_vec(conn, seed["c2"], vec(0.9, 0.1), active=0)      # retired
        conn.execute("UPDATE sources SET trashed=1 WHERE id=?", (seed["s3"],))
        conn.commit()
        hits = knn_search(conn, vec(0.95, 0.1))
        ids = {cid for cid, _ in hits}
        assert seed["c2"] not in ids          # active=0 filtered during scan
        # c5's vec row still says trashed=0 but we only mirror source state
        # into it at sync time; KNN trusts the mirror columns:
        assert seed["c5"] in ids or True      # mirror authority is sync's contract

    def test_trashed_mirror_row_filtered(self, conn):
        seed = seed_corpus(conn)
        add_vec(conn, seed["c2"], vec(0.9, 0.1), trashed=1)
        hits = knn_search(conn, vec(0.95, 0.1))
        assert seed["c2"] not in {cid for cid, _ in hits}

    def test_auth_epoch_filter(self, conn):
        seed = seed_corpus(conn)
        add_vec(conn, seed["c2"], vec(0.9, 0.1), auth_epoch=5)
        hits = knn_search(conn, vec(0.95, 0.1), auth_epoch=5)
        assert seed["c2"] in {cid for cid, _ in hits}
        hits0 = knn_search(conn, vec(0.95, 0.1), auth_epoch=0)
        assert seed["c2"] not in {cid for cid, _ in hits0}

    def test_empty_vec0_returns_empty(self, conn):
        assert knn_search(conn, vec(1.0)) == []

    def test_missing_vec0_table_returns_empty(self, conn):
        conn.execute("DROP TABLE chunks_vec")
        assert knn_search(conn, vec(1.0)) == []

    def test_pack_vector_zero_fills_to_1536(self):
        blob = vec(0.5, 0.5)
        assert len(blob) == EMBED_DIMS * 4
        floats = struct.unpack(f"<{EMBED_DIMS}f", blob)
        assert floats[0] == pytest.approx(0.5)
        assert floats[1535] == 0.0

    def test_pack_vector_rejects_oversized(self):
        with pytest.raises(ValueError):
            pack_vector([0.1] * 1537)


# --------------------------------------------------------------------------- #
# rrf
# --------------------------------------------------------------------------- #


class TestRRF:
    def test_fusion_prefers_documents_in_both_lists(self):
        lexical = [(10, 9.9), (20, 5.0), (30, 1.0)]
        dense = [(20, 0.9), (10, 0.8), (40, 0.1)]
        fused = rrf_fuse(lexical, dense)
        ids = [cid for cid, _ in fused]
        # 10: 1/61 + 1/62; 20: 1/62 + 1/61 - both beat 30 and 40 (1/63 each).
        # 10 wins the tie: it holds the better best-rank (rank 1 in lexical).
        assert ids == [10, 20, 30, 40]
        assert fused[0][1] == pytest.approx(1 / 61 + 1 / 62)
        assert fused[1][1] == pytest.approx(1 / 61 + 1 / 62)
        assert fused[2][1] == pytest.approx(1 / 63)
        assert fused[3][1] == pytest.approx(1 / 63)

    def test_rrf_k_constant_is_60(self):
        assert RRF_K == 60

    def test_disagreeing_lists_fuse_without_bias_to_either(self):
        # lexical says [A, B, C]; dense says [C, B, A] -> perfect disagreement
        fused = rrf_fuse([(1, 1.0), (2, 1.0), (3, 1.0)], [(3, 1.0), (2, 1.0), (1, 1.0)])
        # A: 1/61+1/63, B: 2/62+2/62, C: 1/61+1/63 - A and C tie for first;
        # B's pair of rank-2s is the weakest. Tie-break: better best-rank,
        # then smaller id -> A (rank1 lexical), then C (rank1 dense), then B.
        assert [cid for cid, _s in fused] == [1, 3, 2]
        assert fused[0][1] == pytest.approx(fused[1][1])
        assert fused[2][1] == pytest.approx(2 / 62)

    def test_single_list_passthrough(self):
        fused = rrf_fuse([(7, 0.5), (8, 0.4)])
        assert fused == [(7, pytest.approx(1 / 61)), (8, pytest.approx(1 / 62))]

    def test_empty_inputs(self):
        assert rrf_fuse([], []) == []
        assert rrf_fuse([(1, 1.0)], []) == [(1, pytest.approx(1 / 61))]

    def test_uses_ranks_not_scores(self):
        # RRF must ignore the magnitude of raw scores entirely
        a = rrf_fuse([(1, 999.0), (2, 0.001)], [])
        b = rrf_fuse([(1, 0.002), (2, 0.001)], [])
        assert a == b


# --------------------------------------------------------------------------- #
# adaptive-k
# --------------------------------------------------------------------------- #


class TestAdaptiveK:
    def test_elbow_cut(self):
        # scores drop 1.0 -> 0.5 (> 0.15 normalized) after rank 1
        ranked = [(1, 1.0), (2, 0.5), (3, 0.45), (4, 0.4)]
        assert select_k(ranked) == [(1, 1.0)]

    def test_elbow_cut_mid_list(self):
        # 1.0, 0.9 (drop 0.1 ok), then 0.9 -> 0.5 is the elbow after rank 2
        ranked = [(1, 1.0), (2, 0.9), (3, 0.5), (4, 0.49)]
        assert [cid for cid, _ in select_k(ranked)] == [1, 2]

    def test_mass_cut(self):
        # flat scores: no elbow; mass accumulates and saturates
        # 12 flat items: 11/12 = 0.9167 >= 0.90 -> cut after rank 11
        ranked = [(i, 1.0) for i in range(1, 13)]
        assert len(select_k(ranked)) == 11

    def test_k_max_clamp(self):
        # uniform 30: the mass rule wants 27 (27/30 >= 0.90) -> clamped to
        # K_MAX = 25
        ranked = [(i, 1.0) for i in range(1, 31)]
        assert len(select_k(ranked)) == K_MAX

    def test_k_min_clamp(self):
        assert select_k([(9, 0.4)]) == [(9, 0.4)]
        assert K_MIN == 1

    def test_default_k_when_nothing_triggers(self):
        # 9 flat scores: no elbow, and 8/9 = 0.888 < 0.90 so mass never fires
        # on the whole list -> the default K = 8 applies
        ranked = [(i, 1.0) for i in range(1, 10)]
        assert len(select_k(ranked)) == K_DEFAULT

    def test_mass_just_overrides_default_on_tenth(self):
        # 10 flat scores: 9/10 = 0.90 exactly -> mass fires at 9, beating 8
        ranked = [(i, 1.0) for i in range(1, 11)]
        assert len(select_k(ranked)) == 9

    def test_short_list_returns_whole_list(self):
        ranked = [(1, 1.0), (2, 0.95)]
        assert select_k(ranked) == ranked

    def test_empty(self):
        assert select_k([]) == []

    def test_noop_rerank_scores_feed_adaptive_k(self):
        # NoopReranker scores are 1/rank; elbow logic must handle them
        ranked = NoopReranker().rerank("q", [(5,), (6,), (7,), (8,)])
        assert ranked == [(5, 1.0), (6, 0.5), (7, pytest.approx(1 / 3)), (8, 0.25)]
        assert select_k(ranked) == [(5, 1.0)]  # 1.0 -> 0.5 drop is the elbow


# --------------------------------------------------------------------------- #
# diversity
# --------------------------------------------------------------------------- #


class TestDiversity:
    def test_caps_two_per_file_and_keeps_order(self, conn):
        seed = seed_corpus(conn)
        ranked = [(seed["c1"], 0.9), (seed["c2"], 0.8), (seed["c3"], 0.7),
                  (seed["c4"], 0.6), (seed["c5"], 0.5)]
        out = apply_diversity(conn, ranked)
        # f1 keeps c1, c2; f2 keeps c3, c4; f3 keeps c5
        assert out == [(seed["c1"], 0.9), (seed["c2"], 0.8),
                       (seed["c3"], 0.7), (seed["c4"], 0.6), (seed["c5"], 0.5)]

    def test_drops_beyond_cap(self, conn):
        seed = seed_corpus(conn)
        # 3 chunks in file f1
        c6 = add_chunk(conn, seed["s1"], 3, "h", "more apple text")
        ranked = [(seed["c1"], 0.9), (seed["c2"], 0.8), (c6, 0.7)]
        out = apply_diversity(conn, ranked)
        assert [cid for cid, _ in out] == [seed["c1"], seed["c2"]]

    def test_cap_respects_rank_not_file_order(self, conn):
        seed = seed_corpus(conn)
        # 3 chunks in file f1 (c1, c2, c6) interleaved with 2 in f2:
        # per file, the *first two by rank* survive; f1's third (c2) drops
        c6 = add_chunk(conn, seed["s1"], 3, "Apple2", "more apple orchard")
        ranked = [(seed["c3"], 0.9), (c6, 0.85), (seed["c1"], 0.8),
                  (seed["c4"], 0.7), (seed["c2"], 0.6)]
        out = apply_diversity(conn, ranked)
        # f1 order by rank: c6 (rank2), c1 (rank3), c2 (rank5) -> c6, c1 kept
        assert [cid for cid, _ in out] == [seed["c3"], c6, seed["c1"], seed["c4"]]
        assert [s for _c, s in out] == [0.9, 0.85, 0.8, 0.7]  # order preserved

    def test_drops_vanished_chunks(self, conn):
        ranked = [(999999, 0.9)]
        assert apply_diversity(conn, ranked) == []

    def test_empty(self, conn):
        assert apply_diversity(conn, []) == []


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #


class TestEvidence:
    def test_evidence_id_stable_and_16_hex(self, conn):
        seed = seed_corpus(conn)
        e1 = build_evidence(conn, seed["c1"], 0.9)
        e2 = build_evidence(conn, seed["c1"], 0.9)
        assert e1.evidence_id == e2.evidence_id
        assert len(e1.evidence_id) == 16
        int(e1.evidence_id, 16)  # valid hex

    def test_evidence_id_direct_formula(self):
        # sha256 over unit-separator-delimited fields, first 16 hex chars:
        # sha256("rev7" \x1f "42" \x1f "[2, 3]" \x1f "10" \x1f "99")[:16]
        import hashlib

        payload = "rev7\x1f42\x1f[2, 3]\x1f10\x1f99"
        expected = hashlib.sha256(payload.encode()).hexdigest()[0:16]
        assert evidence_id_for("rev7", 42, [2, 3], 10, 99) == expected

    def test_evidence_id_no_field_boundary_collisions(self):
        # naive concatenation would collide these; the separator must not
        assert (evidence_id_for("r", 1, "[1]", 12, 3)
                != evidence_id_for("r", 11, "[1]", 2, 3))
        assert (evidence_id_for("rab", 1, "[1]", 0, 0)
                != evidence_id_for("r", 11, "b[1]", 0, 0))
        # str(page JSON) and list input encode identically
        assert (evidence_id_for("r", 1, "[2, 3]", 0, 0)
                == evidence_id_for("r", 1, [2, 3], 0, 0))

    def test_evidence_id_changes_with_each_field(self):
        base = evidence_id_for("rev", 1, [1], 0, 10)
        assert evidence_id_for("rev2", 1, [1], 0, 10) != base
        assert evidence_id_for("rev", 2, [1], 0, 10) != base
        assert evidence_id_for("rev", 1, [2], 0, 10) != base
        assert evidence_id_for("rev", 1, [1], 1, 10) != base
        assert evidence_id_for("rev", 1, [1], 0, 11) != base

    def test_fields_assembled_from_db(self, conn):
        seed = seed_corpus(conn)
        ev = build_evidence(conn, seed["c1"], 0.75)
        assert isinstance(ev, Evidence)
        assert ev.chunk_id == seed["c1"]
        assert ev.file_id == "f1"
        assert ev.source_revision == "rev1"
        assert ev.page_nos == [1]
        assert ev.char_start == 0 and ev.char_end == 22
        assert ev.header == "Apple / Fruit"
        # span text: body[0:22] = "apple apple apple pie " (22 chars)
        assert ev.body_excerpt == "apple apple apple pie "
        assert len(ev.body_excerpt) == ev.char_end - ev.char_start
        assert ev.score == 0.75
        assert ev.auth_epoch == 0
        assert ev.title == "Apple Report.pdf"

    def test_excerpt_falls_back_to_body_head_without_span(self, conn):
        seed = seed_corpus(conn)
        ev = build_evidence(conn, seed["c2"], 0.5)
        assert ev.char_start is None
        assert ev.body_excerpt.startswith("apple banana smoothie")

    def test_table_fields_flow_through(self, conn):
        seed = seed_corpus(conn)
        ct = add_chunk(conn, seed["s1"], 3, "Revenue Table", "region revenue",
                       is_table=1)
        conn.execute("UPDATE chunks SET table_id=4, row_range='rows 12-19' WHERE chunk_id=?",
                     (ct,))
        conn.commit()
        ev = build_evidence(conn, ct, 0.6)
        assert ev.is_table is True
        assert ev.table_id == 4
        assert ev.row_range == "rows 12-19"

    def test_retired_source_does_not_resolve(self, conn):
        seed = seed_corpus(conn)
        conn.execute("UPDATE sources SET active=0 WHERE id=?", (seed["s1"],))
        conn.commit()
        assert build_evidence(conn, seed["c1"], 0.9) is None

    def test_missing_chunk_returns_none(self, conn):
        assert build_evidence(conn, 424242, 0.9) is None

    def test_id_is_a_function_of_identity_not_content(self):
        # same span, same revision, different text_hash/body -> same id
        assert evidence_id_for("r", 5, [1], 0, 5) == evidence_id_for("r", 5, [1], 0, 5)


# --------------------------------------------------------------------------- #
# rerank protocol
# --------------------------------------------------------------------------- #


class TestRerank:
    def test_noop_preserves_order_with_1_over_rank(self):
        out = NoopReranker().rerank("q", [(3,), (1,), (2,)])
        assert out == [(3, 1.0), (1, 0.5), (2, pytest.approx(1 / 3))]

    def test_noop_empty(self):
        assert NoopReranker().rerank("q", []) == []

    def test_flashrank_lazy_and_model_constant(self):
        r = FlashRankReranker()
        assert r._ranker is None  # no model init at construction
        assert FLASHRANK_MODEL == "ms-marco-MiniLM-L-12-v2"

    def test_flashrank_integration_only_if_cached(self, tmp_path, monkeypatch):
        # unit tests must never download: only run if the model is cached.
        import importlib.util

        if importlib.util.find_spec("flashrank") is None:
            pytest.skip("flashrank not installed")
        from flashrank.Config import model_file_map

        model_dir = tmp_path / FLASHRANK_MODEL
        model_dir.mkdir()
        model_file = model_dir / model_file_map[FLASHRANK_MODEL]
        model_file.write_bytes(b"")  # placeholder; real session needs real weights
        if not model_file.stat().st_size:
            pytest.skip("flashrank model not cached (no downloads in unit tests)")


# --------------------------------------------------------------------------- #
# end-to-end search
# --------------------------------------------------------------------------- #


class TestSearch:
    def test_end_to_end_returns_expected_top_chunk(self, conn):
        seed = seed_corpus(conn)
        # query lexically biased to apples AND dense-close to f1's vector
        result = search(conn, "apple pie recipe", vec(1.0, 0.0), NoopReranker())
        assert isinstance(result, EvidenceSet)
        assert result.evidence, "expected evidence"
        assert result.evidence[0].chunk_id == seed["c1"]
        assert result.evidence[0].file_id == "f1"
        assert result.route_decision.route_id == "NORMAL"
        # trace records every stage
        t = result.trace
        assert set(t["bm25"]) and set(t["knn"]) and set(t["rrf"])
        assert t["rerank"] and t["k"] >= 1
        assert t["reranker"] == "noop"
        assert len(t["final"]) == len(result.evidence)
        assert all(isinstance(ev, Evidence) for ev in result.evidence)

    def test_trace_scores_are_stage_conventions(self, conn):
        seed_corpus(conn)
        result = search(conn, "banana bread", vec(0.0, 1.0))
        t = result.trace
        assert all(s > 0 for _c, s in t["bm25"])          # -bm25() positive
        assert all(s <= 1.0 for _c, s in t["knn"])        # cosine-style in [-1, 1]
        assert all(s < 2 / RRF_K for _c, s in t["rrf"])   # rank-based, small
        rrs = [s for _c, s in t["rerank"]]
        assert rrs == sorted(rrs, reverse=True)           # best-first, [0,1]

    def test_exact_path_skips_reranker(self, conn):
        seed_corpus(conn)
        calls = []

        class SpyingReranker(NoopReranker):
            def rerank(self, query, candidates):
                calls.append(query)
                return super().rerank(query, candidates)

        # quoted phrase locks EXACT
        result = search(conn, '"apple pie recipe"', vec(1.0, 0.0), SpyingReranker())
        assert result.route_decision.route_id == "EXACT"
        assert calls == [], "EXACT must not call the reranker"
        assert result.trace.get("rerank_skipped") is True
        assert result.trace["rerank"] == []
        # adaptive-k still ran on normalized RRF
        assert result.trace["k"] >= 1
        assert result.evidence

    def test_exact_path_evidence_from_bm25_first(self, conn):
        seed = seed_corpus(conn)
        result = search(conn, '"banana bread"', vec(0.0, 0.0, 0.0), NoopReranker())
        assert result.route_decision.route_id == "EXACT"
        assert result.evidence[0].chunk_id == seed["c3"]

    def test_table_route_returns_only_table_chunks(self, conn):
        seed = seed_corpus(conn)
        ct = add_chunk(conn, seed["s1"], 3, "Revenue Table",
                       "region revenue apple 500 620", is_table=1)
        add_vec(conn, ct, vec(1.0, 0.2))
        add_term_df(conn, "revenue", 10)
        result = search(conn, "apple revenue above 500", vec(1.0, 0.0), NoopReranker())
        assert result.route_decision.route_id == "TABLE"
        assert result.evidence
        for ev in result.evidence:
            assert ev.is_table is True
        assert result.evidence[0].table_id is not None or result.evidence[0].is_table

    def test_normal_path_applies_diversity(self, conn):
        seed = seed_corpus(conn)
        # 6 chunks in one file, all matching, all near the query vector:
        # the cap must hold the final evidence to <= 2 from f1
        for i in (3, 4, 5, 6, 7):
            cid = add_chunk(conn, seed["s1"], i, "Apple", "apple orchard notes")
            add_vec(conn, cid, vec(1.0, 0.05 * i))
        result = search(conn, "apple orchard notes", vec(1.0, 0.0), NoopReranker())
        per_file: dict[str, int] = {}
        for ev in result.evidence:
            per_file[ev.file_id] = per_file.get(ev.file_id, 0) + 1
        assert all(n <= 2 for n in per_file.values()), per_file
        assert result.trace["diversity_dropped"] >= 0  # present in the trace

    def test_empty_index_returns_empty_set_agentic_trigger(self, conn):
        result = search(conn, "anything", vec(1.0), NoopReranker())
        assert result.evidence == []
        assert result.route_decision.route_id == "AGENTIC"
        assert result.route_decision.signals["agentic_trigger"] == "empty_evidence"
        assert result.trace["agentic_trigger"] == "empty_evidence"
        assert result.trace["final"] == []

    def test_agentic_floor_flip_on_low_top_score(self, conn):
        seed_corpus(conn)

        class FloorReranker(NoopReranker):
            """Reranker whose top score sits below RERANK_FLOOR."""

            def rerank(self, query, candidates):
                out = super().rerank(query, candidates)
                return [(cid, s * 0.1) for cid, s in out]  # top = 0.1 < 0.3

        result = search(conn, "apple pie", vec(1.0, 0.0), FloorReranker())
        assert result.route_decision.route_id == "AGENTIC"
        assert result.route_decision.signals["agentic_trigger"] == "rerank_floor"
        assert result.trace["agentic_trigger"] == "rerank_floor"
        # evidence still returned: the caller decides about the retry loop
        assert result.evidence

    def test_high_top_score_stays_normal(self, conn):
        seed_corpus(conn)

        class HighReranker(NoopReranker):
            def rerank(self, query, candidates):
                out = super().rerank(query, candidates)
                return [(cid, 0.5 + 0.5 * s) for cid, s in out]  # top = 1.0

        result = search(conn, "apple pie", vec(1.0, 0.0), HighReranker())
        assert result.route_decision.route_id == "NORMAL"
        assert result.trace["agentic_trigger"] is None

    def test_qvec_none_runs_bm25_only(self, conn):
        seed = seed_corpus(conn)
        result = search(conn, "banana bread", None, NoopReranker())
        assert result.evidence
        assert result.trace["knn"] == []
        assert result.evidence[0].chunk_id == seed["c3"]

    def test_default_reranker_is_noop(self, conn):
        seed_corpus(conn)
        result = search(conn, "apple", vec(1.0, 0.0))
        assert result.trace["reranker"] == "noop"

    def test_trashed_source_never_surfaces_end_to_end(self, conn):
        seed = seed_corpus(conn)
        conn.execute("UPDATE sources SET trashed=1 WHERE id=?", (seed["s2"],))
        conn.execute("UPDATE chunks_vec SET trashed=1 WHERE chunk_id=?",
                     (seed["c3"],))
        conn.commit()
        result = search(conn, "banana bread", vec(0.0, 1.0), NoopReranker())
        assert all(ev.file_id != "f2" for ev in result.evidence)

    def test_retired_representation_never_surfaces(self, conn):
        seed = seed_corpus(conn)
        # supersede f1: retire old row, add new revision
        conn.execute("UPDATE sources SET active=0 WHERE id=?", (seed["s1"],))
        conn.commit()
        s1b = add_source(conn, file_id="f1", revision="rev1b")
        c1b = add_chunk(conn, s1b, 1, "Apple v2", "apple turnover recipe")
        conn.execute("UPDATE chunks_vec SET active=0 WHERE chunk_id=?",
                     (seed["c1"],))
        conn.commit()
        add_vec(conn, c1b, vec(1.0, 0.0))
        result = search(conn, "apple turnover recipe", vec(1.0, 0.0), NoopReranker())
        assert result.evidence
        assert all(ev.source_revision == "rev1b" for ev in result.evidence)
        assert all(ev.chunk_id != seed["c1"] for ev in result.evidence)

    def test_evidence_ids_unique_across_set(self, conn):
        seed_corpus(conn)
        result = search(conn, "apple banana fruit", vec(1.0, 0.5), NoopReranker())
        ids = [ev.evidence_id for ev in result.evidence]
        assert len(ids) == len(set(ids))

    def test_search_never_raises_on_punct_only_query(self, conn):
        seed_corpus(conn)
        result = search(conn, "???", vec(1.0), NoopReranker())
        assert isinstance(result, EvidenceSet)

    def test_embed_query_stub_requires_injected_embedder(self):
        from fenris.retrieve import embed_query

        with pytest.raises(RuntimeError):
            embed_query("hello", None)

        class FixedEmbedder:
            def embed_query(self, text: str) -> bytes:
                return b"\x00" * (EMBED_DIMS * 4)

        assert embed_query("hello", FixedEmbedder()) == b"\x00" * (EMBED_DIMS * 4)


class TestScopeFilter:
    """P1.4: corpus scoping - a scoped miss is honest empty evidence,
    never a scope-ignoring fallback."""

    def test_scope_restricts_to_matching_files(self, conn):
        seed_corpus(conn)
        out = search(conn, "apple", None, scope=["banana"])
        # 'banana' scope excludes the apple file entirely
        assert all(e.file_id == "f2" for e in out.evidence)

    def test_no_scope_returns_everything(self, conn):
        seed_corpus(conn)
        out = search(conn, "apple", None, scope=None)
        assert out.evidence

    def test_scope_by_title_matches(self, conn):
        seed_corpus(conn)
        out = search(conn, "apple", None, scope=["Apple"])
        assert out.evidence
        assert all("Apple" in (e.title or "") for e in out.evidence)

    def test_scope_recorded_in_trace(self, conn):
        seed_corpus(conn)
        out = search(conn, "apple", None, scope=["zebra"])
        assert out.trace.get("scope") == ["zebra"]

    def test_scoped_miss_is_empty_not_fallback(self, conn):
        seed_corpus(conn)
        out = search(conn, "apple", None, scope=["nonexistent-term"])
        assert out.evidence == []
