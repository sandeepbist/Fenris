"""Multi-vault: personal + work Drive as separate namespaces (P2.5).

A vault is one Google ACCOUNT's own My Drive, synced under its own
projection namespace (the drive_id that keys every table). The default
vault "mydrive" is what Fenris always was; additional vaults are
registered via `fenris drive auth --vault work` and live side by side:

- the OS keyring stores one refresh token per vault (service "fenris",
  account "drive-refresh" for the default - EXISTING installs keep their
  token untouched - and "drive-refresh:<vault-id>" for new vaults);
- the projection keeps per-vault sources/chunks/cursors (drive_id was
  namespacing everything since M0 - this module only adds the registry);
- sync runs per vault with that vault's credentials;
- retrieval filters by vault post-fusion (like corpus scope): asking in
  one vault cannot leak another's evidence; a vault-restricted miss is
  an honest INSUFFICIENT.

Registry rules: vault ids match [a-z0-9][a-z0-9-]{0,31}; "mydrive" is
reserved as the default; names are display-only. Removing a vault never
deletes its projection rows - retirement is explicit (`fenris vault rm
--purge` shape is deliberately NOT shipped; ask/digest read the
projection, deleting indexed evidence under running questions would
break the citation contract).

Every SQL statement is one single-line fixed string with ? placeholders.
"""

from __future__ import annotations

import re
import sqlite3
import time

VAULT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}[a-z0-9]$|^[a-z0-9]$")
DEFAULT_VAULT = "mydrive"
DEFAULT_VAULT_NAME = "Personal Drive"

_VAULTS_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS vaults (drive_id TEXT PRIMARY KEY, name TEXT NOT NULL, keyring_account TEXT NOT NULL, created_at TEXT)"
)


def ensure_vaults_table(conn: sqlite3.Connection) -> None:
    """Idempotent lazy migration; seeds the implicit default vault."""
    conn.execute(_VAULTS_SCHEMA)
    row = conn.execute("SELECT drive_id FROM vaults WHERE drive_id = ?", (DEFAULT_VAULT,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO vaults(drive_id, name, keyring_account, created_at) VALUES (?, ?, ?, ?)", (DEFAULT_VAULT, DEFAULT_VAULT_NAME, "drive-refresh", _now_iso()))
    conn.commit()


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def keyring_account_for(vault_id: str) -> str:
    """The keyring account of a vault id (no DB needed - pure mapping)."""
    if vault_id == DEFAULT_VAULT:
        return "drive-refresh"
    return f"drive-refresh:{vault_id}"


def register_vault(conn: sqlite3.Connection, vault_id: str, name: str) -> None:
    """Register a vault after validating its id. Idempotent per id."""
    vault_id = vault_id.strip().lower()
    if not VAULT_ID_RE.match(vault_id):
        raise ValueError("vault id: lowercase letters/digits/dashes, max 32 chars")
    if vault_id == DEFAULT_VAULT:
        raise ValueError("'mydrive' is the default vault and always exists")
    ensure_vaults_table(conn)
    row = conn.execute("SELECT drive_id FROM vaults WHERE drive_id = ?", (vault_id,)).fetchone()
    if row is None:
        conn.execute("INSERT INTO vaults(drive_id, name, keyring_account, created_at) VALUES (?, ?, ?, ?)", (vault_id, name.strip()[:100] or vault_id, keyring_account_for(vault_id), _now_iso()))
        conn.commit()


def remove_vault(conn: sqlite3.Connection, vault_id: str) -> bool:
    """Remove a vault REGISTRATION (projection rows stay; the keyring
    entry must be disconnected first - see disconnect_vault)."""
    if vault_id == DEFAULT_VAULT:
        raise ValueError("the default vault cannot be removed")
    ensure_vaults_table(conn)
    cur = conn.execute("DELETE FROM vaults WHERE drive_id = ?", (vault_id,))
    conn.commit()
    return cur.rowcount > 0


class VaultInfo:
    """One registry row as the API/CLI display it."""

    def __init__(self, drive_id: str, name: str, keyring_account: str, created_at: str | None):
        self.drive_id = drive_id
        self.name = name
        self.keyring_account = keyring_account
        self.created_at = created_at

    def as_dict(self) -> dict:
        return {"drive_id": self.drive_id, "name": self.name,
                "keyring_account": self.keyring_account, "created_at": self.created_at}


def list_vaults(conn: sqlite3.Connection) -> list[VaultInfo]:
    ensure_vaults_table(conn)
    rows = conn.execute("SELECT drive_id, name, keyring_account, created_at FROM vaults ORDER BY drive_id", ()).fetchall()
    return [VaultInfo(r[0], r[1], r[2], r[3]) for r in rows]


def vault_exists(conn: sqlite3.Connection, vault_id: str) -> bool:
    ensure_vaults_table(conn)
    row = conn.execute("SELECT drive_id FROM vaults WHERE drive_id = ?", (vault_id,)).fetchone()
    return row is not None
