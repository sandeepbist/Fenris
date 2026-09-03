"""sync_app: Typer sub-app the CLI registers (run / status / resync)."""

from __future__ import annotations

import typer

from fenris.config import load_config
from fenris.db import connect

app = typer.Typer(help="Synchronize the local index with Google Drive.", no_args_is_help=True)

# Alias under the name the main CLI registers (the registration block in
# fenris.cli imports `sync_app`).
sync_app = app


def _get_client(vault: str = "mydrive"):
    from fenris.drive.auth import load_or_refresh
    from fenris.drive.client import DriveClient
    from fenris.vaults import keyring_account_for

    creds = load_or_refresh(keyring_account_for(vault))
    from googleapiclient.discovery import build

    service = build("drive", "v3", credentials=creds)
    return DriveClient(service)


def _resolve_vault(conn, vault: str | None) -> str:
    """Validate a --vault id against the registry; default 'mydrive'."""
    from fenris.vaults import DEFAULT_VAULT, vault_exists

    vault_id = (vault or DEFAULT_VAULT).strip().lower()
    if vault_id != DEFAULT_VAULT and not vault_exists(conn, vault_id):
        typer.echo(f"unknown vault: {vault_id} (register via `fenris drive auth --vault {vault_id}`)")
        raise typer.Exit(code=2)
    return vault_id


@app.command()
def run(
    vault: str = typer.Option("mydrive", "--vault", help="Vault id to sync (default: mydrive)."),
) -> None:
    """Drain the Drive change feed into the local index."""
    cfg = load_config()
    conn = connect(cfg.db_path)
    try:
        vault_id = _resolve_vault(conn, vault)
        client = _get_client(vault_id)
        from fenris.sync.service import sync_all

        report = sync_all(conn, client, drive_id=vault_id)
        typer.echo(
            f"synced [{vault_id}]: {report.changes_processed} changes "
            f"({report.metadata_only} metadata, {report.content_reingests} re-ingested, "
            f"{report.tombstoned} tombstoned, {report.failed} failed)"
        )
        for err in report.errors[:5]:
            typer.echo(f"  error: {err}")
        if report.resync_triggered:
            typer.echo(f"resync trigger: {report.resync_triggered}")
    finally:
        conn.close()


@app.command()
def status() -> None:
    """Show cursor state, index counts, and staleness (all vaults)."""
    cfg = load_config()
    conn = connect(cfg.db_path)
    try:
        from fenris.sync.service import sync_status

        for k, v in sync_status(conn).items():
            typer.echo(f"{k}: {v}")
    finally:
        conn.close()


@app.command()
def resync(
    vault: str = typer.Option("mydrive", "--vault", help="Vault id to resync (default: mydrive)."),
) -> None:
    """Full re-enumeration escape hatch (fresh token, diff-reconcile)."""
    cfg = load_config()
    conn = connect(cfg.db_path)
    try:
        vault_id = _resolve_vault(conn, vault)
        client = _get_client(vault_id)
        from fenris.sync.service import full_resync

        report = full_resync(conn, client, drive_id=vault_id)
        typer.echo(
            f"resync [{vault_id}] done: {report.changes_processed} changes walked, "
            f"{report.tombstoned} tombstoned by reconciliation"
        )
    finally:
        conn.close()
