"""Initial enumeration: persist the start token FIRST, then list.

Ordering is the correctness point: the page token is captured before
enumeration begins, so any change landing while the (potentially long)
scan runs is replayed by the first drain - never lost. Skipped formats
still get a ``drive_files`` row (renames/moves stay trackable); they
simply never ingest.
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Any

from fenris.ingest import router as router_mod

DRIVE_ID_DEFAULT = "mydrive"


@dataclass(frozen=True)
class EnumerationReport:
    n_files: int = 0
    n_ingestable: int = 0
    n_skipped_by_router: int = 0
    start_token: str = ""
    drive_id: str = DRIVE_ID_DEFAULT
    files: list[dict[str, Any]] = field(default_factory=list)


def _upsert_drive_file(conn: sqlite3.Connection, drive_id: str, meta: dict) -> None:
    """Upsert mutable file metadata. Metadata changes never touch sources."""
    conn.execute(
        """INSERT INTO drive_files(drive_id, file_id, title, path, mime_type,
               trashed, drive_version, resource_key, capabilities_json, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(drive_id, file_id) DO UPDATE SET
             title=excluded.title, path=excluded.path,
             mime_type=excluded.mime_type, trashed=excluded.trashed,
             drive_version=excluded.drive_version,
             resource_key=excluded.resource_key,
             capabilities_json=excluded.capabilities_json,
             updated_at=excluded.updated_at""",
        (
            drive_id,
            meta["id"],
            meta.get("name", ""),
            "",  # path resolved later; parents live in the raw meta
            meta.get("mimeType", ""),
            1 if meta.get("trashed") else 0,
            str(meta.get("version", "")),
            meta.get("resourceKey") or "",
            _capabilities_json(meta),
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ),
    )


def _capabilities_json(meta: dict) -> str:
    import json

    caps = meta.get("capabilities") or {}
    return json.dumps({"canDownload": caps.get("canDownload")})


def is_ingestable(meta: dict) -> bool:
    """Pre-classify by the ingest router (pure; no I/O).

    Shortcut resolution is the sync layer's job; everything the router
    skips (audio/video, unknown binaries, ...) is tracked but never
    ingested.
    """
    mime = meta.get("mimeType", "")
    is_workspace = mime.startswith("application/vnd.google-apps.")
    decision = router_mod.route(mime, is_workspace, is_shortcut=False)
    return decision.route not in (router_mod.Route.SKIP, router_mod.Route.SHORTCUT)


def enumerate_drive(conn: sqlite3.Connection, client, drive_id: str = DRIVE_ID_DEFAULT) -> EnumerationReport:
    """Persist pending_start, then list the whole drive into drive_files.

    The start token is committed BEFORE the first list call - the
    transaction is tiny and contains exactly one write.
    """
    start_token = client.get_start_page_token(drive_id if drive_id != DRIVE_ID_DEFAULT else None)

    conn.execute("BEGIN")
    try:
        conn.execute(
            """INSERT INTO sync_cursor(drive_id, page_token, pending_start, status, updated_at)
               VALUES (?, NULL, ?, 'ok', ?)
               ON CONFLICT(drive_id) DO UPDATE SET
                 pending_start=excluded.pending_start, updated_at=excluded.updated_at""",
            (drive_id, start_token, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    n_files = 0
    n_ingestable = 0
    n_skipped = 0
    seen: list[dict[str, Any]] = []
    conn.execute("BEGIN")
    try:
        for meta in client.list_files():
            if not meta.get("id"):
                continue
            _upsert_drive_file(conn, drive_id, meta)
            n_files += 1
            if is_ingestable(meta):
                n_ingestable += 1
            else:
                n_skipped += 1
            seen.append(meta)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise

    return EnumerationReport(
        n_files=n_files,
        n_ingestable=n_ingestable,
        n_skipped_by_router=n_skipped,
        start_token=start_token,
        drive_id=drive_id,
        files=seen,
    )
