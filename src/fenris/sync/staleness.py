"""Staleness bookkeeping: the M4 seam, made honest.

The answer layer compares a cited source's CURRENT Drive version
(drive_files) against the version captured at INDEX time
(sources_hints). A mismatch is the STALE state's trigger. refresh_hints
runs after every drain so the hint table reflects what was just seen
on the feed - the staleness bound is the poll interval plus drain
duration, never zero, and the answer says which it is.
"""

from __future__ import annotations

import sqlite3
import time

STALENESS_WINDOW_MINUTES = 15  # poll interval + drain time budget


def ensure_hints(conn: sqlite3.Connection) -> None:
    from fenris.generate.verify import ensure_hints_table

    ensure_hints_table(conn)


def refresh_hints(conn: sqlite3.Connection, drive_id: str = "mydrive") -> int:
    """Sync sources_hints with drive_files versions for changed files.

    Idempotent; returns the number of hint rows written. Runs INSIDE the
    drain's post-commit phase - it's a projection refresh, not a source
    of truth.
    """
    from fenris.generate.verify import record_indexed_version

    ensure_hints(conn)
    rows = conn.execute(
        """SELECT df.file_id, df.drive_version
           FROM drive_files df
           JOIN sources s ON s.drive_id = df.drive_id AND s.file_id = df.file_id
           WHERE df.drive_id = ? AND s.active = 1""",
        (drive_id,),
    ).fetchall()
    n = 0
    for file_id, version in rows:
        if version:
            record_indexed_version(conn, drive_id, file_id, str(version))
            n += 1
    conn.commit()
    return n


def last_drain_at(conn: sqlite3.Connection) -> str | None:
    """ISO timestamp of the last successful cursor update."""
    row = conn.execute(
        "SELECT updated_at FROM sync_cursor WHERE drive_id = 'mydrive'"
    ).fetchone()
    return row[0] if row else None


def is_stale_sync(
    conn: sqlite3.Connection,
    max_age_minutes: int = STALENESS_WINDOW_MINUTES,
) -> bool:
    """True when the last drain is older than the freshness window.

    An absent cursor (never synced / resync pending) is stale: the
    answer layer would rather admit staleness than assume freshness.
    """
    stamp = last_drain_at(conn)
    if not stamp:
        return True
    try:
        drained = time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        age = time.time() - time.mktime(time.gmtime() * 0 + drained)
    except (ValueError, TypeError):
        return True
    return age > max_age_minutes * 60
