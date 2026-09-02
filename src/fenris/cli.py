"""Fenris CLI (Typer). One command surface over the six slices.

Commands grow in as milestones land; each sub-app owns its own domain:

- drive (auth/disconnect/status/files)  - src/fenris/drive/cli.py
- sync (run/status/resync)               - src/fenris/sync/cli.py
- ask / eval live here (they compose every slice).
"""

from __future__ import annotations

import typer

from fenris import __version__
from fenris.config import load_config

app = typer.Typer(
    name="fenris",
    help="RAG over your own Google Drive: revision-aware, evidence-cited, locally indexed.",
    no_args_is_help=True,
)


@app.command()
def version() -> None:
    """Print the fenris version."""
    typer.echo(f"fenris {__version__}")


@app.command()
def init() -> None:
    """Create the local data directory and apply the database schema."""
    cfg = load_config()
    typer.echo(f"data dir: {cfg.data_dir}")
    from fenris.db import connect

    conn = connect(cfg.db_path)
    try:
        n_tables = len(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        )
        has_vec = bool(
            conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='chunks_vec'"
            ).fetchone()
        )
    finally:
        conn.close()
    typer.echo(f"schema applied: {n_tables} tables, vec0={'yes' if has_vec else 'no'}")


@app.command()
def doctor() -> None:
    """Check everything that can break, and say what to do about each."""
    from fenris.doctor import all_ok, run_doctor

    results = run_doctor()
    for r in results:
        mark = "ok" if r["ok"] else "FAIL"
        typer.echo(f"[{mark:^4}] {r['name']}: {r['detail']}")
    if all_ok(results):
        typer.echo("all checks passed")
        raise typer.Exit(code=0)
    raise typer.Exit(code=1)


def _open_db():
    from fenris.db import connect

    cfg = load_config()
    return connect(cfg.db_path), cfg


def _make_embedder():
    """Gemini embedder for queries; GOOGLE_API_KEY/GEMINI_API_KEY via the SDK."""
    from google import genai

    from fenris.ingest.embed import GeminiEmbedder

    client = genai.Client()
    return GeminiEmbedder(client)


def _make_search_fn(conn, embedder, use_reranker: bool = True):
    """Partially-apply the retrieval stack for ask()/eval."""
    from fenris.retrieve import search
    from fenris.retrieve.rerank import FlashRankReranker, NoopReranker

    reranker = NoopReranker()
    if use_reranker:
        try:
            reranker = FlashRankReranker()  # lazy model load; may download once
        except (OSError, ValueError, RuntimeError):
            # missing model/cache/network: degrade to the identity reranker
            reranker = NoopReranker()

    def search_fn(query: str):
        qvec = embedder.embed_documents([f"task: search result | query: {query}"])[0]
        return search(conn, query, qvec, reranker=reranker)

    return search_fn


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to answer from your Drive."),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Skip cross-encoder reranking."),
    offline: bool = typer.Option(False, "--offline", help="Offline: retrieval only, no generation API."),
) -> None:
    """Answer a question over the indexed Drive, with cited evidence."""
    conn, _cfg = _open_db()
    try:
        if offline:
            from fenris.generate.generator import FakeGenerator

            generator = FakeGenerator(outputs=[{"state": "INSUFFICIENT", "answer": "",
                                                "cited_evidence_ids": [], "claims_conflict": False,
                                                "missing_evidence": ["offline mode: generation disabled"]}])
            embedder = None
        else:
            from fenris.generate.generator import GeminiGenerator

            try:
                generator = GeminiGenerator()
                embedder = _make_embedder()
            except (ImportError, ValueError, RuntimeError, OSError) as e:
                typer.echo(
                    f"cannot reach the Gemini API ({e.__class__.__name__}).\n"
                    "Set GOOGLE_API_KEY (or GEMINI_API_KEY), or use --offline for "
                    "retrieval-only mode."
                )
                raise typer.Exit(code=2)

        def search_fn(q: str):
            from fenris.retrieve import search
            from fenris.retrieve.rerank import FlashRankReranker, NoopReranker

            reranker = NoopReranker() if (no_rerank or offline) else FlashRankReranker()
            if embedder is None:
                # offline: retrieval without a query vector (BM25-only)
                return search(conn, q, None, reranker=reranker)
            qvec = embedder.embed_documents([f"task: search result | query: {q}"])[0]
            return search(conn, q, qvec, reranker=reranker)

        from fenris.generate.service import ask as ask_service

        packet = ask_service(conn, question, None, generator=generator, search_fn=search_fn)

        if offline:
            # retrieval still ran: show what was found, generation didn't
            typer.echo("(offline mode: retrieval only)")
        typer.echo(packet.answer or f"[{packet.state}]")
        typer.echo()
        if packet.citations:
            typer.echo("Sources:")
            for c in packet.citations:
                loc = f"p.{c.page}" if c.page else "span"
                typer.echo(f"  [{c.evidence_id}] {c.title} ({loc}): {c.quote}")
        if packet.state != "ANSWERABLE":
            typer.echo(f"state: {packet.state}")
        if packet.unverifiable_claims:
            typer.echo(f"unverifiable claims: {len(packet.unverifiable_claims)}")
    finally:
        conn.close()


@app.command(name="eval")
def run_eval(
    suite: str = typer.Option("golden", "--suite", help="Eval suite to run."),
    limit: int = typer.Option(0, "--limit", help="Max cases (0 = all)."),
) -> None:
    """Run an eval suite against the real retrieval stack."""
    conn, _cfg = _open_db()
    try:
        from pathlib import Path

        from fenris.eval.cases import load_cases
        from fenris.eval.runner import run_suite

        cases_dir = Path("eval/cases") / suite
        if not cases_dir.is_dir():
            typer.echo(f"unknown suite: {suite} (no {cases_dir})")
            raise typer.Exit(code=2)
        cases = load_cases(cases_dir)
        if limit:
            cases = cases[:limit]

        embedder = _make_embedder()
        search_fn = _make_search_fn(conn, embedder)

        def retrieve(question: str):
            """Adapt EvidenceSet -> the runner's RetrievedDoc contract."""
            ev_set = search_fn(question)
            evidence = ev_set.evidence if hasattr(ev_set, "evidence") else ev_set
            return [
                {"doc_id": e.file_id, "score": e.score,
                 "char_span": (e.char_start, e.char_end)}
                for e in evidence
            ]

        result = run_suite(conn, cases, retrieve, flag_config={"suite": suite})
        typer.echo(result.markdown)
        if result.overall:
            typer.echo("\noverall (mean of slice means):")
            for k, v in result.overall.items():
                typer.echo(f"  {k}: {v:.3f}" if isinstance(v, float) else f"  {k}: {v}")
    finally:
        conn.close()


# ---- sub-apps -----------------------------------------------------------------
try:
    from fenris.drive.cli import drive_app

    app.add_typer(drive_app, name="drive")
except ImportError:  # drive deps not installed (e.g., CI smoke)
    pass

try:
    from fenris.sync.cli import sync_app

    app.add_typer(sync_app, name="sync")
except ImportError:
    pass


def main() -> None:
    app()


if __name__ == "__main__":
    main()
