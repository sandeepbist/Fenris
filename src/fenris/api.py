"""Fenris local API: a thin HTTP surface over the engine.

The frontend talks to this; the engine stays untouched. Endpoints map
one-to-one onto CLI capabilities so the UI and the terminal never drift:

    GET  /api/health          - schema/vec0 state + index counts
    GET  /api/status          - sync cursor, staleness, source counts
    GET  /api/sources         - indexed sources (active/retired/failed)
    POST /api/sync            - run one sync pass (enumerate/drain)
    POST /api/ask             - question -> AnswerPacket (state+citations)
    POST /api/ask/stream      - same, streamed
    GET  /api/eval/overview   - last eval run per slice (if any)

Everything binds 127.0.0.1 only. This is a single-user local product;
the API never authenticates because it never leaves the machine.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from fenris.config import load_config
from fenris.db import connect

app = FastAPI(title="Fenris", version="0.1.0", docs_url="/api/docs")

# Production: serve the built frontend (frontend/dist) from this process so
# the whole product is one command. Dev keeps Vite on :5173 with its /api
# proxy; CORS below covers that case.
_DIST = Path(__file__).resolve().parent.parent.parent / "frontend" / "dist"
if _DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=_DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(_DIST / "index.html")

# Legal documents, served as plain text so they are readable in the
# browser and linkable from the UI without a markdown renderer.
_LEGAL_DIR = Path(__file__).resolve().parent.parent.parent / "docs" / "legal"
_LEGAL_DOCS = {
    "terms-of-service": "terms-of-service.md",
    "privacy-policy": "privacy-policy.md",
    "security-disclosure": "security-disclosure.md",
}


@app.get("/legal/{doc}", include_in_schema=False)
def legal_doc(doc: str):
    """Serve the markdown legal documents, escaped, on a minimal page."""
    filename = _LEGAL_DOCS.get(doc)
    if filename is None or not (_LEGAL_DIR / filename).exists():
        raise HTTPException(status_code=404, detail="unknown document")
    text = (_LEGAL_DIR / filename).read_text(encoding="utf-8")
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>Fenris — legal</title>"
        "<style>body{max-width:72ch;margin:2rem auto;padding:0 1rem;"
        "font:15px/1.65 -apple-system,'Segoe UI',sans-serif;color:#222;background:#fff}"
        "pre{white-space:pre-wrap;background:none;padding:0;font-family:inherit}"
        "a{color:#0645ad}table{border-collapse:collapse}td,th{border:1px solid #ddd;padding:6px}"
        "</style></head><body><pre>" + escaped + "</pre></body></html>"
    )
    from fastapi.responses import HTMLResponse

    return HTMLResponse(html)

# The dev server runs the Vite frontend on :5173; production serves the
# built assets from this same origin, where CORS is irrelevant.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _db():
    cfg = load_config()
    return connect(cfg.db_path)


@app.get("/api/health")
def health() -> dict:
    cfg = load_config()
    fresh = cfg.db_path.exists()
    body = {
        "ok": True,
        "db_path": str(cfg.db_path),
        "has_db": fresh,
        "index": {},
    }
    if fresh:
        conn = _db()
        try:
            for key, sql in (
                ("sources_active", "SELECT count(*) FROM sources WHERE active=1"),
                ("sources_failed", "SELECT count(*) FROM sources WHERE status='failed'"),
                ("sources_skipped", "SELECT count(*) FROM sources WHERE status='skipped'"),
                ("chunks", "SELECT count(*) FROM chunks"),
                ("tables", "SELECT count(*) FROM tables"),
                ("drive_files", "SELECT count(*) FROM drive_files"),
            ):
                body["index"][key] = conn.execute(sql).fetchone()[0]
        finally:
            conn.close()
    return body


@app.get("/api/status")
def status() -> dict:
    from fenris.sync.service import sync_status

    conn = _db()
    try:
        return sync_status(conn)
    finally:
        conn.close()


@app.get("/api/sources")
def sources(limit: int = 200, status_filter: str | None = None) -> dict:
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT s.file_id, s.title, s.path, s.source_revision, s.status,
                      s.active, s.created_at, df.title as drive_title, df.trashed
               FROM sources s
               LEFT JOIN drive_files df ON df.drive_id = s.drive_id AND df.file_id = s.file_id
               ORDER BY s.created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        cols = ["file_id", "title", "path", "revision", "status",
                "active", "indexed_at", "drive_title", "trashed"]
        out = []
        for r in rows:
            row = dict(zip(cols, r))
            entry = {
                "file_id": row["file_id"],
                "title": row["title"] or row["drive_title"] or row["file_id"],
                "path": row["path"],
                "revision": row["revision"][:12],
                "status": row["status"],
                "active": bool(row["active"]),
                "indexed_at": row["indexed_at"],
                "trashed": bool(row["trashed"]),
            }
            if status_filter and entry["status"] != status_filter:
                continue
            out.append(entry)
        return {"sources": out, "count": len(out)}
    finally:
        conn.close()


class SyncRequest(BaseModel):
    full: bool = False  # escape hatch -> full_resync


@app.post("/api/sync")
def sync(req: SyncRequest) -> dict:
    from fenris.sync.service import full_resync, sync_all

    conn = _db()
    try:
        client = _drive_client()
        report = full_resync(conn, client) if req.full else sync_all(conn, client)
        return {
            "changes_processed": report.changes_processed,
            "content_reingests": report.content_reingests,
            "metadata_only": report.metadata_only,
            "tombstoned": report.tombstoned,
            "failed": report.failed,
            "resync_triggered": report.resync_triggered,
            "errors": report.errors[:20],
        }
    except Exception as e:
        # Not authenticated / no keyring backend: the UI shows a setup hint.
        raise HTTPException(status_code=503, detail=str(e)) from e
    finally:
        conn.close()


class AskRequest(BaseModel):
    question: str
    rerank: bool = True

    # A question is a question, not a document. Caps hostile payloads
    # before they touch retrieval, embedding, or the model (security
    # review: a 10M-char question previously reached the engine).
    model_config = {"str_max_length": 10_000}


def _drive_client():
    from googleapiclient.discovery import build

    from fenris.drive.auth import load_or_refresh
    from fenris.drive.client import DriveClient

    creds = load_or_refresh()
    service = build("drive", "v3", credentials=creds)
    return DriveClient(service)


def _ask_stack(conn):
    """Shared ask wiring: embedder + search_fn + generator (or None).

    Failure modes here are 'no API key configured' / 'SDK import
    broken' / 'client construction refused' - any of which means the
    UI must show a setup hint, not a traceback.
    """
    try:
        from google import genai

        from fenris.generate.generator import GeminiGenerator
        from fenris.ingest.embed import GeminiEmbedder

        client = genai.Client()
        embedder = GeminiEmbedder(client)
        generator = GeminiGenerator()
        return embedder, generator
    except (ImportError, ValueError, RuntimeError, OSError):
        return None, None


@app.post("/api/ask")
def ask(req: AskRequest) -> dict:
    from fenris.generate.service import ask as ask_service
    from fenris.retrieve import search as search_mod
    from fenris.retrieve.rerank import FlashRankReranker, NoopReranker

    conn = _db()
    try:
        embedder, generator = _ask_stack(conn)
        if generator is None:
            raise HTTPException(
                status_code=503,
                detail="Gemini API unavailable. Set GOOGLE_API_KEY on the server.",
            )

        def search_fn(q: str):
            qvec = embedder.embed_documents([f"task: search result | query: {q}"])[0]
            reranker = FlashRankReranker() if req.rerank else NoopReranker()
            return search_mod(conn, q, qvec, reranker=reranker)

        packet = ask_service(conn, req.question, None, generator=generator, search_fn=search_fn)
        return _ask_payload(packet)
    finally:
        conn.close()


def _ask_payload(packet) -> dict:
    """The wire shape shared by /api/ask and the stream's final event."""
    return {
        "state": packet.state,
        "answer": packet.answer,
        "citations": [
            {
                "evidence_id": c.evidence_id, "title": c.title, "page": c.page,
                "quote": c.quote, "revision_date": c.revision_date,
            }
            for c in packet.citations
        ],
        "missing_evidence": packet.missing_evidence,
        "unverifiable_claims": packet.unverifiable_claims,
        "claims_conflict": packet.claims_conflict,
    }


@app.post("/api/ask/stream")
def ask_stream(req: AskRequest):
    """Server-sent events: text deltas as they generate, then the packet.

    Event protocol (one JSON object per SSE `data:` line):
      {"type": "delta", "text": "..."}            streamed display text
      {"type": "done", "answer": {...}}           the full AnswerPacket wire shape
    The connection stays open until "done"; no keepalives needed at
    local-socket latencies.
    """

    from fastapi.responses import StreamingResponse

    from fenris.generate.service import ask_stream as ask_service_stream
    from fenris.retrieve import search as search_mod
    from fenris.retrieve.rerank import FlashRankReranker, NoopReranker

    conn = _db()

    def gen():
        try:
            embedder, generator = _ask_stack(conn)
            if generator is None:
                yield _sse({"type": "error", "detail": "Gemini API unavailable. Set GOOGLE_API_KEY."})
                return

            def search_fn(q: str):
                qvec = embedder.embed_documents([f"task: search result | query: {q}"])[0]
                reranker = FlashRankReranker() if req.rerank else NoopReranker()
                return search_mod(conn, q, qvec, reranker=reranker)

            for item in ask_service_stream(
                conn, req.question, None, generator=generator, search_fn=search_fn
            ):
                if isinstance(item, str):
                    yield _sse({"type": "delta", "text": item})
                else:
                    payload = _ask_payload(item)
                    payload["type"] = "done"
                    yield _sse(payload)
        finally:
            conn.close()

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(obj: dict) -> str:
    import json as _json

    return f"data: {_json.dumps(obj, ensure_ascii=False)}\n\n"


@app.get("/api/eval/overview")
def eval_overview() -> dict:
    conn = _db()
    try:
        row = conn.execute(
            """SELECT id, started_at, verdict FROM eval_runs ORDER BY id DESC LIMIT 5"""
        ).fetchall()
        return {"runs": [{"id": r[0], "started_at": r[1], "verdict": r[2]} for r in row]}
    finally:
        conn.close()


# ---- conversations (P0.2: persisted chat sessions) ---------------------------

import json
import time


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class ConversationCreate(BaseModel):
    title: str | None = None


class ConversationPatch(BaseModel):
    title: str | None = None
    pinned: bool | None = None


class TurnRecord(BaseModel):
    question: str
    state: str
    answer: str = ""
    citations_json: str = "[]"


@app.get("/api/conversations")
def conversations_list() -> dict:
    conn = _db()
    try:
        rows = conn.execute(
            """SELECT id, title, pinned, created_at, updated_at,
                      (SELECT count(*) FROM turns t WHERE t.conversation_id = c.id)
               FROM conversations c
               ORDER BY pinned DESC, updated_at DESC LIMIT 200"""
        ).fetchall()
        return {
            "conversations": [
                {"id": r[0], "title": r[1], "pinned": bool(r[2]),
                 "created_at": r[3], "updated_at": r[4], "turn_count": r[5]}
                for r in rows
            ]
        }
    finally:
        conn.close()


@app.post("/api/conversations")
def conversations_create(req: ConversationCreate) -> dict:
    conn = _db()
    try:
        cur = conn.execute(
            "INSERT INTO conversations(title, created_at, updated_at) VALUES (?, ?, ?)",
            (req.title or "New chat", _now_iso(), _now_iso()),
        )
        conn.commit()
        return {"id": cur.lastrowid, "title": req.title or "New chat", "pinned": False}
    finally:
        conn.close()


@app.get("/api/conversations/{cid}")
def conversation_get(cid: int) -> dict:
    conn = _db()
    try:
        head = conn.execute(
            "SELECT id, title, pinned, created_at, updated_at FROM conversations WHERE id=?",
            (cid,),
        ).fetchone()
        if head is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        turns = conn.execute(
            """SELECT question, state, answer, citations_json, created_at
               FROM turns WHERE conversation_id=? ORDER BY id""",
            (cid,),
        ).fetchall()
        return {
            "id": head[0], "title": head[1], "pinned": bool(head[2]),
            "created_at": head[3], "updated_at": head[4],
            "turns": [
                {"question": t[0], "state": t[1], "answer": t[2],
                 "citations": json.loads(t[3]), "created_at": t[4]}
                for t in turns
            ],
        }
    finally:
        conn.close()


@app.patch("/api/conversations/{cid}")
def conversation_patch(cid: int, req: ConversationPatch) -> dict:
    conn = _db()
    try:
        if req.title is None and req.pinned is None:
            raise HTTPException(status_code=422, detail="nothing to update")
        if req.title is not None:
            conn.execute("UPDATE conversations SET title=? WHERE id=?", (req.title[:200], cid))
        if req.pinned is not None:
            conn.execute("UPDATE conversations SET pinned=? WHERE id=?",
                         (1 if req.pinned else 0, cid))
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


@app.delete("/api/conversations/{cid}")
def conversation_delete(cid: int) -> dict:
    conn = _db()
    try:
        cur = conn.execute("DELETE FROM conversations WHERE id=?", (cid,))
        conn.commit()  # turns cascade
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="no such conversation")
        return {"ok": True}
    finally:
        conn.close()


@app.post("/api/conversations/{cid}/turns")
def conversation_add_turn(cid: int, req: TurnRecord) -> dict:
    """Persist one completed Q/A turn (called by the UI when a stream finishes)."""
    conn = _db()
    try:
        exists = conn.execute(
            "SELECT 1 FROM conversations WHERE id=?", (cid,)
        ).fetchone()
        if exists is None:
            raise HTTPException(status_code=404, detail="no such conversation")
        cur = conn.execute(
            """INSERT INTO turns(conversation_id, question, state, answer,
                                 citations_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (cid, req.question[:10_000], req.state, req.answer[:100_000],
             req.citations_json, _now_iso()),
        )
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?",
                     (_now_iso(), cid))
        conn.commit()
        return {"turn_id": cur.lastrowid}
    finally:
        conn.close()


# ---- jump-to-source (P0.3: the citation viewer) ------------------------------


@app.get("/api/source-view")
def source_view(chunk_id: int) -> dict:
    """The parent document text around one cited chunk.

    Loads the chunk (active representations only - a retired/trashed
    revision 404s, which is the honest answer), plus its neighboring chunks
    of the same source for context, and the cited span offsets the viewer
    highlights. Only what the projection stores: no Drive fetch, works
    offline.
    """
    conn = _db()
    try:
        row = conn.execute(
            """SELECT c.chunk_id, c.header_text, c.body_text, c.char_start, c.char_end,
                      c.ordinal, s.id AS source_id, s.title, s.file_id,
                      s.source_revision, s.modified_time
               FROM chunks c JOIN sources s ON s.id = c.source_id
               WHERE c.chunk_id = ? AND s.active = 1 AND s.trashed = 0""",
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="chunk not in an active source")

        source_id = row[6]
        neighbors = conn.execute(
            """SELECT chunk_id, ordinal, header_text, body_text,
                      char_start, char_end
               FROM chunks WHERE source_id = ? ORDER BY ordinal""",
            (source_id,),
        ).fetchall()
        return {
            "title": row[7] or row[8],
            "file_id": row[8],
            "revision": (row[9] or "")[:12],
            "modified_time": row[10],
            "cited": {
                "chunk_id": row[0], "ordinal": row[5],
                "header": row[1], "char_start": row[3], "char_end": row[4],
            },
            "chunks": [
                {"chunk_id": n[0], "ordinal": n[1], "header": n[2],
                 "body": n[3], "char_start": n[4], "char_end": n[5]}
                for n in neighbors
            ],
        }
    finally:
        conn.close()
