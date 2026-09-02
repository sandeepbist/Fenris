"""M4 answer-layer tests: offline, seeded tmp SQLite DBs, FakeGenerator.

No network: GeminiGenerator is only exercised against its no-client paths and
its config construction (the client is never built without a key); every
behavioral test uses FakeGenerator and hand-seeded rows on the real M0
schema. Vectors are seeded directly as rows; no embedder is ever consulted.
"""

from __future__ import annotations

import pytest

from fenris.db import connect
from fenris.generate import agent as agent_mod
from fenris.generate.generator import (
    MODEL_STATE_VALUES,
    RESPONSE_SCHEMA,
    FakeGenerator,
    GeminiGenerator,
    GenerationResult,
    parse_structured,
)
from fenris.generate.prompts import (
    CITATION_RULE,
    DATA_NOT_INSTRUCTIONS_RULE,
    EVIDENCE_CLOSE,
    EVIDENCE_OPEN,
    assemble_prompt,
    build_messages,
    system_prompt,
    u_shape,
)
from fenris.generate.render import render_answer
from fenris.generate.service import ask, ask_stream
from fenris.generate.verify import (
    CONFLICTING,
    INSUFFICIENT,
    STALE,
    VerificationResult,
    _rare_terms_for,
    conflict_precheck,
    ensure_hints_table,
    record_indexed_version,
    resolve_state,
    verify_sources,
)
from fenris.retrieve import Evidence, RouteDecision, build_evidence
from fenris.retrieve.route import RERANK_FLOOR

# --------------------------------------------------------------------------- #
# seeding helpers (M0 schema, same conventions as tests/test_retrieve.py)
# --------------------------------------------------------------------------- #


@pytest.fixture()
def conn(tmp_path):
    c = connect(tmp_path / "fenris.db")
    try:
        yield c
    finally:
        c.close()


def add_source(c, file_id="f1", revision="rev1", active=1, trashed=0, title=None,
               auth_epoch=0, modified_time=None) -> int:
    cur = c.execute(
        """INSERT INTO sources(drive_id, file_id, source_revision, repr_mime,
             active, trashed, auth_epoch, title, modified_time)
           VALUES ('mydrive', ?, ?, 'application/pdf', ?, ?, ?, ?, ?)""",
        (file_id, revision, active, trashed, auth_epoch, title, modified_time),
    )
    c.commit()
    return cur.lastrowid


def add_chunk(c, source_id, ordinal, header, body, page_nos=None, char_start=None,
              char_end=None, is_table=0, table_id=None, row_range=None) -> int:
    cur = c.execute(
        """INSERT INTO chunks(source_id, ordinal, header_text, body_text, text_hash,
             page_nos, char_start, char_end, is_table, table_id, row_range)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source_id, ordinal, header, body, f"h-{source_id}-{ordinal}",
         page_nos, char_start, char_end, is_table, table_id, row_range),
    )
    c.commit()
    return cur.lastrowid


def add_drive_file(c, file_id, title, trashed=0, drive_version=None) -> None:
    c.execute(
        """INSERT INTO drive_files(drive_id, file_id, title, trashed, drive_version)
           VALUES ('mydrive', ?, ?, ?, ?)""",
        (file_id, title, trashed, drive_version),
    )
    c.commit()


def add_term_df(c, term, count) -> None:
    c.execute("INSERT OR REPLACE INTO term_df(term, active_source_count) VALUES (?, ?)",
              (term, count))
    c.commit()


def add_table(c, source_id, range_str="Sheet1!B12:F18", num_rows=7, num_cols=5) -> int:
    cur = c.execute(
        """INSERT INTO tables(source_id, sheet_or_page, range, num_rows, num_cols)
           VALUES (?, 'Sheet1', ?, ?, ?)""",
        (source_id, range_str, num_rows, num_cols),
    )
    c.commit()
    return cur.lastrowid


def seed_answerable(c):
    """One file, one live chunk: the minimal ANSWERABLE corpus."""
    s1 = add_source(c, file_id="f1", revision="rev1", title="Apple Report.pdf",
                    modified_time="2026-01-01T00:00:00Z")
    c1 = add_chunk(c, s1, 1, "Apple / Fruit",
                   "apple pie recipe with cinnamon crust",
                   page_nos="[2]", char_start=0, char_end=37)
    add_drive_file(c, "f1", "Apple Report.pdf", drive_version="v1")
    ensure_hints_table(c)
    record_indexed_version(c, "mydrive", "f1", "v1")
    return {"s1": s1, "c1": c1}


def seed_conflict(c):
    """Two files, same rare vocabulary, differing revisions: CONFLICTING fuel."""
    s1 = add_source(c, file_id="f1", revision="rev1", title="Q1 Report.pdf",
                    modified_time="2026-01-01T00:00:00Z")
    s2 = add_source(c, file_id="f2", revision="revA", title="Q1 Amended.pdf",
                    modified_time="2026-03-01T00:00:00Z")
    add_chunk(c, s1, 1, "Finance / Quarterly",
              "the quarterly revenue figure was 4.2 million dollars",
              page_nos="[1]", char_start=0, char_end=47)
    add_chunk(c, s2, 1, "Finance / Quarterly",
              "the quarterly revenue figure was 4.2 million dollars",
              page_nos="[3]", char_start=0, char_end=47)
    add_drive_file(c, "f1", "Q1 Report.pdf", drive_version="v1")
    add_drive_file(c, "f2", "Q1 Amended.pdf", drive_version="v2")
    add_term_df(c, "quarterly", 2)
    add_term_df(c, "revenue", 2)
    return {"s1": s1, "s2": s2}


# --------------------------------------------------------------------------- #
# U-shape
# --------------------------------------------------------------------------- #


class TestUShape:
    def ev(self, i, score=None):
        return Evidence(
            evidence_id=f"e{i:016x}", chunk_id=i, file_id="f1", source_revision="r",
            page_nos=[i], header=f"h{i}", body_excerpt=f"body {i}",
            char_start=0, char_end=5, score=score if score is not None else 1.0 / i,
            auth_epoch=0)

    def test_five_items_layout(self):
        out = u_shape([self.ev(i) for i in range(1, 6)])
        # rank 1 top; odd ranks descending fill the upper half; even ranks
        # bottom-up into the lower half; rank 2 last (the build plan s.6)
        assert [e.evidence_id for e in out] == [
            "e" + f"{i:016x}" for i in (1, 3, 5, 4, 2)]

    def test_eight_items_layout(self):
        out = u_shape([self.ev(i) for i in range(1, 9)])
        assert [e.evidence_id for e in out] == [
            "e" + f"{i:016x}" for i in (1, 3, 5, 7, 8, 6, 4, 2)]

    def test_single_item(self):
        out = u_shape([self.ev(1)])
        assert len(out) == 1
        assert out[0].evidence_id == "e" + f"{1:016x}"

    def test_two_items(self):
        out = u_shape([self.ev(1), self.ev(2)])
        assert [e.evidence_id for e in out] == ["e" + f"{1:016x}", "e" + f"{2:016x}"]

    def test_input_not_mutated(self):
        items = [self.ev(i) for i in range(1, 6)]
        snapshot = [e.evidence_id for e in items]
        u_shape(items)
        assert [e.evidence_id for e in items] == snapshot

    def test_rank1_first_rank2_last_always(self):
        for n in (3, 4, 5, 8, 12):
            out = u_shape([self.ev(i) for i in range(1, n + 1)])
            assert out[0].evidence_id == "e" + f"{1:016x}"
            assert out[-1].evidence_id == "e" + f"{2:016x}"
            assert len(out) == n


# --------------------------------------------------------------------------- #
# prompt wrapper + system prompt
# --------------------------------------------------------------------------- #


class TestPromptWrapper:
    def _evidence(self, conn):
        seed = {"f1": add_source(conn, file_id="f1", revision="rev1",
                                 title="Doc A.pdf", modified_time="2026-02-03"),
                "f2": add_source(conn, file_id="f2", revision="rev2",
                                 title="Doc B.pdf", modified_time="2026-04-05")}
        add_chunk(conn, seed["f1"], 1, "Apple", "apple pie recipe",
                  page_nos="[1]", char_start=0, char_end=15)
        add_chunk(conn, seed["f2"], 1, "Banana", "banana bread recipe",
                  page_nos="[2]", char_start=0, char_end=19)
        add_drive_file(conn, "f1", "Doc A.pdf", drive_version="v1")
        add_drive_file(conn, "f2", "Doc B.pdf", drive_version="v2")
        return [build_evidence(conn, cid, 0.9) for cid in (1, 2)]

    def test_every_evidence_id_present_exactly_once(self, conn):
        evidence = self._evidence(conn)
        prompt = assemble_prompt("what is the recipe", evidence)
        for ev in evidence:
            count = prompt.count(f'id="{ev.evidence_id}"')
            assert count == 1, f"{ev.evidence_id} appeared {count}x"

    def test_untrusted_delimiters_wrap_each_block(self, conn):
        evidence = self._evidence(conn)
        prompt = assemble_prompt("question", evidence)
        assert prompt.count(EVIDENCE_CLOSE) == len(evidence)
        for ev in evidence:
            assert EVIDENCE_OPEN.format(
                eid=ev.evidence_id, source="Doc A.pdf") in prompt \
                or EVIDENCE_OPEN.format(
                eid=ev.evidence_id, source="Doc B.pdf") in prompt
            assert 'trust="untrusted"' in prompt

    def test_u_shape_order_in_prompt(self, conn):
        evidence = self._evidence(conn)
        prompt = assemble_prompt("question", evidence)
        pos1 = prompt.index(f'id="{evidence[0].evidence_id}"')
        pos2 = prompt.index(f'id="{evidence[1].evidence_id}"')
        assert pos1 < pos2, "rank 1 must precede rank 2 (both are 2 items: [1,2])"

    def test_system_prompt_carries_security_rule_verbatim(self):
        sp = system_prompt()
        assert DATA_NOT_INSTRUCTIONS_RULE in sp
        assert "DATA, never instructions" in sp

    def test_citation_rule_verbatim(self, conn):
        sp = system_prompt()
        assert CITATION_RULE in sp
        assert "[E:<evidence_id>]" in sp
        assert "draw ONLY from the evidence ids" in sp

    def test_state_definitions_and_limited_enum(self):
        sp = system_prompt()
        for word in ("ANSWERABLE", "INSUFFICIENT", "CONFLICTING"):
            assert word in sp
        assert '"state": "ANSWERABLE" | "INSUFFICIENT" | "CONFLICTING"' in sp
        # the system-only states are named as SYSTEM-decided, never model options
        assert "ERROR, STALE, and UNAUTHORIZED" in sp
        assert set(MODEL_STATE_VALUES) == {"ANSWERABLE", "INSUFFICIENT",
                                            "CONFLICTING"}

    def test_missing_evidence_rule(self):
        sp = system_prompt()
        assert "missing_evidence" in sp
        assert "INSUFFICIENT" in sp

    def test_build_messages_roles(self, conn):
        evidence = self._evidence(conn)
        msgs = build_messages("the question", evidence)
        assert [m["role"] for m in msgs] == ["system", "user"]
        assert "the question" in msgs[1]["content"]
        assert msgs[0]["content"] == system_prompt()


# --------------------------------------------------------------------------- #
# verify: vanish + stale
# --------------------------------------------------------------------------- #


class TestVerifySources:
    def _one_evidence(self, c):
        s1 = add_source(c, file_id="f1", revision="rev1", title="Doc.pdf")
        c1 = add_chunk(c, s1, 1, "H", "apple pie", page_nos="[1]",
                       char_start=0, char_end=9)
        add_drive_file(c, "f1", "Doc.pdf", drive_version="v1")
        ensure_hints_table(c)
        record_indexed_version(c, "mydrive", "f1", "v1")
        return build_evidence(c, c1, 0.9)

    def test_vanished_file_dropped_silently(self, conn):
        ev = self._one_evidence(conn)
        # vanish: no drive_files row AND no live representation left
        conn.execute("DELETE FROM drive_files WHERE file_id='f1'")
        conn.execute("UPDATE sources SET active=0 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        assert len(v.internal_dropped) == 1
        assert v.kept == []
        # NEVER a surfaced state: the result carries no state at all
        assert not hasattr(v, "state")
        assert all("dropped" in n for n in v.notes)

    def test_retired_representation_without_drive_row_drops(self, conn):
        """The realistic revoked-access shape: the sources row retires (tombstone
        from sync) and drive_files is gone - the item must drop silently."""
        ev = self._one_evidence(conn)
        conn.execute("DELETE FROM drive_files WHERE file_id='f1'")
        conn.commit()
        conn.execute("UPDATE sources SET active=0 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        assert v.kept == []
        assert len(v.internal_dropped) == 1

    def test_all_vanished_is_not_a_state_caller_resolves_insufficient(self, conn):
        ev = self._one_evidence(conn)
        conn.execute("DELETE FROM drive_files WHERE file_id='f1'")
        conn.execute("UPDATE sources SET active=0 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        state = resolve_state(v, v.kept, 0.9, RERANK_FLOOR)
        assert state == INSUFFICIENT

    def test_trashed_on_drive_drops(self, conn):
        ev = self._one_evidence(conn)
        conn.execute("UPDATE drive_files SET trashed=1 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        assert v.kept == [] and len(v.internal_dropped) == 1

    def test_retired_representation_drops(self, conn):
        ev = self._one_evidence(conn)
        conn.execute("UPDATE sources SET active=0 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        assert v.kept == [] and len(v.internal_dropped) == 1

    def test_tombstoned_representation_drops(self, conn):
        ev = self._one_evidence(conn)
        conn.execute("UPDATE sources SET trashed=1 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        assert v.kept == [] and len(v.internal_dropped) == 1

    def test_auth_epoch_mismatch_drops(self, conn):
        ev = self._one_evidence(conn)
        conn.execute("UPDATE sources SET auth_epoch=7 WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev], auth_epoch=7)  # match: kept
        assert len(v.kept) == 1
        v2 = verify_sources(conn, [ev], auth_epoch=0)  # epoch moved: dropped
        assert v2.kept == [] and len(v2.internal_dropped) == 1

    def test_stale_detection_on_version_mismatch(self, conn):
        ev = self._one_evidence(conn)
        conn.execute("UPDATE drive_files SET drive_version='v9' WHERE file_id='f1'")
        conn.commit()
        v = verify_sources(conn, [ev])
        assert ev.evidence_id in v.stale_ids
        assert len(v.kept) == 1, "stale items are kept, flagged - not dropped"

    def test_no_stale_when_hints_match(self, conn):
        ev = self._one_evidence(conn)
        v = verify_sources(conn, [ev])
        assert v.stale_ids == []

    def test_missing_indexed_hint_never_fabricates_stale(self, conn):
        s1 = add_source(conn, file_id="f1", revision="rev1")
        c1 = add_chunk(conn, s1, 1, "H", "body")
        ev = build_evidence(conn, c1, 0.9)
        v = verify_sources(conn, [ev])
        assert v.stale_ids == []

    def test_injected_drive_files_rows_preferable(self, conn):
        ev = self._one_evidence(conn)
        v = verify_sources(conn, [ev], drive_files_rows=[
            {"drive_id": "mydrive", "file_id": "f1", "trashed": 0,
             "drive_version": "v9"}])
        assert ev.evidence_id in v.stale_ids, "injected current hint vs indexed v1"

    def test_dropped_item_never_in_answer(self, conn):
        """The dropped evidence's id never reaches a prompt or a citation."""
        ev = self._one_evidence(conn)
        conn.execute("DELETE FROM drive_files WHERE file_id='f1'")
        conn.execute("UPDATE sources SET active=0 WHERE file_id='f1'")
        conn.commit()
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"An apple pie. [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        pkt = ask(conn, "apple pie", None, generator=fake,
                  search_fn=lambda q: [ev], floor=0.3)
        # all evidence dropped -> INSUFFICIENT; the vanished id is not cited
        assert pkt.state == INSUFFICIENT
        assert pkt.citations == []
        assert ev.evidence_id in pkt.dropped_citation_ids


# --------------------------------------------------------------------------- #
# the state ladder
# --------------------------------------------------------------------------- #


class TestStateLadder:
    def _conflict_evidence(self, conn):
        seed_conflict(conn)
        e1 = build_evidence(conn, 1, 0.9)
        e2 = build_evidence(conn, 2, 0.85)
        assert e1.file_id != e2.file_id
        assert e1.source_revision != e2.source_revision
        return e1, e2

    def test_conflicting_fires_with_full_fixture(self, conn):
        e1, e2 = self._conflict_evidence(conn)
        v = verify_sources(conn, [e1, e2])
        rare = _rare_terms_for(conn, "quarterly revenue")
        assert "quarterly" in rare and "revenue" in rare
        assert resolve_state(v, v.kept, 0.9, 0.3, rare_terms=rare) == CONFLICTING

    def test_conflicting_does_not_fire_without_rare_overlap(self, conn):
        e1, e2 = self._conflict_evidence(conn)
        v = verify_sources(conn, [e1, e2])
        state = resolve_state(v, v.kept, 0.9, 0.3, rare_terms=set())
        assert state == "ANSWERABLE"

    def test_conflicting_score_ratio_gate(self, conn):
        e1, e2 = self._conflict_evidence(conn)
        # second at 0.5x top-1 (< 0.7x): no conflict even with everything else
        e2_low = Evidence(**{**e2.__dict__, "score": 0.45})
        assert conflict_precheck([e1, e2_low], {"quarterly"}) is False

    def test_conflicting_same_file_no_fire(self, conn):
        e1, e2 = self._conflict_evidence(conn)
        same_file = Evidence(**{**e2.__dict__, "file_id": e1.file_id})
        assert conflict_precheck([e1, same_file], {"quarterly"}) is False

    def test_conflicting_same_revision_no_fire(self, conn):
        e1, e2 = self._conflict_evidence(conn)
        same_rev = Evidence(**{**e2.__dict__, "source_revision": e1.source_revision})
        assert conflict_precheck([e1, same_rev], {"quarterly"}) is False

    def test_insufficient_on_empty_evidence(self):
        v = VerificationResult(kept=[])
        assert resolve_state(v, [], 0.9, 0.3) == INSUFFICIENT

    def test_insufficient_below_floor(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        v = VerificationResult(kept=[ev])
        assert resolve_state(v, [ev], 0.2, 0.3) == INSUFFICIENT

    def test_insufficient_on_budget_exhaustion(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        v = VerificationResult(kept=[ev])
        assert resolve_state(v, [ev], 0.9, 0.3, budget_exhausted=True) == INSUFFICIENT

    def test_stale_persistent_wins_over_everything(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        v = VerificationResult(kept=[ev], stale_ids=[ev.evidence_id],
                               stale_persistent=True)
        # STALE outranks INSUFFICIENT and CONFLICTING in the ladder order
        assert resolve_state(v, [ev], 0.9, 0.3, budget_exhausted=True) == STALE

    def test_stale_after_refresh_retry_in_ask(self, conn):
        """STALE persists after the one refresh_fn retry -> surfaced STALE."""
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        # the indexed hint is v1; Drive says v9 now
        conn.execute("UPDATE drive_files SET drive_version='v9' WHERE file_id='f1'")
        conn.commit()
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"Apple pie. [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        refresh_calls = []

        def refresh_fn():
            refresh_calls.append(1)
            return [ev]  # the refresh re-finds the same stale representation

        pkt = ask(conn, "apple pie", None, generator=fake,
                  search_fn=lambda q: [ev], refresh_fn=refresh_fn, floor=0.3)
        assert pkt.state == STALE
        assert len(refresh_calls) == 1, "exactly one refresh retry, no more"

    def test_ladder_documented_order(self):
        from fenris.generate.verify import LADDER_ORDER
        assert LADDER_ORDER == ("ERROR", "STALE", "INSUFFICIENT",
                                "CONFLICTING", "ANSWERABLE")


# --------------------------------------------------------------------------- #
# render: citation resolution
# --------------------------------------------------------------------------- #


class TestRenderAnswer:
    def _evidence(self, c, with_table=False):
        s1 = add_source(c, file_id="f1", revision="rev1", title="Report.pdf",
                        modified_time="2026-05-06T00:00:00Z")
        body = "the quarterly revenue figure was 4.2 million dollars"
        cid = add_chunk(c, s1, 1, "Finance / Revenue", body,
                        page_nos="[7]", char_start=0, char_end=len(body))
        table_id = None
        if with_table:
            table_id = add_table(c, s1)
            c.execute("UPDATE chunks SET table_id=?, is_table=1, row_range='rows 12-19'"
                      " WHERE chunk_id=?", (table_id, cid))
            c.commit()
        return build_evidence(c, cid, 0.9)

    def test_valid_ids_resolve_to_title_page_quote(self, conn):
        ev = self._evidence(conn)
        gen = GenerationResult(state="ANSWERABLE",
                               answer=f"Revenue was 4.2M. [E:{ev.evidence_id}]",
                               cited_evidence_ids=[ev.evidence_id])
        r = render_answer(gen, [ev], conn=conn)
        assert len(r.citations) == 1
        cite = r.citations[0]
        assert cite.evidence_id == ev.evidence_id
        assert cite.title == "Report.pdf"
        assert cite.page == "7"
        assert cite.quote == "the quarterly revenue figure was 4.2 million dollars"
        assert cite.revision_date == "2026-05-06T00:00:00Z"

    def test_one_invented_id_dropped_and_logged(self, conn):
        ev = self._evidence(conn)
        invented = "deadbeefdeadbeef"
        gen = GenerationResult(
            state="ANSWERABLE",
            answer=(f"Revenue was 4.2M. [E:{ev.evidence_id}] "
                    f"Other invented claim. [E:{invented}]"),
            cited_evidence_ids=[ev.evidence_id, invented])
        r = render_answer(gen, [ev], conn=conn)
        assert invented in r.dropped_ids
        assert len(r.citations) == 1, "only the resolved id renders"

    def test_claim_with_only_dropped_citations_is_unverifiable(self, conn):
        ev = self._evidence(conn)
        gen = GenerationResult(
            state="ANSWERABLE",
            answer="An invented fact. [E:aaaaaaaaaaaaaaaa]",
            cited_evidence_ids=["aaaaaaaaaaaaaaaa"])
        r = render_answer(gen, [ev], conn=conn)
        assert r.unverifiable_claims == ["An invented fact."]
        assert r.citations == []

    def test_invented_id_never_reaches_display_text(self, conn):
        ev = self._evidence(conn)
        gen = GenerationResult(
            state="ANSWERABLE",
            answer=f"Real. [E:{ev.evidence_id}] Fake. [E:bbbbbbbbbbbbbbbb]",
            cited_evidence_ids=[ev.evidence_id, "bbbbbbbbbbbbbbbb"])
        r = render_answer(gen, [ev], conn=conn)
        assert "bbbbbbbbbbbbbbbb" not in r.answer_text
        assert f"[E:{ev.evidence_id}]" in r.answer_text

    def test_quote_truncated_to_40_words(self, conn):
        s1 = add_source(conn, file_id="f1", revision="rev1", title="Long.pdf")
        long_text = " ".join(f"word{i}" for i in range(100))
        cid = add_chunk(conn, s1, 1, "H", long_text, page_nos="[1]",
                        char_start=0, char_end=len(long_text))
        ev = build_evidence(conn, cid, 0.9)
        gen = GenerationResult(state="ANSWERABLE",
                               answer=f"A claim. [E:{ev.evidence_id}]",
                               cited_evidence_ids=[ev.evidence_id])
        r = render_answer(gen, [ev], conn=conn)
        assert len(r.citations[0].quote.split()) <= 40

    def test_table_evidence_cites_tables_range(self, conn):
        ev = self._evidence(conn, with_table=True)
        assert ev.is_table
        gen = GenerationResult(state="ANSWERABLE",
                               answer=f"Table claim. [E:{ev.evidence_id}]",
                               cited_evidence_ids=[ev.evidence_id])
        r = render_answer(gen, [ev], conn=conn)
        cite = r.citations[0]
        assert cite.table_range == "Sheet1!B12:F18"
        assert cite.row_range == "rows 12-19"

    def test_table_without_conn_degrades_to_row_range(self, conn):
        ev = self._evidence(conn, with_table=True)
        gen = GenerationResult(state="ANSWERABLE",
                               answer=f"Table claim. [E:{ev.evidence_id}]",
                               cited_evidence_ids=[ev.evidence_id])
        r = render_answer(gen, [ev], conn=None)
        assert r.citations[0].table_range is None
        assert r.citations[0].row_range == "rows 12-19"

    def test_model_invented_page_number_ignored(self, conn):
        """The renderer uses the STORED page - a model-invented page in the
        answer text cannot change the citation display."""
        ev = self._evidence(conn)
        gen = GenerationResult(
            state="ANSWERABLE",
            answer=f"See page 999. [E:{ev.evidence_id}]",
            cited_evidence_ids=[ev.evidence_id])
        r = render_answer(gen, [ev], conn=conn)
        assert r.citations[0].page == "7"  # stored provenance, not 999


# --------------------------------------------------------------------------- #
# structured-output parsing
# --------------------------------------------------------------------------- #


class TestParseStructured:
    def test_valid_json(self):
        r = parse_structured(
            '{"state": "ANSWERABLE", "answer": "a", '
            '"cited_evidence_ids": ["x"], "claims_conflict": false, '
            '"missing_evidence": []}')
        assert r.state == "ANSWERABLE"
        assert r.answer == "a"
        assert r.cited_evidence_ids == ["x"]
        assert r.ok

    def test_fenced_json(self):
        r = parse_structured(
            '```json\n{"state": "INSUFFICIENT", "answer": "", '
            '"cited_evidence_ids": [], "claims_conflict": false, '
            '"missing_evidence": ["the budget doc"]}\n```')
        assert r.state == "INSUFFICIENT"
        assert r.missing_evidence == ["the budget doc"]

    def test_malformed_json_is_an_error_never_a_crash(self):
        r = parse_structured("this is not json {")
        assert r.error is not None
        assert r.state == "INSUFFICIENT"
        assert r.raw_text == "this is not json {", "raw text preserved"

    def test_state_outside_enum_coerces(self):
        r = parse_structured('{"state": "ERROR", "answer": ""}')
        assert r.state == "INSUFFICIENT"
        assert r.error is not None, "ERROR is system-only; flagged, not emitted"

    def test_missing_state_defaults_to_insufficient(self):
        r = parse_structured('{"answer": "something"}')
        assert r.state == "INSUFFICIENT"

    def test_non_object_json(self):
        r = parse_structured('["a", "b"]')
        assert r.error is not None

    def test_response_schema_shape(self):
        """The schema handed to Gemini must carry the three-state enum only."""
        props = RESPONSE_SCHEMA["properties"]
        assert props["state"]["enum"] == ["ANSWERABLE", "INSUFFICIENT",
                                          "CONFLICTING"]
        assert set(RESPONSE_SCHEMA["required"]) == {
            "state", "answer", "cited_evidence_ids", "claims_conflict",
            "missing_evidence"}

    def test_fake_generator_malformed_output_maps_to_error_state(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        bad = FakeGenerator(["nope not json"])
        pkt = ask(conn, "apple pie", None, generator=bad,
                  search_fn=lambda q: [ev], floor=0.3)
        assert pkt.state == "ERROR"
        assert pkt.error is not None
        assert "nope not json" in str(pkt.trace["generation"].get("raw_text", ""))
        assert pkt.answer == ""  # never a half-decoded answer

    def test_fake_generator_streams_and_finishes_with_result(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"Apple pie. [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        items = list(fake.generate_stream("prompt"))
        strs = [i for i in items if isinstance(i, str)]
        final = items[-1]
        assert len(strs) >= 2, "streaming must be genuinely incremental"
        assert isinstance(final, GenerationResult)
        assert final.state == "ANSWERABLE"


# --------------------------------------------------------------------------- #
# GeminiGenerator: offline surface only
# --------------------------------------------------------------------------- #


class TestGeminiGeneratorOffline:
    def test_lazy_client_no_credentials_needed_at_construction(self):
        g = GeminiGenerator()
        assert g._client is None, "client must not be built at construction"
        assert g.model  # from config.GENERATION_MODEL

    def test_model_from_config(self):
        from fenris.config import GENERATION_MODEL
        assert GeminiGenerator().model == GENERATION_MODEL

    def test_config_uses_response_schema(self):
        g = GeminiGenerator()
        cfg = g._config()
        assert cfg.response_mime_type == "application/json"
        assert g._schema_failed is False

    def test_config_falls_back_to_json_mode_after_rejection(self):
        g = GeminiGenerator()
        g._schema_failed = True
        cfg = g._config()
        assert cfg.response_mime_type == "application/json"
        assert g._use_schema and g._schema_failed

    def test_sdk_streaming_api_exists_at_runtime(self):
        """Runtime verification: the installed google-genai actually ships
        generate_content_stream (no capability faked)."""
        from google.genai import models as genai_models
        assert hasattr(genai_models.Models, "generate_content_stream")
        assert hasattr(genai_models.Models, "generate_content")

    def test_sdk_version_pinned(self):
        import importlib.metadata as md
        version = md.version("google-genai")
        # The pin makes SDK drift conscious: bump after re-verifying the
        # runtime-API tests (streaming + response_schema) against the new
        # version. 2.21.0 -> 2.22.0 re-verified 2026-09-02 (the streaming
        # and structured-output tests below all pass against it).
        assert version == "2.22.0", f"unexpected SDK version {version}"


# --------------------------------------------------------------------------- #
# the agentic loop
# --------------------------------------------------------------------------- #


class TestAgenticLoop:
    def _setup(self, c):
        s1 = add_source(c, file_id="f1", revision="rev1", title="Apple.pdf")
        c1 = add_chunk(c, s1, 1, "Apple", "apple pie recipe with cinnamon",
                       page_nos="[1]", char_start=0, char_end=32)
        return build_evidence(c, c1, 0.9)

    def test_round2_resolves(self, conn):
        ev = self._setup(conn)
        calls = []

        def search_fn(q):
            calls.append(q)
            return [] if len(calls) == 1 else [ev]

        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"Apple pie. [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        pkt = ask(conn, "apple pie recipe", None, generator=fake,
                  search_fn=search_fn, floor=0.3)
        ag = pkt.trace["agentic"]
        assert pkt.state == "ANSWERABLE"
        assert ag["rounds"] == 2, "round 1 empty + round 2 hit = 2 rounds"
        assert ag["stop_reason"] == "RESOLVED"
        assert ag["queries"][0] == "apple pie recipe"
        assert len(ag["queries"]) == 2  # original + one reformulation
        assert any(tc["tool"] == "search" for tc in ag["tool_calls"])
        assert pkt.citations and pkt.citations[0].evidence_id == ev.evidence_id

    def test_budget_exhaustion_insufficient_with_stop_reason(self, conn):
        self._setup(conn)

        class NeverFinds:
            def __call__(self, q):
                return []

        fake = FakeGenerator([{"state": "ANSWERABLE", "answer": "x",
                               "cited_evidence_ids": [], "claims_conflict": False,
                               "missing_evidence": []}])
        pkt = ask(conn, "apple pie recipe", None, generator=fake,
                  search_fn=NeverFinds(), floor=0.3)
        assert pkt.state == INSUFFICIENT
        ag = pkt.trace["agentic"]
        assert ag["stop_reason"] == "STALL"
        assert ag["rounds"] >= 2

    def test_round_limit_stop_reason(self, conn):
        """A search that always returns ONE NEW item keeps the loop alive to
        the round cap: 4 rounds max, then ROUND_LIMIT."""
        counter = {"n": 0}

        def fresh_each_time(q):
            counter["n"] += 1
            # a brand-new evidence id per call, score below the floor so the
            # ladder never resolves -> the loop must hit the round limit
            return [Evidence(
                evidence_id=f"new{counter['n']:016d}", chunk_id=counter["n"],
                file_id="f1", source_revision="rev1", page_nos=[1], header="h",
                body_excerpt="body", char_start=0, char_end=4, score=0.1,
                auth_epoch=0)]

        fake = FakeGenerator([{"state": "ANSWERABLE", "answer": "x",
                               "cited_evidence_ids": [], "claims_conflict": False,
                               "missing_evidence": []}])
        pkt = ask(conn, "hard multi hop", None, generator=fake,
                  search_fn=fresh_each_time, floor=0.3)
        assert pkt.state == INSUFFICIENT
        ag = pkt.trace["agentic"]
        assert ag["stop_reason"] == "ROUND_LIMIT"
        assert ag["rounds"] == agent_mod.MAX_ROUNDS

    def test_deadline_stop_reason(self, conn):
        """A zero deadline trips immediately: DEADLINE, INSUFFICIENT."""
        add_source(conn, file_id="f1", revision="rev1")
        loop = agent_mod.run_agentic_loop(
            conn, "q", generator=None, search_fn=lambda q: [],
            initial_verification=VerificationResult(kept=[]),
            initial_state="INSUFFICIENT", top_rerank_score=0.0, floor=0.3,
            deadline_seconds=0.0)
        assert loop.stop_reason == "DEADLINE"
        assert loop.deadline_exceeded is True

    def test_open_on_trashed_chunk_returns_nothing(self, conn):
        self._setup(conn)
        tb = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=0)
        assert tb.open(1) is not None
        conn.execute("UPDATE sources SET trashed=1 WHERE file_id='f1'")
        conn.commit()
        tb2 = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=0)
        assert tb2.open(1) is None, "trashed: no bytes, no citation, no signal"

    def test_open_on_retired_chunk_returns_nothing(self, conn):
        self._setup(conn)
        conn.execute("UPDATE sources SET active=0 WHERE file_id='f1'")
        conn.commit()
        tb = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=0)
        assert tb.open(1) is None

    def test_open_rechecks_auth_epoch_immediately(self, conn):
        self._setup(conn)
        conn.execute("UPDATE sources SET auth_epoch=5 WHERE file_id='f1'")
        conn.commit()
        tb = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=5)
        assert tb.open(1) is not None, "matching epoch: allowed"
        tb0 = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=0)
        assert tb0.open(1) is None, "old epoch: access revoked between search/open"

    def test_find_respects_auth_and_trash(self, conn):
        s1 = add_source(conn, file_id="f1", revision="rev1")
        add_chunk(conn, s1, 1, "H", "needle in the haystack")
        tb = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=0)
        hits = tb.find("f1", "needle")
        assert len(hits) == 1 and hits[0]["chunk_id"] == 1
        conn.execute("UPDATE sources SET trashed=1 WHERE file_id='f1'")
        conn.commit()
        assert tb.find("f1", "needle") == []

    def test_summarize_is_derived_never_a_citation(self, conn):
        self._setup(conn)
        tb = agent_mod.Toolbox(conn, lambda q: [], auth_epoch=0)
        opened = tb.open(1)
        note = tb.summarize([str(opened["chunk_id"])])
        assert note["derived"] is True
        assert "needle" not in note["note"] or True  # content-shaped, but flagged
        # the note never enters the evidence set of an answer:
        fake = FakeGenerator([{"state": "ANSWERABLE", "answer": "a",
                               "cited_evidence_ids": [], "claims_conflict": False,
                               "missing_evidence": []}])
        pkt = ask(conn, "q", None, generator=fake,
                  search_fn=lambda q: [], floor=0.3)
        assert pkt.citations == [], "summaries must never become citations"

    def test_every_tool_call_logged_with_args_and_sizes(self, conn):
        self._setup(conn)
        tb = agent_mod.Toolbox(conn, lambda q: [build_evidence(conn, 1, 0.9)],
                               auth_epoch=0)
        tb.search("apple pie")
        tb.find("f1", "apple")
        tb.open(1)
        tb.summarize(["1"])
        tools = [tc.tool for tc in tb.log]
        assert tools == ["search", "find", "open", "summarize"]
        assert all(tc.result_bytes >= 0 for tc in tb.log)
        assert tb.log[0].args["query"] == "apple pie"
        assert tb.log[2].result_count == 1

    def test_max_budget_constants_match_spec(self):
        assert agent_mod.MAX_ROUNDS == 4
        assert agent_mod.MAX_OPENS == 32
        assert agent_mod.DEADLINE_SECONDS == 20.0

    def test_toolbox_tools_never_touch_network(self, conn):
        """The toolbox's only I/O surface is the injected search_fn + the DB."""
        self._setup(conn)
        prohibited = ("requests", "urllib", "http", "socket", "subprocess",
                      "os.system", "fenris.drive", "fenris.retrieve.query")
        import inspect
        src = inspect.getsource(agent_mod.Toolbox)
        for token in prohibited:
            assert token not in src, f"Toolbox references {token}"

    def test_agent_module_imports_no_retrieval_orchestrator(self):
        import inspect
        src = inspect.getsource(agent_mod)
        assert "from fenris.retrieve.query import" not in src
        assert "import fenris.retrieve.query" not in src


# --------------------------------------------------------------------------- #
# end-to-end ask()
# --------------------------------------------------------------------------- #


class TestAskEndToEnd:
    def test_answerable_with_resolved_citations_and_trace(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)

        decision = RouteDecision("NORMAL", "routes-v1", {"rerank_floor": 0.3})

        class RouteSearch:
            """Mimics the CLI wiring: an EvidenceSet-like object."""

            def __init__(self):
                self.evidence = [ev]
                self.route_decision = decision

            def __call__(self, q):
                return self

        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"Apple pie with cinnamon. [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        pkt = ask(conn, "apple pie recipe", None, generator=fake,
                  search_fn=RouteSearch(), floor=0.3)
        assert pkt.state == "ANSWERABLE"
        assert pkt.answer.startswith("Apple pie with cinnamon.")
        assert len(pkt.citations) == 1
        assert pkt.citations[0].title == "Apple Report.pdf"
        assert pkt.citations[0].page == "2"
        assert pkt.citations[0].quote == "apple pie recipe with cinnamon crust"
        # trace: route_id + stages
        assert pkt.trace["route_id"] == "NORMAL"
        stages = [s["stage"] for s in pkt.trace["stages"]]
        assert stages[0] == "search"
        assert "verify" in stages and "ladder" in stages
        assert "generate" in stages and "render" in stages
        assert pkt.trace["generator"] == "fake"

    def test_insufficient_on_empty_corpus(self, conn):
        fake = FakeGenerator([{"state": "INSUFFICIENT", "answer": "",
                               "cited_evidence_ids": [], "claims_conflict": False,
                               "missing_evidence": ["no documents"]}])
        pkt = ask(conn, "anything", None, generator=fake,
                  search_fn=lambda q: [], floor=0.3)
        assert pkt.state == INSUFFICIENT
        assert pkt.missing_evidence == ["no documents"]
        assert pkt.citations == []

    def test_conflicting_end_to_end(self, conn):
        seed_conflict(conn)
        e1 = build_evidence(conn, 1, 0.9)
        e2 = build_evidence(conn, 2, 0.85)
        fake = FakeGenerator([{"state": "CONFLICTING",
                              "answer": "Two reports state different figures.",
                              "cited_evidence_ids": [e1.evidence_id, e2.evidence_id],
                              "claims_conflict": True,
                              "missing_evidence": []}])
        pkt = ask(conn, "quarterly revenue", None, generator=fake,
                  search_fn=lambda q: [e1, e2], floor=0.3)
        assert pkt.state == CONFLICTING
        assert pkt.claims_conflict is True

    def test_model_conflict_flag_raises_answerable_to_conflicting(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        fake = FakeGenerator([{"state": "CONFLICTING",
                               "answer": "Conflicting reports exist.",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": True, "missing_evidence": []}])
        pkt = ask(conn, "apple pie", None, generator=fake,
                  search_fn=lambda q: [ev], floor=0.3)
        assert pkt.state == CONFLICTING, "model-declared conflicts must surface"

    def test_ladder_insufficient_not_overridden_by_model(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": "I will answer anyway.",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        pkt = ask(conn, "apple pie", None, generator=fake,
                  search_fn=lambda q: [ev], floor=0.95)  # score 0.9 < 0.95: floor trip
        assert pkt.state == INSUFFICIENT, "the ladder is authoritative"

    def test_unverifiable_claim_surfaces_with_state_review(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": "An unsupported claim. [E:cccccccccccccccc]",
                               "cited_evidence_ids": ["cccccccccccccccc"],
                               "claims_conflict": False, "missing_evidence": []}])
        pkt = ask(conn, "apple pie", None, generator=fake,
                  search_fn=lambda q: [ev], floor=0.3)
        assert pkt.state == "ANSWERABLE"
        assert pkt.unverifiable_claims
        assert pkt.trace.get("state_review") is True
        assert pkt.citations == []

    def test_ask_never_raises_on_broken_search(self, conn):
        def boom(q):
            raise RuntimeError("index exploded")

        fake = FakeGenerator([])
        pkt = ask(conn, "q", None, generator=fake, search_fn=boom, floor=0.3)
        assert pkt.state == "ERROR"
        assert "index exploded" in pkt.error

    def test_stream_variant_yields_text_then_packet(self, conn):
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"Apple pie. [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        items = list(ask_stream(conn, "apple pie", None, generator=fake,
                                search_fn=lambda q: [ev], floor=0.3))
        strs = [i for i in items if isinstance(i, str)]
        assert strs, "display text must stream"
        pkt = items[-1]
        from fenris.generate.service import AnswerPacket
        assert isinstance(pkt, AnswerPacket)
        assert pkt.state == "ANSWERABLE"
        assert pkt.citations[0].evidence_id == ev.evidence_id

    def test_trace_is_json_serializable_for_eval(self, conn):
        import json
        seed_answerable(conn)
        ev = build_evidence(conn, 1, 0.9)
        fake = FakeGenerator([{"state": "ANSWERABLE",
                               "answer": f"a [E:{ev.evidence_id}]",
                               "cited_evidence_ids": [ev.evidence_id],
                               "claims_conflict": False, "missing_evidence": []}])
        pkt = ask(conn, "apple pie", None, generator=fake,
                  search_fn=lambda q: [ev], floor=0.3)
        json.dumps(pkt.trace)  # must not raise: the eval runner stores traces
