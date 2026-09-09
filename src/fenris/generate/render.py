"""Deterministic citation resolution (the build plan fix on the evidence contract).

The model may select evidence ids, but THIS renderer is authoritative: it
resolves the model-cited ``[E:<id>]`` markers against the retrieved evidence
set, and model-invented ids, file ids, page numbers, or citation numbers never
reach the user - only resolved evidence ids do.

- an id that is not in the retrieved set -> the citation is DROPPED and the id
 is logged as ``unresolved_ids``;
- a claim (a sentence in the answer) whose citations ALL dropped is counted in
 ``unverifiable_claims`` - the ">= 1 -> state review" signal;
- display citations carry: title, page, a <= 40-word span quote taken from the
 stored ``body_text[char_start:char_end]`` (the citation frame - verbatim,
 re-checkable, never model-paraphrased), and the revision date;
- table evidence cites the table region via ``tables.range`` (``table_id`` ->
 the ``tables`` row's ``range``, e.g. ``Sheet1!B12:F18``), so a hit names an
 exact region rather than a prose page.

Everything here is pure over its inputs (plus an optional read-only DB
connection for the ``tables`` lookup); nothing streams out unverified.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from fenris.generate.generator import GenerationResult
from fenris.retrieve import Evidence

# [E:<16-hex-id>] citation marker in the model's answer text
CITATION_RE = re.compile(r"\[E:([0-9a-zA-Z]+)\]")

# sentences: split on terminal punctuation followed by whitespace/end
_CLAIM_RE = re.compile(r"[^.!?]*[.!?]")

# the display quote is at most this many words (the build plan:
# "<= 40-word quote")
QUOTE_MAX_WORDS = 40


@dataclass
class Citation:
    """One resolved, displayable citation (resolved ids only, by construction).

 ``chunk_id`` + span offsets exist for the jump-to-source viewer; they are
 populated by the renderer from retrieved evidence, never accepted from
 the model, so a forged citation still cannot fabricate a jump target.
 """

    evidence_id: str
    title: str
    page: str
    quote: str
    revision_date: str | None = None
    file_id: str | None = None
    source_revision: str | None = None
    chunk_id: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    table_range: str | None = None
    row_range: str | None = None


@dataclass
class RenderedAnswer:
    """The renderer's output: verified display data + what failed to verify.

 - ``citations``: every RESOLVED citation, in first-use order, deduped.
 - ``dropped_ids``: model-cited ids absent from the retrieved set.
 - ``unverifiable_claims``: answer sentences whose citations all dropped
 (a model can cite an invented id and still be fluent - those sentences
 are exactly the hallucination surface, so they are named, not shown).
 - ``answer_text``: the answer with unresolved ``[E:...]`` markers removed
 (the display layer shows the resolved citation list instead of raw
 markers; no invented id ever survives into user-visible text).
 """

    answer_text: str = ""
    citations: list[Citation] = field(default_factory=list)
    dropped_ids: list[str] = field(default_factory=list)
    unverifiable_claims: list[str] = field(default_factory=list)
    state: str = ""


# --------------------------------------------------------------------------- #
# span quoting
# --------------------------------------------------------------------------- #


def _quote(ev: Evidence) -> str:
    """The <= 40-word display quote for one evidence item.

 The quote is the stored span (``body_excerpt`` already IS the
 ``body_text[char_start:char_end]`` slice - see fenris.retrieve.evidence);
 when the span is missing the header substitutes, and the quote is always
 truncated to QUOTE_MAX_WORDS words. The text is NEVER paraphrased or
 model-rewritten: what the user sees is what was indexed.
 """
    text = (ev.body_excerpt or ev.header or "").strip()
    if not text:
        return ""
    words = text.split()
    if len(words) <= QUOTE_MAX_WORDS:
        return text
    return " ".join(words[:QUOTE_MAX_WORDS])


def _page_label(ev: Evidence) -> str:
    if ev.page_nos:
        return ", ".join(str(p) for p in ev.page_nos)
    if ev.is_table:
        return ev.row_range or "-"
    return "-"


def _table_range(conn: sqlite3.Connection | None, table_id: int | None) -> str | None:
    """The tables.range for a table citation (e.g. 'Sheet1!B12:F18'), or None.

 Read-only; a missing table or a broken connection degrades to None rather
 than raising - the citation still resolves, it just names the chunk's
 row_range instead of the table's full range.
 """
    if conn is None or table_id is None:
        return None
    try:
        row = conn.execute(
            "SELECT range FROM tables WHERE table_id = ?", (int(table_id),)
        ).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] else None


# --------------------------------------------------------------------------- #
# the renderer
# --------------------------------------------------------------------------- #


def render_answer(generation: GenerationResult, evidence_set: list[Evidence],
                  conn: sqlite3.Connection | None = None) -> RenderedAnswer:
    """Resolve a generation's citations against the retrieved evidence set.

 Pure and never-raising: a malformed or empty generation yields an empty
 render with the raw state preserved (the caller decides what that means -
 typically the caller has already resolved the state via the ladder).

 Resolution rules (the build plan, "the renderer is authoritative"):

 - only ids present in ``evidence_set`` resolve; every other cited id is
 dropped and logged in ``dropped_ids``;
 - a sentence (a claim) whose citation set is empty after resolution counts
 as an unverifiable claim;
 - citation display data comes from the STORED evidence record (title, page
 from provenance, quote from the stored span, revision date) - never
 from anything the model wrote.
 """
    ev_by_id: dict[str, Evidence] = {
        ev.evidence_id: ev for ev in evidence_set}
    rendered = RenderedAnswer(state=generation.state)
    answer = generation.answer or ""

    # 1. resolve every cited id in first-use order, deduped
    seen_order: list[str] = []
    for match in CITATION_RE.finditer(answer):
        eid = match.group(1)
        if eid not in ev_by_id:
            if eid not in rendered.dropped_ids:
                rendered.dropped_ids.append(eid)
            continue
        if eid not in seen_order:
            seen_order.append(eid)

    for eid in seen_order:
        ev = ev_by_id[eid]
        rendered.citations.append(Citation(
            evidence_id=eid,
            title=ev.title or ev.path or ev.file_id,
            page=_page_label(ev),
            quote=_quote(ev),
            revision_date=ev.modified_time,
            file_id=ev.file_id,
            source_revision=ev.source_revision,
            chunk_id=ev.chunk_id,
            char_start=ev.char_start,
            char_end=ev.char_end,
            table_range=_table_range(conn, ev.table_id) if ev.is_table else None,
            row_range=ev.row_range if ev.is_table else None,
        ))

    # 2. split the answer into claims (sentences); a claim whose citations all
    # dropped is unverifiable.
    for claim in _CLAIM_RE.findall(answer):
        claim_text = claim.strip()
        if not claim_text:
            continue
        cited = CITATION_RE.findall(claim)
        if not cited:
            # a factual sentence with no citation at all is a rule violation;
            # it is reported, not silently shown
            if generation.state == "ANSWERABLE" and len(claim_text.split()) > 2:
                rendered.unverifiable_claims.append(claim_text)
            continue
        if all(cid not in ev_by_id for cid in cited):
            rendered.unverifiable_claims.append(claim_text)

    # 3. strip unresolved markers from the display text: no invented id is
    # ever user-visible. Resolved markers stay inline (they are verified).
    def _strip(match: re.Match) -> str:
        return match.group(0) if match.group(1) in ev_by_id else ""

    rendered.answer_text = CITATION_RE.sub(_strip, answer)
    rendered.answer_text = re.sub(r"[\t]{2,}", " ", rendered.answer_text)
    return rendered
