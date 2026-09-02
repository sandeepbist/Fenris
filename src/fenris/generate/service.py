"""The M4 orchestrator: ``ask`` - one question to one AnswerPacket.

Pipeline (the build plan, the M4 row):

    search (injected)
      -> verify (per-item: vanished/trashed/retired/re-authed dropped silently,
                 version-hint mismatches flagged stale)
      -> resolve the state ladder
      -> if non-answerable AND agentic budget remains: the bounded retry loop
      -> STALE refresh: drain-and-retry ONCE (refresh_fn), report STALE if
         it persists
      -> assemble the prompt (U-shape + untrusted wrapper)
      -> generate (structured output)
      -> render citations (deterministic; invented ids dropped + logged)
      -> AnswerPacket

Seams (all dependency-injected; this module imports NO retrieval orchestrator):

- ``search_fn(query) -> EvidenceSet | list[Evidence]`` - the CLI wires
  ``fenris.retrieve.search`` (partially applied with conn/qvec/reranker).
- ``refresh_fn() -> EvidenceSet | list[Evidence]`` - the STALE refresh. The
  actual Drive drain is M5's sync; for M4 the default is "re-run search_fn
  once with the same query" (the document says: accept a refresh_fn callable
  that defaults to re-running search_fn - the M5 integrator swaps in a
  drain-then-re-search callable at this seam, nothing else changes).
- ``generator`` - anything satisfying the Generator protocol
  (GeminiGenerator in production, FakeGenerator in tests).

ERROR handling: any exception on the non-ERROR-reserved paths is caught by
``ask`` and surfaced as the system ERROR state with the reason recorded -
ERROR wraps the whole ladder (the build plan), it is never produced by the ladder
itself. Malformed generator output also maps to ERROR (raw text preserved in
the packet for debugging), never a crash.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from fenris.generate import agent as agent_mod
from fenris.generate import prompts
from fenris.generate import render as render_mod
from fenris.generate import verify as verify_mod
from fenris.generate.generator import GenerationResult
from fenris.generate.verify import (
    ANSWERABLE,
    CONFLICTING,
    ERROR,
    INSUFFICIENT,
    STALE,
)
from fenris.retrieve import Evidence


@dataclass
class AnswerPacket:
    """The final M4 product: what the CLI streams to the user."""

    state: str
    answer: str = ""
    citations: list = field(default_factory=list)
    unverifiable_claims: list[str] = field(default_factory=list)
    missing_evidence: list[str] = field(default_factory=list)
    dropped_citation_ids: list[str] = field(default_factory=list)
    evidence_ids: list[str] = field(default_factory=list)
    claims_conflict: bool = False
    trace: dict = field(default_factory=dict)
    error: str | None = None


def _as_evidence_list(result) -> list[Evidence]:
    if result is None:
        return []
    evidence = getattr(result, "evidence", None)
    if isinstance(evidence, list):
        return [e for e in evidence if isinstance(e, Evidence)]
    if isinstance(result, list):
        return [e for e in result if isinstance(e, Evidence)]
    return []


def _trace_route(result) -> str | None:
    decision = getattr(result, "route_decision", None)
    return getattr(decision, "route_id", None) if decision is not None else None


# --------------------------------------------------------------------------- #
# ask
# --------------------------------------------------------------------------- #


def ask(conn: sqlite3.Connection, query: str, qvec, *, generator, search_fn,
        drive_files_rows=None, auth_epoch: int = 0, refresh_fn=None,
        floor: float | None = None) -> AnswerPacket:
    """Answer one question end-to-end. Never raises: system faults -> ERROR.

    - ``qvec``: the pre-embedded query (passed through to nothing here - the
      caller's ``search_fn`` closure already holds it; the parameter exists
      so the CLI signature mirrors the retrieval contract).
    - ``search_fn(query) -> EvidenceSet | list[Evidence]``: the injected
      retrieval entry point.
    - ``drive_files_rows``: current Drive-side metadata for the verification
      pass (rows from the ``drive_files`` table, or an injected view of them).
    - ``refresh_fn() -> EvidenceSet | list[Evidence]``: the STALE refresh;
      defaults to re-running ``search_fn`` once (M5 swaps in the real drain).
    - ``floor``: the INSUFFICIENT rerank floor; defaults to
      ``fenris.retrieve.RERANK_FLOOR``.

    Returns AnswerPacket with the state ladder's verdict, the rendered
    citations, unverifiable claims, and a JSON-traceable ``trace`` dict
    (route_id, stages, tool calls) for the eval runner.
    """
    from fenris.retrieve import RERANK_FLOOR

    if floor is None:
        floor = RERANK_FLOOR

    trace: dict = {
        "query": query,
        "route_id": None,
        "policy_version": None,
        "stages": [],
        "tool_calls": [],
        "verification": {},
        "agentic": None,
        "generator": getattr(generator, "name", "unknown"),
        "dropped_citation_ids": [],
        "state_history": [],
    }

    def _stage(name: str, **payload) -> None:
        trace["stages"].append({"stage": name, **payload})

    try:
        # ------------------------------------------------------------------ #
        # 1. search (injected) + 2. verify
        # ------------------------------------------------------------------ #
        result = search_fn(query)
        evidence = _as_evidence_list(result)
        trace["route_id"] = _trace_route(result)
        if trace["route_id"] is None:
            decision = getattr(result, "route_decision", None)
            trace["policy_version"] = getattr(decision, "policy_version", None)
        _stage("search", evidence_count=len(evidence))

        verification = verify_mod.verify_sources(
            conn, evidence, drive_files_rows, auth_epoch=auth_epoch)
        kept = verification.kept
        trace["verification"] = {
            "kept": len(kept),
            "internal_dropped": len(verification.internal_dropped),
            "stale_ids": list(verification.stale_ids),
            "notes": verification.notes,
        }
        _stage("verify", kept=len(kept), dropped=len(verification.internal_dropped),
               stale=len(verification.stale_ids))

        top_score = kept[0].score if kept else 0.0
        rare = verify_mod._rare_terms_for(conn, query)

        # ------------------------------------------------------------------ #
        # 3. the state ladder on the first pass
        # ------------------------------------------------------------------ #
        state = verify_mod.resolve_state(verification, kept, top_score, floor,
                                         rare_terms=rare)
        trace["state_history"].append(state)
        _stage("ladder", state=state, top_score=top_score, floor=floor)

        # ------------------------------------------------------------------ #
        # 4. non-answerable + budget remains -> the bounded retry loop
        # ------------------------------------------------------------------ #
        if state != ANSWERABLE and state != STALE:
            unresolved = agent_mod._unresolved_from_state(
                verification, verification, top_score, floor)
            if unresolved:
                loop = agent_mod.run_agentic_loop(
                    conn, query, generator=generator, search_fn=search_fn,
                    initial_verification=verification, initial_state=state,
                    top_rerank_score=top_score, floor=floor,
                    auth_epoch=auth_epoch)
                loop_trace = agent_mod.trace_from(loop, state)
                trace["agentic"] = loop_trace
                trace["tool_calls"] = loop_trace["tool_calls"]
                _stage("agentic", rounds=loop.rounds,
                       stop_reason=loop.stop_reason,
                       evidence=len(loop.evidence))

                merged = {ev.evidence_id: ev for ev in kept}
                for ev in loop.evidence:
                    merged.setdefault(ev.evidence_id, ev)
                evidence = list(merged.values())

                # Budget trips resolve to an explicit INSUFFICIENT (never an
                # invented answer): re-run the ladder with the budget flag set
                # so the stop reason - not a guess - decides the state.
                if loop.stop_reason in (agent_mod.STOP_DEADLINE,
                                        agent_mod.STOP_STALL,
                                        agent_mod.STOP_ROUND_LIMIT,
                                        agent_mod.STOP_OPEN_LIMIT):
                    budget_exhausted = loop.stop_reason in (
                        agent_mod.STOP_DEADLINE, agent_mod.STOP_ROUND_LIMIT,
                        agent_mod.STOP_OPEN_LIMIT)
                    state = verify_mod.resolve_state(
                        verification, evidence, top_score, floor,
                        budget_exhausted=budget_exhausted, rare_terms=rare)
                    trace["state_history"].append(state)
                    _stage("agentic_ladder", state=state,
                           stop_reason=loop.stop_reason)

                # re-verify the merged evidence so dropped/stale stay honest
                verification = verify_mod.verify_sources(
                    conn, evidence, drive_files_rows, auth_epoch=auth_epoch)
                kept = verification.kept
                evidence = kept
                trace["verification"] = {
                    "kept": len(kept),
                    "internal_dropped": len(verification.internal_dropped),
                    "stale_ids": list(verification.stale_ids),
                    "notes": verification.notes,
                }

        # ------------------------------------------------------------------ #
        # 5. STALE refresh: drain-and-retry ONCE, then report STALE if it
        #    persists (the actual Drive drain is M5's; the seam is refresh_fn)
        # ------------------------------------------------------------------ #
        if verification.has_stale and not verification.stale_persistent:
            _stage("stale_refresh", attempted=True)
            refresh = refresh_fn
            if refresh is None:
                refresh = lambda: search_fn(query)
            try:
                refreshed = _as_evidence_list(refresh())
            except Exception:  # noqa: BLE001 - refresh failure keeps the old pass
                refreshed = []
            evidence = _merge_prefer(refreshed, evidence)
            verification = verify_mod.verify_sources(
                conn, evidence, drive_files_rows, auth_epoch=auth_epoch)
            verification.stale_persistent = verification.has_stale
            kept = verification.kept
            top_score = kept[0].score if kept else 0.0
            trace["verification"] = {
                "kept": len(kept),
                "internal_dropped": len(verification.internal_dropped),
                "stale_ids": list(verification.stale_ids),
                "notes": verification.notes,
                "refresh_attempted": True,
            }
            _stage("stale_recheck", stale_persistent=verification.stale_persistent)

        # The loop's fresh evidence must be judged on ITS top score, not the
        # first pass's (the first pass's low score is exactly what started the
        # loop). This mirrors the re-resolution the loop performed internally.
        if trace.get("agentic") and trace["agentic"].get("stop_reason") == "RESOLVED":
            top_score = max((ev.score for ev in kept), default=0.0)
            state = verify_mod.resolve_state(verification, kept, top_score,
                                             floor, rare_terms=rare)
            if not trace["state_history"] or trace["state_history"][-1] != state:
                trace["state_history"].append(state)
            _stage("agentic_resolved", state=state, top_score=top_score)

        state = verify_mod.resolve_state(verification, kept, top_score, floor,
                                         rare_terms=rare)
        if not trace["state_history"] or trace["state_history"][-1] != state:
            trace["state_history"].append(state)

        # ------------------------------------------------------------------ #
        # 6. assemble the prompt (U-shape) + 7. generate
        # ------------------------------------------------------------------ #
        prompt = prompts.assemble_prompt(query, kept)
        _stage("prompt", evidence_count=len(kept), chars=len(prompt))
        generation = generator.generate(prompt)
        _stage("generate", state=generation.state, ok=generation.ok)
        trace["generation"] = {
            "model_state": generation.state,
            "cited_evidence_ids": list(generation.cited_evidence_ids),
            "claims_conflict": generation.claims_conflict,
            "missing_evidence": list(generation.missing_evidence),
            "error": generation.error,
        }

        if generation.error is not None:
            # malformed structured output: ERROR state, raw text preserved
            packet = AnswerPacket(
                state=ERROR, answer="", citations=[],
                unverifiable_claims=[], error=generation.error,
                trace=trace)
            packet.trace["generation"]["raw_text"] = generation.raw_text
            packet.trace["error"] = generation.error
            return packet

        # 8. render the citations (the renderer is authoritative)
        rendered = render_mod.render_answer(generation, kept, conn=conn)
        _stage("render", citations=len(rendered.citations),
               dropped=len(rendered.dropped_ids),
               unverifiable=len(rendered.unverifiable_claims))
        trace["dropped_citation_ids"] = list(rendered.dropped_ids)

        # 9. the final state: the system ladder is authoritative when the model
        #    disagrees; the model's claims_conflict can only *raise* the state
        #    to CONFLICTING (the build plan: pre-check OR model-declared), never lower
        #    it - an ANSWERABLE ladder verdict survives an abstaining model,
        #    and vice versa: an INSUFFICIENT ladder verdict is never overridden
        #    to ANSWERABLE by the model.
        final_state = state
        if state == ANSWERABLE and generation.claims_conflict:
            final_state = CONFLICTING
        if state == ANSWERABLE and generation.state == "CONFLICTING":
            final_state = CONFLICTING
        if state == ANSWERABLE and generation.state == "INSUFFICIENT":
            final_state = INSUFFICIENT
        if rendered.unverifiable_claims and final_state == ANSWERABLE:
            # >= 1 unverifiable claim on an answerable answer -> state review:
            # surface the claims, keep the answer, mark for review
            trace["state_review"] = True
        if final_state in (INSUFFICIENT, STALE):
            # The ladder overruled the model: an INSUFFICIENT/STALE verdict
            # means the evidence does not support generation, so the model's
            # answer text must not reach the user in ANY form - stripped
            # markers would read as confident uncited claims (found by the
            # security review: fabricated text displayed under INSUFFICIENT).
            rendered.answer_text = ""
            if rendered.unverifiable_claims:
                rendered.unverifiable_claims = []
        trace["state_history"].append(final_state)

        return AnswerPacket(
            state=final_state,
            answer=rendered.answer_text,
            citations=rendered.citations,
            unverifiable_claims=rendered.unverifiable_claims,
            missing_evidence=list(generation.missing_evidence),
            dropped_citation_ids=rendered.dropped_ids,
            evidence_ids=[ev.evidence_id for ev in kept],
            claims_conflict=generation.claims_conflict,
            trace=trace,
        )

    except Exception as exc:  # noqa: BLE001 - ERROR wraps the whole ladder
        trace["error"] = f"{type(exc).__name__}: {exc}"
        _stage("error", reason=str(exc))
        return AnswerPacket(state=ERROR, answer="", citations=[],
                            unverifiable_claims=[],
                            error=f"{type(exc).__name__}: {exc}", trace=trace)


def _merge_prefer(primary: list[Evidence],
                  secondary: list[Evidence]) -> list[Evidence]:
    """Merge two evidence lists, preferring primary order, deduping by id."""
    out: dict[str, Evidence] = {}
    for ev in primary:
        out.setdefault(ev.evidence_id, ev)
    for ev in secondary:
        out.setdefault(ev.evidence_id, ev)
    return list(out.values())


def ask_stream(conn: sqlite3.Connection, query: str, qvec, *, generator,
               search_fn, drive_files_rows=None, auth_epoch: int = 0,
               refresh_fn=None, floor: float | None = None):
    """Streaming variant of :func:`ask` for the CLI's ``fenris ask``.

    The retrieval/verify/ladder/prompt stages run identically; the only
    difference is the generation stage, which yields the generator's text
    chunks as they arrive and then yields the final AnswerPacket. Callers
    consume: ``for chunk_or_packet in ask_stream(...)`` - str items are
    display text, the last item is the AnswerPacket.
    """
    from fenris.retrieve import RERANK_FLOOR

    if floor is None:
        floor = RERANK_FLOOR

    trace: dict = {
        "query": query,
        "route_id": None,
        "policy_version": None,
        "stages": [],
        "tool_calls": [],
        "verification": {},
        "agentic": None,
        "generator": getattr(generator, "name", "unknown"),
        "dropped_citation_ids": [],
        "state_history": [],
    }

    def _stage(name: str, **payload) -> None:
        trace["stages"].append({"stage": name, **payload})

    try:
        result = search_fn(query)
        evidence = _as_evidence_list(result)
        trace["route_id"] = _trace_route(result)
        _stage("search", evidence_count=len(evidence))

        verification = verify_mod.verify_sources(
            conn, evidence, drive_files_rows, auth_epoch=auth_epoch)
        kept = verification.kept
        top_score = kept[0].score if kept else 0.0
        rare = verify_mod._rare_terms_for(conn, query)
        _stage("verify", kept=len(kept))

        state = verify_mod.resolve_state(verification, kept, top_score, floor,
                                         rare_terms=rare)
        trace["state_history"].append(state)

        if verification.has_stale and not verification.stale_persistent:
            refresh = refresh_fn or (lambda: search_fn(query))
            try:
                refreshed = _as_evidence_list(refresh())
            except Exception:  # noqa: BLE001
                refreshed = []
            evidence = _merge_prefer(refreshed, evidence)
            verification = verify_mod.verify_sources(
                conn, evidence, drive_files_rows, auth_epoch=auth_epoch)
            verification.stale_persistent = verification.has_stale
            kept = verification.kept
            top_score = kept[0].score if kept else 0.0
            _stage("stale_recheck", stale_persistent=verification.stale_persistent)
            state = verify_mod.resolve_state(verification, kept, top_score,
                                             floor, rare_terms=rare)

        prompt = prompts.assemble_prompt(query, kept)
        _stage("prompt", evidence_count=len(kept))

        chunks: list[str] = []
        final_result: GenerationResult | None = None
        for item in generator.generate_stream(prompt):
            if isinstance(item, GenerationResult):
                final_result = item
            elif isinstance(item, str):
                chunks.append(item)
                yield item

        generation = final_result or GenerationResult(
            state=INSUFFICIENT, raw_text="".join(chunks),
            error="stream produced no structured result")
        _stage("generate", state=generation.state)

        if generation.error is not None:
            packet = AnswerPacket(state=ERROR, error=generation.error, trace=trace)
            packet.trace["generation"] = {"error": generation.error,
                                          "raw_text": generation.raw_text}
            yield packet
            return

        rendered = render_mod.render_answer(generation, kept, conn=conn)
        trace["dropped_citation_ids"] = rendered.dropped_ids

        final_state = state
        if generation.claims_conflict and final_state == ANSWERABLE:
            final_state = CONFLICTING
        if final_state == ANSWERABLE and generation.state == "CONFLICTING":
            final_state = CONFLICTING
        if rendered.unverifiable_claims and final_state == ANSWERABLE:
            trace["state_review"] = True
        if final_state in (INSUFFICIENT, STALE):
            # Same rule as ask(): a ladder verdict that overrules the model
            # withholds the fabricated answer text entirely (security review).
            rendered.answer_text = ""
            if rendered.unverifiable_claims:
                rendered.unverifiable_claims = []
        trace["state_history"].append(final_state)

        yield AnswerPacket(
            state=final_state,
            answer=rendered.answer_text,
            citations=rendered.citations,
            unverifiable_claims=rendered.unverifiable_claims,
            missing_evidence=list(generation.missing_evidence),
            dropped_citation_ids=rendered.dropped_ids,
            evidence_ids=[ev.evidence_id for ev in kept],
            claims_conflict=generation.claims_conflict,
            trace=trace,
        )
    except Exception as exc:  # noqa: BLE001 - ERROR wraps the whole ladder
        trace["error"] = f"{type(exc).__name__}: {exc}"
        yield AnswerPacket(state=ERROR, error=f"{type(exc).__name__}: {exc}",
                           trace=trace)
