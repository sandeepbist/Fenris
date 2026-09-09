"""Typer sub-app for the Drive connection: `fenris drive <cmd>`.

The integrator wires ``drive_app`` into the main CLI (fenris.cli) - this
module deliberately does not touch it. Commands:

- ``auth`` - run the PKCE loopback OAuth flow, store the refresh token
- ``disconnect`` - revoke the grant at Google AND delete the keyring entry
- ``status`` - token present? quota counters for this session
- ``files list`` - enumerate Drive with the the build plan parameters

Quota counters are process-local by design: `status` reports what THIS
session spent (the build plan keeps them as visibility, not persistence - sync
accounts for cost across runs via the units it actually consumes).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from fenris.drive import auth as auth_mod
from fenris.drive.client import DriveClient
from fenris.drive.quota import QuotaCounter

drive_app = typer.Typer(
    name="drive",
    help="Google Drive connection: OAuth, disconnect, status, file enumeration.",
    no_args_is_help=True,
)

files_app = typer.Typer(help="Enumerate files in the connected Drive.")
drive_app.add_typer(files_app, name="files")

#: Default location for the OAuth client secrets (overridable per command).
DEFAULT_CLIENT_SECRETS = Path("client_secrets.json")

_FAIL_FAST_EXIT = 2


def _die(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(_FAIL_FAST_EXIT)


def _ok(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.GREEN)


@drive_app.command()
def auth(
    client_secrets: Annotated[
        Optional[Path],  # noqa: UP045 - typer resolves Optional[...] at runtime
        typer.Option(
            "--client-secrets",
            help="Path to the Google OAuth client secrets JSON (Desktop app type).",
        ),
    ] = None,
    port: Annotated[int, typer.Option(help="Loopback port for the OAuth redirect.")] = 8080,
    no_browser: Annotated[
        bool, typer.Option("--no-browser", help="Print the auth URL instead of opening a browser.")
    ] = False,
    vault: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--vault", help="Vault id to connect (P2.5): 'work' stores its token"
                                      " under a separate keyring account. Default: the main Drive."),
    ] = None,
) -> None:
    """Run the PKCE loopback OAuth flow and store the refresh token in the OS keyring."""
    secrets = client_secrets or DEFAULT_CLIENT_SECRETS
    from fenris.vaults import DEFAULT_VAULT, keyring_account_for

    vault_id = (vault or DEFAULT_VAULT).strip().lower()
    account = keyring_account_for(vault_id)
    if vault_id != DEFAULT_VAULT:
        # register the vault so ls/pickers see it (idempotent)
        from fenris.config import load_config
        from fenris.db import connect
        from fenris.vaults import register_vault

        conn = connect(load_config().db_path)
        try:
            register_vault(conn, vault_id, vault_id)
        except ValueError as e:
            _die(str(e))
        finally:
            conn.close()
    try:
        auth_mod.run_auth_flow(secrets, port=port, open_browser=not no_browser,
                               keyring_account=account)
    except auth_mod.AuthError as e:
        _die(str(e))
    if vault_id != DEFAULT_VAULT:
        _ok(f"Vault '{vault_id}' connected. Refresh token stored in the OS keyring "
            f"(service {auth_mod.KEYRING_SERVICE!r}, account {account!r}).")
    else:
        _ok("Drive connected. Refresh token stored in the OS keyring "
            f"(service {auth_mod.KEYRING_SERVICE!r}, account {auth_mod.KEYRING_ACCOUNT!r}).")


@drive_app.command()
def disconnect(
    vault: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--vault", help="Vault id to disconnect (default: the main Drive)."),
    ] = None,
) -> None:
    """Revoke the Google grant and delete the stored token (never silent)."""
    from fenris.vaults import DEFAULT_VAULT, keyring_account_for

    account = keyring_account_for((vault or DEFAULT_VAULT).strip().lower())
    try:
        revoked = auth_mod.disconnect(keyring_account=account)
    except auth_mod.AuthError as e:
        _die(str(e))
    if revoked:
        _ok(f"Disconnected: grant revoked at Google and keyring entry {account!r} deleted.")
    else:
        typer.echo("Nothing to disconnect: no stored token found.")


@drive_app.command()
def status(
    vault: Annotated[
        Optional[str],  # noqa: UP045
        typer.Option("--vault", help="Vault id to check (default: the main Drive)."),
    ] = None,
) -> None:
    """Show connection state and this session's Drive quota counters."""
    from fenris.vaults import DEFAULT_VAULT, keyring_account_for

    account = keyring_account_for((vault or DEFAULT_VAULT).strip().lower())
    if auth_mod.token_present(account):
        typer.echo(f"token: present (keyring {auth_mod.KEYRING_SERVICE}/{account})")
        typer.echo(f"scope: {auth_mod.DRIVE_SCOPE}")
    else:
        typer.echo("token: not found - run `fenris drive auth`")
    # Session counters (fresh per process; --limit runs below add to them).
    quota = QuotaCounter()
    snap = quota.snapshot()
    typer.echo(f"quota this session: {snap.units_used} units over {snap.total_calls} calls")
    if snap.errors:
        typer.echo(f"errors this session: {dict(snap.errors)}")


@files_app.command("list")
def files_list(
    limit: Annotated[
        Optional[int],  # noqa: UP045
        typer.Option("--limit", "-n", help="Stop after N files (None = enumerate all)."),
    ] = None,
    client_secrets: Annotated[
        Optional[Path],  # noqa: UP045
        typer.Option("--client-secrets", help="Path to the OAuth client secrets JSON."),
    ] = None,
) -> None:
    """Enumerate files (the build plan parameters) and print one line each."""
    del client_secrets  # creds come from the keyring; kept for CLI symmetry
    try:
        creds = auth_mod.load_or_refresh()
    except auth_mod.AuthError as e:
        _die(str(e))
        return
    try:
        from googleapiclient.discovery import build

        service = build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception as e:  # noqa: BLE001 - surfaced as a user-facing failure
        _die(f"Could not build the Drive service: {e}")
        return
    from fenris.drive.errors import FenrisDriveError

    client = DriveClient(service)
    shown = 0
    try:
        for meta in client.list_files():
            typer.echo(
                f"{meta.get('id', '?')}\t{meta.get('mimeType', '?')}\t"
                f"trashed={1 if meta.get('trashed') else 0}\t{meta.get('name', '')}"
            )
            shown += 1
            if limit is not None and shown >= limit:
                break
    except FenrisDriveError as e:
        _die(f"enumeration failed: {e}")
    typer.echo(
        f"-- {shown} file(s) shown; quota this session: "
        f"{client.quota.units_used} units",
        err=True,
    )
