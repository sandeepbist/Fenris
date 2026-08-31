"""Database connection helper: opens SQLite, loads sqlite-vec, applies schema."""

from __future__ import annotations

import sqlite3

import fenris.schema as schema


def connect(db_path, apply: bool = True) -> sqlite3.Connection:
    """Open the projection DB with sqlite-vec loaded and schema applied.

    `apply=False` is for read-only paths in tests that want a raw DB.
    """
    from pathlib import Path

    parent = Path(db_path).parent
    if str(parent) not in ("", "."):
        parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.enable_load_extension(True)
        import sqlite_vec

        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    except Exception:
        # No sqlite-vec in the environment (e.g., CI smoke tests): schema still
        # applies; vec0 is created lazily by apply_vec0 when the ext is present.
        conn.enable_load_extension(False)
    if apply:
        schema.apply_schema(conn)
        try:
            schema.apply_vec0(conn)
        except Exception:
            pass
        conn.commit()
    return conn
