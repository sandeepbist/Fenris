"""Database connection helper: opens SQLite, loads sqlite-vec, applies schema."""

from __future__ import annotations

import sqlite3

from fenris import schema


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
    except (ImportError, AttributeError, sqlite3.Error, OSError):
        # No sqlite-vec or no loadable-extension support in this
        # interpreter (e.g., macOS pythons without it): schema still
        # applies; vec0 is created lazily by apply_vec0 when possible.
        try:
            conn.enable_load_extension(False)
        except (AttributeError, sqlite3.Error):
            pass
    if apply:
        schema.apply_schema(conn)
        try:
            schema.apply_vec0(conn)
        except sqlite3.Error:
            pass
        conn.commit()
    return conn
