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
    finally:
        conn.close()


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
