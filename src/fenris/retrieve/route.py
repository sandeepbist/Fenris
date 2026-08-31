"""Routing: the deterministic, first-match-wins policy table (the build plan).

Decision order (the build plan, first match wins):

1. EXACT    - quoted phrase, OR any query term whose ``term_df.active_source_count``
              is <= RARE_DF_THRESHOLD (document-frequency over DISTINCT sources;
              chunk-df is not doc-df), OR a filename/title token.
2. TABLE    - numeric/date tokens AND comparative lexemes AND at least one
              ``is_table=1`` chunk is a live candidate.
3. AGENTIC  - assigned *after* rerank by the orchestrator when the top-1 rerank
              score is below RERANK_FLOOR, or when evidence comes back empty.
              ``route()`` itself never returns AGENTIC (it has no scores yet);
              it exposes the floor so the orchestrator can apply it.
4. NORMAL   - the default pipeline.

Routing is pure lookup: no scoring, no network, never raises on an empty
``term_df`` (a missing term has no row -> not rare -> not EXACT-by-rarity).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

# --- policy constants (provisional; calibrated in M6) -------------------------

POLICY_VERSION = "routes-v1"

# document-frequency (over DISTINCT active sources) at or below which a term is
# considered rare and locks the EXACT route
RARE_DF_THRESHOLD = 3

# rerank top-1 score below this flips NORMAL -> AGENTIC; also the INSUFFICIENT
# floor (the build plan). Provisional; calibrated in M6 against the golden
# suite. Lives here so routing policy stays in one versioned place.
RERANK_FLOOR = 0.3

# comparative lexemes that, combined with numeric/date tokens, signal TABLE
TABLE_LEXEMES = (
    "above", "below", "over", "under", "greater", "less", "more than", "less than",
    "at least", "at most", "higher", "lower", "bigger", "smaller", "most", "least",
    "max", "maximum", "min", "minimum", "average", "mean", "median", "total",
    "sum", "difference", "compare", "comparison", "versus", "vs", "between",
    "increase", "decrease", "growth", "change", "trend", "rank", "top", "bottom",
    "cheapest", "largest", "highest", "lowest", "fastest", "slowest", "best",
    "worst", "exceeds", "surpass", "drop", "rise", "fell", "climbed",
)

# filename/title match triggers when one of these extensions appears in the
# query (``report.pdf``, ``budget.xlsx``). A bare extension query is also a
# filename token.
FILE_EXTENSIONS = (
    ".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".html", ".epub", ".csv",
    ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".ods", ".odt", ".odp", ".rtf",
)

ROUTES = ("EXACT", "TABLE", "AGENTIC", "NORMAL")

# --- tokenizers ----------------------------------------------------------------

# unicode61-ish: alphanumerics + underscore run together, decimals split.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

# numbers (incl. decimals, thousands "1,234", percents "12.5%") and dates
# (ISO "2025-03-14", slashed "3/14/2025", "14/03/2025", month names)
_NUM_RE = re.compile(r"\d[\d,.]*%?")
_DATE_RE = re.compile(
    r"\d{4}-\d{1,2}-\d{1,2}"          # 2025-03-14
    r"|\d{1,2}/\d{1,2}/\d{2,4}"       # 3/14/2025 or 14/03/2025
    r"|\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b"  # March 14
    r"|\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\b"  # 14 March
)

_QUOTED_RE = re.compile(r'"([^"]*)"')
_FILENAME_RE = re.compile(r"\b[\w.-]+\.(?:pdf|docx|xlsx|pptx|txt|md|html|epub|csv"
                          r"|png|jpg|jpeg|tif|tiff|ods|odt|odp|rtf)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RouteDecision:
    """The chosen route plus every signal that produced it.

    ``signals`` is a plain dict (JSON-traceable) with at least:
    ``quoted_phrase``, ``filename_tokens``, ``rare_terms``, ``tokens``,
    ``numeric_tokens``, ``date_tokens``, ``comparative_lexemes``,
    ``table_candidates_exist``, ``rerank_floor``.
    """

    route_id: str
    policy_version: str
    signals: dict = field(default_factory=dict)


# --- signal extraction ---------------------------------------------------------


def _tokens(query: str) -> list[str]:
    return _TOKEN_RE.findall(query.lower())


def has_quoted_phrase(query: str) -> bool:
    """True when the query contains a non-empty double-quoted phrase."""
    return any(phrase.strip() for phrase in _QUOTED_RE.findall(query))


def filename_tokens(query: str) -> list[str]:
    """Tokens from the query that look like a filename/title reference:
    ``budget.xlsx`` or a bare ``xlsx``/``pdf`` extension word."""
    out: list[str] = []
    for m in _FILENAME_RE.finditer(query):
        out.append(m.group(0).lower())
    for tok in _tokens(query):
        if "." + tok in FILE_EXTENSIONS:
            out.append(tok)
    return list(dict.fromkeys(out))  # dedupe, keep order


def _raw_terms(query: str) -> list[str]:
    """Whitespace tokens with punctuation stripped (not _TOKEN_RE: that would
    split ``report.pdf``; here we want the literal 'report.pdf' title word
    too, and dedupe against the filename list)."""
    out: list[str] = []
    for raw in query.lower().split():
        tok = raw.strip("`~!@#$%^&*()-_=+[]{};:'\",.<>/?\\|").strip()
        if tok and tok not in out:
            out.append(tok)
    return out


def rare_terms(conn: sqlite3.Connection, terms: list[str],
               threshold: int = RARE_DF_THRESHOLD) -> list[str]:
    """Terms with ``term_df.active_source_count <= threshold`` (df over
    DISTINCT sources). Terms missing from ``term_df`` are not rare - absence
    of stats means "never indexed", which must not fabricate an EXACT route.
    Empty terms or empty term_df -> [], never raises."""
    terms = [t for t in dict.fromkeys(terms) if t]
    if not terms:
        return []
    try:
        rows = conn.execute(
            "SELECT term, active_source_count FROM term_df WHERE term IN "
            f"({','.join('?' * len(terms))})", terms).fetchall()
    except sqlite3.Error:
        return []
    return [term for term, df in rows if df <= threshold]


def numeric_tokens(query: str) -> list[str]:
    return _NUM_RE.findall(query)


def date_tokens(query: str) -> list[str]:
    return _DATE_RE.findall(query)


def comparative_lexemes(query: str) -> list[str]:
    q = query.lower()
    return [lex for lex in TABLE_LEXEMES if re.search(rf"\b{re.escape(lex)}\b", q)]


def table_candidates_exist(conn: sqlite3.Connection) -> bool:
    """True when at least one live (active, untrashed) chunk has is_table=1."""
    try:
        row = conn.execute(
            "SELECT 1 FROM chunks c JOIN sources s ON s.id = c.source_id"
            " WHERE c.is_table = 1 AND s.active = 1 AND s.trashed = 0"
            " LIMIT 1").fetchone()
    except sqlite3.Error:
        return False
    return row is not None


# --- the route ------------------------------------------------------------------


def route(query: str, conn: sqlite3.Connection) -> RouteDecision:
    """Evaluate the policy table; first match wins. Never raises on an empty
    term_df or an empty corpus."""
    tokens = _tokens(query)
    fn_tokens = filename_tokens(query)
    terms = _raw_terms(query)
    quoted = has_quoted_phrase(query)
    rare = rare_terms(conn, terms)
    numeric = numeric_tokens(query)
    dates = date_tokens(query)
    lexemes = comparative_lexemes(query)
    has_table_chunks = table_candidates_exist(conn)

    signals = {
        "tokens": tokens,
        "quoted_phrase": quoted,
        "filename_tokens": fn_tokens,
        "rare_terms": rare,
        "numeric_tokens": numeric,
        "date_tokens": dates,
        "comparative_lexemes": lexemes,
        "table_candidates_exist": has_table_chunks,
        "rerank_floor": RERANK_FLOOR,
    }

    # 1. EXACT: quoted phrase, rare term, or filename/title token
    if quoted or rare or fn_tokens:
        return RouteDecision("EXACT", POLICY_VERSION, signals)

    # 2. TABLE: numeric/date tokens + comparative lexemes + live table chunks
    if (numeric or dates) and lexemes and has_table_chunks:
        return RouteDecision("TABLE", POLICY_VERSION, signals)

    # 3. AGENTIC: decided post-rerank by the orchestrator (floor / empty
    #    evidence); route() returns NORMAL here and the orchestrator applies
    #    the AGENTIC flip. The floor is carried in the signals for trace.
    # 4. NORMAL: default
    return RouteDecision("NORMAL", POLICY_VERSION, signals)
