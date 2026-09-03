"""Pre-generation verification + the answer-state ladder (the build plan).

The verification step runs *after* evidence selection and *before* the prompt
is built: cited sources are checked against the local index state (the
``sources``/``drive_files`` rows - a local read, no network; the full "drain
Drive then re-check" refresh is M5's sync, plumbed in as ``refresh_fn``).

Two layers:

- :func:`verify_sources` - the per-item pass. Items whose file vanished or
  whose representation was retired/trashed/re-auth-epoch'd are dropped and
  logged as ``internal_dropped`` (UNAUTHORIZED is an internal per-item filter,
  NEVER a surfaced state - dropping silently preserves the "never reveal that
  restricted content exists" rule). Items whose cited ``drive_version`` hint
  differs from the indexed one are flagged ``stale``.
- :func:`resolve_state` - the strict ladder below.

The ladder order (STRICT - first match wins; documented once, applied once):

1. **ERROR**     - set by the CALLER on an exception/system fault; this module
                   never returns it, it just reserves the slot so every layer
                   speaks the same enum.
2. **STALE**     - a stale flag that persisted after ONE refresh retry
                   (``stale_persistent=True`` on the verification result).
3. **INSUFFICIENT** - empty evidence after filtering, OR top rerank score
                   below the calibrated floor, OR the agentic budget was
                   exhausted (``budget_exhausted=True``).
4. **CONFLICTING** - the deterministic pre-check fires: two top candidates
                   from different files, both scoring >= 0.7x the top-1, with
                   overlapping ``term_df``-rare terms and differing revisions.
5. **ANSWERABLE** - otherwise.

UNAUTHORIZED is deliberately absent: it is a per-item internal filter, not a
rung. If EVERY item drops, the ladder falls through to INSUFFICIENT via the
empty-evidence rule.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from fenris.retrieve import Evidence

# the surfaced answer states; ERROR/STALE/UNAUTHORIZED are system-only
ANSWERABLE = "ANSWERABLE"
INSUFFICIENT = "INSUFFICIENT"
CONFLICTING = "CONFLICTING"
STALE = "STALE"
ERROR = "ERROR"

# system-only states the model never sees / never emits (fix #7)
SYSTEM_ONLY_STATES = ("ERROR", "STALE", "UNAUTHORIZED")

# CONFLICTING pre-check constants (the build plan)
CONFLICT_SCORE_RATIO = 0.7          # candidate >= 0.7x the top-1 score
CONFLICT_RARE_DF_THRESHOLD = 3      # a "rare" term: term_df <= this
LADDER_ORDER = (ERROR, STALE, INSUFFICIENT, CONFLICTING, ANSWERABLE)


@dataclass
class VerificationResult:
    """Outcome of the per-item verification pass.

    - ``kept``: evidence that survived (order preserved, scores untouched).
    - ``internal_dropped``: vanished/unauthorized/retired items - logged,
      NEVER surfaced as a state and never revealed to the model or user.
    - ``stale_ids``: evidence ids whose cited ``drive_version``/representation
      hint differs from what the index holds now.
    - ``stale_persistent``: True when a refresh was already attempted and the
      stale flag did not clear (the caller retries exactly once - see
      :func:`fenris.generate.service.ask`).
    """

    kept: list[Evidence] = field(default_factory=list)
    internal_dropped: list[Evidence] = field(default_factory=list)
    stale_ids: list[str] = field(default_factory=list)
    stale_persistent: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def has_stale(self) -> bool:
        return bool(self.stale_ids)


def _row(conn: sqlite3.Connection, sql: str, args: tuple):
    try:
        return conn.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None


# --------------------------------------------------------------------------- #
# the per-item verification pass
# --------------------------------------------------------------------------- #


def verify_sources(conn: sqlite3.Connection, evidence: list[Evidence],
                   drive_files_rows: list | None = None,
                   auth_epoch: int = 0) -> VerificationResult:
    """Verify each evidence item against the local index; never raises.

    An item DROPS (recorded in ``internal_dropped`` - the UNAUTHORIZED path:
    silent, never surfaced as a state, never revealed) when any of:

    - its file vanished: no ``drive_files`` row AND no live representation
      (both the Drive view and the index view are gone);
    - its file is trashed, or its representation is retired (``active=0``)
      or tombstoned (``sources.trashed=1``);
    - its representation was re-indexed under a different auth epoch (access
      was revoked and re-granted: the old chunks must not surface).

    An item is flagged STALE (kept in place, but the ladder will report the
    state) when the CURRENT Drive-side version hint (injected row, or the
    ``drive_files`` table) differs from the hint captured at INDEX time
    (``sources_hints``; written by ingest/sync via :func:`record_indexed_version`).
    A hint that is NULL/None on either side means "no hint" and is never
    treated as a mismatch (Workspace files often carry no version hint;
    absence of a hint must not fabricate staleness).

    ``drive_files_rows`` lets tests/callers inject the current Drive-side
    metadata without touching the DB: a list of mappings with at least
    ``file_id`` and optionally ``trashed``/``drive_version``/``drive_id``;
    when given it takes precedence over the ``drive_files`` table for the
    vanish/trash/version checks.
    """
    result = VerificationResult()
    ensure_hints_table(conn)      # idempotent; no-op on read-only DBs
    injected: dict[tuple[str, str], dict] = {}
    for row in drive_files_rows or []:
        if row is None:
            continue
        mapping = row if isinstance(row, dict) else dict(row)
        drive_id = str(mapping.get("drive_id", "mydrive"))
        injected[(drive_id, str(mapping.get("file_id", "")))] = mapping

    for ev in evidence:
        drive_id = ev.drive_id or "mydrive"
        src = _row(
            conn,
            "SELECT id, active, trashed, auth_epoch, source_revision"
            " FROM sources WHERE file_id = ? AND drive_id = ?",
            (ev.file_id, drive_id))
        file_row = injected.get((drive_id, ev.file_id))
        if file_row is None:
            file_row = _row(
                conn,
                "SELECT trashed, drive_version FROM drive_files"
                " WHERE file_id = ? AND drive_id = ?",
                (ev.file_id, drive_id))
            if file_row is not None:
                file_row = {"trashed": file_row[0], "drive_version": file_row[1]}

        # --- the vanish / authorization checks (silent drop) ----------------
        # "vanished" = no drive_files row AND no live representation: the file
        # is gone from Drive's view of the world. (A live sources row with no
        # drive_files row is an ingest bookkeeping gap, not evidence that the
        # file vanished - it must not silently delete a citable answer.)
        if file_row is None and src is None:
            result.internal_dropped.append(ev)
            result.notes.append(f"dropped {ev.evidence_id}: file vanished")
            continue
        if file_row is not None and int(file_row.get("trashed") or 0):
            result.internal_dropped.append(ev)
            result.notes.append(f"dropped {ev.evidence_id}: file trashed on drive")
            continue
        if src is not None and (
            int(src[1] or 0) == 0            # representation retired
            or int(src[2] or 0) == 1         # representation tombstoned
            or int(src[3] or 0) != int(auth_epoch)  # re-authed out from under us
        ):
            result.internal_dropped.append(ev)
            result.notes.append(f"dropped {ev.evidence_id}: representation "
                                f"unauthorized/retired")
            continue

        # --- the staleness check (kept, flagged) ----------------------------
        # The CURRENT Drive-side hint (injected row or the drive_files table)
        # vs the hint captured at INDEX time (sources_hints). A mismatch means
        # the file moved on Drive after we indexed it - flag, do not drop.
        indexed_version = None
        if src is not None:
            indexed_version = _sources_drive_version(conn, drive_id, ev.file_id)
        cited_hint = file_row.get("drive_version") if file_row else None
        if (cited_hint is not None and indexed_version is not None
                and str(cited_hint) != str(indexed_version)):
            result.stale_ids.append(ev.evidence_id)
            result.notes.append(
                f"stale {ev.evidence_id}: drive_version "
                f"{cited_hint!r} != indexed {indexed_version!r}")
        result.kept.append(ev)
    return result


def _sources_drive_version(conn: sqlite3.Connection, drive_id: str, file_id: str):
    """The indexed representation's ``drive_version`` hint, per file.

    The M0 ``sources`` table does not carry the hint; this reads it from the
    ``sources_hints`` side table (created lazily, idempotently, by
    :func:`ensure_hints_table` - a read-only-DB-compatible upgrade: creation
    is skipped silently when the DB is read-only or the table already
    exists). M5's sync will move the hint onto ``sources`` itself; when it
    does, this function swaps to that column read with zero call-site
    changes.
    """
    try:
        row = conn.execute(
            "SELECT drive_version FROM sources_hints WHERE drive_id = ?"
            " AND file_id = ?", (drive_id, file_id)).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row and row[0] is not None else None


def ensure_hints_table(conn: sqlite3.Connection) -> bool:
    """Create the ``sources_hints`` side table if missing. Idempotent.

    ``(drive_id, file_id) -> drive_version``: the version hint captured at
    INDEX time, which the verification step compares against the CURRENT
    Drive-side hint to detect staleness (the build plan s.6: "items whose file
    vanished or whose drive_version/checksum moved are handled per-item").
    Returns False (and does nothing) when the DB cannot accept the table
    (read-only DBs degrade to "no hint", which never fabricates staleness).
    """
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS sources_hints ("
            " drive_id TEXT NOT NULL,"
            " file_id TEXT NOT NULL,"
            " drive_version TEXT,"
            " PRIMARY KEY (drive_id, file_id))")
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def record_indexed_version(conn: sqlite3.Connection, drive_id: str, file_id: str,
                           drive_version: str | None) -> None:
    """Persist the version hint a representation was indexed from.

    Called by ingest/sync (M2.5+/M5) at representation-commit time. Safe to
    call before ensure_hints_table: a missing table is a silent no-op, never
    a raise (the hint is advisory - its absence means "cannot check", not
    "stale").
    """
    try:
        conn.execute(
            "INSERT OR REPLACE INTO sources_hints(drive_id, file_id, drive_version)"
            " VALUES (?, ?, ?)", (drive_id, file_id, drive_version))
        conn.commit()
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------- #
# the CONFLICTING pre-check
# --------------------------------------------------------------------------- #


def _rare_terms_for(conn: sqlite3.Connection, query: str) -> set[str]:
    """Terms of the query whose term_df.active_source_count <= threshold."""
    if not query:
        return set()
    tokens: list[str] = []
    for raw in query.lower().split():
        tok = "".join(ch if ch.isalnum() or ch == "_" else "" for ch in raw)
        if tok:
            tokens.append(tok)
    tokens = list(dict.fromkeys(tokens))
    if not tokens:
        return set()
    try:
        rows = conn.execute(
            f"SELECT term, active_source_count FROM term_df WHERE term IN "
            f"({','.join('?' * len(tokens))})", tokens).fetchall()
    except sqlite3.Error:
        return set()
    return {t for t, df in rows if df <= CONFLICT_RARE_DF_THRESHOLD}


def conflict_precheck(evidence: list[Evidence], rare_terms: set[str]) -> bool:
    """Deterministic CONFLICTING pre-check (the build plan), no DB needed.

    Fires when ALL hold, on the two top candidates:

    - they come from different files;
    - the second scores >= 0.7x the first;
    - they overlap on at least one *rare* term (the query's term_df-rare
      vocabulary - two random docs sharing "the" is not a conflict);
    - their revisions differ (different revision dates/content generations).

    ``rare_terms`` is the query's rare-term set (see :func:`_rare_terms_for`);
    passing an empty set disables the check, which callers do when term_df
    carries no signal for the query.
    """
    if len(evidence) < 2 or not rare_terms:
        return False
    first, second = evidence[0], evidence[1]
    if first.file_id == second.file_id:
        return False
    if first.score < 0 or second.score < CONFLICT_SCORE_RATIO * first.score:
        return False
    if first.source_revision == second.source_revision:
        return False
    a = _evidence_terms(first)
    b = _evidence_terms(second)
    return bool(rare_terms & a & b)


def _evidence_terms(ev: Evidence) -> set[str]:
    text = f"{ev.header}\n{ev.body_excerpt}".lower()
    return {
        "".join(ch if ch.isalnum() or ch == "_" else "" for ch in w)
        for w in text.split()
    } - {""}


# --------------------------------------------------------------------------- #
# the ladder
# --------------------------------------------------------------------------- #


AnswerState = str


def resolve_state(verification: VerificationResult,
                  evidence_after_filter: list[Evidence],
                  top_rerank_score: float,
                  floor: float,
                  budget_exhausted: bool = False,
                  rare_terms: set[str] | None = None,
                  rerank_scale: bool = True) -> AnswerState:
    """Apply the strict ladder to one verified pass. Pure, never raises.

    Ladder order (STRICT, first match wins):

    1. ERROR        - never returned here; reserved for the caller's
                      exception path (documented so the enum is total).
    2. STALE        - ``verification.stale_persistent``: a stale source that
                      survived ONE refresh retry. (A first-time stale flag is
                      NOT a final state - the caller refreshes and retries
                      once before this fires.)
    3. INSUFFICIENT - no evidence after filtering (covers the
                      every-item-dropped case), OR ``budget_exhausted``, OR
                      ``top_rerank_score`` below ``floor`` **when the score
                      is on the reranker's scale** (``rerank_scale=True``).
                      The EXACT route skips reranking by design, so its top
                      score is on the RRF scale (~1/61) - incomparable with
                      the FlashRank-calibrated floor; passing
                      ``rerank_scale=False`` for that pass exempts it from
                      the floor test (an exact full-phrase match is the
                      strongest relevance signal the engine has).
    4. CONFLICTING  - :func:`conflict_precheck` fires on the top two.
    5. ANSWERABLE   - otherwise.

    ``top_rerank_score`` is the top rerank score of the *pass being judged*
    (after filtering); pass 0.0 for an empty set. UNAUTHORIZED never appears:
    it is an internal per-item filter inside :func:`verify_sources`.
    """
    if verification.stale_persistent:
        return STALE
    if not evidence_after_filter:
        return INSUFFICIENT
    if rerank_scale and top_rerank_score < floor:
        return INSUFFICIENT
    if budget_exhausted:
        return INSUFFICIENT
    if rare_terms is None:
        terms: set[str] = set()
    else:
        terms = rare_terms
    if conflict_precheck(evidence_after_filter, terms):
        return CONFLICTING
    return ANSWERABLE
