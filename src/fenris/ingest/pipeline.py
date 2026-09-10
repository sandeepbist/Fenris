"""ingest_file: route -> parse -> chunk -> headers -> embed -> ONE transaction.

The correctness invariant (the build plan sections 4-5): network work never happens
inside a DB transaction. Embedding runs first; the transaction is short and
only writes. Crash before commit leaves nothing half-written.

What one call does:

1. Route (router.route) - skip-listed formats write one ``sources`` row
 with status='skipped' and the reason, nothing else. Shortcuts are
 routed away (the target file is ingested on its own merits).
2. Parse (parse.parse_bytes) - bounded, never raises; failures write
 status='failed' + error.
3. Chunk (chunk.chunk_body / build_table_record) - children, parents, and
 table row-blocks; tables never touch the splitter.
4. Headers (headers.build_header) - deterministic, zero LLM calls.
5. Embed (embed.Embedder) - chunk-hash dedup: a text_hash already stored
 reuses its chunks_vec embedding and skips the API call. Parents are
 kept but never embedded.
6. One transaction: {retire superseded sources rows for this file, insert
 the new sources row, insert chunks + tables + chunks_vec rows (with
 active/trashed/auth_epoch mirrored from the source), update term_df}.

Re-ingesting an unchanged representation (same source_revision and
toolchain) touches nothing - no new rows, no writes. A revision bump
retires the old representation inside the same transaction (old rows
active=0, their chunks_vec rows active=0).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass

from fenris.ingest import chunk as chunk_mod
from fenris.ingest import parse as parse_mod
from fenris.ingest import router as router_mod
from fenris.ingest import scanner as scanner_mod
from fenris.ingest.chunk import CHUNKER_FULL_VERSION
from fenris.ingest.headers import build_header
from fenris.ingest.parse import PARSER_VERSION, _table_page_label

DRIVE_ID_DEFAULT = "mydrive"

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_UNCHANGED = "unchanged"


@dataclass(frozen=True)
class FileMeta:
    """Drive file metadata the pipeline needs (a narrow view of drive_files)."""

    drive_id: str = DRIVE_ID_DEFAULT
    file_id: str = ""
    title: str = ""
    path: str = ""
    mime_type: str = ""
    modified_time: str = ""
    source_revision: str = ""       # md5Checksum, or 'exp:'+sha256(export bytes)
    drive_version: str = ""         # change-detection hint only, never identity
    trashed: bool = False
    auth_epoch: int = 0
    is_workspace: bool = False
    is_shortcut: bool = False
    resource_key: str = ""


@dataclass(frozen=True)
class IngestResult:
    """Outcome of one ingest_file call (nothing thrown at the caller)."""

    status: str                    # ok | failed | skipped | unchanged | routed_away
    source_id: int | None = None
    reason: str | None = None
    chunks_inserted: int = 0
    tables_inserted: int = 0
    chunks_embedded: int = 0       # API-billed embeds (dedup skipped the rest)
    chunks_deduped: int = 0        # reused stored embeddings


def _body_hash(body_text: str) -> str:
    """sha256(body) - the cross-folder dedup key (text_hash correctly
 includes the header, so folder-differing duplicates never collide)."""
    h = hashlib.sha256()
    h.update((body_text or "").encode("utf-8"))
    return h.hexdigest()


def _now_iso() -> str:
    """UTC index-time stamp (the sources.created_at digest/ordering column)."""
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _text_hash(header_text: str, body_text: str) -> str:
    """sha256(header+body) - the chunk-level dedup + resume key."""
    h = hashlib.sha256()
    h.update(header_text.encode("utf-8"))
    h.update(body_text.encode("utf-8"))
    return h.hexdigest()


def _json_pages(pages: tuple[int, ...]) -> str:
    return json.dumps(list(pages))


def _chunker_identity() -> str:
    """The effective chunker version: byte-identical to CHUNKER_FULL_VERSION
 in deterministic mode (zero churn); +ctx-llm-v1 in llm mode (R3), so a
 mode switch re-represents instead of mixing vector spaces."""
    from fenris.config import contextual_headers_mode
    from fenris.ingest.contextualize import chunker_identity

    return chunker_identity(CHUNKER_FULL_VERSION, contextual_headers_mode())


def _representation_key(meta: FileMeta, target_mime: str, model_id: str) -> tuple:
    """The sources UNIQUE-constraint tuple: identity, never Drive's version."""
    return (
        meta.drive_id,
        meta.file_id,
        meta.source_revision,
        target_mime,
        PARSER_VERSION,
        _chunker_identity(),
        model_id,
    )


def _existing_source_row(conn, meta: FileMeta, target_mime: str, model_id: str):
    """The sources row for this exact representation, if any (dedup path)."""
    return conn.execute(
        """SELECT id, active FROM sources WHERE drive_id=? AND file_id=?
 AND source_revision=? AND repr_mime=? AND parser_version=?
 AND chunker_version=? AND embedding_model=?""",
        _representation_key(meta, target_mime, model_id),
    ).fetchone()


def _existing_hash_map(conn, hashes: list[str]) -> dict[str, bytes]:
    """Stored embeddings for chunk texts seen before — same contextual MODE
 only (R3: an llm-mode payload differs per document even for identical
 bodies, so a deterministic-era vector must never back an llm-mode embed).
 The join follows this file's own existing convention."""
    from fenris.config import contextual_headers_mode
    from fenris.ingest.contextualize import chunker_identity

    mode_version = chunker_identity(CHUNKER_FULL_VERSION, contextual_headers_mode())
    out: dict[str, bytes] = {}
    try:
        for start in range(0, len(hashes), 500):
            batch = hashes[start : start + 500]
            placeholders = ",".join("?" * len(batch))
            rows = conn.execute(
                f"""SELECT c.text_hash, v.embedding
                      FROM chunks c JOIN chunks_vec v ON v.chunk_id = c.chunk_id
                      JOIN sources s ON s.id = c.source_id
                     WHERE c.text_hash IN ({placeholders}) AND s.chunker_version = ?""",
                [*batch, mode_version],
            ).fetchall()
            for text_hash, embedding in rows:
                out[text_hash] = embedding
    except sqlite3.Error:
        # no vec0 table when sqlite-vec is unloadable: nothing to reuse
        return {}
    return out


def _normalize_terms(text: str) -> list[str]:
    """Distinct normalized terms of one text (term_df vocabulary)."""
    return chunk_mod._normalize_token(text).split()


def _terms_of_source(conn, source_id: int) -> list[str]:
    """The term vocabulary of one source's chunks (for term_df retirement)."""
    seen: dict[str, None] = {}
    rows = conn.execute(
        "SELECT body_text FROM chunks WHERE source_id=?", (source_id,)
    ).fetchall()
    for (body,) in rows:
        for term in _normalize_terms(body):
            seen.setdefault(term, None)
    return list(seen)


def _bump_term_df(conn, terms: list[str], delta: int) -> None:
    """Apply +1/-1 to term_df for each term (distinct active sources count)."""
    for term in terms:
        row = conn.execute(
            "SELECT active_source_count FROM term_df WHERE term=?", (term,)
        ).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO term_df(term, active_source_count) VALUES (?, ?)",
                (term, max(0, delta)),
            )
        else:
            conn.execute(
                "UPDATE term_df SET active_source_count = ? WHERE term=?",
                (max(0, row[0] + delta), term),
            )


def _retire_old_sources(conn, meta: FileMeta) -> list[int]:
    """Set active=0 on this file's other active representations; return ids.

 Also mirrors the retirement into chunks_vec so filtered KNN drops them
 during the scan (verified vec0 behavior, the build plan). Must run
 inside the ingest transaction.
 """
    retired: list[int] = []
    rows = conn.execute(
        """SELECT id FROM sources WHERE drive_id=? AND file_id=? AND active=1""",
        (meta.drive_id, meta.file_id),
    ).fetchall()
    for (old_id,) in rows:
        conn.execute(
            "UPDATE sources SET active=0 WHERE id=?", (old_id,)
        )
        try:
            conn.execute(
                """UPDATE chunks_vec SET active=0 WHERE chunk_id IN
 (SELECT chunk_id FROM chunks WHERE source_id=?)""",
                (old_id,),
            )
        except sqlite3.Error:
            # no vec0 table when sqlite-vec is unloadable; the mirror is
            # best-effort by design (KNN is degraded in that environment)
            pass
        retired.append(old_id)
    return retired


def _insert_source(conn, meta: FileMeta, target_mime: str, model_id: str) -> int:
    """Insert the new sources row (the representation manifest entry)."""
    cur = conn.execute(
        """INSERT INTO sources(drive_id, file_id, source_revision, repr_mime,
 parser_version, chunker_version, embedding_model, embed_dims,
 active, trashed, auth_epoch, status, error, attempts, title, path,
 modified_time, resource_key, created_at)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            meta.drive_id,
            meta.file_id,
            meta.source_revision,
            target_mime,
            PARSER_VERSION,
            CHUNKER_FULL_VERSION,
            model_id,
            _model_dims(model_id),
            1,
            1 if meta.trashed else 0,
            meta.auth_epoch,
            STATUS_OK,
            None,
            1,
            meta.title,
            meta.path,
            meta.modified_time,
            meta.resource_key,
            _now_iso(),
        ),
    )
    return cur.lastrowid


def _model_dims(model_id: str) -> int:
    """Parse ':dims' from the model id ('gemini-embedding-001:1536')."""
    try:
        return int(model_id.rsplit(":", 1)[1])
    except (IndexError, ValueError):
        return 1536


def _write_status_row(
    conn, meta: FileMeta, target_mime: str, status: str, error: str | None
) -> int:
    """A skip/failed representation: one sources row, no content work.

 The row is inserted (never OR REPLACE: replacing a UNIQUE row deletes
 first, and the delete would cascade away a still-referenced
 representation's chunks). If the identical skip row already exists it
 is simply re-recorded via the UNIQUE tuple update.
 """
    with conn:
        retired = _retire_old_sources(conn, meta)
        row = conn.execute(
            """SELECT id FROM sources WHERE drive_id=? AND file_id=?
 AND source_revision=? AND repr_mime=? AND parser_version=?
 AND chunker_version=? AND embedding_model=?""",
            (
                meta.drive_id,
                meta.file_id,
                meta.source_revision,
                target_mime,
                PARSER_VERSION,
                CHUNKER_FULL_VERSION,
                "none:0",
            ),
        ).fetchone()
        if row is not None:
            source_id = row[0]
            conn.execute(
                """UPDATE sources SET status=?, error=?, active=1, trashed=?,
 auth_epoch=?, attempts=attempts+1 WHERE id=?""",
                (
                    status,
                    error,
                    1 if meta.trashed else 0,
                    meta.auth_epoch,
                    source_id,
                ),
            )
        else:
            cur = conn.execute(
                """INSERT INTO sources(drive_id, file_id, source_revision,
 repr_mime, parser_version, chunker_version, embedding_model,
 embed_dims, active, trashed, auth_epoch, status, error,
 attempts, title, path, modified_time, resource_key,
 created_at)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    meta.drive_id,
                    meta.file_id,
                    meta.source_revision,
                    target_mime,
                    PARSER_VERSION,
                    CHUNKER_FULL_VERSION,
                    "none:0",
                    0,
                    1,
                    1 if meta.trashed else 0,
                    meta.auth_epoch,
                    status,
                    error,
                    1,
                    meta.title,
                    meta.path,
                    meta.modified_time,
                    meta.resource_key,
                    _now_iso(),
                ),
            )
            source_id = cur.lastrowid
        for old_id in retired:
            _bump_term_df(conn, _terms_of_source(conn, old_id), -1)
    return source_id


def ingest_file(
    conn,
    client,
    file_meta: FileMeta,
    content_bytes: bytes,
    cfg=None,
) -> IngestResult:
    """Ingest one Drive file revision into the projection.

 Args:
 conn: the sqlite connection (fenris.db.connect).
 client: the DriveClient (reserved for the export path; the bytes
 arrive here as content_bytes so no network runs in tests).
 file_meta: FileMeta with the representation identity.
 content_bytes: the download (or export) bytes for this revision.
 cfg: optional config exposing ``cfg.embedder``; a FakeEmbedder is
 used when omitted - tests stay offline.

 Returns:
 IngestResult. Never raises for parse failures - they are recorded
 as status='failed' rows and the caller's loop continues.
 """
    from fenris.ingest.embed import FakeEmbedder
    from fenris.ingest.guards import (
        content_within_text_cap,
        sanitize_file_meta,
    )

    embedder = cfg.embedder if cfg is not None and hasattr(cfg, "embedder") else FakeEmbedder()

    # -- hostile-input bounds (security review: metadata bombs, control chars)
    file_meta = sanitize_file_meta(file_meta)

    # -- text-content storage bound (a 100 MB text file -> ~586 MB projection)
    if not content_within_text_cap(file_meta.mime_type, content_bytes):
        source_id = _write_status_row(
            conn, file_meta, file_meta.mime_type, STATUS_SKIPPED,
            f"text_cap:{len(content_bytes)}",
        )
        return IngestResult(
            status=STATUS_SKIPPED, source_id=source_id,
            reason="text_content_over_cap",
        )

    # -- route ---------------------------------------------------------------
    decision = router_mod.route(
        file_meta.mime_type, file_meta.is_workspace, file_meta.is_shortcut
    )
    if decision.route == router_mod.Route.SHORTCUT:
        return IngestResult(status="routed_away", reason=decision.reason)
    if decision.route == router_mod.Route.SKIP:
        source_id = _write_status_row(
            conn, file_meta, decision.target_mime or file_meta.mime_type,
            STATUS_SKIPPED, decision.reason,
        )
        return IngestResult(
            status=STATUS_SKIPPED, source_id=source_id, reason=decision.reason
        )
    target_mime = decision.target_mime

    # -- parse (bounded, never raises) ---------------------------------------
    parsed = parse_mod.parse_bytes(content_bytes, target_mime, file_meta.title)
    if parsed.status == "skipped":
        source_id = _write_status_row(
            conn, file_meta, target_mime, STATUS_SKIPPED,
            parsed.error or "skipped",
        )
        return IngestResult(
            status=STATUS_SKIPPED, source_id=source_id, reason=parsed.error
        )
    if parsed.status == "failed":
        source_id = _write_status_row(
            conn, file_meta, target_mime, STATUS_FAILED,
            parsed.error or "parse_failed",
        )
        return IngestResult(
            status=STATUS_FAILED, source_id=source_id, reason=parsed.error
        )

    # -- unchanged representation: touch nothing -----------------------------
    existing = _existing_source_row(conn, file_meta, target_mime, embedder.model_id)
    if existing is not None and existing[1] == 1:
        return IngestResult(
            status=STATUS_UNCHANGED, source_id=existing[0], reason="same_revision"
        )

    # -- chunk (children + parents; tables bypass the splitter) ---------------
    records = chunk_mod.chunk_body(parsed.body_text, parsed.body_spans)
    table_records = [
        chunk_mod.build_table_record(
            index,
            table,
            sheet_or_page=_table_page_label(table),
            region=f"{_table_page_label(table)}!table{index}",
        )
        for index, table in enumerate(parsed.tables)
    ]

    # -- headers + text_hash + scanner (annotate, never block) ---------------
    prepared: list[dict] = []
    for record in records:
        header = build_header(file_meta.title, file_meta.path, record.section_path)
        flagged, _hits = scanner_mod.scan_chunk(header, record.body_text)
        prepared.append(
            {
                "kind": "body",
                "record": record,
                "header": header,
                "text_hash": _text_hash(header, record.body_text),
                "body_hash": _body_hash(record.body_text),
                "flagged": flagged,
                "context": None,  # R3 contextual mode fills this; default off
            }
        )
    table_blocks: list[dict] = []
    for t_index, table_record in enumerate(table_records):
        for block in table_record.row_blocks:
            header = build_header(file_meta.title, file_meta.path, ())
            flagged, _hits = scanner_mod.scan_chunk(header, block.text)
            table_blocks.append(
                {
                    "kind": "table",
                    "table_index": t_index,
                    "block": block,
                    "header": header,
                    "text_hash": _text_hash(header, block.text),
                    "flagged": flagged,
                }
            )

    # embeddable items: body children + table row-blocks (parents kept, not
    # embedded; every item carries an is_table/table_id/row_range payload)
    embeddable: list[dict] = [
        item
        for item in (*prepared, *table_blocks)
        if not (item["kind"] == "body" and item["record"].is_parent)
    ]
    for idx, item in enumerate(embeddable):
        if item["kind"] == "body":
            item["_body_index"] = prepared.index(item)
        else:
            item["_table_block_index"] = table_blocks.index(item)

    # -- contextual chunk headers (R3, dark by default) ------------------------
    # Only body children, only in llm mode with a generator wired; the
    # context flows into the embed payload and chunks.context_text ONLY
    # (never body_text/header_text/spans — the citation frame is frozen).
    from fenris.config import contextual_headers_mode

    if contextual_headers_mode() == "llm" and hasattr(cfg, "context_generator"):
        from fenris.ingest.contextualize import contextualize

        try:
            contexts = contextualize(
                conn, parsed.body_text, prepared,
                cfg.context_generator.generate_raw,
                getattr(cfg.context_generator, "model_id", "unknown"),
            )
            for idx2, ctx in contexts.items():
                prepared[idx2]["context"] = ctx
        except Exception:  # noqa: BLE001 - the feature degrades, ingest never fails
            contexts = {}

    # -- embed (outside any transaction; hash dedup reuses stored vectors) ----
    # Within-batch reuse keys on the ACTUAL embed payload (the _embed_hash
    # seam): under llm mode, two chunks with identical text_hash can carry
    # different contexts, so their vectors must never be conflated.
    stored = (
        _existing_hash_map(conn, [item["text_hash"] for item in embeddable])
        if embeddable
        else {}
    )
    need: list[dict] = [
        item for item in embeddable if item["text_hash"] not in stored
    ]
    by_payload: dict[str, bytes] = {}
    need_unique: list[dict] = []
    for item in need:
        payload_hash = hashlib.sha256(_embed_text(item).encode("utf-8")).hexdigest()
        if payload_hash in by_payload:
            item["_reuse_payload_hash"] = payload_hash
            continue
        need_unique.append(item)
    texts = [_embed_text(item) for item in need_unique]
    new_vectors = embedder.embed_documents(texts) if texts else []
    for item, vec in zip(need_unique, new_vectors):
        payload_hash = hashlib.sha256(_embed_text(item).encode("utf-8")).hexdigest()
        by_payload[payload_hash] = vec
    vec_by_hash = {item["text_hash"]: vec for item, vec in zip(need_unique, new_vectors)}
    for item in need:
        reuse = item.get("_reuse_payload_hash")
        if reuse is not None and item["text_hash"] not in vec_by_hash:
            vec_by_hash[item["text_hash"]] = by_payload[reuse]

    # -- ONE transaction: writes only ----------------------------------------
    with conn:
        retired = _retire_old_sources(conn, file_meta)
        source_id = _insert_source(conn, file_meta, target_mime, embedder.model_id)

        # tables first: chunks reference table_id
        table_ids: dict[int, int] = {}
        for t_index, table_record in enumerate(table_records):
            cur = conn.execute(
                """INSERT INTO tables(source_id, sheet_or_page, range, columns_json,
 row_blocks_json, num_rows, num_cols, formulas_json,
 merged_cells_json, header_hierarchy_json)
 VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    table_record.sheet_or_page,
                    table_record.range,
                    table_record.columns_json,
                    table_record.row_blocks_json,
                    table_record.num_rows,
                    table_record.num_cols,
                    table_record.formulas_json,
                    table_record.merged_cells_json,
                    table_record.header_hierarchy_json,
                ),
            )
            table_ids[t_index] = cur.lastrowid

        # chunks: parents + children first, then table row-blocks; ordinals
        # are unique per source (UNIQUE(source_id, ordinal))
        chunk_id_by_item: list[int] = []
        base_ordinal = len(prepared)
        for item in prepared:
            record = item["record"]
            cur = conn.execute(
                """INSERT INTO chunks(source_id, ordinal, parent_chunk_id, page_nos,
 section_path, header_text, body_text, char_start, char_end,
 text_hash, body_hash, context_text, is_table, table_id, row_range, flagged, token_count)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    record.ordinal,
                    None,
                    _json_pages(record.page_nos),
                    json.dumps(list(record.section_path))
                    if record.section_path
                    else None,
                    item["header"],
                    record.body_text,
                    record.char_start,
                    record.char_end,
                    item["text_hash"],
                    item.get("body_hash"),
                    item.get("context"),
                    0,
                    None,
                    None,
                    1 if item["flagged"] else 0,
                    record.token_count,
                ),
            )
            chunk_id_by_item.append(cur.lastrowid)
        body_chunk_ids = list(chunk_id_by_item)

        # parent_chunk_id backfill: children point at their parent row
        ordinal_to_rowid = {
            item["record"].ordinal: chunk_id_by_item[idx]
            for idx, item in enumerate(prepared)
        }
        for item in prepared:
            record = item["record"]
            if record.is_parent or record.parent_ordinal is None:
                continue
            conn.execute(
                "UPDATE chunks SET parent_chunk_id=? WHERE chunk_id=?",
                (ordinal_to_rowid[record.parent_ordinal], ordinal_to_rowid[record.ordinal]),
            )

        # table row-block chunks: is_table=1, table_id, row_range; char offsets
        # are into the block's own synthesized text (the citation frame)
        for t_block_idx, item in enumerate(table_blocks):
            block = item["block"]
            table_id = table_ids[item["table_index"]]
            cur = conn.execute(
                """INSERT INTO chunks(source_id, ordinal, parent_chunk_id, page_nos,
 section_path, header_text, body_text, char_start, char_end,
 text_hash, body_hash, is_table, table_id, row_range, flagged, token_count)
 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id,
                    base_ordinal + t_block_idx,
                    None,
                    _json_pages(table_records[item["table_index"]].page_nos),
                    None,
                    item["header"],
                    block.text,
                    0,
                    len(block.text),
                    item["text_hash"],
                    None,  # tables never dedup: a table hit cites table_id+range
                    1,
                    table_id,
                    f"rows {block.start_row + 1}-{block.end_row}",
                    1 if item["flagged"] else 0,
                    chunk_mod.count_tokens(block.text),
                ),
            )
            chunk_id_by_item.append(cur.lastrowid)

        # chunks_vec for every embeddable item: new API vectors and dedup-reused
        # stored vectors, metadata mirrored from the source. When sqlite-vec
        # is not loadable (e.g. pythons without extension support), the vec0
        # table never exists: vectors are skipped and lexical retrieval
        # carries the index - the same degradation KNN already applies.
        vec_ready = True
        try:
            conn.execute("SELECT 1 FROM chunks_vec LIMIT 1")
        except sqlite3.Error:
            vec_ready = False
        if vec_ready:
            for item in embeddable:
                text_hash = item["text_hash"]
                vector = vec_by_hash.get(text_hash) or stored.get(text_hash)
                if vector is None:
                    continue
                conn.execute(
                    """INSERT INTO chunks_vec(chunk_id, embedding, active, trashed,
 auth_epoch) VALUES (?,?,?,?,?)""",
                    (_embeddable_chunk_id(item, body_chunk_ids, base_ordinal,
                                          source_id, conn),
                     vector, 1, 1 if file_meta.trashed else 0, file_meta.auth_epoch),
                )

        # term_df: new source contributes its terms; retired ones decontributed
        _bump_term_df(conn, _source_terms_from_items(prepared, table_blocks), +1)
        for old_id in retired:
            _bump_term_df(conn, _terms_of_source(conn, old_id), -1)

    return IngestResult(
        status=STATUS_OK,
        source_id=source_id,
        chunks_inserted=len(prepared) + len(table_blocks),
        tables_inserted=len(table_records),
        chunks_embedded=len(new_vectors),
        chunks_deduped=sum(1 for item in embeddable if item["text_hash"] in stored),
    )


def _embed_text(item: dict) -> str:
    """The text sent to the embedder: header + body (~600 tokens); under
 contextual mode the LLM context sits between them (R3) — the context
 improves retrieval, never the citation frame."""
    body = item["record"].body_text if item["kind"] == "body" else item["block"].text
    header = item["header"]
    context = item.get("context")
    if item["kind"] == "body" and context:
        return f"{header}\n\n{context}\n\n{body}"
    return f"{header}\n{body}"


def _embeddable_chunk_id(
    item: dict,
    body_chunk_ids: list[int],
    base_ordinal: int,
    source_id: int,
    conn,
) -> int:
    """Resolve the chunk rowid for an embeddable item (body child or block)."""
    if item["kind"] == "body":
        return body_chunk_ids[item["_body_index"]]
    ordinal = base_ordinal + item["_table_block_index"]
    row = conn.execute(
        "SELECT chunk_id FROM chunks WHERE source_id=? AND ordinal=?",
        (source_id, ordinal),
    ).fetchone()
    return row[0]


def _source_terms_from_items(prepared: list[dict], table_blocks: list[dict]) -> list[str]:
    """Distinct terms over every chunk body of the new representation."""
    seen: dict[str, None] = {}
    for item in (*prepared, *table_blocks):
        body = item["record"].body_text if item["kind"] == "body" else item["block"].text
        for term in _normalize_terms(body):
            seen.setdefault(term, None)
    return list(seen)
