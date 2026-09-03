"""Contextual chunk headers (2026-09 round, R3 — Anthropic-style).

The largest measured retrieval-side win in the frontier survey: LLM
context that situates each chunk in its document, prepended to the
EMBED payload (stage A; the FTS side is a gated follow-up). 35%
top-20-retrieval-failure cut embed-only, 49% with BM25, 67% with
rerank (Anthropic, primary; prompt published verbatim).

Citation safety architecture (the part that must be exactly right):

- ``chunks.header_text`` keeps the DETERMINISTIC header in both modes —
  display headers, the renderer's quote source, and the CONFLICTING
  pre-check's evidence terms are untouched.
- The LLM context lives in ``chunks.context_text`` and flows ONLY into
  the embed payload. It never enters ``body_text``, ``char_start/end``,
  ``header_text``, or anything ``evidence_id_for`` reads. ``text_hash``
  stays exactly sha256(header+body) in BOTH modes.
- Determinism: contexts are cached by content (sha256(header||body) +
  model id), temperature 0, so re-ingests reuse the identical context.
- Representation identity: the effective mode flows into
  ``sources.chunker_version`` — deterministic mode records today's
  byte-identical value (zero churn for existing installs); llm mode
  records ``+ctx-llm-v1`` so a mode switch re-represents instead of
  silently mixing vector spaces.
- Embedding reuse: ``_existing_hash_map`` reuses vectors by text_hash
  across any source; under llm mode the payload differs per document
  even for identical bodies, so reuse is disabled across modes via the
  chunker-version filter (and within-batch reuse keys on the ACTUAL
  payload hash — the _embed_hash seam — so a stored vector always
  matches what it embeds).

Every SQL statement is one single-line fixed string with ? placeholders.
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from typing import Protocol

# recorded chunker-version suffix for llm mode (identity, never parsed)
HEADER_VERSION_LLM = "ctx-llm-v1"

# hostile-output bounds: contexts are short situating sentences
MAX_CONTEXT_CHARS = 512

_ANTHROPIC_PROMPT = (
    "<document>\n{{WHOLE_DOCUMENT}}\n</document>\n"
    "Here is the chunk we want to situate within the whole document\n"
    "<chunk>\n{{CHUNK_CONTENT}}\n</chunk>\n"
    "Please give a short succinct context to situate this chunk within "
    "the overall document for the purposes of improving search retrieval "
    "of the chunk. Answer only with the succinct context and nothing else."
)


class ContextGenerator(Protocol):
    """Raw-text seam: a generator that returns free text (not structured)."""

    def generate_raw(self, prompt: str) -> str: ...


def anthropic_prompt(document_text: str, chunk_text: str) -> str:
    """Anthropic's published contextual-retrieval prompt, verbatim."""
    return (_ANTHROPIC_PROMPT
            .replace("{{WHOLE_DOCUMENT}}", document_text)
            .replace("{{CHUNK_CONTENT}}", chunk_text))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_cache(conn: sqlite3.Connection) -> None:
    """Idempotent side-table for generated contexts (content-keyed)."""
    conn.execute("CREATE TABLE IF NOT EXISTS chunk_context_cache (cache_key TEXT NOT NULL, model_id TEXT NOT NULL, context_text TEXT NOT NULL, created_at TEXT, PRIMARY KEY (cache_key, model_id))")
    conn.commit()


def _cache_key(header: str, body: str) -> str:
    h = hashlib.sha256()
    h.update(header.encode("utf-8"))
    h.update(b"\x1f")
    h.update(body.encode("utf-8"))
    return h.hexdigest()


def get_cached(conn: sqlite3.Connection, cache_key: str, model_id: str) -> str | None:
    row = conn.execute("SELECT context_text FROM chunk_context_cache WHERE cache_key = ? AND model_id = ?", (cache_key, model_id)).fetchone()
    return row[0] if row else None


def put_cached(conn: sqlite3.Connection, cache_key: str, model_id: str,
               context_text: str) -> None:
    conn.execute("INSERT OR REPLACE INTO chunk_context_cache(cache_key, model_id, context_text, created_at) VALUES (?, ?, ?, ?)", (cache_key, model_id, context_text, _now_iso()))


def contextualize(conn: sqlite3.Connection, document_text: str,
                   prepared_items: list[dict], context_fn, model_id: str) -> dict[int, str]:
    """Generate/fetch context per body item; returns {prepared-index: ctx}.

    Deterministic via the cache: same header+body+model never calls the
    generator twice. Table blocks are NOT contextualized (synthesized
    text; the deterministic header already carries sheet/page/region).
    A generator failure for one chunk degrades to no context for that
    chunk — ingest never fails on the feature.
    """
    ensure_cache(conn)
    out: dict[int, str] = {}
    doc = document_text[:50_000]  # bound the document side of the prompt
    for idx, item in enumerate(prepared_items):
        header = item.get("header") or ""
        body = (item.get("record").body_text if item.get("record") else "") or ""
        if not body:
            continue
        key = _cache_key(header, body)
        cached = get_cached(conn, key, model_id)
        if cached is not None:
            out[idx] = cached[:MAX_CONTEXT_CHARS]
            continue
        try:
            raw = context_fn(anthropic_prompt(doc, body)) or ""
        except Exception as exc:  # noqa: BLE001 - the feature degrades, never breaks ingest
            import logging

            logging.getLogger("fenris.ingest").debug(
                "context generation failed for one chunk (%s), skipping: %s",
                type(exc).__name__, exc)
            continue
        ctx = raw.strip()[:MAX_CONTEXT_CHARS]
        if not ctx:
            continue
        put_cached(conn, key, model_id, ctx)
        out[idx] = ctx
    conn.commit()
    return out


def chunker_identity(base_version: str, mode: str) -> str:
    """The effective chunker identity recorded in sources.chunker_version.

    Deterministic mode records the base version byte-identically (zero
    churn for existing installs); llm mode appends the ctx marker so a
    mode switch produces a new representation generation.
    """
    if mode == "llm":
        return f"{base_version}+{HEADER_VERSION_LLM}"
    return base_version
