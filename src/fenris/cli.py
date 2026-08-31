"""Fenris CLI (Typer). Subcommands grow in as milestones land."""

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


def main() -> None:
    app()


if __name__ == "__main__":
    main()
