"""Golden-suite runner: cases -> injected retrieval -> metrics -> DB + report.

Contract (the integrator adapts the real retriever to this later; this module
imports NOTHING from fenris.retrieve):

 retrieve(question: str) -> list[RetrievedDoc]

where RetrievedDoc has doc_id + char_span (see fenris.eval.metrics). The
runner calls it once per case, scores the result, writes eval_runs /
eval_case_results rows through the real schema, and renders a markdown report
with per-slice tables, a clearly-separated overall row (mean of slice means),
and a per-case failures list suitable for regression diffing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .cases import SLICES, EvalCase, validate_all
from .corpus import AnswerSpan
from .metrics import (
    ALL_METRIC_NAMES,
    RetrievedDoc,
    SliceMetrics,
    aggregate_by_slice,
    ir_metrics_for_case,
    span_recall_for_case,
    state_correct_for_case,
)

SUITE_NAME = "golden"
GENERATION = "gen-v1"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    """Everything recorded for one case (feeds DB row + report)."""

    case_id: str
    slice: str                       # schema field name per the build plan
    retrieved_doc_ids: list[str] = field(default_factory=list)
    metrics: dict[str, float] = field(default_factory=dict)
    state_correct: float = 0.0
    observed_state: str | None = None
    error: str | None = None

    def metrics_json(self) -> str:
        payload = {
            **{k: self.metrics.get(k) for k in ALL_METRIC_NAMES},
            "retrieved": self.retrieved_doc_ids,
            "state_correct": self.state_correct,
            "observed_state": self.observed_state,
        }
        if self.error:
            payload["error"] = self.error
        return json.dumps(payload, sort_keys=True)

    @property
    def gate_pass(self) -> int:
        """1 when this case passes its own gate.

 Deliberately strict for the skeleton: a miss on recall@5 or
 span_recall fails the case; hard_negative cases additionally require
 nDCG@10 == 1.0 (no displacement by a near-duplicate), and every
 answerable case requires the correct answer state. The M6 gate
 calibration will replace exact-1.0 checks with real thresholds.
 """
        must_be_one = ("recall@5", "ndcg@10", "span_recall") \
            if self.slice == "hard_negative" else ("recall@5", "span_recall")
        if self.slice == "unanswerable":
            return int(self.state_correct == 1.0)
        if self.state_correct != 1.0:
            return 0
        return int(all(self.metrics.get(m, 0.0) == 1.0 for m in must_be_one))


@dataclass
class RunResult:
    """The output of run_suite: rows written + per-slice metrics + report."""

    run_id: int
    suite: str = SUITE_NAME
    n_cases: int = 0
    case_results: list[CaseResult] = field(default_factory=list)
    slices: dict[str, SliceMetrics] = field(default_factory=dict)
    overall: dict[str, float] = field(default_factory=dict)
    overall_definition: str = ""
    markdown: str = ""
    db_path: str = ""

    def failed_case_ids(self) -> list[str]:
        return [cr.case_id for cr in self.case_results if cr.gate_pass == 0]


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _observed_state(case: EvalCase, retrieved: list[RetrievedDoc]) -> str:
    """State ladder for the golden skeleton: ANSWERED iff anything retrieved.

 The full ladder (STALE/CONFLICTING/...) arrives with the query path (the answer layer);
 this deterministic proxy is exact for this suite because every answerable
 case expects ANSWERED and every unanswerable case expects INSUFFICIENT
 with no relevant docs in the corpus.
 """
    return "ANSWERED" if retrieved else "INSUFFICIENT"


def entailment_rate_for_case(case: EvalCase, checker) -> float | None:
    """Mean per-sentence NLI entailment of the case's golden answer against
 its retrieved evidence union. None when the case yields nothing to score
 (no answer text or no evidence) — recorded as absent, never zero."""
    answer = getattr(case, "golden_answer", None) or ""
    evidence = getattr(case, "evidence_texts", None) or []
    if not answer or not evidence:
        return None
    from fenris.generate.groundedness import apply_groundedness

    result = apply_groundedness(
        answer, [], checker, threshold=0.0, surviving_floor=0)
    # with no cited ids, every sentence scores against the fallback premise
    if not result.per_sentence:
        return None
    kept = [p["score"] for p in result.per_sentence]
    return sum(kept) / len(kept) if kept else None


def run_suite(
    conn: sqlite3.Connection,
    cases: list[EvalCase],
    retrieve_fn,
    flag_config: dict | None = None,
    manifest_spans: dict[str, list[AnswerSpan]] | None = None,
    git_sha: str | None = None,
    groundedness_checker=None,
) -> RunResult:
    """Run all cases through the injected retriever; persist + report.

 - retrieve_fn: callable(question: str) -> list[RetrievedDoc]
 - flag_config: JSON-serializable dict stored on the run row (ablations)
 - manifest_spans: doc_id -> golden spans (optional; cases carry their own)
 - groundedness_checker: optional NLI checker; when given, each case's
 golden answer sentences are scored against their evidence unions and
 the mean lands in metrics as ``entailment_rate`` (absent when None —
 the aggregation skips missing values, so offline runs are unaffected).

 Writes one eval_runs row and one eval_case_results row per case through
 the real schema, then returns the RunResult with the markdown report.
 Retrieval exceptions are caught per case and recorded as errors (state
 INSUFFICIENT, metrics zeroed) - a crashing case never aborts the run.
 """
    flag_config = dict(flag_config or {})
    if groundedness_checker is not None:
        flag_config["groundedness"] = getattr(groundedness_checker, "name", "unknown")
    started_at = datetime.now(UTC).isoformat(timespec="seconds")

    case_results: list[CaseResult] = []
    case_metrics: dict[str, dict[str, float]] = {}

    for case in cases:
        retrieved: list[RetrievedDoc] = []
        error: str | None = None
        try:
            retrieved = [r if isinstance(r, RetrievedDoc) else RetrievedDoc(
                doc_id=r["doc_id"],
                score=float(r.get("score", 0.0)),
                char_span=(tuple(r["char_span"]) if r.get("char_span") else None),
            ) for r in retrieve_fn(case.question)]
        except Exception as exc:  # noqa: BLE001 - retriever is injected; per-case isolation
            retrieved = []
            error = f"{type(exc).__name__}: {exc}"

        observed = "INSUFFICIENT" if error else _observed_state(case, retrieved)
        ir = ir_metrics_for_case(case, retrieved)
        sr = span_recall_for_case(case, retrieved, manifest_spans)
        sc = state_correct_for_case(case, observed)

        metrics = {**ir, "span_recall": sr, "state_correct": sc}
        if groundedness_checker is not None:
            er = entailment_rate_for_case(case, groundedness_checker)
            if er is not None:
                metrics["entailment_rate"] = er
        case_metrics[case.id] = metrics

        cr = CaseResult(
            case_id=case.id,
            slice=case.slice,
            retrieved_doc_ids=[r.doc_id for r in retrieved],
            metrics={**ir, "span_recall": sr},
            state_correct=sc,
            observed_state=observed,
            error=error,
        )
        case_results.append(cr)

    report = aggregate_by_slice(case_metrics, cases)

    cur = conn.execute(
        """INSERT INTO eval_runs(started_at, git_sha, flag_config, generation, verdict)
 VALUES (?, ?, ?, ?, ?)""",
        (started_at, git_sha, json.dumps(flag_config, sort_keys=True), GENERATION, None),
    )
    run_id = cur.lastrowid
    for cr in case_results:
        conn.execute(
            """INSERT INTO eval_case_results(run_id, case_id, suite, slice,
 metrics_json, gate_pass)
 VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, cr.case_id, SUITE_NAME, cr.slice, cr.metrics_json(), cr.gate_pass),
        )
    verdict = "pass" if all(cr.gate_pass == 1 for cr in case_results) else "fail"
    conn.execute("UPDATE eval_runs SET verdict=? WHERE id=?", (verdict, run_id))
    conn.commit()

    result = RunResult(
        run_id=run_id,
        n_cases=len(cases),
        case_results=case_results,
        slices=report.slices,
        overall=report.overall,
        overall_definition=report.overall_definition,
        db_path=str(getattr(conn, "path", "")),
    )
    result.markdown = render_report(result)
    return result


# ---------------------------------------------------------------------------
# Markdown report
# ---------------------------------------------------------------------------


def _fmt(v: float) -> str:
    if v is None:
        return "-"
    return f"{v:.3f}"


def render_report(result: RunResult) -> str:
    """Markdown: per-slice tables, separate overall row, per-case failures."""
    lines: list[str] = []
    lines.append(f"# Fenris eval report - {result.suite} suite (run {result.run_id})")
    lines.append("")
    lines.append(f"- cases: {result.n_cases}")
    lines.append(f"- verdict: {'pass' if not result.failed_case_ids() else 'fail'}")
    lines.append(f"- overall row: {result.overall_definition}")
    lines.append("")
    lines.append("## Per-slice metrics")
    lines.append("")
    lines.append("Slices are never blended; each row below is computed only from")
    lines.append("that slice's cases. `overall` is the mean of the slice means.")
    lines.append("")
    header = ["slice", "n"] + list(ALL_METRIC_NAMES)
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "|".join(["---"] * len(header)) + "|")
    for slice_name in SLICES:
        s = result.slices.get(slice_name)
        if s is None:
            continue
        row = [slice_name, str(s.n_cases)] + [
            _fmt(s.metrics.get(m)) for m in ALL_METRIC_NAMES]
        lines.append("| " + " | ".join(row) + " |")
    lines.append("| **overall** (mean of slice means) | - | " + " | ".join(
        _fmt(result.overall.get(m)) for m in ALL_METRIC_NAMES) + " |")
    lines.append("")
    failures = [cr for cr in result.case_results if cr.gate_pass == 0]
    lines.append("## Per-case failures")
    lines.append("")
    if not failures:
        lines.append("None.")
    else:
        lines.append("| case | slice | recall@5 | ndcg@10 | span_recall | state_correct |")
        lines.append("|---|---|---|---|---|---|")
        for cr in sorted(failures, key=lambda c: (c.slice, c.case_id)):
            m = cr.metrics
            sc = cr.state_correct
            lines.append("| " + " | ".join([
                cr.case_id, cr.slice,
                _fmt(m.get("recall@5", 0.0)), _fmt(m.get("ndcg@10", 0.0)),
                _fmt(m.get("span_recall", 0.0)), _fmt(sc),
            ]) + " |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry helpers (validation + run in one call)
# ---------------------------------------------------------------------------


def validate_cases_against_manifest(
    cases: list[EvalCase],
    manifest_docs: dict[str, dict],
) -> None:
    """Validate all cases against the loaded manifest; raises on any problem."""
    validate_all(cases, manifest_docs)


def save_report(result: RunResult, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result.markdown, encoding="utf-8")
    return path
