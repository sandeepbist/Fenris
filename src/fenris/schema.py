"""SQLite schema: the rebuildable read projection of Drive.

Laid out in the build plan Two load-bearing rules live here:

- Representation identity is (drive_id, file_id, source_revision, repr_mime,
 parser_version, chunker_version, embedding_model) - never Drive's `version`,
 which a rename bumps without a content change.
- At most one active representation per file: enforced by partial unique index,
 not by hope.

All DDL is idempotent (CREATE IF NOT EXISTS / OR REPLACE) so re-running the
migrations on an existing DB is always safe; schema_version gates forward-only
changes.
"""

import sqlite3

SCHEMA_VERSION = 2

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS schema_version (
 version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_cursor (
 drive_id TEXT PRIMARY KEY, -- 'mydrive' or shared-drive id
 page_token TEXT,
 pending_start TEXT,
 status TEXT NOT NULL DEFAULT 'ok', -- ok | resync | stale
 updated_at TEXT
);

CREATE TABLE IF NOT EXISTS drive_files (-- mutable file metadata; upsert target
 drive_id TEXT NOT NULL,
 file_id TEXT NOT NULL,
 title TEXT,
 path TEXT,
 mime_type TEXT,
 trashed INTEGER NOT NULL DEFAULT 0,
 drive_version TEXT, -- change-detection hint ONLY, never identity
 resource_key TEXT,
 capabilities_json TEXT, -- includes canDownload
 updated_at TEXT,
 PRIMARY KEY (drive_id, file_id)
);

CREATE TABLE IF NOT EXISTS sources (-- revision manifest: one row per representation
 id INTEGER PRIMARY KEY,
 drive_id TEXT NOT NULL,
 file_id TEXT NOT NULL,
 source_revision TEXT NOT NULL, -- md5Checksum, or 'exp:'+sha256(export bytes)
 repr_mime TEXT NOT NULL,
 parser_version TEXT,
 chunker_version TEXT,
 embedding_model TEXT,
 embed_dims INTEGER,
 active INTEGER NOT NULL DEFAULT 1,
 trashed INTEGER NOT NULL DEFAULT 0,
 auth_epoch INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL DEFAULT 'ok', -- ok|failed|unreadable|pending_export|skipped
 error TEXT,
 attempts INTEGER NOT NULL DEFAULT 0,
 title TEXT,
 path TEXT,
 modified_time TEXT,
 resource_key TEXT,
 created_at TEXT,
 UNIQUE(drive_id, file_id, source_revision, repr_mime,
 parser_version, chunker_version, embedding_model)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_one_active
 ON sources(drive_id, file_id) WHERE active = 1;

CREATE TABLE IF NOT EXISTS chunks (
 chunk_id INTEGER PRIMARY KEY,
 source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 ordinal INTEGER NOT NULL,
 parent_chunk_id INTEGER,
 page_nos TEXT, -- JSON [int]; Docling provenance
 section_path TEXT,
 header_text TEXT NOT NULL,
 body_text TEXT NOT NULL,
 char_start INTEGER,
 char_end INTEGER, -- offsets into body_text (citation frame)
 text_hash TEXT NOT NULL, -- sha256(header+body)
 body_hash TEXT, -- sha256(body): the cross-folder dedup key (v2)
 context_text TEXT, -- LLM chunk context, embed/FTS payload only (v2, dark)
 is_table INTEGER NOT NULL DEFAULT 0,
 table_id INTEGER,
 row_range TEXT,
 flagged INTEGER NOT NULL DEFAULT 0,
 token_count INTEGER,
 UNIQUE(source_id, ordinal)
);
CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id);
CREATE INDEX IF NOT EXISTS idx_chunks_text_hash ON chunks(text_hash);

-- external-content FTS5: values read back from chunks; triggers keep it in sync
-- (FTS5 does NOT auto-sync - a missing trigger row means BM25 silently misses it)
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
 header_text, body_text,
 content='chunks', content_rowid='chunk_id',
 tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
 INSERT INTO chunks_fts(rowid, header_text, body_text)
 VALUES (new.chunk_id, new.header_text, new.body_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
 INSERT INTO chunks_fts(chunks_fts, rowid, header_text, body_text)
 VALUES ('delete', old.chunk_id, old.header_text, old.body_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
 INSERT INTO chunks_fts(chunks_fts, rowid, header_text, body_text)
 VALUES ('delete', old.chunk_id, old.header_text, old.body_text);
 INSERT INTO chunks_fts(rowid, header_text, body_text)
 VALUES (new.chunk_id, new.header_text, new.body_text);
END;

-- vec0 created separately: sqlite-vec extension must be loaded first, and
-- pre-v1 sqlite-vec DDL has no IF NOT EXISTS guarantee in older builds.

CREATE TABLE IF NOT EXISTS tables (-- typed TableRecord
 table_id INTEGER PRIMARY KEY,
 source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
 sheet_or_page TEXT,
 range TEXT, -- 'Sheet1!B12:F18' / PDF region
 columns_json TEXT,
 row_blocks_json TEXT,
 num_rows INTEGER,
 num_cols INTEGER,
 formulas_json TEXT,
 merged_cells_json TEXT,
 header_hierarchy_json TEXT
);

CREATE TABLE IF NOT EXISTS term_df (-- doc-frequency stats for routing
 term TEXT PRIMARY KEY,
 active_source_count INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS source_artifacts (-- optional DoclingDocument JSON
 source_id INTEGER PRIMARY KEY REFERENCES sources(id) ON DELETE CASCADE,
 path TEXT NOT NULL,
 bytes INTEGER NOT NULL,
 last_accessed TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversations (-- persisted chat sessions
 id INTEGER PRIMARY KEY,
 title TEXT NOT NULL DEFAULT 'New chat',
 pinned INTEGER NOT NULL DEFAULT 0,
 created_at TEXT,
 updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_conversations_pinned
 ON conversations(pinned DESC, updated_at DESC);

CREATE TABLE IF NOT EXISTS turns (-- one Q/A pair per row
 id INTEGER PRIMARY KEY,
 conversation_id INTEGER NOT NULL
 REFERENCES conversations(id) ON DELETE CASCADE,
 question TEXT NOT NULL,
 state TEXT NOT NULL, -- the answer state at completion
 answer TEXT NOT NULL DEFAULT '',
 citations_json TEXT NOT NULL DEFAULT '[]',-- resolved Citation[] payloads
 created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_conversation ON turns(conversation_id);

CREATE TABLE IF NOT EXISTS eval_runs (
 id INTEGER PRIMARY KEY,
 started_at TEXT,
 git_sha TEXT,
 flag_config TEXT,
 generation TEXT,
 verdict TEXT
);
CREATE TABLE IF NOT EXISTS eval_case_results (
 run_id INTEGER NOT NULL REFERENCES eval_runs(id),
 case_id TEXT NOT NULL,
 suite TEXT NOT NULL,
 slice TEXT,
 metrics_json TEXT,
 gate_pass INTEGER,
 PRIMARY KEY (run_id, case_id)
);
"""

VEC0_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec USING vec0(
 chunk_id INTEGER PRIMARY KEY,
 embedding float[1536],
 active INTEGER,
 trashed INTEGER,
 auth_epoch INTEGER
);
"""


def apply_schema(conn) -> None:
    """Create all tables/triggers if missing; stamp schema_version if absent.

 Safe to call on every startup: everything is IF NOT EXISTS.
 """
    conn.executescript(SCHEMA)
    try:
        conn.execute("SELECT count(*) FROM chunks_vec")
    except sqlite3.Error:
        # vec0 not present yet - caller must load sqlite-vec first and then
        # call apply_vec0(); we do not silently skip the dense index.
        pass
    # v1 -> v2 column ensure (body_hash, context_text): idempotent, so
    # shared INSERTs never break a DB created with the old DDL.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)", ()).fetchall()}
    if "body_hash" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN body_hash TEXT")
    if "context_text" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN context_text TEXT")
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    if row is None:
        conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
    elif row[0] < SCHEMA_VERSION:
        conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))
    conn.commit()


def apply_vec0(conn) -> None:
    """Create the vec0 table. Requires sqlite-vec loaded via conn.load_extension."""
    try:
        conn.execute("SELECT count(*) FROM chunks_vec")
    except sqlite3.Error:
        conn.execute(VEC0_DDL)
        conn.commit()
