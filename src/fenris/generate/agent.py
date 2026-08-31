"""The bounded agentic retry loop (the build plan; the design notes).

Budgets, all enforced, all recorded:

- MAX_ROUNDS = 4 retrieval rounds;
- MAX_OPENS = 32 ``open`` calls per request;
- DEADLINE_SECONDS = 20 wall-clock.

Tools are typed, read-only, and LOCAL: ``search`` / ``find`` / ``open`` /
``summarize`` over the SQLite projection only. Nothing here touches the
network, Drive, URLs, or a shell - read-only by construction.

- ``search`` delegates to the SAME injected search callable the CLI wires
  (dependency injection: this module imports nothing from
  ``fenris.retrieve.query``; it may import types).
- ``open`` re-checks authorization IMMEDIATELY before returning content:
  ``sources.active = 1 AND sources.trashed = 0`` AND the current
  ``auth_epoch`` - a chunk that was retired/trashed/re-authed between search
  and open returns nothing (the design notes' acceptance test: revoke access between
  search and open; no bytes or citation escape).
- ``find`` locates chunks in one file by a text pattern (local, read-only).
- ``summarize`` produces a derived working note that is NEVER an authoritative
  citation - the final answer cites raw ``open`` evidence only.

Loop policy (deterministic - no model in the loop; the model sees only the
final assembled prompt):

1. the initial search result is passed in by the caller (``service.ask``);
2. a follow-up round runs only when the current state is non-ANSWERABLE,
   unresolved claims remain, and budget allows;
3. each follow-up round reformulates the query from the missing-evidence
   signal (deterministic rewrite: the original query plus the distinct
   missing-evidence keywords), searches again, and merges NEW evidence;
4. stall detection: 2 consecutive rounds with no new evidence -> stop with
   stop reason STALL (the loop then reports INSUFFICIENT, never an invented
   answer - the design notes: "force the budget to zero; verify an explicit abstention
   state rather than an invented answer").

Stop reasons (recorded on the trace): RESOLVED / ROUND_LIMIT / OPEN_LIMIT /
DEADLINE / STALL. Every tool call is logged (tool name, args, result count,
bytes) into the trace dict that feeds the eval runner.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field

from fenris.generate.generator import GenerationResult, parse_structured
from fenris.generate.verify import ANSWERABLE, VerificationResult
from fenris.retrieve import Evidence

MAX_ROUNDS = 4
MAX_OPENS = 32
DEADLINE_SECONDS = 20.0

STOP_RESOLVED = "RESOLVED"
STOP_ROUND_LIMIT = "ROUND_LIMIT"
STOP_OPEN_LIMIT = "OPEN_LIMIT"
STOP_DEADLINE = "DEADLINE"
STOP_STALL = "STALL"


# --------------------------------------------------------------------------- #
# tool results + the toolbox
# --------------------------------------------------------------------------- #


@dataclass
class ToolCall:
    """One logged tool invocation: name, args, and result size."""

    tool: str
    args: dict = field(default_factory=dict)
    result_count: int = 0
    result_bytes: int = 0
    error: str | None = None


@dataclass
class AgenticResult:
    """Everything the retry loop produced for one ask."""

    evidence: list[Evidence] = field(default_factory=list)   # merged, deduped
    rounds: int = 0
    stop_reason: str = STOP_RESOLVED
    unresolved_claims: list[str] = field(default_factory=list)
    generation: GenerationResult | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    queries: list[str] = field(default_factory=list)
    deadline_exceeded: bool = False


def _evidence_key(ev: Evidence) -> str:
    """Identity key for evidence dedup (chunk-level ids are already unique)."""
    return ev.evidence_id


class Toolbox:
    """The four typed read-only tools, bound to a connection and an epoch.

    ``search_fn`` is the INJECTED search callable the CLI wires (signature:
    ``search_fn(query: str) -> list[Evidence]`` or an EvidenceSet-like object
    with ``.evidence``); the toolbox never imports the retrieval orchestrator.
    ``auth_epoch`` is the epoch every access is re-checked against.
    """

    def __init__(self, conn: sqlite3.Connection, search_fn, auth_epoch: int = 0):
        self.conn = conn
        self.search_fn = search_fn
        self.auth_epoch = auth_epoch
        self.log: list[ToolCall] = []
        self.open_count = 0
        self.bytes_returned = 0
        self._opened_cache: dict[str, dict] = {}

    # -- logging --------------------------------------------------------------

    def _log(self, tool: str, args: dict, result, *, count=None, nbytes=None) -> None:
        if count is None:
            count = len(result) if isinstance(result, (list, tuple)) else 0
        if nbytes is None:
            try:
                nbytes = len(str(result).encode("utf-8"))
            except Exception:  # noqa: BLE001 - logging must never raise
                nbytes = 0
        self.log.append(ToolCall(tool=tool, args=args, result_count=int(count),
                                  result_bytes=int(nbytes)))
        self.bytes_returned += int(nbytes)

    # -- search ---------------------------------------------------------------

    def search(self, query: str, top_k: int | None = None) -> list[Evidence]:
        """Run the injected search; returns evidence records (best-first).

        A corpus filter is not a tool argument here: the injected search_fn
        already carries the route/filters - the tool never widens them (a
        generated subquery may never expand the allowed tenant or ACL scope).
        """
        try:
            result = self.search_fn(query)
        except Exception as exc:  # noqa: BLE001 - a tool failure ends the round
            self._log("search", {"query": query}, [], count=0, nbytes=0)
            self.log[-1].error = f"{type(exc).__name__}: {exc}"
            return []
        items = _as_evidence_list(result)
        if top_k is not None and top_k >= 0:
            items = items[:top_k]
        self._log("search", {"query": query, "top_k": top_k}, items)
        return items

    # -- find -----------------------------------------------------------------

    def find(self, file_id: str, pattern: str) -> list[dict]:
        """Locate chunks in ONE file by a text pattern. Local, read-only.

        Returns the live chunk rows (chunk_id, ordinal, page_nos, header,
        span, is_table) for chunks whose text contains the pattern. Retired/
        trashed/re-authed sources are excluded by the same guard ``open`` uses.
        """
        args = {"file_id": file_id, "pattern": pattern}
        try:
            rows = self.conn.execute(
                """SELECT c.chunk_id, c.ordinal, c.page_nos, c.header_text,
                     c.char_start, c.char_end, c.is_table
                   FROM chunks c JOIN sources s ON s.id = c.source_id
                   WHERE s.file_id = ? AND s.active = 1 AND s.trashed = 0
                     AND s.auth_epoch = ?
                     AND (c.header_text LIKE ? OR c.body_text LIKE ?)
                   ORDER BY c.ordinal""",
                (file_id, self.auth_epoch, f"%{pattern}%", f"%{pattern}%"),
            ).fetchall()
        except sqlite3.Error as exc:
            self._log("find", args, [], count=0, nbytes=0)
            self.log[-1].error = f"sqlite: {exc}"
            return []
        out = [
            {"chunk_id": r[0], "ordinal": r[1], "page_nos": r[2],
             "header": r[3], "char_start": r[4], "char_end": r[5],
             "is_table": bool(r[6])}
            for r in rows
        ]
        self._log("find", args, out)
        return out

    # -- open -----------------------------------------------------------------

    def open(self, chunk_id: int) -> dict | None:
        """Return one chunk's exact stored text + provenance, or None.

        Re-checks authorization IMMEDIATELY: ``sources.active = 1``,
        ``sources.trashed = 0``, and ``sources.auth_epoch = <current>``. A
        chunk retired, tombstoned, or re-authed since the search returns
        NOTHING (no bytes, no citation, no existence signal). The returned
        record is immutable evidence: chunk_id, file_id, revision, pages,
        span, header, and the exact stored body text. Opened records are
        cached by evidence id so ``summarize`` can work over them without a
        second content read.
        """
        args = {"chunk_id": chunk_id}
        self.open_count += 1
        try:
            row = self.conn.execute(
                """SELECT c.chunk_id, c.header_text, c.body_text, c.page_nos,
                     c.char_start, c.char_end, s.file_id, s.source_revision,
                     s.title, s.auth_epoch
                   FROM chunks c JOIN sources s ON s.id = c.source_id
                   WHERE c.chunk_id = ?
                     AND s.active = 1 AND s.trashed = 0 AND s.auth_epoch = ?""",
                (chunk_id, self.auth_epoch),
            ).fetchone()
        except sqlite3.Error as exc:
            self._log("open", args, None, count=0, nbytes=0)
            self.log[-1].error = f"sqlite: {exc}"
            return None
        if row is None:
            # denied or vanished: nothing escapes, not even that it existed
            self._log("open", args, None, count=0, nbytes=0)
            return None
        record = {
            "chunk_id": row[0], "header": row[1], "body_text": row[2],
            "page_nos": row[3], "char_start": row[4], "char_end": row[5],
            "file_id": row[6], "source_revision": row[7], "title": row[8],
            "auth_epoch": row[9],
        }
        self._log("open", args, record, count=1,
                  nbytes=len((record["body_text"] or "").encode("utf-8")))
        cache_key = str(record["chunk_id"])
        self._opened_cache[cache_key] = record
        return record

    # -- summarize ------------------------------------------------------------

    def summarize(self, evidence_ids: list[str], max_tokens: int = 128) -> dict:
        """A derived working note over opened evidence - never a citation.

        Deterministic MVP summarize: the headers + span heads of the opened
        evidence, bounded to ``max_tokens`` (heuristic token count). The final
        answer must cite raw ``open`` evidence, never this note (the design notes
        2146-2157), so the note carries its evidence ids for traceability and
        the caller never feeds it to the citation renderer as evidence.
        """
        args = {"evidence_ids": list(evidence_ids), "max_tokens": max_tokens}
        lines: list[str] = []
        count = 0
        for eid in evidence_ids:
            record = self._opened_cache.get(str(eid))
            if record is None:
                continue
            lines.append(f"[{eid}] {record['header']}: "
                         f"{(record['body_text'] or '')[:160]}")
            count += 1
        note = "\n".join(lines)
        # heuristic tokens: whitespace words
        words = note.split()
        if len(words) > max_tokens:
            note = " ".join(words[:max_tokens])
        self._log("summarize", args, note, count=count,
                  nbytes=len(note.encode("utf-8")))
        return {"note": note, "evidence_ids": list(evidence_ids),
                "derived": True}


def _as_evidence_list(result) -> list[Evidence]:
    """Accept a list[Evidence] or an EvidenceSet-like object (``.evidence``)."""
    if result is None:
        return []
    evidence = getattr(result, "evidence", None)
    if isinstance(evidence, list):
        return [e for e in evidence if isinstance(e, Evidence)]
    if isinstance(result, list):
        return [e for e in result if isinstance(e, Evidence)]
    return []


# --------------------------------------------------------------------------- #
# the loop
# --------------------------------------------------------------------------- #


def _reformulate(query: str, missing_evidence: list[str]) -> str:
    """Deterministic query rewrite from the missing-evidence signal.

    MVP has no model in the loop: the rewrite appends the distinct informative
    words of the missing-evidence notes to the original query. The ORIGINAL
    question is always kept (the design notes: never replace the original
    irreversibly) and every rewrite is recorded in the trace's ``queries``.
    """
    stop = {"the", "a", "an", "of", "to", "for", "and", "or", "is", "are",
            "was", "were", "in", "on", "about", "missing", "evidence",
            "not", "no", "any", "with", "from", "that", "this"}
    extra: list[str] = []
    for note in missing_evidence:
        for word in note.lower().replace(",", " ").split():
            word = "".join(ch if ch.isalnum() or ch == "_" else "" for ch in word)
            if len(word) > 2 and word not in stop and word not in extra:
                extra.append(word)
    if not extra:
        return query
    return f"{query} {' '.join(extra[:8])}".strip()


def run_agentic_loop(conn: sqlite3.Connection, query: str, *,
                     generator, search_fn, initial_verification: VerificationResult,
                     initial_state: str, top_rerank_score: float = 0.0,
                     floor: float = 0.0, auth_epoch: int = 0,
                     max_rounds: int = MAX_ROUNDS, max_opens: int = MAX_OPENS,
                     deadline_seconds: float = DEADLINE_SECONDS) -> AgenticResult:
    """Drive the bounded retry loop for one non-answerable first pass.

    Round 1 IS the caller's initial search (already run): the loop starts from
    its verification + state and only spends further rounds when the state is
    non-ANSWERABLE. Each round: reformulate -> search -> merge new evidence ->
    re-verify -> re-resolve the state; the loop stops at the first
    ANSWERABLE/CONFLICTING resolution or when a budget trips.

    Budgets (all recorded in the trace): rounds <= max_rounds, opens <=
    max_opens, wall clock <= deadline_seconds. Stall detection: 2 consecutive
    rounds yielding no new evidence -> stop reason STALL. Every stop maps to
    an explicit abstention (INSUFFICIENT) - never an invented answer.
    """
    deadline = time.monotonic() + deadline_seconds
    toolbox = Toolbox(conn, search_fn, auth_epoch=auth_epoch)

    merged: dict[str, Evidence] = {}
    for ev in initial_verification.kept:
        merged[ev.evidence_id] = ev

    result = AgenticResult(
        evidence=list(merged.values()),
        rounds=1,
        stop_reason=STOP_RESOLVED,
    )
    result.queries.append(query)

    state = initial_state
    unresolved = list(_unresolved_from_state(state, initial_verification,
                                             top_rerank_score, floor))
    result.unresolved_claims = unresolved

    # round 1 already resolved: nothing to retry
    if state == ANSWERABLE or not unresolved:
        result.tool_calls = toolbox.log
        return result

    no_new_streak = 0
    rounds_used = 1

    while rounds_used < max_rounds:
        if time.monotonic() > deadline:
            result.stop_reason = STOP_DEADLINE
            result.deadline_exceeded = True
            break
        if toolbox.open_count >= max_opens:
            result.stop_reason = STOP_OPEN_LIMIT
            break

        reformulated = _reformulate(query, unresolved)
        result.queries.append(reformulated)
        found = toolbox.search(reformulated)

        new_ids = [ev.evidence_id for ev in found if ev.evidence_id not in merged]
        for ev in found:
            merged.setdefault(ev.evidence_id, ev)

        rounds_used += 1
        result.rounds = rounds_used

        if not new_ids:
            no_new_streak += 1
            if no_new_streak >= 2:
                result.stop_reason = STOP_STALL
                break
            # one empty round so far: one more reformulation is allowed
            continue
        no_new_streak = 0

        # re-verify the merged set and re-resolve the state on the fresh pass
        recheck = _reverify(toolbox, merged.values())
        # the fresh pass's top score is the best score among the NEW items -
        # old items were already judged below the floor
        fresh_top = max((ev.score for ev in merged.values()), default=0.0)
        result.unresolved_claims = unresolved
        recheck_state = _resolve_with_floor(recheck, merged.values(),
                                            fresh_top, floor)
        if recheck_state == ANSWERABLE:
            result.stop_reason = STOP_RESOLVED
            state = ANSWERABLE
            break
        if recheck_state == "CONFLICTING":
            result.stop_reason = STOP_RESOLVED
            state = "CONFLICTING"
            break
        unresolved = [
            (f"round {rounds_used}: state {recheck_state}, "
             f"{len(new_ids)} new items")
        ]

    else:
        result.stop_reason = STOP_ROUND_LIMIT

    if rounds_used >= max_rounds and result.stop_reason == STOP_RESOLVED \
            and state != ANSWERABLE and state != "CONFLICTING":
        result.stop_reason = STOP_ROUND_LIMIT

    result.evidence = list(merged.values())
    result.tool_calls = toolbox.log
    return result


def _unresolved_from_state(state: str, verification: VerificationResult,
                           top_score: float, floor: float) -> list[str]:
    """The named unresolved-claim signal for a non-answerable first pass.

    A second search is allowed ONLY when the current evidence leaves a named
    claim unresolved, contradictory, or below the calibrated support floor
    (the design notes) - so the signal must be concrete: which condition fired.
    """
    if state == ANSWERABLE:
        return []
    notes: list[str] = []
    if not verification.kept:
        notes.append("no evidence survived verification for this question")
    if verification.has_stale:
        notes.append("cited sources are stale: refresh needed")
    if top_score < floor:
        notes.append(f"top score {top_score:.3f} below floor {floor:.3f}")
    if state == "CONFLICTING":
        notes.append("evidence conflicts; a clarifying source is missing")
    if state == "INSUFFICIENT":
        notes.append("evidence insufficient: what is missing is unnamed so far")
    return notes or [f"state {state}"]


def _reverify(toolbox: Toolbox, evidence) -> VerificationResult:
    """Re-run the per-item verification over the merged evidence set."""
    from fenris.generate.verify import verify_sources

    return verify_sources(toolbox.conn, list(evidence), None,
                          auth_epoch=toolbox.auth_epoch)


def _resolve_with_floor(verification: VerificationResult, evidence,
                        top_score: float, floor: float) -> str:
    from fenris.generate.verify import resolve_state

    return resolve_state(verification, list(evidence), top_score, floor)


def trace_from(result: AgenticResult, state: str) -> dict:
    """The JSON-traceable summary of one loop run (feeds the eval runner)."""
    return {
        "agentic": True,
        "rounds": result.rounds,
        "stop_reason": result.stop_reason,
        "state": state,
        "queries": result.queries,
        "unresolved_claims": result.unresolved_claims,
        "deadline_exceeded": result.deadline_exceeded,
        "tool_calls": [
            {
                "tool": tc.tool,
                "args": tc.args,
                "result_count": tc.result_count,
                "result_bytes": tc.result_bytes,
                "error": tc.error,
            }
            for tc in result.tool_calls
        ],
        "evidence_ids": [ev.evidence_id for ev in result.evidence],
    }


# re-exported for callers that build GenerationResults from loop output
__all__ = [
    "DEADLINE_SECONDS",
    "MAX_OPENS",
    "MAX_ROUNDS",
    "AgenticResult",
    "ToolCall",
    "Toolbox",
    "parse_structured",
    "run_agentic_loop",
    "trace_from",
]
