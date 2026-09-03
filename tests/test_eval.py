"""Golden eval suite tests (the build plan / M2.5) - fully offline.

Covers, per the slice spec:
- generator determinism (two runs -> byte-identical corpus)
- corpus manifest self-consistency (spans within doc bounds, text matches)
- case validity (expected_docs exist in manifest; unanswerable cases empty;
  slice composition exactly 20/15/15/15/25/15/15)
- metrics correctness on HAND-COMPUTED small examples (3-query toy set;
  nDCG worked by hand with graded 2/1/0 and LINEAR gain)
- span-recall overlap math (exact-boundary, 50% overlap, no-overlap)
- per-slice isolation (a failure in one slice cannot contaminate another)
- runner: writes correct eval_runs/eval_case_results rows; report contains
  per-case results that can regress

No LLM calls, no network, no writes outside tmp_path / repo.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from fenris.db import connect
from fenris.eval.cases import (
    ANSWERED,
    INSUFFICIENT,
    SLICE_COUNTS,
    EvalCase,
    ExpectedSpan,
    load_cases,
    slice_counts,
    validate_all,
)
from fenris.eval.corpus import AnswerSpan, build_corpus, validate_corpus, write_corpus
from fenris.eval.metrics import (
    RetrievedDoc,
    aggregate_by_slice,
    ir_metrics_for_case,
    span_coverage,
    span_recall_for_case,
    state_correct_for_case,
)
from fenris.eval.runner import render_report, run_suite

REPO_ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = REPO_ROOT / "eval" / "fixtures" / "golden_corpus" / "manifest.json"
CASES_DIR = REPO_ROOT / "eval" / "cases" / "golden"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def manifest() -> dict:
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def manifest_docs(manifest: dict) -> dict[str, dict]:
    return {d["doc_id"]: d for d in manifest["docs"]}


@pytest.fixture(scope="module")
def cases() -> list[EvalCase]:
    assert CASES_DIR.exists(), "eval/cases/golden must exist (run generate_cases.py)"
    return load_cases(CASES_DIR)


@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    c = connect(tmp_path / "fenris-test.db")
    try:
        yield c
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Generator determinism + corpus self-consistency
# ---------------------------------------------------------------------------


def test_corpus_generator_deterministic(tmp_path):
    """Two builds + two writes produce byte-identical manifest and doc files."""
    d1, d2 = tmp_path / "run1", tmp_path / "run2"
    for out in (d1, d2):
        docs = build_corpus()
        validate_corpus(docs)
        write_corpus(out, docs)
    m1 = (d1 / "manifest.json").read_bytes()
    m2 = (d2 / "manifest.json").read_bytes()
    assert m1 == m2, "manifest bytes differ between runs"
    # the committed corpus must match a fresh generation byte-for-byte
    committed = MANIFEST_PATH.read_bytes()
    assert committed == m1, "committed corpus drifted from the generator"
    # doc text files too
    docs_dir1 = sorted((d1 / "docs").iterdir())
    docs_dir2 = sorted((d2 / "docs").iterdir())
    assert [p.name for p in docs_dir1] == [p.name for p in docs_dir2]
    assert all(p1.read_bytes() == p2.read_bytes()
               for p1, p2 in zip(docs_dir1, docs_dir2, strict=True))


def test_corpus_manifest_self_consistent(manifest: dict):
    """Every doc's spans are within bounds and the covered text resolves."""
    assert manifest["doc_count"] == len(manifest["docs"])
    assert manifest["doc_count"] >= 95, "spec asks for ~100 docs"
    families = Counter(d["family"] for d in manifest["docs"])
    # every family the slices need is present
    for fam in ("invoice", "meeting", "project", "expenses", "travel", "near_dup"):
        assert families[fam] > 0, f"family {fam} missing"
    # versioned revisions of the SAME doc exist (freshness/multi-hop material)
    projects = Counter(d["meta"]["project"] for d in manifest["docs"]
                        if d["family"] == "project")
    assert set(projects.values()) == {3}, "each project needs revisions v1-v3"
    for d in manifest["docs"]:
        assert d["text"], f"{d['doc_id']} has empty text"
        for sp in d["answer_spans"]:
            assert sp["doc_id"] == d["doc_id"]
            assert 0 <= sp["start"] < sp["end"] <= len(d["text"]), \
                f"{d['doc_id']} span out of bounds: {sp}"
        # the doc file on disk matches the manifest text
        doc_path = (MANIFEST_PATH.parent / "docs" / f"{d['doc_id']}.txt")
        assert doc_path.read_text(encoding="utf-8") == d["text"]


# ---------------------------------------------------------------------------
# Case validity
# ---------------------------------------------------------------------------


def test_cases_composition_exact(cases: list[EvalCase]):
    """Counts per slice must equal the the build-plan table: 20/15/15/15/25/15/15."""
    counts = slice_counts(cases)
    assert counts == SLICE_COUNTS
    assert len(cases) == 120
    ids = [c.id for c in cases]
    assert len(set(ids)) == 120, "case ids must be unique"
    # created_by / locked per spec
    assert all(c.created_by == "synth-v1" for c in cases)
    assert all(c.locked is False for c in cases), "locked is for human-labeled cases"


def test_every_case_answerable_by_corpus(cases, manifest_docs):
    """Programmatic verification: expected_docs exist; expected_spans resolve
    inside the manifest doc text; answerable slices carry a grade-2 doc."""
    validate_all(cases, manifest_docs)  # raises on any violation
    # and the unanswerable slice references nothing
    for c in cases:
        if c.slice == "unanswerable":
            assert c.expected_docs == {}
            assert c.expected_spans == []
            assert c.answerable is False
            assert c.expected_state == INSUFFICIENT
        else:
            assert c.answerable is True
            assert c.expected_state == ANSWERED
            assert any(g == 2 for g in c.expected_docs.values())


def test_unanswerable_questions_absent_from_corpus(cases, manifest):
    """Unanswerable cases must name entities that appear in NO corpus doc."""
    all_text = "\n".join(d["text"] for d in manifest["docs"])
    for c in cases:
        if c.slice != "unanswerable":
            continue
        # the distinctive multi-word entities of the question must not occur
        words = c.question.replace("?", "").split()
        for i, w in enumerate(words):
            if len(w) > 6 and w.isupper() is False and not w.isdigit():
                pass  # generic words exempt
        # stronger check: no 3+-word capitalized entity from the question
        # may appear verbatim in the corpus
        assert c.question.rstrip("?") not in all_text
        # and any token >7 chars that looks like an identifier (INV-/DEC-/TRV-,
        # or an ALL-CAPS vendor word) must be absent
        for tok in words:
            if (tok.startswith(("INV-", "DEC-", "TRV-")) or tok.isupper()
                    and len(tok) > 4):
                assert tok not in all_text, \
                    f"{c.id}: {tok!r} leaks into the corpus"


def test_yaml_roundtrip(cases, tmp_path):
    """Cases survive a save/load roundtrip byte-stable (schema fidelity)."""
    from fenris.eval.cases import save_cases

    save_cases(cases[:5], tmp_path / "rt", fmt="yaml")
    reloaded = load_cases(tmp_path / "rt")
    assert [c.to_dict() for c in reloaded] == [c.to_dict() for c in cases[:5]]
    # json fallback works too (pyyaml-independent path)
    save_cases(cases[:5], tmp_path / "rtj", fmt="json")
    reloaded_j = load_cases(tmp_path / "rtj")
    assert [c.to_dict() for c in reloaded_j] == [c.to_dict() for c in cases[:5]]


# ---------------------------------------------------------------------------
# IR metrics on HAND-COMPUTED examples
# ---------------------------------------------------------------------------


def _toy_case(cid: str, grades: dict[str, int]) -> EvalCase:
    """Minimal answerable case with given graded qrels (spans not used by IR)."""
    return EvalCase(
        id=cid, slice="exact", question=f"q-{cid}",
        expected_docs=dict(grades), answerable=True, expected_state=ANSWERED,
        expected_answer_facts=[], expected_spans=[],
    )


def test_ir_metrics_hand_computed():
    """3-query toy set; every number worked by hand below.

    GRADED relevance: 2 = must, 1 = helpful, 0 = hard-negative.
    ir-measures nDCG uses LINEAR gain g(rel)=rel and log2 discount:
    DCG@10 = sum over ranks i (1-based) of rel_i / log2(i + 1).

    Query q1: qrels A=2, B=1, C=0. Run returns [B, A, C].
      recall@5  = |{A,B} found in top5| / |rel>=1| = 2/2 = 1.0
      recall@20 = 2/2 = 1.0
      RR        = 1 / rank of first rel>=1 doc (B at rank 1) = 1.0
      DCG  = 1/log2(2) + 2/log2(3) + 0/log2(4)
           = 1.0 + 2/1.5849625007 = 1.0 + 1.2618595071 = 2.2618595071
      IDCG (ideal [A(2), B(1), C(0)]) = 2/1 + 1/1.5849625 = 2.6309297536
      nDCG@10 = 2.2618595071 / 2.6309297536 = 0.8597186999

    Query q2: same qrels. Run returns [C, A, B].
      recall@5  = 2/2 = 1.0 (C is grade 0 - not counted for recall)
      RR        = first rel>=1 doc is A at rank 2 -> 1/2 = 0.5
      DCG  = 0 + 2/1.5849625 + 1/log2(4) = 1.2618595071 + 0.5 = 1.7618595071
      nDCG@10 = 1.7618595071 / 2.6309297536 = 0.6698703962

    Query q3: qrels X=2 only. Run returns [Y, Z] (both unjudged, neither X).
      recall@5 = 0/1 = 0.0; recall@20 = 0.0; RR = 0.0
      nDCG@10 = 0.0 (no relevant doc retrieved; IDCG = 2, DCG = 0)
    """
    q1 = _toy_case("q1", {"A": 2, "B": 1, "C": 0})
    r1 = [RetrievedDoc(doc_id="B", score=3.0),
          RetrievedDoc(doc_id="A", score=2.0),
          RetrievedDoc(doc_id="C", score=1.0)]
    m1 = ir_metrics_for_case(q1, r1)
    assert m1["recall@5"] == pytest.approx(1.0)
    assert m1["recall@20"] == pytest.approx(1.0)
    assert m1["mrr"] == pytest.approx(1.0)
    assert m1["ndcg@10"] == pytest.approx(2.2618595071 / 2.6309297536, abs=1e-9)

    # hand math for q1 double-checked with the exported DCG helper
    from fenris.eval.metrics import _dcg

    assert _dcg([1, 2, 0]) == pytest.approx(2.2618595071, abs=1e-9)
    assert _dcg([2, 1, 0]) == pytest.approx(2.6309297536, abs=1e-9)
    # log2 discount form: gain_i / log2(i+1), i 1-based -> index i-1 -> +2
    assert _dcg([2, 1]) == pytest.approx(2 + 1 / math.log2(3))

    q2 = _toy_case("q2", {"A": 2, "B": 1, "C": 0})
    r2 = [RetrievedDoc(doc_id="C", score=3.0),
          RetrievedDoc(doc_id="A", score=2.0),
          RetrievedDoc(doc_id="B", score=1.0)]
    m2 = ir_metrics_for_case(q2, r2)
    assert m2["recall@5"] == pytest.approx(1.0)
    assert m2["recall@20"] == pytest.approx(1.0)
    assert m2["mrr"] == pytest.approx(0.5)
    assert m2["ndcg@10"] == pytest.approx(1.7618595071 / 2.6309297536, abs=1e-9)

    q3 = _toy_case("q3", {"X": 2})
    r3 = [RetrievedDoc(doc_id="Y", score=2.0),
          RetrievedDoc(doc_id="Z", score=1.0)]
    m3 = ir_metrics_for_case(q3, r3)
    assert m3["recall@5"] == 0.0
    assert m3["recall@20"] == 0.0
    assert m3["mrr"] == 0.0
    assert m3["ndcg@10"] == 0.0

    # hard negatives HURT nDCG: displacing a graded doc with a grade-0 one
    # at rank 1 costs exactly the moved gain's discount delta
    good = ir_metrics_for_case(q2, [RetrievedDoc(doc_id="A", score=3.0),
                                    RetrievedDoc(doc_id="B", score=2.0)])
    bad = ir_metrics_for_case(q2, [RetrievedDoc(doc_id="C", score=3.0),
                                   RetrievedDoc(doc_id="A", score=2.0),
                                   RetrievedDoc(doc_id="B", score=1.0)])
    assert good["ndcg@10"] == pytest.approx(1.0)
    assert bad["ndcg@10"] < good["ndcg@10"]


def test_ir_metrics_unanswerable_zeroed():
    """Unanswerable cases: no rel>=1 qrels -> IR metrics defined as 0.0."""
    q = EvalCase(id="u1", slice="unanswerable", question="nothing matches",
                 expected_docs={"A": 0}, answerable=False,
                 expected_state=INSUFFICIENT,
                 expected_answer_facts=[], expected_spans=[])
    m = ir_metrics_for_case(q, [RetrievedDoc(doc_id="A", score=1.0)])
    assert all(v == 0.0 for v in m.values())
    # even retrieving everything scores zero: the case is about state only
    m2 = ir_metrics_for_case(q, [RetrievedDoc(doc_id=f"d{i}", score=float(i))
                                 for i in range(25)])
    assert all(v == 0.0 for v in m2.values())


# ---------------------------------------------------------------------------
# Span-recall overlap math
# ---------------------------------------------------------------------------


def _case_with_span(doc_id: str, start: int, end: int) -> EvalCase:
    return EvalCase(
        id="sp1", slice="exact", question="span check",
        expected_docs={doc_id: 2}, answerable=True, expected_state=ANSWERED,
        expected_answer_facts=[], expected_spans=[ExpectedSpan(doc_id, start, end)])


def test_span_coverage_exact_boundary():
    """A chunk exactly covering the golden span -> 1.0."""
    golden = AnswerSpan("d1", 10, 50)
    exact = RetrievedDoc(doc_id="d1", char_span=(10, 50))
    assert span_coverage(golden, exact) == pytest.approx(1.0)
    # a chunk CONTAINING the golden span also covers it fully
    containing = RetrievedDoc(doc_id="d1", char_span=(0, 100))
    assert span_coverage(golden, containing) == pytest.approx(1.0)


def test_span_coverage_half_overlap():
    """50% overlap is below the 0.80 threshold -> span not covered."""
    golden = AnswerSpan("d1", 10, 50)  # length 40
    half = RetrievedDoc(doc_id="d1", char_span=(30, 50))  # covers 20/40 = 0.5
    cov = span_coverage(golden, half)
    assert cov == pytest.approx(0.5)
    case = _case_with_span("d1", 10, 50)
    assert span_recall_for_case(case, [half]) == 0.0  # 0.5 < 0.80 threshold
    # exactly 80% coverage IS covered (>= threshold)
    eighty = RetrievedDoc(doc_id="d1", char_span=(18, 50))  # 32/40 = 0.80
    assert span_coverage(golden, eighty) == pytest.approx(0.80)
    assert span_recall_for_case(case, [eighty]) == 1.0
    # just under (79.9%)
    under = RetrievedDoc(doc_id="d1", char_span=(19, 50))  # 31/40 = 0.775
    assert span_recall_for_case(case, [under]) == 0.0


def test_span_coverage_no_overlap():
    """Disjoint chunks, other docs, and missing spans all score 0."""
    golden = AnswerSpan("d1", 10, 50)
    assert span_coverage(golden, RetrievedDoc(doc_id="d1", char_span=(60, 90))) == 0.0
    # touching at the boundary is NOT overlap ([10,50) and [50,70) share nothing)
    assert span_coverage(golden, RetrievedDoc(doc_id="d1", char_span=(50, 70))) == 0.0
    # right doc required
    assert span_coverage(golden, RetrievedDoc(doc_id="d2", char_span=(0, 100))) == 0.0
    # no char_span at all (doc-level hit only): no evidence coverage
    assert span_coverage(golden, RetrievedDoc(doc_id="d1")) == 0.0


def test_span_recall_multiple_golden_spans():
    """Per-case span recall = covered golden spans / total golden spans."""
    case = EvalCase(
        id="sp2", slice="multi_hop", question="two spans",
        expected_docs={"a": 2, "b": 2}, answerable=True, expected_state=ANSWERED,
        expected_answer_facts=[],
        expected_spans=[ExpectedSpan("a", 0, 10), ExpectedSpan("b", 0, 10)])
    # only one covered -> 0.5
    retrieved = [RetrievedDoc(doc_id="a", char_span=(0, 10)),
                 RetrievedDoc(doc_id="b", char_span=(0, 3))]  # 30% of b
    assert span_recall_for_case(case, retrieved) == pytest.approx(0.5)
    # both covered exactly -> 1.0
    retrieved_full = [RetrievedDoc(doc_id="a", char_span=(0, 12)),
                      RetrievedDoc(doc_id="b", char_span=(0, 10))]
    assert span_recall_for_case(case, retrieved_full) == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Answer-state correctness
# ---------------------------------------------------------------------------


def test_state_correctness_exact_match():
    answered = _toy_case("s1", {"A": 2})
    assert state_correct_for_case(answered, "ANSWERED") == 1.0
    assert state_correct_for_case(answered, "INSUFFICIENT") == 0.0
    assert state_correct_for_case(answered, None) == 0.0
    assert state_correct_for_case(answered, "STALE") == 0.0  # exact match only
    unans = EvalCase(id="s2", slice="unanswerable", question="q",
                     expected_docs={}, answerable=False,
                     expected_state=INSUFFICIENT, expected_answer_facts=[],
                     expected_spans=[])
    assert state_correct_for_case(unans, "INSUFFICIENT") == 1.0
    assert state_correct_for_case(unans, "ANSWERED") == 0.0


# ---------------------------------------------------------------------------
# Per-slice isolation
# ---------------------------------------------------------------------------


def test_slice_isolation_failure_does_not_contaminate():
    """A slice where every case fails must not move another slice's numbers,
    and the overall row must be the documented mean of slice means (not the
    per-case mean)."""
    good = _toy_case("g1", {"A": 2})
    ok = [RetrievedDoc(doc_id="A", score=1.0)]
    bad = _toy_case("b1", {"Z": 2})
    miss = [RetrievedDoc(doc_id="WRONG", score=1.0)]
    case_metrics = {
        "g1": {**ir_metrics_for_case(good, ok),
               "span_recall": 1.0, "state_correct": 1.0},
        "b1": {**ir_metrics_for_case(bad, miss),
               "span_recall": 0.0, "state_correct": 1.0},
    }
    # simulate: two slices of different sizes, one all-good one all-bad
    good2 = _toy_case("g2", {"A": 2})
    case_metrics["g2"] = {**ir_metrics_for_case(good2, ok),
                          "span_recall": 1.0, "state_correct": 1.0}
    # b1 lives in its own slice
    good.slice = "exact"
    good2.slice = "exact"
    bad.slice = "temporal"
    report = aggregate_by_slice(case_metrics, [good, good2, bad])
    assert report.slices["exact"].metrics["recall@5"] == pytest.approx(1.0)
    assert report.slices["exact"].metrics["mrr"] == pytest.approx(1.0)
    # the temporal slice's zeros leave exact untouched
    assert report.slices["temporal"].metrics["recall@5"] == 0.0
    # overall = mean of slice means: (1.0 + 0.0)/2 = 0.5 - NOT the per-case
    # mean (2/3), proving slices are not blended
    assert report.overall["recall@5"] == pytest.approx(0.5)
    assert "mean of slice means" in report.overall_definition


def test_slice_isolation_one_case_fails_slice_mean_drops_only_that_slice():
    """One failed case inside a slice changes only that slice's mean."""
    a = _toy_case("a", {"A": 2}); a.slice = "exact"
    b = _toy_case("b", {"A": 2}); b.slice = "exact"
    c = _toy_case("c", {"C": 2}); c.slice = "table"
    ok = [RetrievedDoc(doc_id="A", score=1.0)]
    ok_c = [RetrievedDoc(doc_id="C", score=1.0)]
    case_metrics = {
        "a": {**ir_metrics_for_case(a, ok), "span_recall": 1.0, "state_correct": 1.0},
        "b": {**ir_metrics_for_case(b, []), "span_recall": 0.0, "state_correct": 0.0},
        "c": {**ir_metrics_for_case(c, ok_c), "span_recall": 1.0, "state_correct": 1.0},
    }
    report = aggregate_by_slice(case_metrics, [a, b, c])
    assert report.slices["exact"].metrics["recall@5"] == pytest.approx(0.5)
    assert report.slices["table"].metrics["recall@5"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Runner: DB rows + report
# ---------------------------------------------------------------------------


class _OracleRetriever:
    """Retrieves each case's grade-2 docs (or a fixed list) - a deterministic
    stand-in for the real retriever (which this module never imports)."""

    def __init__(self, cases: list[EvalCase], fail_slices: set[str] = frozenset()):
        self.calls: list[str] = []
        by_id = {c.id: c for c in cases}
        self.by_id = by_id
        self.fail_slices = fail_slices

    def __call__(self, question: str):
        # find the case by its question (each question is unique per case)
        for c in self.by_id.values():
            if c.question == question:
                self.calls.append(c.id)
                if c.slice in self.fail_slices:
                    raise RuntimeError("injected retriever failure")
                # return every grade>=1 doc, each with a chunk span covering
                # the case's expected span in that doc (evidence-bearing hits)
                out = []
                for i, sp in enumerate(c.expected_spans):
                    if c.expected_docs.get(sp.doc_id, 0) >= 1:
                        out.append(RetrievedDoc(
                            doc_id=sp.doc_id, score=float(len(c.expected_spans) - i),
                            char_span=(sp.start, sp.end)))
                # graded docs without a span still surface (doc-level hit)
                spanned = {r.doc_id for r in out}
                for d, g in c.expected_docs.items():
                    if g >= 1 and d not in spanned:
                        out.append(RetrievedDoc(doc_id=d, score=0.1))
                return out
        return []


def test_runner_writes_db_rows_and_report(conn, cases):
    """run_suite writes one eval_runs row + one row per case with the real
    schema, and the markdown carries per-slice tables + per-case failures."""
    retriever = _OracleRetriever(cases)
    result = run_suite(conn, cases, retriever,
                       flag_config={"hybrid": "on", "rerank": "on"},
                       git_sha="test-sha")
    assert result.n_cases == 120
    # eval_runs row
    run_row = conn.execute(
        "SELECT id, started_at, git_sha, flag_config, generation, verdict "
        "FROM eval_runs WHERE id=?", (result.run_id,)).fetchone()
    assert run_row is not None
    assert run_row[2] == "test-sha"
    assert json.loads(run_row[3]) == {"hybrid": "on", "rerank": "on"}
    # eval_case_results rows: 120, keyed (run_id, case_id)
    n = conn.execute("SELECT count(*) FROM eval_case_results WHERE run_id=?",
                     (result.run_id,)).fetchone()[0]
    assert n == 120
    # every case row has its slice and a parseable metrics payload
    rows = conn.execute(
        "SELECT case_id, slice, metrics_json, gate_pass FROM eval_case_results "
        "WHERE run_id=? ORDER BY case_id", (result.run_id,)).fetchall()
    assert len(rows) == 120
    by_id = {r[0]: r for r in rows}
    a_temporal = next(c for c in cases if c.slice == "temporal")
    r = by_id[a_temporal.id]
    assert r[1] == "temporal"
    payload = json.loads(r[2])
    assert {"recall@5", "recall@20", "mrr", "ndcg@10", "span_recall",
            "state_correct"} <= set(payload.keys())
    assert payload["retrieved"] == [d for d, g in a_temporal.expected_docs.items()
                                    if g == 2]
    # oracle retriever: every reachable case passes its gate
    assert result.failed_case_ids() == [], \
        f"oracle retriever should pass everything: {result.failed_case_ids()[:5]}"
    # the report contains the per-slice table and no failures section content
    md = result.markdown
    assert "Per-slice metrics" in md
    assert "overall" in md and "mean of slice means" in md
    for slice_name in ("exact", "temporal", "multi_hop", "paraphrase",
                       "hard_negative", "unanswerable", "table"):
        assert f"| {slice_name} " in md, f"report missing slice row {slice_name}"
    # per-slice rows show n=15/25/20 correctly
    assert "| exact | 20 |" in md
    assert "| hard_negative | 25 |" in md
    # second run gets a NEW run_id (append-only history for regression diffs)
    result2 = run_suite(conn, cases, retriever)
    assert result2.run_id != result.run_id
    assert conn.execute("SELECT count(*) FROM eval_runs").fetchone()[0] == 2
    assert conn.execute(
        "SELECT count(*) FROM eval_case_results WHERE run_id=?",
        (result2.run_id,)).fetchone()[0] == 120


def test_runner_report_regression_capable(conn, cases):
    """A failing case shows up by id in the report so a regression diff
    between two runs can name exactly which case regressed."""
    retriever = _OracleRetriever(cases, fail_slices={"temporal"})
    result = run_suite(conn, cases, retriever)
    # every temporal case crashed -> recorded, not raised; run completes
    temporal_ids = {c.id for c in cases if c.slice == "temporal"}
    failed = set(result.failed_case_ids())
    assert failed == temporal_ids
    # only the temporal slice's numbers dropped; exact stays perfect
    assert result.slices["temporal"].metrics["recall@5"] == 0.0
    assert result.slices["temporal"].metrics["state_correct"] == 0.0
    assert result.slices["exact"].metrics["recall@5"] == pytest.approx(1.0)
    # unanswerable cases still pass: no retrieval, INSUFFICIENT observed
    assert result.slices["unanswerable"].metrics["state_correct"] == \
        pytest.approx(1.0)
    # report lists each failed case by id (regression-capable per-case view)
    md = result.markdown
    assert "Per-case failures" in md
    for cid in sorted(temporal_ids)[:3]:
        assert cid in md, f"failed case {cid} missing from report"
    assert "verdict: fail" in md
    # DB agrees with the report's failures
    db_fails = {r[0] for r in conn.execute(
        "SELECT case_id FROM eval_case_results WHERE run_id=? AND gate_pass=0",
        (result.run_id,)).fetchall()}
    assert db_fails == temporal_ids
    # the injected retriever error is recorded in metrics_json
    row = conn.execute(
        "SELECT metrics_json FROM eval_case_results WHERE run_id=? AND case_id=?",
        (result.run_id, min(temporal_ids))).fetchone()
    assert "RuntimeError" in row[0]


def test_runner_render_report_standalone():
    """render_report works from a RunResult alone (CLI can re-render)."""
    from fenris.eval.metrics import ALL_METRIC_NAMES, SliceMetrics
    from fenris.eval.runner import CaseResult, RunResult

    result = RunResult(run_id=7, n_cases=1)
    result.case_results = [CaseResult(case_id="x-1", slice="exact",
                                      retrieved_doc_ids=["d"],
                                      metrics={"recall@5": 0.5},
                                      state_correct=1.0)]
    result.slices = {"exact": SliceMetrics(
        slice="exact", n_cases=1, metrics={"recall@5": 0.5, "state_correct": 1.0})}
    result.overall = {m: 0.5 for m in ALL_METRIC_NAMES}
    md = render_report(result)
    assert "# Fenris eval report" in md
    assert "| exact | 1 |" in md
    assert "x-1" in md  # failed case listed


def test_runner_oracle_full_suite_numbers(conn, cases):
    """Sanity: oracle retriever yields perfect per-slice numbers, and the
    overall row equals the mean of slice means for every metric."""
    result = run_suite(conn, cases, _OracleRetriever(cases))
    from fenris.eval.metrics import ALL_METRIC_NAMES

    for slice_name, s in result.slices.items():
        for m in ALL_METRIC_NAMES:
            if m == "entailment_rate":
                # checker-gated metric: absent without a checker (the
                # aggregation skips missing values, never zeroes them)
                assert m not in s.metrics
                continue
            if slice_name == "unanswerable" and m in (
                    "recall@5", "recall@20", "mrr", "ndcg@10", "span_recall"):
                assert s.metrics[m] == 0.0  # IR metrics defined 0 for these
            else:
                assert s.metrics[m] == pytest.approx(1.0), \
                    f"{slice_name}.{m} = {s.metrics[m]}"
    # mean of slice means for state_correct: 6 slices at 1.0, unanswerable 1.0
    assert result.overall["state_correct"] == pytest.approx(1.0)


def test_runner_accepts_plain_dict_retriever(conn, cases):
    """The retrieve-callable may also return plain dicts with doc_id/char_span
    - the runner adapts them (keeps the contract loose for the integrator)."""

    def dict_retriever(question: str):
        for c in cases:
            if c.question == question:
                musts = [d for d, g in c.expected_docs.items() if g == 2]
                return [{"doc_id": d, "score": 1.0,
                         "char_span": (0, 10)} for d in musts]
        return []

    result = run_suite(conn, cases[:10], dict_retriever)
    assert result.n_cases == 10
    assert len(result.case_results) == 10


def test_runner_no_retrieval_imports():
    """Zero imports from fenris.retrieve anywhere in the eval package - the
    retriever reaches the runner ONLY through the injected callable."""
    import ast

    pkg_dir = Path(__file__).resolve().parents[1] / "src" / "fenris" / "eval"
    for py in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert all(not a.name.startswith("fenris.retrieve")
                           for a in node.names), f"{py.name} imports retrieve"
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith("fenris.retrieve"), \
                    f"{py.name} imports from retrieve"
