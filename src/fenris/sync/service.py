"""sync_all: the top-level sync orchestration + status reporting."""

from __future__ import annotations

import sqlite3
from typing import Any

from fenris.sync.changes import SyncReport, drain
from fenris.sync.enumerate import DRIVE_ID_DEFAULT, enumerate_drive, is_ingestable


def sync_all(
    conn: sqlite3.Connection,
    client,
    cfg=None,
    drive_id: str = DRIVE_ID_DEFAULT,
    *,
    pace_seconds: float | None = None,
) -> SyncReport:
    """One sync pass: resync triggers -> enumerate if needed -> drain.

    If the cursor is missing entirely, enumerate first (which persists
    pending_start before listing) and then drain so changes landing
    during the initial scan are replayed.
    """
    report = SyncReport(drive_id=drive_id)
    row = conn.execute(
        "SELECT page_token, pending_start FROM sync_cursor WHERE drive_id = ?",
        (drive_id,),
    ).fetchone()

    if row is None or (not row[0] and not row[1]):
        enum = enumerate_drive(conn, client, drive_id)
        report.resync_triggered = "initial_enumeration"
        # Re-ingest what enumeration saw: the pending_start token means the
        # change feed replays every file created BEFORE the token, so files
        # that predate the token are not in the feed. Queue them explicitly.
        from fenris.sync.ingest_queue import IngestQueue

        queue = IngestQueue(pace_seconds=pace_seconds)
        for meta in enum.files:

            mime = meta.get("mimeType", "")
            if mime.startswith("application/vnd.google-apps.") or not is_ingestable(meta):
                pass
            if not is_ingestable(meta):
                continue
            try:
                from fenris.sync.changes import _fetch_content

                fm, data, status = _fetch_content(conn, client, drive_id, meta)
                if status != "unreadable" and data is not None:
                    staged = queue.stage(fm, data, cfg)
                    conn.execute("BEGIN")
                    try:
                        queue.apply(conn, staged)
                        conn.execute("COMMIT")
                    except Exception:
                        conn.execute("ROLLBACK")
                        raise
                    report.content_reingests += 1
            except (OSError, ValueError, sqlite3.Error) as e:
                # one bad file never aborts the initial scan
                report.errors.append(f"{meta.get('id')}: {e}")
                continue

    drain_report = drain(conn, client, drive_id, cfg=cfg, pace_seconds=pace_seconds)
    if drain_report.resync_triggered and not report.resync_triggered:
        report.resync_triggered = drain_report.resync_triggered
    report.pages_drained = drain_report.pages_drained
    report.changes_processed = drain_report.changes_processed
    report.metadata_only += drain_report.metadata_only
    report.content_reingests += drain_report.content_reingests
    report.tombstoned = drain_report.tombstoned
    report.unchanged = drain_report.unchanged
    report.skipped = drain_report.skipped
    report.failed = drain_report.failed
    report.errors.extend(drain_report.errors)
    report.quota_snapshot = drain_report.quota_snapshot
    return report


def full_resync(conn: sqlite3.Connection, client, cfg=None, drive_id: str = DRIVE_ID_DEFAULT, *, pace_seconds: float | None = None) -> SyncReport:
    """The escape hatch: fresh token, re-enumerate, diff-reconcile.

    Files present locally but absent from a fresh enumeration are
    tombstoned by the diff (they were deleted outside the feed's
    coverage, e.g. a corrupt cursor ate their change entries). New
    files arrive via the drain that follows enumeration.
    """
    from fenris.sync.changes import _tombstone_file

    conn.execute("DELETE FROM sync_cursor WHERE drive_id = ?", (drive_id,))
    conn.commit()

    # Reconcile FIRST against current Drive reality: every active local
    # representation whose file no longer enumerates is tombstoned.
    conn.execute("BEGIN")
    try:
        remote_ids = {
            m["id"] for m in client.list_files() if m.get("id")
        }
        local_rows = conn.execute(
            "SELECT DISTINCT file_id FROM sources WHERE drive_id = ? AND active = 1",
            (drive_id,),
        ).fetchall()
        for (file_id,) in local_rows:
            if file_id not in remote_ids:
                _tombstone_file(conn, drive_id, file_id)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    report = sync_all(conn, client, cfg, drive_id, pace_seconds=pace_seconds)
    report.resync_triggered = "full_resync"
    return report


def sync_status(conn: sqlite3.Connection) -> dict[str, Any]:
    """A snapshot for the CLI: cursor state, counts, staleness."""
    from fenris.sync.staleness import is_stale_sync, last_drain_at

    cursor = conn.execute(
        "SELECT page_token, status, updated_at FROM sync_cursor WHERE drive_id = 'mydrive'"
    ).fetchone()
    counts: dict[str, int] = {}
    for key, sql in (
        ("drive_files", "SELECT count(*) FROM drive_files"),
        ("active_sources", "SELECT count(*) FROM sources WHERE active = 1"),
        ("retired_sources", "SELECT count(*) FROM sources WHERE active = 0"),
        ("chunks", "SELECT count(*) FROM chunks"),
    ):
        counts[key] = conn.execute(sql).fetchone()[0]
    return {
        "cursor": {"page_token": cursor[0], "status": cursor[1]} if cursor else None,
        "last_drain_at": last_drain_at(conn),
        "stale_sync": is_stale_sync(conn),
        **counts,
    }
