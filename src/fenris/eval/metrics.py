"""Eval metrics for the golden suite (the build plan).

IR metrics (ir-measures 0.4.3, graded relevance 2/1/0):

- recall@5, recall@20 (ir_measures.R)
- RR -> MRR per slice (ir_measures.RR averaged)
- nDCG@10 with GRADED relevance. ir-measures/pytrec_eval uses LINEAR gain:
 gain(rel) = rel (verified empirically in tests/test_eval.py against a
 hand computation), and log2 discount. Graded qrels come in as
 ir_measures.Qrel(query_id, doc_id, relevance) and runs as ScoredDoc.

Hand-rolled metrics ir-measures does not cover:

- evidence-span recall: a golden span is COVERED when a retrieved chunk's
 char range overlaps >= 80% of it (coverage fraction of the golden span).
- answer-state correctness: exact string match of expected_state.
- per-slice aggregation: slices are NEVER blended into one number. Every
 metric is reported per slice; a separate OVERALL row is the unweighted
 mean of slice means and is documented as such (it exists for trend
 reading, not for gating).

Interface: pure functions over (case, retrieved) - no retrieval imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import ir_measures as irm
from ir_measures import Qrel, ScoredDoc

from .cases import EvalCase
from .corpus import AnswerSpan

# a golden span counts covered when the retrieved chunk overlaps >= 80% of it
SPAN_COVERAGE_THRESHOLD = 0.80

IR_MEASURE_NAMES = ("recall@5", "recall@20", "mrr", "ndcg@10")


# ---------------------------------------------------------------------------
# Retriever-facing types
# ---------------------------------------------------------------------------


@dataclass
class RetrievedDoc:
    """What the injected retrieval callable returns per hit.

 ``doc_id`` maps to a corpus manifest doc; ``char_span`` is the (start, end)
 tuple of the chunk's text within that doc - the span-recall interface.
 """

    doc_id: str
    score: float = 0.0
    char_span: tuple[int, int] | None = None  # (start, end) into doc text

    @property
    def start(self) -> int:
        return self.char_span[0] if self.char_span else 0

    @property
    def end(self) -> int:
        return self.char_span[1] if self.char_span else 0


RetrieveFn = "callable[[str], list[RetrievedDoc]]"


# ---------------------------------------------------------------------------
# Per-case IR scoring (ir-measures, graded)
# ---------------------------------------------------------------------------


def _case_qrels(case: EvalCase) -> list[Qrel]:
    """Graded qrels: 2 must / 1 helpful / 0 hard-negative.

 Hard-negative docs are qrels at grade 0 so ir-measures sees them as
 non-relevant: retrieving them does not help recall and HURTS nDCG (a
 grade-0 doc in the top ranks displaces graded docs).
 """
    return [Qrel(case.id, doc_id, grade) for doc_id, grade in case.expected_docs.items()]


def _case_run(case: EvalCase, retrieved: list[RetrievedDoc]) -> list[ScoredDoc]:
    # score descending = rank order; enforce it explicitly so the metric is
    # never sensitive to how the retriever sorted (use position as rank)
    return [
        ScoredDoc(case.id, r.doc_id, float(len(retrieved) - i))
        for i, r in enumerate(retrieved)
    ]


def _ideal_ranking(grades: list[int]) -> list[int]:
    """IDCG ranking for nDCG hand-checks in tests (linear gain, log2 discount)."""
    return sorted(grades, reverse=True)


def _dcg(grades: list[int]) -> float:
    """Linear-gain DCG: sum gain_i / log2(i+1), i 1-based. Exported for tests."""
    return sum(g / math.log2(i + 2) for i, g in enumerate(grades))


def ir_metrics_for_case(case: EvalCase, retrieved: list[RetrievedDoc]) -> dict[str, float]:
    """Per-case recall@5, recall@20, RR, nDCG@10 (graded, ir-measures).

 Unanswerable cases have no grade>=1 docs, so every IR metric is defined as
 0.0 by ir-measures; the runner scores them on state correctness only and
 this function returns zeros (documented, and the caller excludes them from
 slice means).
 """
    if case.slice == "unanswerable" or not any(
        g >= 1 for g in case.expected_docs.values()
    ):
        return {name: 0.0 for name in IR_MEASURE_NAMES}
    qrels = _case_qrels(case)
    run = _case_run(case, retrieved)
    agg = irm.calc_aggregate(
        [irm.R@5, irm.R@20, irm.RR, irm.nDCG@10], qrels, run)
    return {
        "recall@5": float(agg[irm.R@5]),
        "recall@20": float(agg[irm.R@20]),
        "mrr": float(agg[irm.RR]),
        "ndcg@10": float(agg[irm.nDCG@10]),
    }


# ---------------------------------------------------------------------------
# Evidence-span recall (hand-rolled)
# ---------------------------------------------------------------------------


def span_coverage(golden: AnswerSpan, retrieved: RetrievedDoc) -> float:
    """Fraction of the golden span covered by one retrieved chunk (0.0-1.0)."""
    if retrieved.char_span is None or retrieved.doc_id != golden.doc_id:
        return 0.0
    chunk = AnswerSpan(retrieved.doc_id, retrieved.start, retrieved.end)
    return golden.overlaps_fraction(chunk)


def span_recall_for_case(
    case: EvalCase,
    retrieved: list[RetrievedDoc],
    manifest_spans: dict[str, list[AnswerSpan]] | None = None,
    threshold: float = SPAN_COVERAGE_THRESHOLD,
) -> float:
    """Fraction of the case's golden spans covered at >= threshold.

 Golden spans come from the case's expected_spans when present, else from
 the manifest's per-doc answer spans for every grade>=1 doc (the runner
 passes the case's own spans; manifest fallback keeps the interface usable
 for ad-hoc analysis).
 """
    if case.slice == "unanswerable":
        return 0.0
    gold: list[AnswerSpan] = []
    if case.expected_spans:
        gold = [AnswerSpan(s.doc_id, s.start, s.end) for s in case.expected_spans]
    elif manifest_spans is not None:
        for doc_id, grade in case.expected_docs.items():
            if grade >= 1:
                gold.extend(manifest_spans.get(doc_id, []))
    if not gold:
        return 0.0
    covered = 0
    for g in gold:
        best = 0.0
        for r in retrieved:
            if r.doc_id != g.doc_id:
                continue
            cov = span_coverage(g, r)
            best = max(best, cov)
        if best >= threshold:
            covered += 1
    return covered / len(gold)


# ---------------------------------------------------------------------------
# Answer-state correctness
# ---------------------------------------------------------------------------


def state_correct_for_case(case: EvalCase, actual_state: str | None) -> float:
    """1.0 when the observed state exactly matches expected_state, else 0.0.

 ``actual_state=None`` means the pipeline crashed / produced nothing: that
 is a miss, not an exception, in the golden suite.
 """
    return 1.0 if actual_state == case.expected_state else 0.0


# ---------------------------------------------------------------------------
# Slice aggregation - never blended
# ---------------------------------------------------------------------------

ALL_METRIC_NAMES = IR_MEASURE_NAMES + ("span_recall", "state_correct", "entailment_rate")


@dataclass
class SliceMetrics:
    """Metrics for ONE slice. Never mixed with another slice's cases."""

    slice: str                       # schema field name per the build plan
    n_cases: int
    metrics: dict[str, float] = field(default_factory=dict)
    case_ids: list[str] = field(default_factory=list)

    def to_row(self) -> dict:
        return {"slice": self.slice, "n_cases": self.n_cases, **self.metrics}


@dataclass
class MetricsReport:
    """Per-slice metrics + a clearly-separated overall row."""

    slices: dict[str, SliceMetrics] = field(default_factory=dict)
    overall: dict[str, float] = field(default_factory=dict)
    overall_definition: str = (
        "mean of slice means (unweighted; for trend reading only, never a gate)")

    def per_case_failures(
        self,
        case_metrics: dict[str, dict[str, float]],
    ) -> list[dict]:
        """Cases with any zero-metric or below-floor value, for the report."""
        failures = []
        for case_id, m in case_metrics.items():
            bad = {k: v for k, v in m.items()
                   if k in ALL_METRIC_NAMES and v is not None and v < 1.0}
            if bad:
                failures.append({"case_id": case_id, "below": bad})
        return failures


def aggregate_by_slice(
    case_metrics: dict[str, dict[str, object]],
    cases: list[EvalCase],
) -> MetricsReport:
    """Aggregate per-case metrics into per-slice means.

 ``case_metrics``: case_id -> {metric_name: float or None}. Slices are
 computed independently: a failure in one slice cannot change another
 slice's numbers. The overall row is the mean of slice means and carries
 ``overall_definition`` so no reader mistakes it for a per-case mean.
 """
    report = MetricsReport()
    by_slice: dict[str, list[EvalCase]] = {}
    for c in cases:
        by_slice.setdefault(c.slice, []).append(c)
    for slice_name, slice_cases in sorted(by_slice.items()):
        means: dict[str, float] = {}
        for metric in ALL_METRIC_NAMES:
            vals = []
            for c in slice_cases:
                v = case_metrics.get(c.id, {}).get(metric)
                if v is not None:
                    vals.append(float(v))
            if vals:
                means[metric] = sum(vals) / len(vals)
        report.slices[slice_name] = SliceMetrics(
            slice=slice_name,
            n_cases=len(slice_cases),
            metrics=means,
            case_ids=[c.id for c in slice_cases],
        )
    # overall = mean of slice means, unweighted, documented (NOT per-case mean)
    for metric in ALL_METRIC_NAMES:
        vals = [s.metrics[metric] for s in report.slices.values() if metric in s.metrics]
        if vals:
            report.overall[metric] = sum(vals) / len(vals)
    return report
