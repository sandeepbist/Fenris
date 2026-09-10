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
    """Gemini embedder for queries; GOOGLE_API_KEY/GEMINI_API_KEY via the SDK.

    FENRIS_EMBEDDER=fake swaps in the deterministic offline embedder -
    the golden eval runs this way on CI, where no API key exists.
    """
    import os

    from fenris.ingest.embed import FakeEmbedder, GeminiEmbedder

    if os.environ.get("FENRIS_EMBEDDER") == "fake":
        return FakeEmbedder()
    from google import genai

    client = genai.Client()
    return GeminiEmbedder(client)


def _make_search_fn(conn, embedder, use_reranker: bool = True):
    """Partially-apply the retrieval stack for ask()/eval."""
    from fenris.retrieve import search
    from fenris.retrieve.rerank import NoopReranker, resolve_reranker

    def search_fn(query: str):
        reranker = resolve_reranker()[0] if use_reranker else NoopReranker()
        qvec = embedder.embed_documents([f"task: search result | query: {query}"])[0]
        return search(conn, query, qvec, reranker=reranker)

    return search_fn


@app.command()
def ask(
    question: str = typer.Argument(..., help="The question to answer from your Drive."),
    no_rerank: bool = typer.Option(False, "--no-rerank", help="Skip cross-encoder reranking."),
    offline: bool = typer.Option(False, "--offline", help="Offline: retrieval only, no generation API."),
    vault: str = typer.Option(None, "--vault", help="Restrict the question to one vault (P2.5)."),
) -> None:
    """Answer a question over the indexed Drive, with cited evidence."""
    conn, _cfg = _open_db()
    try:
        vaults = None
        if vault:
            from fenris.vaults import vault_exists

            if not vault_exists(conn, vault.strip().lower()):
                typer.echo(f"unknown vault: {vault}")
                raise typer.Exit(code=2)
            vaults = [vault.strip().lower()]
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
            from fenris.retrieve.rerank import NoopReranker, resolve_reranker

            if no_rerank or offline:
                reranker = NoopReranker()
            else:
                reranker, reason = resolve_reranker()
                if "unavailable" in reason or "unknown" in reason:
                    typer.echo(f"reranker: {reason}", err=True)
            if embedder is None:
                # offline: retrieval without a query vector (BM25-only)
                return search(conn, q, None, reranker=reranker, vaults=vaults)
            qvec = embedder.embed_documents([f"task: search result | query: {q}"])[0]
            return search(conn, q, qvec, reranker=reranker, vaults=vaults)

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
        from fenris.eval.seed import seed_golden_corpus

        cases_dir = Path("eval/cases") / suite
        if not cases_dir.is_dir():
            typer.echo(f"unknown suite: {suite} (no {cases_dir})")
            raise typer.Exit(code=2)

        # The suite runs over the real index: seed the corpus first
        # (idempotent; a fresh FENRIS_HOME ingests all 99 docs, a warm
        # one re-checks revisions and writes nothing).
        n_docs, families = seed_golden_corpus(conn)
        typer.echo(f"corpus: {n_docs} docs across {len(families)} families")
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

        # The run row records the mode so offline numbers (fake embedder,
        # noop reranker) are never mistaken for calibrated Gemini runs.
        import os

        flags = {
            "suite": suite,
            "embedder": getattr(embedder, "model_id", embedder.__class__.__name__),
            "reranker": "noop" if os.environ.get("FENRIS_RERANKER") == "noop" else "default",
        }
        result = run_suite(conn, cases, retrieve, flag_config=flags)
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

try:
    from fenris.digest.cli import app as digest_app

    app.add_typer(digest_app, name="digest")
except ImportError:
    pass

vault_app = typer.Typer(help="Manage vaults (P2.5): one Google account per namespace.",
                       no_args_is_help=True)
app.add_typer(vault_app, name="vault")


@vault_app.command("ls")
def vault_ls() -> None:
    """List registered vaults with live keyring state."""
    from fenris.drive.auth import token_present
    from fenris.vaults import list_vaults

    conn, _cfg = _open_db()
    try:
        rows = list_vaults(conn)
        for v in rows:
            state = "connected" if token_present(v.keyring_account) else "no token"
            typer.echo(f"[{v.drive_id}] {v.name} - {state} (keyring account {v.keyring_account!r})")
    finally:
        conn.close()


@vault_app.command("rm")
def vault_rm(vault_id: str) -> None:
    """Remove a vault REGISTRATION (projection rows stay; disconnect first)."""
    from fenris.vaults import remove_vault

    conn, _cfg = _open_db()
    try:
        if remove_vault(conn, vault_id.strip().lower()):
            typer.echo(f"vault {vault_id} removed (indexed rows stay; "
                       "revoke its grant via `fenris drive disconnect --vault`)")
        else:
            typer.echo(f"no such vault: {vault_id}")
            raise typer.Exit(code=1)
    except ValueError as e:
        typer.echo(str(e))
        raise typer.Exit(code=2)
    finally:
        conn.close()


# ---- fetched-weights registry (2026-09 retrieval round) -------------------------

models_app = typer.Typer(help="Fetched model weights: list, status, fetch.",
                         no_args_is_help=True)
app.add_typer(models_app, name="models")


@models_app.command("ls")
def models_ls() -> None:
    """List the weight registry with local availability."""
    from fenris import models as models_mod

    for name, entry in models_mod.MODELS.items():
        state = "fetched" if models_mod.available(name) else "not fetched"
        size = entry["size_bytes"] // (1024 * 1024)
        typer.echo(f"[{name}] {state} - {entry['what']} (~{size} MB)")
    typer.echo("fetch with: fenris models fetch <name> (explicit, verified)")


@models_app.command("fetch")
def models_fetch(name: str) -> None:
    """Fetch + verify (sha256 + smoke test) one model's weights."""
    import shutil

    from fenris import models as models_mod

    free = shutil.disk_usage(".").free
    entry = models_mod.MODELS.get(name)
    need = (entry["size_bytes"] if entry else 0) + models_mod.FETCH_FREE_BYTES
    if free < need:
        typer.echo(f"not enough free space (~{need // (1024*1024)} MB needed)")
        raise typer.Exit(code=1)
    try:
        path = models_mod.fetch(name)
    except (ValueError, OSError) as e:
        typer.echo(str(e))
        raise typer.Exit(code=2)
    typer.echo(f"fetched + verified: {name} -> {path}")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
